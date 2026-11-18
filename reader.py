"""証拠PDFの文字と位置を読む。

  - スキャナでOCRしたPDFは、テキスト層を文字ごとの位置つきで読む（PyMuPDF。ネットワークは使わない）
  - テキスト層がないページ（文字がほとんど取れないページ）は、tokumeiocr（PaddleOCR）に読ませる。
    tokumeiocr.exe の ocr-text コマンドを呼び、文字と位置の JSON を受け取る。tokumeiocr は OCR の間、外部への接続を遮断する。
    tokumeiocr がないPCでは、そのページは「読めなかった」として手入力にまわす

読んだ文字は、規則（rules.py）で探しやすいようにそろえる。
  - NFKC で正規化（全角の数字・英字・記号を半角に。「１８時」→「18時」）
  - 文字の間の空白を消す（スキャナのOCRは「令 和 6 年」のように空白を入れることがある）。英数字どうしの間の空白だけ1つ残す
  - 同じ高さに並ぶ文字のかたまり（表の「項目名」と「値」など）は、左から順に空白1つでつないで1行にする
そろえた後の1文字ごとに、元のページ上の位置（PDFの座標、ポイント）を持っておく。確認画面で枠を出すため。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

MIN_CHARS = 20          # これより文字が少ないページは、テキスト層がないとみなす
READER_VERSION = 1      # 読み方を変えたら上げる（キャッシュを読み直す）
SUFFIXES = (".pdf",)


@dataclass
class Page:
    no: int                      # 1から
    width: float
    height: float
    text: str = ""               # そろえた文字。行は \n で区切る
    boxes: list = field(default_factory=list)   # text の1文字ごとの位置 [x0, y0, x1, y1]（区切りの空白・改行は None）
    source: str = "text"         # text（テキスト層）／ocr（tokumeiocr）／none（読めなかった）

    def box_of(self, start: int, end: int):
        """text[start:end] を囲む枠。行をまたぐときは行ごとの枠のリスト。"""
        rects: list[list[float]] = []
        cur = None
        for b in self.boxes[start:end]:
            if b is None:
                if cur:
                    rects.append(cur)
                    cur = None
                continue
            if cur is None:
                cur = list(b)
            else:
                cur = [min(cur[0], b[0]), min(cur[1], b[1]), max(cur[2], b[2]), max(cur[3], b[3])]
        if cur:
            rects.append(cur)
        return [[round(v, 1) for v in r] for r in rects]


@dataclass
class Doc:
    path: str
    pages: list[Page]
    sha1: str = ""

    @property
    def text(self) -> str:
        return "\n\f\n".join(p.text for p in self.pages)

    def unread_pages(self) -> list[int]:
        return [p.no for p in self.pages if p.source == "none"]

    def to_json(self) -> dict:
        return {"version": READER_VERSION, "path": self.path, "sha1": self.sha1,
                "pages": [asdict(p) for p in self.pages]}

    @classmethod
    def from_json(cls, d: dict) -> "Doc":
        return cls(d["path"], [Page(**p) for p in d["pages"]], d.get("sha1", ""))


# --- 文字をそろえる -----------------------------------------------------------------

def _is_ascii_word(c: str) -> bool:
    return c.isascii() and (c.isalnum() or c in ".,-/:")


def _norm_chars(chars):
    """[(文字, 位置)] → そろえた [(文字, 位置)]。NFKC にし、空白は英数字どうしの間だけ1つ残す。"""
    out = []
    pending_space = False
    for c, b in chars:
        n = unicodedata.normalize("NFKC", c)
        for ch in n:
            if ch.isspace():
                pending_space = True
                continue
            if pending_space and out and _is_ascii_word(out[-1][0]) and _is_ascii_word(ch):
                out.append((" ", None))
            pending_space = False
            out.append((ch, b))
    return out


def _rows(segments):
    """文字のかたまり [(y0, y1, x0, [(文字, 位置)])] を、同じ高さのものごとにまとめて行にする。"""
    segs = sorted(segments, key=lambda s: ((s[0] + s[1]) / 2, s[2]))
    rows: list[list] = []
    for s in segs:
        mid = (s[0] + s[1]) / 2
        h = max(s[1] - s[0], 1)
        for r in rows:
            rmid = (r[0] + r[1]) / 2
            if abs(mid - rmid) < max(h, r[1] - r[0]) * 0.5:
                r[0], r[1] = min(r[0], s[0]), max(r[1], s[1])
                r[2].append(s)
                break
        else:
            rows.append([s[0], s[1], [s]])
    rows.sort(key=lambda r: (r[0] + r[1]) / 2)
    return [sorted(r[2], key=lambda s: s[2]) for r in rows]


def _page_from_segments(no, width, height, segments, source) -> Page:
    text: list[str] = []
    boxes: list = []
    for row in _rows(segments):
        if text:
            text.append("\n")
            boxes.append(None)
        first = True
        for s in row:
            chars = _norm_chars(s[3])
            if not chars:
                continue
            if not first:
                text.append(" ")
                boxes.append(None)
            first = False
            for ch, b in chars:
                text.append(ch)
                boxes.append([round(v, 1) for v in b] if b is not None else None)
    return Page(no=no, width=round(width, 1), height=round(height, 1), text="".join(text), boxes=boxes, source=source)


def _visible(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


# --- テキスト層 ---------------------------------------------------------------------

def _text_layer_segments(page):
    segs = []
    raw = page.get_text("rawdict", flags=0)
    for b in raw.get("blocks", []):
        for line in b.get("lines", []):
            chars = [(c["c"], c["bbox"]) for s in line.get("spans", []) for c in s.get("chars", [])]
            if not chars:
                continue
            x0, y0, x1, y1 = line["bbox"]
            segs.append((y0, y1, x0, chars))
    return segs


def read_text_layer(path: Path):
    """戻り値: Page のリスト（テキスト層のないページは source="none"）。"""
    import pymupdf

    pages = []
    with pymupdf.open(str(path)) as doc:
        for i, page in enumerate(doc, 1):
            r = page.rect
            p = _page_from_segments(i, r.width, r.height, _text_layer_segments(page), "text")
            if _visible(p.text) < MIN_CHARS:
                p.source = "none"
            pages.append(p)
    return pages


# --- tokumeiocr（PaddleOCR）--------------------------------------------------------

def find_tokumeiocr(tool_dir: str | os.PathLike | None) -> list[str] | None:
    """tokumeiocr を呼ぶコマンド。exe 版（tokumeiocr.exe）か Python 版（.venv と tokumeiocr.py）。なければ None。"""
    cands = [Path(tool_dir)] if tool_dir else []
    cands += [Path(r"C:\tokumeiocr")]
    for d in cands:
        exe = d / "tokumeiocr.exe"
        if exe.is_file():
            return [str(exe)]
        py = d / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if py.is_file() and (d / "tokumeiocr.py").is_file():
            return [str(py), str(d / "tokumeiocr.py")]
    return None


class OcrError(Exception):
    pass


def ocr_pages(path: Path, pages: list[int], command: list[str], timeout: int = 1800) -> dict[int, Page]:
    """tokumeiocr の ocr-text で、指定したページを読む。戻り値: {ページ番号: Page}。

    受け取る JSON: {"pages": [{"no", "width", "height", "lines": [{"text", "chars": [[x0,y0,x1,y1], ...]}]}]}
    （chars は text の1文字ごとの位置。PDFの座標、ポイント）
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "ocr.json"
        cmd = command + ["ocr-text", str(path), str(out), "--pages", ",".join(map(str, pages))]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OcrError(f"tokumeiocr を動かせませんでした（{e}）") from None
        if r.returncode != 0 or not out.is_file():
            msg = (r.stderr or r.stdout).strip().splitlines()[-3:]
            raise OcrError("tokumeiocr で読めませんでした：" + " ".join(msg))
        data = json.loads(out.read_text(encoding="utf-8"))
    res = {}
    for pg in data.get("pages", []):
        segs = []
        for ln in pg.get("lines", []):
            t, bs = ln.get("text", ""), ln.get("chars") or []
            if not t or len(bs) != len(t):
                continue
            chars = list(zip(t, bs))
            y0 = min(b[1] for b in bs)
            y1 = max(b[3] for b in bs)
            x0 = min(b[0] for b in bs)
            segs.append((y0, y1, x0, chars))
        res[pg["no"]] = _page_from_segments(pg["no"], pg["width"], pg["height"], segs, "ocr")
    return res


# --- まとめ --------------------------------------------------------------------------

def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_doc(path: Path, cache_dir: Path | None = None, ocr_command: list[str] | None = None,
             log=lambda s: None) -> Doc:
    """PDFを読む。cache_dir があれば、同じ中身のファイルは前回の結果を使う（OCRに時間がかかるため）。"""
    path = Path(path)
    sha = file_sha1(path)
    cache = Path(cache_dir) / f"{sha}.json" if cache_dir else None
    if cache and cache.is_file():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("version") == READER_VERSION:
                doc = Doc.from_json(d)
                doc.path = str(path)
                if not (doc.unread_pages() and ocr_command):   # 前回読めなかったページは、OCRがあれば読み直す
                    return doc
        except (ValueError, KeyError, TypeError):
            pass
    doc = Doc(str(path), read_text_layer(path), sha)
    todo = doc.unread_pages()
    if todo and ocr_command:
        log(f"{path.name}：テキスト層のないページ {len(todo)} ページを tokumeiocr で読みます（1ページ十数秒）")
        got = ocr_pages(path, todo, ocr_command)
        doc.pages = [got.get(p.no, p) if p.source == "none" else p for p in doc.pages]
        for p in doc.pages:
            if p.source == "ocr" and _visible(p.text) < 5:
                p.source = "none"
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc.to_json(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache)
    return doc


def render_png(path: Path, page_no: int, zoom: float = 1.5) -> bytes:
    """確認画面に出すページの画像（PNG）。"""
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        if not 1 <= page_no <= doc.page_count:
            raise ValueError("ページがありません")
        pix = doc[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


if __name__ == "__main__":   # 開発用：python reader.py 証拠.pdf → そろえた文字を出す
    for s in (sys.stdout,):
        s.reconfigure(encoding="utf-8")
    d = read_doc(Path(sys.argv[1]), ocr_command=find_tokumeiocr(None))
    for p in d.pages:
        print(f"--- {p.no}ページ（{p.source}）")
        print(p.text)

"""書面を作る中心の処理（画面・HTTP とは切り離してテストできるようにする）。

スキル（kotsu-sojou/）のスクリプトは書き換えずにそのまま使う。
  build_kyoutuu.py  case.json → 共通書式xlsx ＋ 証拠リスト
  build_sojou.py    case.json ＋ office.json ＋ 合計額 → 訴状docx ＋ mints入力メモ

スキルでは、合計額（損害額一覧表 F74・F105）を人が Web版Excel で見て打ち込んでいた。
アプリでは、出力した xlsx の数式を formulas（Excel の数式を Python で計算するライブラリ）で計算して、その値を使う。
計算が Excel・LibreOffice と同じになることは、tests/test_core.py で見本4件の全セルを照合して確かめている。
死亡事案では、build_sojou.py が相続等一覧表の数式を再現して出す原告ごとの請求額と、
xlsx の「請求額合計」（45行＋物損70行）を照らし合わせ、違えば止める。
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import sys
from pathlib import Path


def _base() -> Path:
    # exe（PyInstaller）にしたときは、同梱したファイルの置き場所（_internal）から読む
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


SKILL_DIR = _base() / "kotsu-sojou"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import build_kyoutuu as bk  # noqa: E402
import build_sojou as bs  # noqa: E402
from xlsx_patch import fill  # noqa: E402

TEMPLATE_XLSX = SKILL_DIR / "assets" / "20260514kyoutuushosiki.xlsx"
TEMPLATE_DOCX = SKILL_DIR / "assets" / "202602sojou_songaibaishou_itiranhyouriyou.docx"
S_SONGAI = "損害額一覧表"
S_SOUZOKU = "相続等一覧表（死亡事案で使用）"
SOUZOKU_COLS = "FGHIJKLM"   # 相続等一覧表の原告の列（8人まで）


class BuildError(Exception):
    """スクリプトが止まったとき（行数超過・証拠キー未定義・全角数字混入など）。画面にそのまま出す。"""


@contextlib.contextmanager
def _skill_call():
    """スキルのスクリプトを呼ぶ。SystemExit・assert で止まったら BuildError に、警告（stderr）は集めて返す。"""
    err = io.StringIO()
    out = io.StringIO()
    warnings: list[str] = []
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            yield warnings
    except SystemExit as e:
        raise BuildError(str(e.code) if e.code not in (None, 0) else "スクリプトが止まりました") from None
    except AssertionError as e:
        raise BuildError(str(e) or "入力に誤りがあります") from None
    except (KeyError, TypeError, ValueError) as e:
        raise BuildError(f"case.json の形が正しくありません（{type(e).__name__}: {e}）") from None
    finally:
        warnings.extend(line.strip() for line in err.getvalue().splitlines() if line.strip())


def build_xlsx(case: dict, out: Path) -> tuple[list[dict], list[str]]:
    """共通書式xlsx と 証拠リストJSON を出す。戻り値: (証拠リスト, 警告)。build_kyoutuu.py の __main__ と同じ処理。"""
    out = Path(out)
    case = copy.deepcopy(case)   # スクリプトが case を書き換えても、画面の case には響かないように
    with _skill_call() as warnings:
        bk.cells_meta.clear()
        data, ov = bk.build_cells(case)
        evlist = bk.number_evidence(data, case["evidence"], case.get("sojou", {}).get("ev_order", []))
        for sh in data:
            data[sh] = {k: v for k, v in data[sh].items() if v is not None}
        unhide = {S_SOUZOKU: (10, 13)} if bk.cells_meta.get("wide") else None
        fill(str(TEMPLATE_XLSX), str(out), data, ov, unhide_cols=unhide)
    ev_path = out.with_name(out.stem + "_証拠リスト.json")
    ev_path.write_text(json.dumps(evlist, ensure_ascii=False, indent=1), encoding="utf-8")
    return evlist, warnings


# --- 合計の計算 ------------------------------------------------------------------

def evaluate(xlsx: Path) -> dict[tuple[str, str], object]:
    """xlsx の数式を全部計算する。戻り値: {(シート名, セル番地): 値}。1件で数秒かかる。"""
    import formulas  # 読み込みが重いので、使うときに読む

    with contextlib.redirect_stderr(io.StringIO()):   # 進み具合の表示（tqdm）を出さない
        sol = formulas.ExcelModel().loads(str(xlsx)).finish().calculate()
    vals: dict[tuple[str, str], object] = {}
    for k, v in sol.items():
        m = re.match(r"'\[.*?\](.*)'!([A-Z]+\d+)$", str(k))
        if not m:
            continue
        x = getattr(v, "value", v)
        try:
            if getattr(x, "shape", None) == (1, 1):
                x = x[0][0]
        except Exception:
            pass
        if hasattr(x, "item"):          # numpy の数
            x = x.item()
        vals[(m[1].upper(), m[2])] = x
    return vals


def _num(v) -> int:
    if v in (None, ""):
        return 0
    if isinstance(v, (int, float)):
        return round(v)
    raise BuildError(f"計算できないセルがあります（{v}）。入力を確かめてください")


def totals(xlsx: Path, case: dict | None = None) -> dict:
    """訴状に入れる合計額。F74（人損）・F105（物損）・F113（法定利率）と、死亡事案なら原告ごとの請求額。"""
    vals = evaluate(xlsx)
    S, R = S_SONGAI.upper(), S_SOUZOKU.upper()
    res = {"amount": _num(vals.get((S, "F74"))), "busson": _num(vals.get((S, "F105"))),
           "rate": vals.get((S, "F113"))}
    heirs = ((case or {}).get("shibou") or {}).get("heirs") or []
    if heirs:
        res["plaintiffs"] = [
            {"name": h["name"], "col": col,
             "jinson": _num(vals.get((R, f"{col}45"))), "busson": _num(vals.get((R, f"{col}70")))}
            for h, col in zip(heirs, SOUZOKU_COLS)]
    return res


def check_plaintiffs(case: dict, tot: dict) -> None:
    """死亡事案：build_sojou.py の再現（原告ごとの請求額）と、xlsx の数式の計算（45行＋70行）が同じか。"""
    if not tot.get("plaintiffs"):
        return
    with _skill_call():
        claims = bs.make_claims(copy.deepcopy(case), tot["amount"], tot["busson"])
    by_name = {c["row"]["name"]: c["row"]["total"] for c in claims if c.get("row")}
    bad = []
    for p in tot["plaintiffs"]:
        if p["name"] not in by_name:
            continue
        xl = p["jinson"] + p["busson"]
        if by_name[p["name"]] != xl:
            bad.append(f"{p['name']}：訴状 {by_name[p['name']]:,}円 ／ 相続等一覧表 {xl:,}円")
    if bad:
        raise BuildError("原告ごとの請求額が、相続等一覧表の計算と合いません。入力を確かめてください：" + "、".join(bad))


def build_docx(case: dict, office: dict, out: Path, amount: int, busson: int, full: bool = False) -> list[str]:
    """訴状docx と mints入力メモ（訴状_mints入力メモ.md）を出す。戻り値: 警告。"""
    if not (amount or busson):
        raise BuildError("請求額が0円です。損害の入力を確かめてください")
    with _skill_call() as warnings:
        bs.build(copy.deepcopy(case), office, amount, busson, str(out), str(TEMPLATE_DOCX), full)
    return warnings


def build_all(case: dict, office: dict, outdir: Path, *, full: bool = False,
              xlsx_name: str = "原告一覧表.xlsx", docx_name: str = "訴状.docx") -> dict:
    """共通書式xlsx → 合計の計算 → 訴状docx。戻り値: 出力したファイル・合計額・証拠リスト・警告。"""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    xlsx = outdir / xlsx_name
    evlist, warn1 = build_xlsx(case, xlsx)
    tot = totals(xlsx, case)
    check_plaintiffs(case, tot)
    files = [xlsx, xlsx.with_name(xlsx.stem + "_証拠リスト.json")]
    warn2: list[str] = []
    if case.get("sojou"):
        docx = outdir / docx_name
        warn2 = build_docx(case, office, docx, tot["amount"], tot["busson"], full)
        files += [docx, docx.with_name(docx.stem + "_mints入力メモ.md")]
    return {"files": [str(f) for f in files], "totals": tot, "evidence": evlist, "warnings": warn1 + warn2}

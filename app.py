"""確認画面のデータと処理（HTTP とは切り離してテストできるようにする）。

事件フォルダ1つを1件として扱う。
  <事件フォルダ>/                 証拠PDF（サブフォルダも見る。名前が _ で始まるフォルダと「訴状」フォルダは見ない）
  <事件フォルダ>/訴状/case.json   入力の本体（スキル kotsu-sojou と同じ形。Claude Desktop のスキルからも使える）
  <事件フォルダ>/訴状/            書面（原告一覧表.xlsx・_証拠リスト.json・訴状.docx・_mints入力メモ.md）
  <事件フォルダ>/_訴状アプリ/      読み取りのキャッシュ、確認の状態（state.json）、AI送信記録.txt

流れ
  1. 証拠PDFを読む（reader）→ 種類と値の候補を拾う（rules）。AIを使うときは ai も
  2. 確認画面で候補を○／×にし、値を直して「反映」→ case.json に入る（merge）
  3. 入力の画面で足りないところを入れる・判断の項目を決める
  4. 「書面を作る」→ 共通書式xlsx → 合計の計算 → 訴状docx（core）
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

import core
import merge
import reader
import rules

OUT_DIR = "訴状"
WORK_DIR = "_訴状アプリ"
CASE_FILE = "case.json"
STATE_VERSION = 1
SETTINGS_DEFAULT = {
    "office": {"office_name": "", "zip": "", "address": "", "tel": "", "fax": "", "lawyer_spaced": "",
               "seal_mark": False},
    "ai": {"enabled": False, "aws_region": "ap-northeast-1", "aws_profile": "",
           "model_id": "jp.anthropic.claude-sonnet-4-6"},
    "tokumeiocr_dir": r"C:\tokumeiocr",
}


class AppError(Exception):
    pass


# --- 設定（事務所情報・AI・tokumeiocr の場所）----------------------------------------------

def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "koutsu-sojou" / "設定.json"


def load_settings() -> dict:
    s = copy.deepcopy(SETTINGS_DEFAULT)
    p = settings_path()
    if p.is_file():
        try:
            got = json.loads(p.read_text(encoding="utf-8"))
            for k, v in got.items():
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
        except ValueError:
            pass
    return s


def save_settings(s: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json(p, s)


def _write_json(p: Path, obj) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def new_case() -> dict:
    return {"meta": {"case_no": "", "plaintiff": "", "defendant": ""}, "evidence": {},
            "jiko": {"date": None, "time": None, "place": "", "ev": [], "vehicles": []},
            "sekinin": [], "songai": {"items": []},
            "sojou": {"date": None, "court": "", "plaintiff": {"zip": "", "address": "", "name": ""},
                      "defendants": [], "hosoku": [], "keii": [], "souzoku": False, "shucho_ichiran": False},
            "todo": []}


class CaseApp:
    def __init__(self, case_dir, *, settings: dict | None = None, ai_client=None):
        self.case_dir = Path(case_dir).resolve()
        if not self.case_dir.is_dir():
            if self.case_dir.is_file():
                raise AppError("事件フォルダを指定してください（ファイルが指定されました）: %s" % self.case_dir)
            raise AppError("事件フォルダが見つかりません: %s" % self.case_dir)
        if self.case_dir.parent == self.case_dir:
            raise AppError("ドライブの直下そのものは事件フォルダにできません")
        self.out_dir = self.case_dir / OUT_DIR
        self.work_dir = self.case_dir / WORK_DIR
        self.case_path = self.out_dir / CASE_FILE
        self.state_path = self.work_dir / "state.json"
        self.settings = settings if settings is not None else load_settings()
        self.ai_client = ai_client      # テスト用（偽の Bedrock）
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.case = self._load_case()
        self.state = self._load_state()

    # --- 読み書き ---
    def _load_case(self) -> dict:
        if self.case_path.is_file():
            try:
                return json.loads(self.case_path.read_text(encoding="utf-8"))
            except ValueError as e:
                raise AppError(f"{self.case_path} が読めません（JSONの誤り: {e}）。直すか、別の名前にしてください") from None
        return new_case()

    def _load_state(self) -> dict:
        if self.state_path.is_file():
            try:
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
                if st.get("version") == STATE_VERSION:
                    return st
            except ValueError:
                pass
        return {"version": STATE_VERSION, "files": {}, "drafts": []}

    def _save_case(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.case_path, self.case)

    def _save_state(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.state_path, self.state)

    # --- 証拠のファイル ---
    def pdfs(self) -> list[str]:
        out = []
        for root, dirs, files in os.walk(self.case_dir):
            rel_root = Path(root).relative_to(self.case_dir)
            dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")) and not (rel_root == Path(".") and d == OUT_DIR))
            for f in sorted(files):
                if f.lower().endswith(reader.SUFFIXES) and not f.startswith("~$"):
                    out.append((rel_root / f).as_posix())
        return out

    def _path(self, rel: str) -> Path:
        p = (self.case_dir / rel).resolve()
        try:
            p.relative_to(self.case_dir)
        except ValueError:
            raise AppError("事件フォルダの外のファイルは開けません") from None
        if not p.is_file():
            raise AppError(f"ファイルがありません: {rel}")
        return p

    def ocr_command(self):
        return reader.find_tokumeiocr(self.settings.get("tokumeiocr_dir"))

    def _owners(self) -> dict:
        return {f["evkey"]: rel for rel, f in self.state["files"].items() if f.get("evkey")}

    def read_file(self, rel: str, force: bool = False) -> dict:
        """1通を読み、候補を作る。前に決めた○／×と直した値は、同じ候補があれば引き継ぐ。"""
        with self.lock:
            p = self._path(rel)
            try:
                doc = reader.read_doc(p, self.work_dir / "cache", self.ocr_command())
            except reader.OcrError as e:
                doc = reader.read_doc(p, self.work_dir / "cache", None)
                warn = str(e)
            else:
                warn = ""
            old = self.state["files"].get(rel, {})
            if old.get("sha1") == doc.sha1 and old.get("cands") is not None and not force:
                return self.file_view(rel)
            kind, cands = rules.extract(doc, p.name, self.case.get("jiko", {}).get("date"))
            cj = [c.to_json() for c in cands]
            ev = cj[0]
            evf = {f["name"]: f for f in ev["fields"]}
            evf["key"]["value"] = merge.unique_key(self.case, evf["key"]["value"], rel, self._owners())
            prev = {(c["label"], json.dumps(c["op"], sort_keys=True)): c for c in old.get("cands", [])}
            for c in cj:
                o = prev.get((c["label"], json.dumps(c["op"], sort_keys=True)))
                if o is not None:
                    c["accepted"], c["fields"], c["applied"] = o.get("accepted"), o["fields"], o.get("applied", False)
            self.state["files"][rel] = {"sha1": doc.sha1, "kind": kind, "evkey": old.get("evkey"), "cands": cj,
                                        "ai_cands": old.get("ai_cands", []) if old.get("sha1") == doc.sha1 else [],
                                        "unread": doc.unread_pages(), "pages": len(doc.pages), "warn": warn}
            self._save_state()
            return self.file_view(rel)

    def file_view(self, rel: str) -> dict:
        f = self.state["files"].get(rel)
        if f is None:
            return {"rel": rel, "read": False}
        return {"rel": rel, "read": True, "kind": f["kind"], "kind_name": rules.KIND_NAMES.get(f["kind"], f["kind"]),
                "evkey": f.get("evkey"), "pages": f.get("pages", 0), "unread": f.get("unread", []),
                "warn": f.get("warn", ""), "cands": f["cands"] + f.get("ai_cands", [])}

    def _doc(self, rel: str) -> reader.Doc:
        return reader.read_doc(self._path(rel), self.work_dir / "cache", None)

    def page_png(self, rel: str, page: int) -> bytes:
        return reader.render_png(self._path(rel), page)

    def ai_read(self, rel: str) -> dict:
        import ai
        with self.lock:
            if not self.settings.get("ai", {}).get("enabled"):
                raise AppError("AIは使わない設定です（設定の画面で「Bedrock を使う」にしてください）")
            if rel not in self.state["files"]:
                self.read_file(rel)
            doc = self._doc(rel)
        try:
            _, cands = ai.read_with_ai(doc, self.settings["ai"], work_dir=self.work_dir, client=self.ai_client)
        except ai.AiError as e:
            raise AppError(str(e)) from None
        with self.lock:
            f = self.state["files"][rel]
            js = [c.to_json() for c in cands]
            for c in js:
                if c["op"]["kind"] == "evidence":     # 証拠キーは規則の候補と同じにしておく
                    ev = next((x for x in f["cands"] if x["op"]["kind"] == "evidence"), None)
                    if ev:
                        k = next(x for x in ev["fields"] if x["name"] == "key")["value"]
                        next(x for x in c["fields"] if x["name"] == "key")["value"] = k
            f["ai_cands"] = js
            self._save_state()
            return self.file_view(rel)

    def decide(self, rel: str, decisions: dict) -> dict:
        """確認画面の○／×と直した値を保存し、○のうちまだ入れていないものを case.json に入れる。

        decisions: {候補id: {"accepted": true/false/null, "fields": {名前: 値}}}
        """
        with self.lock:
            f = self.state["files"].get(rel)
            if f is None:
                raise AppError("先に読み取ってください")
            allc = f["cands"] + f.get("ai_cands", [])
            for c in allc:
                d = (decisions or {}).get(c["id"])
                if not isinstance(d, dict):
                    continue
                acc = d.get("accepted")
                if acc not in (True, False, None):
                    raise AppError("○／×の値が正しくありません")
                changed = False
                for fl in c["fields"]:
                    if fl["name"] in (d.get("fields") or {}):
                        v = d["fields"][fl["name"]]
                        v = "" if v is None else v
                        if not isinstance(v, (str, int, float)):
                            raise AppError("値の形が正しくありません")
                        if str(v) != str(fl["value"]):
                            fl["value"] = v
                            changed = True
                if changed or acc is not True:
                    c["applied"] = False
                c["accepted"] = acc
            todo = [c for c in allc if c.get("accepted") and not c.get("applied")]
            for c in todo:     # 証拠キーは、ほかの書類のキーと重ならないようにしてから入れる
                if c["op"]["kind"] == "evidence":
                    kf = next(x for x in c["fields"] if x["name"] == "key")
                    kf["value"] = merge.unique_key(self.case, kf["value"], rel, self._owners())
            ev_in_todo = any(c["op"]["kind"] == "evidence" for c in todo)
            evkey, errors = merge.apply_doc(self.case, todo, None if ev_in_todo else f.get("evkey"))
            failed = {e.split("：", 1)[0] for e in errors}
            for c in todo:
                if c["label"] not in failed:
                    c["applied"] = True
            if not any(c["op"]["kind"] == "evidence" and c.get("accepted") for c in allc):
                evkey = None    # 証拠を×にした（case.json の証拠は消さない。入力の画面で消せる）
            f["evkey"] = evkey
            self._save_state()
            self._save_case()
            return {"file": self.file_view(rel), "errors": errors, "case": self.case}

    # --- 下書き ---
    def ai_draft(self, what: str, keys: list[str]) -> dict:
        import ai
        if not self.settings.get("ai", {}).get("enabled"):
            raise AppError("AIは使わない設定です（設定の画面で「Bedrock を使う」にしてください）")
        by_key = {f.get("evkey"): rel for rel, f in self.state["files"].items() if f.get("evkey")}
        docs = []
        for k in keys or []:
            if k not in by_key:
                raise AppError(f"証拠キー {k} の書類がまだ確認されていません")
            docs.append((k, self._doc(by_key[k])))
        try:
            c = ai.draft_with_ai(what, docs, self.case, self.settings["ai"], work_dir=self.work_dir,
                                 client=self.ai_client)
        except ai.AiError as e:
            raise AppError(str(e)) from None
        with self.lock:
            self.state["drafts"] = [d for d in self.state["drafts"] if d["id"] != c.id] + [c.to_json()]
            self._save_state()
            return {"drafts": self.state["drafts"]}

    def decide_draft(self, did: str, accepted: bool, body: str) -> dict:
        with self.lock:
            d = next((x for x in self.state["drafts"] if x["id"] == did), None)
            if d is None:
                raise AppError("下書きがありません")
            d["fields"][0]["value"] = str(body or "")
            d["accepted"] = bool(accepted)
            if accepted:
                try:
                    merge.apply_cand(self.case, d, None)
                except merge.MergeError as e:
                    raise AppError(str(e)) from None
                self.state["drafts"] = [x for x in self.state["drafts"] if x["id"] != did]
                self._save_case()
            self._save_state()
            return {"drafts": self.state["drafts"], "case": self.case}

    # --- 入力 ---
    def save_case(self, case) -> dict:
        if not isinstance(case, dict):
            raise AppError("case の形が正しくありません")
        with self.lock:
            self.case = case
            self._save_case()
            return {"ok": True, "checks": self.checks()}

    def save_settings(self, s) -> dict:
        if not isinstance(s, dict):
            raise AppError("設定の形が正しくありません")
        with self.lock:
            merged = load_settings()
            for k in ("office", "ai"):
                if isinstance(s.get(k), dict):
                    merged[k].update(s[k])
            if isinstance(s.get("tokumeiocr_dir"), str):
                merged["tokumeiocr_dir"] = s["tokumeiocr_dir"]
            save_settings(merged)
            self.settings = merged
            return {"settings": self.settings_view()}

    def settings_view(self) -> dict:
        s = copy.deepcopy(self.settings)
        s["tokumeiocr_found"] = bool(self.ocr_command())
        return s

    # --- 確認 ---
    def checks(self) -> dict:
        """書面を作る前に見ておくところ。missing＝足りない入力、judgment＝人が決める項目、todo＝不明点。"""
        c = self.case
        missing = []

        def need(cond, msg, step):
            if not cond:
                missing.append({"msg": msg, "step": step})
        j = c.get("jiko", {})
        need(j.get("date"), "事故日", "jiko")
        need(j.get("place"), "事故の場所", "jiko")
        need(j.get("vehicles"), "関係車両（1台以上）", "jiko")
        need(c.get("sekinin"), "責任原因（1件以上）", "jiko")
        sj = c.get("sojou", {})
        need(sj.get("court"), "提出先の裁判所", "jiko")
        need(sj.get("date"), "訴状の日付", "jiko")
        if c.get("shibou"):
            need(sj.get("plaintiffs"), "原告（相続人）", "jiko")
        else:
            need((sj.get("plaintiff") or {}).get("name"), "原告の氏名", "jiko")
        need(sj.get("defendants"), "被告（1人以上）", "jiko")
        need(c.get("shogai") or c.get("busson"), "人損（けが）か物損の入力", "injury")
        office = self.settings.get("office", {})
        need(office.get("office_name") and office.get("address"), "事務所の情報（設定の画面）", "settings")
        judg = []

        def walk(o, path):
            if isinstance(o, dict):
                if o.get("judgment"):
                    judg.append({"path": path, "label": o.get("key") or path})
                for k, v in o.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")
        walk(c, "")
        unconfirmed = [rel for rel in self.pdfs() if not self.state["files"].get(rel, {}).get("evkey")]
        return {"missing": missing, "judgment": judg, "todo": c.get("todo", []), "unconfirmed": unconfirmed}

    # --- 書面 ---
    def build(self, full: bool = False) -> dict:
        with self.lock:
            case = merge.strip_private(self.case)
            office = self.settings.get("office", {})
            try:
                res = core.build_all(case, office, self.out_dir, full=full)
            except core.BuildError as e:
                raise AppError(str(e)) from None
            except PermissionError as e:
                raise AppError(f"{Path(e.filename or '').name} を書き換えられませんでした。Excel・Word で開いているときは閉じてから、もう一度押してください") from None
            self._save_case()
            res["files"] = [Path(f).name for f in res["files"]]
            return res

    # --- まとめ ---
    def data(self) -> dict:
        with self.lock:
            files = []
            for rel in self.pdfs():
                f = self.state["files"].get(rel)
                files.append({"rel": rel, "read": f is not None,
                              "kind_name": rules.KIND_NAMES.get(f["kind"], "") if f else "",
                              "evkey": (f or {}).get("evkey"),
                              "open": sum(1 for c in (f or {}).get("cands", []) + (f or {}).get("ai_cands", [])
                                          if c.get("accepted") is None and c["op"]["kind"] != "info"),
                              "unread": (f or {}).get("unread", [])})
            return {"case_dir": str(self.case_dir), "case_name": self.case_dir.name, "case": self.case,
                    "files": files, "settings": self.settings_view(), "checks": self.checks(),
                    "drafts": self.state.get("drafts", []),
                    "outputs": sorted(p.name for p in self.out_dir.glob("*") if p.is_file()) if self.out_dir.is_dir() else []}

    def open(self, which: str) -> dict:
        targets = {"case": self.case_dir, "out": self.out_dir, "xlsx": self.out_dir / "原告一覧表.xlsx",
                   "docx": self.out_dir / "訴状.docx", "memo": self.out_dir / "訴状_mints入力メモ.md"}
        if which.startswith("file:"):
            p = self._path(which[5:])
        elif which in targets:
            p = targets[which]
        else:
            raise AppError("開けないものです")
        if not p.exists():
            raise AppError(f"まだありません: {p.name}")
        if os.name == "nt":
            os.startfile(str(p))  # noqa: S606（このPCのエクスプローラー・Excel・Word で開く）
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}

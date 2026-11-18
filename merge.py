"""確認画面で○にした候補を case.json に入れる。

候補（rules.Cand / ai.py の候補）の op ごとに入れ方を決めている。同じ候補を2回入れても二重にならないようにする
（レセプトの行・治療の期間・自賠責の受領は、同じ中身の行があれば足さない）。
証拠を参照する値（レセプトの行の証拠、自賠責の受領の証拠など）は、その書類の証拠キーを使うので、
先に「証拠」の候補を○にしておく必要がある（apply_doc が順番を守る）。
"""

from __future__ import annotations

import datetime as dt
import re

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class MergeError(Exception):
    pass


def _val(fields, name):
    for f in fields:
        if f["name"] == name:
            return f.get("value")
    return None


def _int(v, label, allow_empty=True):
    if v in (None, ""):
        if allow_empty:
            return None
        raise MergeError(f"{label}が空です")
    try:
        return int(str(v).replace(",", "").strip())
    except ValueError:
        raise MergeError(f"{label}は数字で入れてください（{v}）") from None


def _date(v, label, allow_empty=True):
    if v in (None, ""):
        if allow_empty:
            return None
        raise MergeError(f"{label}が空です")
    try:
        return dt.date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise MergeError(f"{label}は 2024-01-31 の形で入れてください（{v}）") from None


def _set_path(case, path, value):
    cur = case
    for k in path[:-1]:
        if isinstance(k, int):
            while len(cur) <= k:
                cur.append({})
            cur = cur[k]
        else:
            cur = cur.setdefault(k, [] if isinstance(path[path.index(k) + 1], int) else {})
    last = path[-1]
    if isinstance(last, int):
        while len(cur) <= last:
            cur.append(None)
    cur[last] = value


def _item(case, key, create=True):
    items = case.setdefault("songai", {}).setdefault("items", [])
    for it in items:
        if it.get("key") == key:
            return it
    if not create:
        return None
    it = {"key": key}
    items.append(it)
    return it


def unique_key(case, base: str, owner: str | None, owners: dict) -> str:
    """証拠キーを、ほかの書類と重ならないようにする（receipt → receipt_2 …）。owners: {キー: 書類}。"""
    base = re.sub(r"[^a-z0-9_]", "_", (base or "shoko").lower()).strip("_") or "shoko"
    if not base[0].isalpha():
        base = "s_" + base
    key, n = base, 1
    while key in case.get("evidence", {}) and owners.get(key) != owner:
        n += 1
        key = f"{base}_{n}"
    return key


def apply_cand(case: dict, c: dict, evkey: str | None) -> str | None:
    """1つの候補を入れる。戻り値: 証拠の候補なら、そのキー。"""
    op, fs = c["op"], c["fields"]
    kind = op["kind"]
    j = bool(c.get("judgment"))

    def need_ev():
        if not evkey:
            raise MergeError("この書類の「証拠」の候補を先に○にしてください（証拠キーが要ります）")
        return evkey

    if kind == "info":
        return None
    if kind == "evidence":
        key = _val(fs, "key")
        if not key or not KEY_RE.match(key):
            raise MergeError(f"証拠キーは半角の英小文字・数字・_ で、英字から始めてください（{key}）")
        ev = {"title": _val(fs, "title") or "", "date": _date(_val(fs, "date"), "作成日"),
              "author": _val(fs, "author") or "", "purpose": _val(fs, "purpose") or ""}
        if _val(fs, "kind"):
            ev["kind"] = _val(fs, "kind")
        old = case.setdefault("evidence", {}).get(key, {})
        for k in ("jikkyo_author", "taisho", "basho"):     # 画面のほかの場所で入れた項目は残す
            if k in old:
                ev[k] = old[k]
        case["evidence"][key] = ev
        return key
    if kind == "set":
        v = _val(fs, "value")
        vt = next((f["vtype"] for f in fs if f["name"] == "value"), "text")
        if vt == "date":
            v = _date(v, c["label"], False)
        elif vt == "int":
            v = _int(v, c["label"], False)
        elif vt == "time" and not re.match(r"^\d{2}:\d{2}$", str(v or "")):
            raise MergeError("時刻は 18:00 の形で入れてください")
        _set_path(case, op["path"], v)
        ev = op.get("ev_path")
        if ev and evkey:
            lst = case
            for k in ev[:-1]:
                lst = lst.setdefault(k, {})
            arr = lst.setdefault(ev[-1], [])
            if evkey not in arr:
                arr.append(evkey)
        return None
    if kind == "party":
        role = _val(fs, "role")
        if role == "入れない":
            return None
        if role not in ("原告", "被告"):
            raise MergeError("原告か被告かを選んでください")
        person = {"name": _val(fs, "name") or "", "address": _val(fs, "address") or ""}
        sj = case.setdefault("sojou", {})
        if role == "原告":
            sj.setdefault("plaintiff", {}).update(person)
        else:
            ds = sj.setdefault("defendants", [])
            if not any(d.get("name") == person["name"] for d in ds):
                ds.append({"zip": "", **person})
        typ, reg = _val(fs, "type"), _val(fs, "reg")
        if typ or reg:
            side = role + "側"
            vs = case.setdefault("jiko", {}).setdefault("vehicles", [])
            v = next((x for x in vs if x.get("side") == side), None)
            if v is None:
                v = {"side": side, "type": "", "reg": "", "driver": role, "owner": role, "ev": []}
                vs.append(v)
            if typ:
                v["type"] = typ
            if reg:
                v["reg"] = reg
            if evkey and evkey not in v.setdefault("ev", []):
                v["ev"].append(evkey)
        return None
    if kind == "receipt":
        row = [_val(fs, "hospital") or "", _val(fs, "ym") or "", _int(_val(fs, "nyu"), "入院日数"),
               _int(_val(fs, "tsu"), "通院日数"), _int(_val(fs, "amount"), "金額", False), need_ev()]
        if not re.match(r"^[RH]\d{1,2}\.\d{1,2}$", row[1]):
            raise MergeError(f"年月は R6.1 の形で入れてください（{row[1]}）")
        rs = case.setdefault("receipts", [])
        if row not in rs:
            rs.append(row)
        return None
    if kind == "treatment":
        t = {"hospital": _val(fs, "hospital") or "", "type": _val(fs, "type"),
             "start": _date(_val(fs, "start"), "始まり", False), "end": _date(_val(fs, "end"), "終わり", False)}
        if t["type"] not in ("入院", "通院"):
            raise MergeError("入院か通院かを選んでください")
        tr = case.setdefault("shogai", {}).setdefault("treatment", [])
        if t not in tr:
            tr.append(t)
            tr.sort(key=lambda x: (x["start"], x["type"] != "入院"))
        return None
    if kind == "fixed":
        fx = {"date": _date(_val(fs, "date"), "症状固定日", False), "part": _val(fs, "part") or ""}
        lst = case.setdefault("shogai", {}).setdefault("fixed", [])
        if not any(x.get("date") == fx["date"] and x.get("part") == fx["part"] for x in lst):
            lst.append(fx)
        return None
    if kind == "payment":
        p = {"date": _date(_val(fs, "date"), "受領日", False), "amount": _int(_val(fs, "amount"), "受領額", False),
             "ev": need_ev()}
        it = _item(case, "自賠責保険金")
        it.pop("amount", None)
        it.setdefault("method", "genpon")
        it["judgment"] = True
        pays = it.setdefault("payments", [])
        if not any(x["date"] == p["date"] and x["amount"] == p["amount"] for x in pays):
            pays.append(p)
            pays.sort(key=lambda x: x["date"])
        return None
    if kind == "item":
        it = _item(case, op["key"])
        if op["key"] == "後遺障害慰謝料":
            g = _int(_val(fs, "grade"), "等級", False)
            if not 1 <= g <= 14:
                raise MergeError("等級は1〜14で入れてください")
            it.update({"amount": "akai", "grade": g, "judgment": True})
        return None
    if kind == "item_add":
        key = _val(fs, "key")
        amt = _int(_val(fs, "amount"), "金額", False)
        it = _item(case, key)
        srcs = it.setdefault("_src", [])
        tag = f"{evkey}:{amt}"
        if tag in srcs:
            return None
        srcs.append(tag)
        it["amount"] = (it.get("amount") if isinstance(it.get("amount"), int) else 0) + amt
        if evkey:
            it.setdefault("ev", [])
            if evkey not in it["ev"]:
                it["ev"].append(evkey)
        it["judgment"] = j or it.get("judgment", False)
        return None
    if kind == "income":
        iss = case.setdefault("songai", {}).setdefault("issitsu", [])
        if not iss:
            iss.append({"loss_rate": None, "living": 0, "end_age": 67})
        iss[0].update({"income": _int(_val(fs, "income"), "基礎収入", False),
                       "income_reason": _val(fs, "reason") or "", "judgment": True})
        if evkey:
            iss[0]["income_ev"] = sorted(set(iss[0].get("income_ev", [])) | {evkey})
        return None
    if kind in ("kyugyo_daily", "kyugyo_end"):
        ky = case.setdefault("songai", {}).setdefault("kyugyo", [])
        if not ky:
            ky.append({"end": None, "daily": None, "count": "期間", "rate": 1.0})
        if kind == "kyugyo_daily":
            ky[0].update({"daily": _int(_val(fs, "daily"), "日額", False), "daily_reason": _val(fs, "reason") or "",
                          "judgment": True})
            if evkey:
                ky[0]["daily_ev"] = sorted(set(ky[0].get("daily_ev", [])) | {evkey})
        else:
            ky[0].update({"end": _date(_val(fs, "end"), "終期", False), "count": "期間", "judgment": True})
            if evkey:
                ky[0]["count_ev"] = sorted(set(ky[0].get("count_ev", [])) | {evkey})
        return None
    if kind == "busson_item":
        key = _val(fs, "key")
        if not key or key.startswith("（"):
            return None
        b = case.setdefault("busson", {}).setdefault("items", [])
        it = next((x for x in b if x.get("key") == key), None)
        if it is None:
            it = {"key": key}
            b.append(it)
        it.update({"amount": _int(_val(fs, "amount"), "金額", False), "reason": _val(fs, "reason") or "",
                   "judgment": True})
        if evkey:
            it["ev"] = sorted(set(it.get("ev", [])) | {evkey})
        return None
    raise MergeError(f"知らない入れ方です: {kind}")


def apply_doc(case: dict, cands: list[dict], evkey: str | None = None) -> tuple[str | None, list[str]]:
    """1つの書類の候補のうち、○（accepted=True）のものを入れる。証拠の候補を先に入れる。

    戻り値: (証拠キー, エラーのリスト)。エラーの候補は入れずに続ける。
    """
    errors = []
    order = sorted((c for c in cands if c.get("accepted")), key=lambda c: c["op"]["kind"] != "evidence")
    for c in order:
        try:
            k = apply_cand(case, c, evkey)
            if k:
                evkey = k
        except MergeError as e:
            errors.append(f"{c['label']}：{e}")
    return evkey, errors


def strip_private(case: dict) -> dict:
    """書面を作る前に、アプリだけが使う印（_src）を外した写しを作る。"""
    import copy
    c = copy.deepcopy(case)
    for it in c.get("songai", {}).get("items", []):
        it.pop("_src", None)
    return c

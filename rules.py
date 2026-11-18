"""証拠PDFの文字から、case.json に入れる値の候補を拾う（決まった規則で。AIは使わない）。

1通ごとに
  1. 書類の種類を決める（表題の語のうち、文書のいちばん前に出てくるもの）
  2. 種類ごとの規則で値を拾い、候補（Cand）にする。候補には、入れる場所（op）と、拾ったページ・位置（rects）を付ける
  3. 証拠の標目・作成日・作成者の下書き（証拠キーの候補）も、候補の1つとして出す
候補はそのままでは case.json に入らない。確認画面で人が○にしたものだけが入る（app.py の apply_cands）。
拾えなかった項目は候補が出ないだけで、画面で手入力する。

判断が要る項目（甲乙のどちらが原告か、自賠責の充当方法、等級、基礎収入など）には judgment を付ける。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from reader import Doc, Page

# --- 書類の種類 ------------------------------------------------------------------------

# (種類, 表題の語, 証拠の標目の既定, 作成者の既定, 立証趣旨の既定, 証拠キーの既定)
KINDS = [
    ("jiko_shomei", ["交通事故証明書"], "交通事故証明書", "自動車安全運転センター",
     "本件事故の発生日時、場所、当事者等", "jiko_shomei"),
    ("bukken", ["物件事故報告書"], None, None, None, "bukken"),
    ("keiji", ["刑事記録"], None, None, None, "keiji"),
    ("jikkyo", ["実況見分調書"], None, None, None, "jikkyo"),
    ("kouisho_shindan", ["後遺障害診断書"], "後遺障害診断書", None, "原告の後遺障害の内容", "kouisho_shindan"),
    ("nintei", ["後遺障害等級認定", "等級認定結果", "認定結果のご通知", "事前認定結果"], "後遺障害等級認定結果通知書",
     "損害保険料率算出機構", "自賠責保険における等級認定の結果", "nintei"),
    ("chouzai", ["調剤報酬明細書"], "調剤報酬明細書", None, "原告の薬剤費", "receipt"),
    ("receipt", ["診療報酬明細書"], "診療報酬明細書", None, "原告の治療経過及び治療費", "receipt"),
    ("shindansho", ["診断書"], "診断書", None, "原告の傷病名及び治療経過", "shindansho"),
    ("kyugyo_shomei", ["休業損害証明書"], "休業損害証明書", "勤務先", "原告の職業及び休業の事実", "kyugyo_shomei"),
    ("gensen", ["源泉徴収票"], "源泉徴収票", "勤務先", "原告の事故前年の収入", "gensen"),
    ("kyuyo", ["給与明細", "給料明細", "支給明細"], "給与明細書", "勤務先", "原告の事故前の収入", "kyuyo"),
    ("kotsuhi", ["通院交通費明細書"], "通院交通費明細書", "原告", "原告の通院交通費", "kotsuhi"),
    ("shiharai", ["支払通知", "支払のご案内", "お支払いのご案内", "お支払のご案内", "保険金のお支払", "支払明細"],
     "保険金支払通知書", None, None, "shiharai"),
    ("mitsumori", ["御見積書", "見積書", "御見積"], None, None, None, "mitsumori"),
    ("ryoshu", ["領収書", "領収証"], None, None, None, "ryoshu"),
    ("seikyu", ["請求書"], "請求書", None, None, "seikyu"),
    ("kaitou", ["回答書"], "回答書", None, None, "kaitou"),
]
KIND_NAMES = {
    "jiko_shomei": "交通事故証明書", "bukken": "物件事故報告書", "keiji": "刑事記録", "jikkyo": "実況見分調書",
    "kouisho_shindan": "後遺障害診断書", "nintei": "後遺障害の認定結果", "chouzai": "調剤報酬明細書",
    "receipt": "診療報酬明細書", "shindansho": "診断書", "kyugyo_shomei": "休業損害証明書", "gensen": "源泉徴収票",
    "kyuyo": "給与明細", "kotsuhi": "通院交通費明細書", "shiharai": "保険金の支払通知", "mitsumori": "見積書",
    "ryoshu": "領収書", "seikyu": "請求書", "kaitou": "回答書", "unknown": "（種類が分からない書類）",
}
# スキルの kind（build_kyoutuu.apply_presets が標目・立証趣旨の定型を入れる）
SKILL_KIND = {"bukken": "bukken", "jikkyo": "jikkyo", "keiji": "keiji", "mitsumori": "mitsumori", "ryoshu": "ryoshu"}


def classify(doc: Doc) -> str:
    text = doc.text
    best = ("unknown", len(text) + 1)
    for kind, words, *_ in KINDS:
        for w in words:
            i = text.find(w)
            if i >= 0 and i < best[1]:
                best = (kind, i)
    return best[0]


# --- 候補 ----------------------------------------------------------------------------

@dataclass
class Cand:
    label: str                     # 画面に出す項目名
    op: dict                       # 入れ方（app.apply_cands が解釈する）
    fields: list                   # [{"name", "label", "vtype", "value", "options"?}] 画面で直せる値
    page: int | None = None
    rects: list = field(default_factory=list)
    quote: str = ""                # 拾った元の文字
    note: str = ""
    judgment: bool = False
    source: str = "規則"
    id: str = ""
    accepted: bool | None = None   # None＝まだ決めていない、True＝○、False＝×

    def to_json(self):
        return asdict(self)


def F(name, label, vtype, value, options=None):
    d = {"name": name, "label": label, "vtype": vtype, "value": "" if value is None else value}
    if options:
        d["options"] = options
    return d


# --- 値の読み方 ----------------------------------------------------------------------

ERA = {"令和": 2018, "平成": 1988, "昭和": 1925, "R": 2018, "H": 1988, "S": 1925}
DATE = (r"(?:(?P<era>令和|平成|昭和|R|H|S)\s?(?P<ey>元|\d{1,2})\s?[年.]\s?(?P<em>\d{1,2})\s?[月.]\s?(?P<ed>\d{1,2})\s?日?"
        r"|(?P<y>(?:19|20)\d{2})\s?[年/.-]\s?(?P<m>\d{1,2})\s?[月/.-]\s?(?P<d>\d{1,2})\s?日?)")
DATE_RE = re.compile(DATE)
MONEY = r"(?:¥\s?)?(?P<amt>\d{1,3}(?:,\d{3})+|\d+)\s?円?"


def iso(m: re.Match, prefix: str = "") -> str | None:
    g = m.groupdict()
    era, ey, em, ed = (g.get(prefix + k) for k in ("era", "ey", "em", "ed"))
    try:
        if era:
            y = ERA[era] + (1 if ey == "元" else int(ey))
            mo, d = int(em), int(ed)
        else:
            y, mo, d = int(g[prefix + "y"]), int(g[prefix + "m"]), int(g[prefix + "d"])
        import datetime as dt
        return dt.date(y, mo, d).isoformat()
    except (TypeError, ValueError, KeyError):
        return None


def to_int(s: str) -> int | None:
    try:
        return int(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def wareki_ym(y: int, m: int) -> str:
    """治療費等集計表の年月（R6.1 の形）。"""
    if y >= 2019 and not (y == 2019 and m < 5):
        return f"R{y - 2018}.{m}"
    return f"H{y - 1988}.{m}"


class Finder:
    """ページの文字から正規表現で探し、見つけた場所（ページ・枠）を付けて返す。"""

    def __init__(self, doc: Doc):
        self.doc = doc

    def find(self, pattern, pages=None, flags=0):
        for p in self._pages(pages):
            m = re.search(pattern, p.text, flags)
            if m:
                return p, m
        return None, None

    def find_all(self, pattern, pages=None, flags=0):
        for p in self._pages(pages):
            for m in re.finditer(pattern, p.text, flags):
                yield p, m

    def _pages(self, pages):
        return [p for p in self.doc.pages if pages is None or p.no in pages]

    @staticmethod
    def where(p: Page, m: re.Match, group=0):
        s, e = m.span(group)
        return dict(page=p.no, rects=p.box_of(s, e), quote=p.text[s:e])


def _row_rest(text: str, pos: int) -> str:
    """pos から行の終わりまで。"""
    e = text.find("\n", pos)
    return text[pos:] if e < 0 else text[pos:e]


# --- 種類ごとの規則 --------------------------------------------------------------------

VEHICLE_TYPES = ["普通乗用自動車", "普通貨物自動車", "大型乗用自動車", "大型貨物自動車", "普通自動二輪車",
                 "小型自動二輪車", "原動機付自転車", "電動アシスト付き自転車", "自転車", "歩行者"]
VEHICLE_ALIASES = [("軽乗用", "普通乗用自動車"), ("軽貨物", "普通貨物自動車"), ("普通乗用", "普通乗用自動車"),
                   ("自家用乗用", "普通乗用自動車"), ("普通貨物", "普通貨物自動車"), ("大型乗用", "大型乗用自動車"),
                   ("大型貨物", "大型貨物自動車"), ("中型", "普通貨物自動車"), ("大型自動二輪", "普通自動二輪車"),
                   ("普通自動二輪", "普通自動二輪車"), ("自動二輪", "普通自動二輪車"), ("小型二輪", "小型自動二輪車"),
                   ("原付", "原動機付自転車"), ("原動機付", "原動機付自転車"), ("電動アシスト", "電動アシスト付き自転車"),
                   ("自転車", "自転車"), ("歩行者", "歩行者")]
ROLES = ["原告", "被告", "入れない"]


def vehicle_type(s: str) -> str | None:
    for t in VEHICLE_TYPES:
        if t in s:
            return t
    for a, t in VEHICLE_ALIASES:
        if a in s:
            return t
    return None


def r_jiko_shomei(f: Finder):
    out = []
    p, m = f.find(r"発生日時\D{0,6}?" + DATE + r"\D{0,4}?(?P<hh>\d{1,2})\s?時\s?(?P<mm>\d{1,2})?\s?分?")
    if p:
        out.append(Cand("事故の日", {"kind": "set", "path": ["jiko", "date"]},
                        [F("value", "事故日", "date", iso(m))], **f.where(p, m)))
        hh, mm = int(m["hh"]), int(m["mm"] or 0)
        if hh < 24 and mm < 60:
            out.append(Cand("事故の時刻", {"kind": "set", "path": ["jiko", "time"]},
                            [F("value", "時刻", "time", f"{hh:02d}:{mm:02d}")], **f.where(p, m)))
    p, m = f.find(r"発生場所\s?(?P<v>[^\n]+)")
    if p:
        v = re.sub(r"\s?(付近|地先)$", r"\1", m["v"].strip())
        out.append(Cand("事故の場所", {"kind": "set", "path": ["jiko", "place"]},
                        [F("value", "場所", "text", v)], **f.where(p, m, "v")))
    # 甲・乙：氏名・住所・車種・車両番号は、出てきた順に1つ目が甲、2つ目が乙
    names = list(f.find_all(r"氏名\s?(?P<v>[^\s\d(（]{1,12}(?:\s[^\s\d(（]{1,8})?)"))
    addrs = list(f.find_all(r"住所\s?(?P<v>[^\n]+?)(?=\s(?:氏名|電話|TEL|生年)|$|\n)"))
    types = list(f.find_all(r"車種\s?(?P<v>\S+)"))
    regs = list(f.find_all(r"車両番号\s?(?P<v>[^\s\d]{1,4}\s?\d{1,3}\s?[ぁ-んA-Z]\s?\d{1,2}-?\d{1,2}|\S+)"))
    for i, (ko, role) in enumerate((("甲", "被告"), ("乙", "原告"))):
        nm = names[i] if i < len(names) else None
        if not nm:
            continue
        ad = addrs[i] if i < len(addrs) else None
        ty = types[i] if i < len(types) else None
        rg = regs[i] if i < len(regs) else None
        pg, mt = nm
        w = f.where(pg, mt, "v")
        out.append(Cand(
            f"{ko}（{'第一' if ko == '甲' else '第二'}当事者）", {"kind": "party", "ko": ko},
            [F("role", "原告か被告か", "choice", role, ROLES),
             F("name", "氏名", "text", mt["v"].replace(" ", "")),
             F("address", "住所", "text", ad[1]["v"].strip() if ad else ""),
             F("type", "車種", "choice", (vehicle_type(ty[1]["v"]) if ty else None) or "", [""] + VEHICLE_TYPES),
             F("reg", "車両番号", "text", rg[1]["v"].strip() if rg else "")],
            note="交通事故証明書の甲は、ふつう過失の大きい方（第一当事者）。どちらが原告かを確かめる",
            judgment=True, **w))
    p, m = f.find(r"(?P<v>人身|物件)事故")
    if p:
        out.append(Cand("人身事故か物件事故か（参考）", {"kind": "info"},
                        [F("value", "種別", "text", m["v"] + "事故")], **f.where(p, m)))
    return out


def _hospital(f: Finder, pages=None, suffix=r"病院|医院|クリニック|診療所|整形外科|接骨院|整骨院|医療センター"):
    p, m = f.find(r"(?:医療機関(?:の)?名称|保険医療機関(?:の)?名称|病院名|薬局名|名称)\s?(?P<v>\S{2,30})", pages)
    if p and re.search(suffix, m["v"]):
        return p, m
    return f.find(r"(?P<v>[^\s:：、。()（）]{1,24}(?:" + suffix + r"))", pages)


def r_receipt(f: Finder, kind: str):
    """診療報酬明細書・調剤報酬明細書。1つのPDFに何か月分か入っていれば、月ごとに1行。"""
    out = []
    pharmacy = kind == "chouzai"
    seen = set()
    for p in f.doc.pages:
        m = re.search(r"(?:(?P<era>令和|平成|R|H)\s?)?(?P<ey>元|\d{1,2})\s?年\s?(?P<em>\d{1,2})\s?月\s?分", p.text)
        if not m:
            continue
        era = m["era"] or "令和"
        y = ERA[era] + (1 if m["ey"] == "元" else int(m["ey"]))
        ym = wareki_ym(y, int(m["em"]))
        hp, hm = _hospital(f, [p.no], r"薬局" if pharmacy else
                           r"病院|医院|クリニック|診療所|整形外科|接骨院|整骨院|医療センター")
        hospital = hm["v"] if hm else ""
        if (hospital, ym) in seen:      # 同じ月の続きのページ
            continue
        seen.add((hospital, ym))
        title_part = p.text[:200]
        inpatient = (not pharmacy) and "入院" in title_part and "入院外" not in title_part
        dm = re.search(r"(?:診療)?実日数\s?(?P<v>\d{1,3})", p.text) or re.search(r"(?:入院|通院|外来)\s?(?P<v>\d{1,2})\s?日", p.text)
        days = int(dm["v"]) if dm else None
        am = re.search(r"(?:合計|請求額|請求金額|総額|負担金額|金額)\D{0,12}?" + MONEY.replace("円?", "円"), p.text)
        note = ""
        if am:
            amount = to_int(am["amt"])
        else:
            pm = re.search(r"(?:合計|請求|総)\D{0,8}?(?P<amt>\d{1,3}(?:,\d{3})+|\d{2,})\s?点", p.text)
            amount = to_int(pm["amt"]) * 10 if pm else None
            note = "点数×10円で計算した。自賠責・自由診療で単価が違うときは直す" if pm else "金額が拾えなかった"
            am = pm
        out.append(Cand(
            f"{'調剤' if pharmacy else '診療'}報酬明細書 {ym}", {"kind": "receipt"},
            [F("hospital", "医療機関", "text", hospital),
             F("ym", "年月（R6.1の形）", "text", ym),
             F("nyu", "入院日数", "int", "" if pharmacy or not inpatient else days),
             F("tsu", "通院日数", "int", "" if pharmacy or inpatient else days),
             F("amount", "金額（円）", "int", amount)],
            note=note, **Finder.where(p, am or m)))
    return out


def r_shindansho(f: Finder, kind: str):
    out = []
    p, m = f.find(r"傷病名\s?(?P<v>[^\n]+)")
    if p:
        out.append(Cand("傷病名", {"kind": "set", "path": ["shogai", "injuries"]},
                        [F("value", "傷病名", "text", m["v"].strip())], **f.where(p, m, "v")))
    hp, hm = _hospital(f)
    hospital = hm["v"] if hm else ""
    if kind == "kouisho_shindan":
        p, m = f.find(r"症状固定日\D{0,6}?" + DATE)
        if p:
            out.append(Cand("症状固定日", {"kind": "fixed"},
                            [F("date", "症状固定日", "date", iso(m)), F("part", "部位", "text", "")],
                            **f.where(p, m)))
        return out
    period = DATE.replace("?P<", "?P<a") + r"\s?(?:から|より|〜|~|-|至)\s?" + DATE.replace("?P<", "?P<b")
    for typ, pat in (("入院", r"入院\D{0,10}?"), ("通院", r"(?:通院|外来|治療期間|加療期間|診療期間)\D{0,10}?")):
        for p, m in f.find_all(pat + period):
            a, b = iso(m, "a"), iso(m, "b")
            if a and b:
                out.append(Cand(f"{typ}の期間", {"kind": "treatment"},
                                [F("hospital", "医療機関", "text", hospital),
                                 F("type", "入院か通院か", "choice", typ, ["入院", "通院"]),
                                 F("start", "始まり", "date", a), F("end", "終わり", "date", b)],
                                **f.where(p, m)))
    return out


GRADE_RE = r"(?P<heigo>併合)?\s?第?\s?(?P<g>\d{1,2})\s?級(?:\s?(?P<go>\d{1,2})\s?号)?"


def r_nintei(f: Finder):
    out = []
    p, m = f.find(GRADE_RE)
    if p and 1 <= int(m["g"]) <= 14:
        label = f"{'併合' if m['heigo'] else ''}第{m['g']}級" + (f"{m['go']}号" if m["go"] else "")
        out.append(Cand("後遺障害慰謝料（赤い本・認定の等級で）", {"kind": "item", "key": "後遺障害慰謝料"},
                        [F("grade", "等級（1〜14）", "int", int(m["g"]))],
                        note=f"自賠責の認定は{label}。主張する等級が違うときは直す", judgment=True, **f.where(p, m)))
        out.append(Cand("自賠責の認定（参考）", {"kind": "info"}, [F("value", "認定", "text", label)], **f.where(p, m)))
    p, m = f.find(r"非該当")
    if p:
        out.append(Cand("自賠責の認定（参考）", {"kind": "info"}, [F("value", "認定", "text", "非該当")], **f.where(p, m)))
    return out


def _insurer(f: Finder):
    return f.find(r"(?P<v>[^\s:：、。()（）]{1,24}(?:保険株式会社|損害保険株式会社|共済(?:組合|連合会)?|保険会社))")


def r_shiharai(f: Finder):
    out = []
    pa, ma = f.find(r"(?:お?支払(?:い)?(?:保険)?金?額|支払保険金|振込金額|お支払金額|支払額)\D{0,12}?" + MONEY.replace("円?", "円"))
    pd, md = f.find(r"(?:お?支払(?:い)?(?:日|年月日)|振込日|お振込日)\D{0,6}?" + DATE)
    if not pa:
        return out
    amount = to_int(ma["amt"])
    date = iso(md) if pd else ""
    if "自賠責" in f.doc.text:
        out.append(Cand("自賠責保険金の受領", {"kind": "payment"},
                        [F("date", "受領日", "date", date), F("amount", "受領額（円）", "int", amount)],
                        note="充当方法（元本から控除／確定遅延損害金に先に充当）は判断の画面で決める",
                        judgment=True, **f.where(pa, ma)))
    else:
        out.append(Cand("任意保険金（既払金）", {"kind": "item_add"},
                        [F("key", "項目", "choice", "任意保険金(その他)", ["任意保険金(治療費)", "任意保険金(その他)"]),
                         F("amount", "金額（円）", "int", amount)],
                        note="治療費として病院に払われたものか、それ以外かを確かめる。同じ項目の額に足す",
                        judgment=True, **f.where(pa, ma)))
    return out


def r_gensen(f: Finder, jiko_date: str | None):
    out = []
    p, m = f.find(r"支払金額\D{0,24}?(?P<amt>\d{1,3}(?:,\d{3})+|\d{5,})")
    if not p:
        return out
    income = to_int(m["amt"])
    ym = re.search(r"(?P<era>令和|平成)\s?(?P<ey>元|\d{1,2})\s?年分", f.doc.text)
    yr = f"{ym['era'][0] if ym['era'] == '令和' else 'H'}{ym['ey']}" if ym else ""
    yr = yr.replace("令", "R")
    reason = f"事故前年（{yr}）の源泉徴収票の支払金額" if yr else "源泉徴収票の支払金額"
    out.append(Cand("逸失利益の基礎収入（年額）", {"kind": "income"},
                    [F("income", "基礎収入（円）", "int", income), F("reason", "理由", "text", reason)],
                    note="実収入によるか、センサスによるか（主婦なら高い方）は判断の画面で決める", judgment=True,
                    **f.where(p, m)))
    out.append(Cand("休業損害の日額", {"kind": "kyugyo_daily"},
                    [F("daily", "日額（円）", "int", income // 365),
                     F("reason", "理由", "text", f"{reason}{income:,}円÷365日")],
                    note="源泉徴収票の年額÷365（円未満切捨て）。休業損害証明書の3か月分で出すときは直す", judgment=True,
                    **f.where(p, m)))
    p2, m2 = f.find(r"支払者\D{0,12}?(?:氏名|名称)\s?(?P<v>[^\n\s]{2,30})")
    if p2:
        out.append(Cand("勤務先（参考）", {"kind": "info"}, [F("value", "支払者", "text", m2["v"])], **f.where(p2, m2, "v")))
    _ = jiko_date
    return out


def r_kyugyo(f: Finder):
    out = []
    period = DATE.replace("?P<", "?P<a") + r"\s?(?:から|より|〜|~|-|至)\s?" + DATE.replace("?P<", "?P<b")
    p, m = f.find(r"(?:休業期間|休んだ期間|欠勤期間|休業した期間)\D{0,10}?" + period)
    if p:
        out.append(Cand("休業の終わりの日（期間1）", {"kind": "kyugyo_end"},
                        [F("end", "終期", "date", iso(m, "b"))],
                        note=f"休業期間 {iso(m, 'a')}〜{iso(m, 'b')}。始期は事故日になる", judgment=True, **f.where(p, m)))
    p, m = f.find(r"職種\s?(?P<v>[^\n\s]{1,20})")
    if p:
        out.append(Cand("職業", {"kind": "set", "path": ["shogai", "job"]},
                        [F("value", "職業", "text", m["v"])], **f.where(p, m, "v")))
    return out


BUSSON_KEYS = ["車両修理費", "レッカー費用", "代車費用", "車両時価額", "買替諸費用", "評価損", "休車損害", "（入れない）"]


def r_amount_doc(f: Finder, kind: str):
    """見積書・領収書・請求書：総額（税込）と日付。何の費用かは画面で選ぶ。"""
    out = []
    pats = [r"(?:御見積金額|お見積金額|見積金額|ご請求金額|御請求金額|請求金額|ご請求額|領収金額|合計金額|総合計|総額)"
            r"\D{0,14}?" + MONEY.replace("円?", "円?"),
            r"(?:合計|計)\s?\(?税込\)?\D{0,8}?" + MONEY,
            r"¥\s?(?P<amt>\d{1,3}(?:,\d{3})+)"]
    p = m = None
    for pat in pats:
        p, m = f.find(pat)
        if p and to_int(m["amt"]) and to_int(m["amt"]) >= 100:
            break
        p = m = None
    if not p:
        return out
    text = f.doc.text
    guess = ("レッカー費用" if "レッカー" in text else "代車費用" if "代車" in text else
             "車両修理費" if re.search(r"修理|板金|塗装|部品", text) else "（入れない）")
    out.append(Cand(f"{KIND_NAMES[kind]}の金額", {"kind": "busson_item"},
                    [F("key", "物損の項目", "choice", guess, BUSSON_KEYS),
                     F("amount", "金額（円・税込）", "int", to_int(m["amt"])),
                     F("reason", "理由等", "text", "")],
                    note="物損の項目でなければ「（入れない）」のまま。分損か全損かは判断の画面で決める",
                    judgment=True, **f.where(p, m)))
    return out


# --- 証拠の下書き ------------------------------------------------------------------

def _doc_date(f: Finder) -> tuple[str | None, Page | None, re.Match | None]:
    for lab in (r"(?:証明日|作成日|発行日|交付日|診断日|作成年月日|発行年月日|見積日|請求日)\D{0,6}?",
                r"上記のとおり(?:証明|診断)\D{0,10}?"):
        p, m = Finder(f.doc).find(lab + DATE)
        if p:
            return iso(m), p, m
    return None, None, None


def evidence_cand(f: Finder, kind: str, filename: str) -> Cand:
    row = next((k for k in KINDS if k[0] == kind), None)
    title, author, purpose = (row[2], row[3], row[4]) if row else (None, None, None)
    if kind in ("receipt", "chouzai", "shindansho", "kouisho_shindan"):
        hp, hm = _hospital(f, None, r"薬局" if kind == "chouzai" else
                           r"病院|医院|クリニック|診療所|整形外科|接骨院|整骨院|医療センター")
        if hm:
            author = hm["v"]
            if kind == "receipt":
                title = f"診療報酬明細書（{author}）"
    if kind in ("shiharai", "nintei"):
        pi, mi = _insurer(f)
        if mi and kind == "shiharai":
            author = mi["v"]
        if kind == "shiharai":
            title = "自賠責保険金支払通知書" if "自賠責" in f.doc.text else "保険金支払通知書"
            purpose = "自賠責保険金の支払を受けた事実" if "自賠責" in f.doc.text else "任意保険金の支払を受けた事実"
    if kind in ("mitsumori", "ryoshu", "seikyu"):
        p, m = f.find(r"(?P<v>(?:株式会社|有限会社)\s?[^\s\n]{1,20}|[^\s\n:：]{1,20}(?:株式会社|有限会社|自動車|モータース|オート|ボデー))")
        if m:
            author = m["v"]
    date, p, m = _doc_date(f)
    if title is None:
        title = re.sub(r"\.pdf$", "", filename, flags=re.I)
    base = row[5] if row else "shoko"
    fields = [F("key", "証拠キー（半角英小文字と_）", "text", base),
              F("title", "標目", "text", title or ""),
              F("date", "作成日", "date", date or ""),
              F("author", "作成者", "text", author or ""),
              F("purpose", "立証趣旨", "text", purpose or "")]
    if kind in SKILL_KIND:
        fields.append(F("kind", "定型（スキルの kind）", "text", SKILL_KIND[kind]))
    w = Finder.where(p, m) if p else {}
    return Cand(f"証拠（{KIND_NAMES.get(kind, kind)}）", {"kind": "evidence"}, fields,
                note="標目・作成者・立証趣旨を確かめる。定型（kind）があるときは標目・立証趣旨はスキルの定型が入る", **w)


# --- 入口 ------------------------------------------------------------------------

def extract(doc: Doc, filename: str, jiko_date: str | None = None) -> tuple[str, list[Cand]]:
    """戻り値: (書類の種類, 候補のリスト)。先頭は必ず証拠の下書き。"""
    kind = classify(doc)
    f = Finder(doc)
    cands = [evidence_cand(f, kind, filename)]
    if kind == "jiko_shomei":
        cands += r_jiko_shomei(f)
    elif kind in ("receipt", "chouzai"):
        cands += r_receipt(f, kind)
    elif kind in ("shindansho", "kouisho_shindan"):
        cands += r_shindansho(f, kind)
    elif kind == "nintei":
        cands += r_nintei(f)
    elif kind == "shiharai":
        cands += r_shiharai(f)
    elif kind == "gensen":
        cands += r_gensen(f, jiko_date)
    elif kind == "kyugyo_shomei":
        cands += r_kyugyo(f)
    elif kind in ("mitsumori", "ryoshu", "seikyu"):
        cands += r_amount_doc(f, kind)
    for i, c in enumerate(cands):
        c.id = f"r{i}"
    return kind, cands

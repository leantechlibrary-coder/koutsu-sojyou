"""Bedrock（Claude Sonnet 4.6、ZDR）で読み取る・下書きする（任意。使うときだけ）。

  - 確認画面で「AIで読む」「AIで下書き」を押したときだけ、選んだ書類を送る。ほかのときは通信しない
  - 送るのは、その書類のページの画像（PNG）と、テキスト層から読んだ文字
  - 答えは規則（rules.py）と同じ形の候補にして、同じ確認画面を通す。人が○にしたものだけが case.json に入る
  - 答えの「引用」がPDFの文字の中に本当にあるかを照らし合わせ、見つかれば枠を出す。見つからなければ「要確認」にする
  - 送った記録（日時・ファイル名・ページ数・モデル）を作業フォルダの AI送信記録.txt に残す

AWS の認証は、ふだんの AWS の設定（~/.aws の認証情報・プロファイル、環境変数）をそのまま使う。
anthropic の SDK は 1.x に固定している（Bedrock が新しいAPIバージョンを受け付けるのは Claude 4.7 以降のため）。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import unicodedata
from pathlib import Path

from reader import Doc, render_png
from rules import BUSSON_KEYS, ROLES, VEHICLE_TYPES, Cand, F, wareki_ym

DEFAULTS = {"aws_region": "ap-northeast-1", "aws_profile": "", "model_id": "jp.anthropic.claude-sonnet-4-6"}
MAX_PAGES = 20          # 1回に送るページの上限
MAX_TOKENS = 16000


class AiError(Exception):
    pass


def make_client(settings: dict):
    try:
        from anthropic import AnthropicBedrock
    except ImportError:
        raise AiError("anthropic（Bedrock 用）が入っていません。setup.bat をやり直してください") from None
    return AnthropicBedrock(aws_region=settings.get("aws_region") or DEFAULTS["aws_region"],
                            aws_profile=settings.get("aws_profile") or None, timeout=300.0, max_retries=2)


# --- 読み取り --------------------------------------------------------------------------

TYPES = {
    "evidence": "証拠の標目・作成日・作成者・立証趣旨（1通に1つ）。values: title, date, author, purpose",
    "jiko_date": "事故日。values: value（YYYY-MM-DD）",
    "jiko_time": "事故の時刻。values: value（HH:MM）",
    "jiko_place": "事故の場所。values: value",
    "party": "当事者（交通事故証明書の甲・乙など）。values: ko（甲/乙など）, name, address, type（車種）, reg（車両番号）",
    "injuries": "傷病名。values: value",
    "job": "被害者の職業。values: value",
    "treatment": "入院・通院の期間。values: hospital, type（入院/通院）, start, end",
    "fixed": "症状固定日。values: date, part（部位）",
    "receipt": "診療報酬明細書・調剤報酬明細書の1か月分。values: hospital, year, month, nyu（入院日数）, tsu（通院日数）, amount（円）",
    "jibai_payment": "自賠責保険金の受領。values: date, amount",
    "nini_payment": "任意保険金の支払（既払金）。values: key（任意保険金(治療費)/任意保険金(その他)）, amount",
    "grade": "後遺障害の認定等級。values: grade（1〜14の数字。非該当なら出さない）, text（認定の文言）",
    "income": "事故前年の年収（源泉徴収票の支払金額など）。values: income, reason",
    "kyugyo_end": "休業期間の終わりの日。values: end",
    "busson": "物損の金額（見積書・領収書・請求書）。values: key（車両修理費/レッカー費用/代車費用/車両時価額/買替諸費用/評価損/休車損害）, amount, reason",
}

TOOL = {
    "name": "report_items",
    "description": "書類から読み取った項目を報告する。読み取れなかった項目は出さない。",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "書類の種類（交通事故証明書、診断書、診療報酬明細書など）"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(TYPES)},
                        "values": {"type": "object", "additionalProperties": {"type": ["string", "number", "null"]}},
                        "page": {"type": "integer", "description": "読み取ったページ（1から）"},
                        "quote": {"type": "string", "description": "根拠にした書類の文字を、書類のとおりに短く（30字まで）"},
                        "uncertain": {"type": "boolean", "description": "手書きで読みにくい・推測を含むなら true"},
                    },
                    "required": ["type", "values", "page", "quote"],
                },
            },
        },
        "required": ["kind", "items"],
    },
}

SYSTEM = """あなたは法律事務所で交通事故の損害賠償請求訴訟の訴状を作る補助をしています。
渡された証拠書類（ページの画像と、OCRで読んだ文字）から、訴状と裁判所の共通書式に必要な項目を読み取り、report_items で報告してください。

守ること：
- 書類に書いてあることだけを報告する。推測で埋めない。読めない・書いていない項目は出さない
- 日付は YYYY-MM-DD（和暦は西暦に直す）、金額は円の整数（カンマなし）
- quote には、その値の根拠にした書類の文字を、書類に書かれているとおりに短く写す（OCRの文字を優先）
- 手書きで読みにくいもの、判断を含むものは uncertain を true にする
- 診療報酬明細書は1か月分ごとに1つの receipt にする。点数しかなければ amount は出さず、quote に点数を写す
- 交通事故証明書の甲・乙は、ko に「甲」「乙」と書く（どちらが原告かは人が決めるので書かない）"""


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", s or "") if not c.isspace())


def locate(doc: Doc, page: int | None, quote: str):
    """引用を、そろえたページの文字の中から探す。戻り値: (ページ番号, 枠) か (None, [])。"""
    q = _norm(quote)
    if len(q) < 2:
        return None, []
    pages = sorted(doc.pages, key=lambda p: p.no != page)
    for p in pages:
        idx = [i for i, ch in enumerate(p.text) if not ch.isspace()]
        flat = "".join(p.text[i] for i in idx)
        k = flat.find(q)
        if k >= 0:
            return p.no, p.box_of(idx[k], idx[k + len(q) - 1] + 1)
    return None, []


def _s(v):
    return "" if v is None else str(v).strip()


def to_cands(doc: Doc, items: list[dict]) -> list[Cand]:
    """AIの答え → 候補。知らない type・形の崩れたものは捨てる。"""
    out: list[Cand] = []
    for it in items:
        t, v = it.get("type"), it.get("values") or {}
        if t not in TYPES or not isinstance(v, dict):
            continue
        g = lambda k: _s(v.get(k))  # noqa: E731
        if t == "evidence":
            c = Cand("証拠（AIの下書き）", {"kind": "evidence"},
                     [F("key", "証拠キー（半角英小文字と_）", "text", "shoko"), F("title", "標目", "text", g("title")),
                      F("date", "作成日", "date", g("date")), F("author", "作成者", "text", g("author")),
                      F("purpose", "立証趣旨", "text", g("purpose"))])
        elif t in ("jiko_date", "jiko_time", "jiko_place", "injuries", "job"):
            path, label, vt = {"jiko_date": (["jiko", "date"], "事故の日", "date"),
                               "jiko_time": (["jiko", "time"], "事故の時刻", "time"),
                               "jiko_place": (["jiko", "place"], "事故の場所", "text"),
                               "injuries": (["shogai", "injuries"], "傷病名", "text"),
                               "job": (["shogai", "job"], "職業", "text")}[t]
            c = Cand(label, {"kind": "set", "path": path}, [F("value", label, vt, g("value"))])
        elif t == "party":
            ko = g("ko")
            c = Cand(f"当事者（{ko or '?'}）", {"kind": "party", "ko": ko},
                     [F("role", "原告か被告か", "choice", "入れない", ROLES), F("name", "氏名", "text", g("name")),
                      F("address", "住所", "text", g("address")),
                      F("type", "車種", "choice", g("type") if g("type") in VEHICLE_TYPES else "", [""] + VEHICLE_TYPES),
                      F("reg", "車両番号", "text", g("reg"))], judgment=True,
                     note="どちらが原告かを選ぶ")
        elif t == "treatment":
            c = Cand(f"{g('type') or '治療'}の期間", {"kind": "treatment"},
                     [F("hospital", "医療機関", "text", g("hospital")),
                      F("type", "入院か通院か", "choice", g("type") if g("type") in ("入院", "通院") else "通院",
                        ["入院", "通院"]),
                      F("start", "始まり", "date", g("start")), F("end", "終わり", "date", g("end"))])
        elif t == "fixed":
            c = Cand("症状固定日", {"kind": "fixed"}, [F("date", "症状固定日", "date", g("date")),
                                                     F("part", "部位", "text", g("part"))])
        elif t == "receipt":
            try:
                ym = wareki_ym(int(float(g("year"))), int(float(g("month"))))
            except ValueError:
                ym = ""
            c = Cand(f"報酬明細書 {ym}", {"kind": "receipt"},
                     [F("hospital", "医療機関", "text", g("hospital")), F("ym", "年月（R6.1の形）", "text", ym),
                      F("nyu", "入院日数", "int", g("nyu")), F("tsu", "通院日数", "int", g("tsu")),
                      F("amount", "金額（円）", "int", g("amount"))])
        elif t == "jibai_payment":
            c = Cand("自賠責保険金の受領", {"kind": "payment"},
                     [F("date", "受領日", "date", g("date")), F("amount", "受領額（円）", "int", g("amount"))],
                     judgment=True)
        elif t == "nini_payment":
            key = g("key") if g("key") in ("任意保険金(治療費)", "任意保険金(その他)") else "任意保険金(その他)"
            c = Cand("任意保険金（既払金）", {"kind": "item_add"},
                     [F("key", "項目", "choice", key, ["任意保険金(治療費)", "任意保険金(その他)"]),
                      F("amount", "金額（円）", "int", g("amount"))], judgment=True)
        elif t == "grade":
            c = Cand("後遺障害慰謝料（赤い本・認定の等級で）", {"kind": "item", "key": "後遺障害慰謝料"},
                     [F("grade", "等級（1〜14）", "int", g("grade"))], judgment=True, note=g("text"))
        elif t == "income":
            c = Cand("逸失利益の基礎収入（年額）", {"kind": "income"},
                     [F("income", "基礎収入（円）", "int", g("income")), F("reason", "理由", "text", g("reason"))],
                     judgment=True)
        elif t == "kyugyo_end":
            c = Cand("休業の終わりの日（期間1）", {"kind": "kyugyo_end"}, [F("end", "終期", "date", g("end"))],
                     judgment=True)
        else:   # busson
            key = g("key") if g("key") in BUSSON_KEYS else "（入れない）"
            c = Cand("物損の金額", {"kind": "busson_item"},
                     [F("key", "物損の項目", "choice", key, BUSSON_KEYS), F("amount", "金額（円・税込）", "int", g("amount")),
                      F("reason", "理由等", "text", g("reason"))], judgment=True)
        c.source = "AI"
        c.quote = _s(it.get("quote"))
        page = it.get("page") if isinstance(it.get("page"), int) else None
        c.page, c.rects = locate(doc, page, c.quote)
        notes = [c.note] if c.note else []
        if not c.rects:
            c.page = page
            notes.append("AIの引用がPDFの文字に見つからない（画像だけのページ・手書きなど）。ページを見て確かめる")
            c.judgment = True
        if it.get("uncertain"):
            notes.append("AIが「読みにくい・推測を含む」とした")
            c.judgment = True
        c.note = "。".join(notes)
        out.append(c)
    for i, c in enumerate(out):
        c.id = f"a{i}"
    return out


def _page_blocks(doc: Doc, pages: list[int]):
    blocks = []
    for no in pages:
        png = render_png(Path(doc.path), no, zoom=1.5)
        blocks.append({"type": "text", "text": f"--- {no}ページの画像 ---"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.standard_b64encode(png).decode("ascii")}})
    text = "\n".join(f"--- {p.no}ページのOCRの文字（{p.source}）---\n{p.text}" for p in doc.pages if p.no in pages)
    blocks.append({"type": "text", "text": text})
    return blocks


def _log(work_dir: Path | None, what: str, doc: Doc, pages, model: str):
    if not work_dir:
        return
    work_dir.mkdir(parents=True, exist_ok=True)
    with open(work_dir / "AI送信記録.txt", "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}\t{what}\t{Path(doc.path).name}\t"
                f"{len(pages)}ページ\t{model}\n")


def _call(client, model: str, system: str, content: list, tools=None, tool_name=None):
    kw = {}
    if tools:
        kw = {"tools": tools, "tool_choice": {"type": "tool", "name": tool_name}}
    try:
        msg = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system,
                                     messages=[{"role": "user", "content": content}], **kw)
    except Exception as e:   # 認証・権限・ネットワーク・モデルIDの誤りなど。画面にそのまま出す
        raise AiError(f"Bedrock に送れませんでした（{type(e).__name__}: {str(e)[:300]}）") from None
    if getattr(msg, "stop_reason", None) == "refusal":
        raise AiError("AIが答えませんでした（refusal）。手入力してください")
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise AiError("答えが長すぎて途中で切れました。ページを分けて送ってください")
    return msg


def read_with_ai(doc: Doc, settings: dict, *, pages: list[int] | None = None, work_dir: Path | None = None,
                 client=None) -> tuple[str, list[Cand]]:
    """書類を Bedrock に送って読み取る。戻り値: (書類の種類, 候補)。"""
    pages = pages or [p.no for p in doc.pages][:MAX_PAGES]
    if len(pages) > MAX_PAGES:
        raise AiError(f"1回に送れるのは {MAX_PAGES} ページまでです")
    model = settings.get("model_id") or DEFAULTS["model_id"]
    client = client or make_client(settings)
    _log(work_dir, "読み取り", doc, pages, model)
    content = _page_blocks(doc, pages) + [{"type": "text", "text": "項目の種類：\n" + "\n".join(
        f"- {k}: {v}" for k, v in TYPES.items()) + "\n\nこの書類から読み取れる項目を report_items で報告してください。"}]
    msg = _call(client, model, SYSTEM, content, [TOOL], TOOL["name"])
    use = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if use is None:
        raise AiError("AIの答えの形が想定と違いました")
    data = use.input if isinstance(use.input, dict) else json.loads(use.input)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return _s(data.get("kind")), to_cands(doc, items)


# --- 下書き（補足主張・本訴に至る経緯）----------------------------------------------------

DRAFT_SYSTEM = """あなたは法律事務所で交通事故の訴状（裁判所の一覧表利用版）の下書きを手伝っています。
渡された証拠書類と事件の情報をもとに、指示された部分の文章を、訴状の文体（である調、簡潔）で下書きしてください。
- 書類に書いてある事実だけを使う。足りない事実は「【要確認：○○】」と書いて空けておく
- 証拠を引くときは、渡した証拠キーを使って（{証:キー}）の形で書く（例：（{証:jikkyo}））
- 見出しや番号は付けない。段落ごとに改行で区切る"""

DRAFT_KINDS = {
    "hosoku_jiko": ("補足主張（事故態様）", "補足主張のうち「事故態様」の段落"),
    "hosoku_kashitsu": ("補足主張（過失）", "補足主張のうち、被告の過失・原告に過失がないことの段落"),
    "keii": ("本訴に至る経緯", "本訴に至る経緯（交渉の経過）の段落"),
}


def draft_with_ai(what: str, docs: list[tuple[str, Doc]], case: dict, settings: dict, *,
                  work_dir: Path | None = None, client=None) -> Cand:
    """下書き。docs: [(証拠キー, Doc)]。戻り値: 下書きの候補（op: sojou の hosoku／keii に入れる）。"""
    if what not in DRAFT_KINDS:
        raise AiError("知らない下書きの種類です")
    title, ask = DRAFT_KINDS[what]
    model = settings.get("model_id") or DEFAULTS["model_id"]
    client = client or make_client(settings)
    content = []
    for key, d in docs:
        pages = [p.no for p in d.pages][:MAX_PAGES]
        _log(work_dir, f"下書き（{title}）", d, pages, model)
        content.append({"type": "text", "text": f"=== 証拠キー {key}：{Path(d.path).name} ==="})
        content += _page_blocks(d, pages)
    facts = {k: case.get(k) for k in ("jiko", "sekinin", "shogai") if case.get(k)}
    content.append({"type": "text", "text": "事件の情報（case.json の一部）：\n" + json.dumps(facts, ensure_ascii=False)
                    + f"\n\n{ask}を下書きしてください。本文だけを出力してください。"})
    msg = _call(client, model, DRAFT_SYSTEM, content)
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise AiError("AIの下書きが空でした")
    paras = [re.sub(r"^\s*[0-9０-９]+[.．、)）]\s*", "", s).strip() for s in text.split("\n") if s.strip()]
    op = ({"kind": "draft", "where": "keii"} if what == "keii" else
          {"kind": "draft", "where": "hosoku", "title": "事故態様" if what == "hosoku_jiko" else "過失"})
    c = Cand(f"{title}の下書き", op, [F("body", "本文（段落ごとに改行）", "textarea", "\n".join(paras))],
             note="AIの下書き。事実と証拠の引用を必ず確かめる。【要確認】の箇所を埋める", judgment=True, source="AI")
    c.id = f"draft_{what}"
    return c

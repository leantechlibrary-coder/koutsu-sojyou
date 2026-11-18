"""ai.py：Bedrock には送らず、偽のクライアントで答えの扱い（候補への変換・引用の照合・下書き）を確かめる。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ai
import merge
import reader
from tests.pdfgen import make_pdf


class FakeClient:
    def __init__(self, blocks, stop="tool_use"):
        self.sent = []
        self.messages = SimpleNamespace(create=self._create)
        self.blocks, self.stop = blocks, stop

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(content=self.blocks, stop_reason=self.stop)


def tool(items, kind="交通事故証明書"):
    return [SimpleNamespace(type="tool_use", input={"kind": kind, "items": items})]


class TestAi(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        p = make_pdf(self.dir / "j.pdf", [[(50, 100, "交通事故証明書"), (50, 130, "発生日時 令和６年１月１日 １８時００分頃"),
                                          (50, 160, "発生場所 東京都千代田区霞が関１丁目")]])
        self.doc = reader.read_doc(p)

    def tearDown(self):
        self.td.cleanup()

    def test_read_items_to_cands(self):
        c = FakeClient(tool([
            {"type": "jiko_date", "values": {"value": "2024-01-01"}, "page": 1, "quote": "令和６年１月１日"},
            {"type": "jiko_place", "values": {"value": "東京都千代田区霞が関1丁目"}, "page": 1, "quote": "霞が関一丁目"},
            {"type": "party", "values": {"ko": "甲", "name": "山田太郎"}, "page": 1, "quote": "山田太郎", "uncertain": True},
            {"type": "nonsense", "values": {}, "page": 1, "quote": "x"},
        ]))
        kind, cands = ai.read_with_ai(self.doc, {}, work_dir=self.dir / "work", client=c)
        self.assertEqual(kind, "交通事故証明書")
        self.assertEqual(len(cands), 3)
        d, place, party = cands
        self.assertEqual(d.source, "AI")
        self.assertTrue(d.rects)                       # 引用（全角）がそろえた文字の中で見つかった
        self.assertFalse(d.judgment)
        self.assertFalse(place.rects)                  # 書類にない引用 → 要確認
        self.assertTrue(place.judgment)
        self.assertIn("見つからない", place.note)
        self.assertIn("読みにくい", party.note)
        sent = c.sent[0]
        self.assertEqual(sent["tool_choice"], {"type": "tool", "name": "report_items"})
        self.assertNotIn("temperature", sent)
        kinds = [b["type"] for b in sent["messages"][0]["content"]]
        self.assertIn("image", kinds)
        log = (self.dir / "work" / "AI送信記録.txt").read_text(encoding="utf-8")
        self.assertIn("j.pdf", log)

    def test_receipt_to_wareki(self):
        c = FakeClient(tool([{"type": "receipt", "values": {"hospital": "A整形外科", "year": 2024, "month": 1,
                                                            "nyu": 31, "tsu": None, "amount": 357800},
                              "page": 1, "quote": "交通事故証明書"}]))
        _, cands = ai.read_with_ai(self.doc, {}, client=c)
        f = {x["name"]: x["value"] for x in cands[0].fields}
        self.assertEqual((f["ym"], f["nyu"], f["tsu"]), ("R6.1", "31", ""))

    def test_errors(self):
        with self.assertRaises(ai.AiError):
            ai.read_with_ai(self.doc, {}, client=FakeClient([], stop="refusal"))
        with self.assertRaises(ai.AiError):
            ai.read_with_ai(self.doc, {}, client=FakeClient([SimpleNamespace(type="text", text="?")]))

        class Boom:
            messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("AccessDenied")))
        with self.assertRaises(ai.AiError) as cm:
            ai.read_with_ai(self.doc, {}, client=Boom())
        self.assertIn("AccessDenied", str(cm.exception))

    def test_draft_into_case(self):
        c = FakeClient([SimpleNamespace(type="text", text="1. 本件事故は、被告車が赤信号を無視したものである（{証:jikkyo}）。\n\n2. 原告に過失はない。")],
                       stop="end_turn")
        cand = ai.draft_with_ai("hosoku_jiko", [("jikkyo", self.doc)], {"jiko": {"date": "2024-01-01"}}, {}, client=c)
        d = cand.to_json()
        d["accepted"] = True
        case = {"sojou": {"hosoku": [{"title": "事故態様", "body": ["古い"]}]}}
        merge.apply_cand(case, d, None)
        self.assertEqual(case["sojou"]["hosoku"][0]["body"],
                         ["本件事故は、被告車が赤信号を無視したものである（{証:jikkyo}）。", "原告に過失はない。"])
        k = ai.draft_with_ai("keii", [], {}, {}, client=c).to_json()
        k["accepted"] = True
        merge.apply_cand(case, k, None)
        self.assertEqual(len(case["sojou"]["keii"]), 2)


if __name__ == "__main__":
    unittest.main()

"""reader.py・rules.py：合成PDFから、書類の種類と値の候補を拾う。"""
import tempfile
import unittest
from pathlib import Path

import reader
import rules
from tests.pdfgen import image_only_pdf, make_pdf, spaced


def fields(c):
    return {f["name"]: f["value"] for f in c.fields}


def by_label(cands, label):
    return [c for c in cands if c.label.startswith(label)]


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def read(self, name, pages):
        p = make_pdf(self.dir / name, pages)
        return rules.extract(reader.read_doc(p), name)


class TestReader(Base):
    def test_normalize_and_rows(self):
        p = make_pdf(self.dir / "a.pdf", [[(50, 100, "発生日時"), (200, 100, spaced("令和６年１月１日")),
                                          (50, 130, "ABC  123"), (50, 160, "この行は文字数を足すための行です。")]])
        d = reader.read_doc(p)
        self.assertEqual(d.pages[0].text.splitlines()[0], "発生日時 令和6年1月1日")
        self.assertIn("ABC 123", d.pages[0].text)
        self.assertEqual(d.pages[0].source, "text")

    def test_box_of_points_at_text(self):
        p = make_pdf(self.dir / "a.pdf", [[(50, 100, "発生日時"), (200, 100, "令和6年1月1日 これは試しの文字列です")]])
        pg = reader.read_doc(p).pages[0]
        i = pg.text.index("令和")
        r = pg.box_of(i, i + 2)[0]
        self.assertAlmostEqual(r[0], 200, delta=1)
        self.assertTrue(88 < r[1] < 100 < r[3] < 104)

    def test_image_only_page_is_unread(self):
        d = reader.read_doc(image_only_pdf(self.dir / "s.pdf"))
        self.assertEqual(d.unread_pages(), [1])

    def test_cache(self):
        p = make_pdf(self.dir / "a.pdf", [[(50, 100, "交通事故証明書 これはキャッシュの試しです")]])
        d1 = reader.read_doc(p, self.dir / "cache")
        self.assertEqual(len(list((self.dir / "cache").glob("*.json"))), 1)
        d2 = reader.read_doc(p, self.dir / "cache")
        self.assertEqual(d1.pages[0].text, d2.pages[0].text)

    def test_ocr_command_used_for_unread_pages(self):
        """テキスト層のないページだけ tokumeiocr（ocr-text）に渡す。ここでは偽の tokumeiocr で確かめる。"""
        import json
        import sys
        fake = self.dir / "fake_ocr.py"
        fake.write_text(
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "assert args[0] == 'ocr-text' and args[3] == '--pages'\n"
            "pages = [int(x) for x in args[4].split(',')]\n"
            "t = '交通事故証明書 発生日時 令和6年1月1日'\n"
            "lines = [{'text': t, 'chars': [[50 + 10 * i, 100, 60 + 10 * i, 112] for i in range(len(t))]}]\n"
            "json.dump({'pages': [{'no': n, 'width': 595, 'height': 842, 'lines': lines} for n in pages]},"
            " open(args[2], 'w', encoding='utf-8'), ensure_ascii=False)\n", encoding="utf-8")
        d = reader.read_doc(image_only_pdf(self.dir / "s.pdf"), ocr_command=[sys.executable, str(fake)])
        self.assertEqual(d.pages[0].source, "ocr")
        self.assertIn("令和6年1月1日", d.pages[0].text)
        _ = json


class TestJikoShomei(Base):
    PAGE = [
        (200, 60, "交 通 事 故 証 明 書"),
        (50, 120, "発生日時"), (150, 120, spaced("令和６年１月１日") + "　１８時００分頃"),
        (50, 140, "発生場所"), (150, 140, "東京都千代田区霞が関１丁目１番地先"),
        (50, 180, "甲"), (80, 180, "住所"), (150, 180, "神奈川県横浜市中区本町２丁目２番"),
        (80, 200, "氏名"), (150, 200, "山 田 太 郎"), (400, 200, "（４５歳）"),
        (80, 220, "車種"), (150, 220, "普通乗用"), (300, 220, "車両番号"), (380, 220, "横浜 300 あ 12-34"),
        (50, 260, "乙"), (80, 260, "住所"), (150, 260, "東京都千代田区丸の内１丁目１番"),
        (80, 280, "氏名"), (150, 280, "鈴木花子"), (400, 280, "（３８歳）"),
        (80, 300, "車種"), (150, 300, "自動二輪"), (300, 300, "車両番号"), (380, 300, "品川 な 5678"),
        (50, 340, "事故類型"), (150, 340, "車両相互　出会い頭衝突"),
        (50, 380, "照会記録簿の種別"), (200, 380, "人身事故"),
        (50, 760, "上記の事項を確認したことを証明します。"),
        (50, 780, "証明日"), (150, 780, "令和６年２月１日"), (300, 780, "自動車安全運転センター東京都事務所長"),
    ]

    def test_extract(self):
        kind, cands = self.read("甲001 交通事故証明書.pdf", [self.PAGE])
        self.assertEqual(kind, "jiko_shomei")
        ev = fields(cands[0])
        self.assertEqual((ev["key"], ev["title"], ev["date"]), ("jiko_shomei", "交通事故証明書", "2024-02-01"))
        self.assertEqual(fields(by_label(cands, "事故の日")[0])["value"], "2024-01-01")
        self.assertEqual(fields(by_label(cands, "事故の時刻")[0])["value"], "18:00")
        self.assertEqual(fields(by_label(cands, "事故の場所")[0])["value"], "東京都千代田区霞が関1丁目1番地先")
        ko, otsu = by_label(cands, "甲")[0], by_label(cands, "乙")[0]
        self.assertEqual(fields(ko)["name"], "山田太郎")
        self.assertEqual(fields(ko)["role"], "被告")
        self.assertEqual(fields(ko)["type"], "普通乗用自動車")
        self.assertEqual(fields(ko)["reg"], "横浜300あ12-34")   # 字間の空白はそろえるときに消える
        self.assertEqual(fields(ko)["address"], "神奈川県横浜市中区本町2丁目2番")
        self.assertEqual(fields(otsu)["name"], "鈴木花子")
        self.assertEqual(fields(otsu)["role"], "原告")
        self.assertEqual(fields(otsu)["type"], "普通自動二輪車")
        self.assertTrue(ko.judgment)
        self.assertTrue(by_label(cands, "事故の日")[0].rects)


class TestReceipt(Base):
    def page(self, ym, days, amount, inpatient=False):
        return [(150, 60, "診療報酬明細書（" + ("医科入院" if inpatient else "医科入院外") + "）"),
                (50, 90, ym + "分"), (50, 120, "医療機関の名称"), (200, 120, "Ａ整形外科"),
                (50, 150, "傷病名"), (200, 150, "頸椎捻挫"),
                (50, 180, "診療実日数"), (200, 180, f"{days}日"),
                (50, 500, "合計"), (200, 500, f"{amount:,}円")]

    def test_months(self):
        kind, cands = self.read("receipt.pdf", [self.page("令和６年１月", 31, 357800, True),
                                                self.page("令和６年２月", 12, 54320),
                                                self.page("令和６年２月", 12, 54320)])   # 続きのページ
        self.assertEqual(kind, "receipt")
        rows = [fields(c) for c in cands if c.op["kind"] == "receipt"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"hospital": "A整形外科", "ym": "R6.1", "nyu": 31, "tsu": "", "amount": 357800})
        self.assertEqual(rows[1]["tsu"], 12)
        self.assertEqual(fields(cands[0])["title"], "診療報酬明細書（A整形外科）")

    def test_points_fallback(self):
        pg = [(150, 60, "診療報酬明細書"), (50, 90, "令和6年3月分"), (50, 120, "B医院"),
              (50, 180, "実日数 4"), (50, 500, "合計"), (200, 500, "1,234点")]
        _, cands = self.read("r.pdf", [pg])
        c = [c for c in cands if c.op["kind"] == "receipt"][0]
        self.assertEqual(fields(c)["amount"], 12340)
        self.assertIn("点数×10", c.note)

    def test_pharmacy(self):
        pg = [(150, 60, "調剤報酬明細書"), (50, 90, "令和6年1月分"), (50, 120, "薬局名"), (150, 120, "Ｃ調剤薬局"),
              (50, 500, "請求金額"), (200, 500, "8,900円")]
        kind, cands = self.read("c.pdf", [pg])
        self.assertEqual(kind, "chouzai")
        f = fields([c for c in cands if c.op["kind"] == "receipt"][0])
        self.assertEqual((f["hospital"], f["nyu"], f["tsu"], f["amount"]), ("C調剤薬局", "", "", 8900))


class TestOthers(Base):
    def test_shindansho(self):
        pg = [(250, 60, "診 断 書"), (50, 100, "傷病名"), (150, 100, "右足骨折、頸椎捻挫"),
              (50, 140, "入院"), (150, 140, "令和6年1月1日から令和6年1月31日まで"),
              (50, 160, "通院"), (150, 160, "令和6年2月1日から令和6年10月31日まで"),
              (50, 700, "上記のとおり診断します。"), (50, 720, "令和6年11月5日"), (300, 740, "Ａ整形外科")]
        kind, cands = self.read("d.pdf", [pg])
        self.assertEqual(kind, "shindansho")
        self.assertEqual(fields(by_label(cands, "傷病名")[0])["value"], "右足骨折、頸椎捻挫")
        tr = [fields(c) for c in cands if c.op["kind"] == "treatment"]
        self.assertIn({"hospital": "A整形外科", "type": "入院", "start": "2024-01-01", "end": "2024-01-31"}, tr)
        self.assertIn({"hospital": "A整形外科", "type": "通院", "start": "2024-02-01", "end": "2024-10-31"}, tr)

    def test_kouisho_and_nintei(self):
        _, c1 = self.read("k.pdf", [[(200, 60, "自動車損害賠償責任保険後遺障害診断書"), (50, 100, "傷病名"),
                                     (150, 100, "右膝内側半月板損傷"), (50, 140, "症状固定日"),
                                     (150, 140, "令和６年１０月３１日")]])
        self.assertEqual(fields(by_label(c1, "症状固定日")[0])["date"], "2024-10-31")
        kind, c2 = self.read("n.pdf", [[(150, 60, "自賠責保険 後遺障害等級認定結果のご通知"),
                                        (50, 120, "認定等級"), (150, 120, "併合第１１級")]])
        self.assertEqual(kind, "nintei")
        g = by_label(c2, "後遺障害慰謝料")[0]
        self.assertEqual(fields(g)["grade"], 11)
        self.assertIn("併合第11級", g.note)

    def test_shiharai(self):
        pg = [(150, 60, "自賠責保険金 お支払いのご案内"), (50, 100, "お支払金額"), (200, 100, "１，２００，０００円"),
              (50, 120, "お支払日"), (200, 120, "令和６年３月１５日"), (50, 700, "○○損害保険株式会社")]
        kind, cands = self.read("p.pdf", [pg])
        self.assertEqual(kind, "shiharai")
        f = fields(by_label(cands, "自賠責保険金の受領")[0])
        self.assertEqual((f["date"], f["amount"]), ("2024-03-15", 1200000))
        self.assertEqual(fields(cands[0])["author"], "○○損害保険株式会社")

    def test_gensen(self):
        pg = [(150, 60, "令和５年分　給与所得の源泉徴収票"), (50, 120, "支払金額"), (150, 120, "内"),
              (200, 120, "7,300,000"), (50, 400, "支払者"), (100, 400, "氏名又は名称"), (200, 400, "株式会社ＡＢＣ")]
        kind, cands = self.read("g.pdf", [pg])
        self.assertEqual(kind, "gensen")
        self.assertEqual(fields(by_label(cands, "逸失利益")[0])["income"], 7300000)
        d = fields(by_label(cands, "休業損害の日額")[0])
        self.assertEqual(d["daily"], 20000)
        self.assertIn("R5", d["reason"])

    def test_mitsumori(self):
        pg = [(250, 60, "御 見 積 書"), (50, 100, "見積日"), (150, 100, "令和7年10月20日"),
              (50, 140, "御見積金額"), (200, 140, "¥825,000-（税込）"), (50, 200, "板金・塗装　一式"),
              (350, 700, "株式会社ヤマダ自動車")]
        kind, cands = self.read("m.pdf", [pg])
        self.assertEqual(kind, "mitsumori")
        f = fields(by_label(cands, "見積書の金額")[0])
        self.assertEqual((f["key"], f["amount"]), ("車両修理費", 825000))
        ev = fields(cands[0])
        self.assertEqual((ev["date"], ev["kind"]), ("2025-10-20", "mitsumori"))

    def test_unknown(self):
        kind, cands = self.read("x.pdf", [[(50, 100, "なにかのメモ。とくに書類の表題はありません。")]])
        self.assertEqual(kind, "unknown")
        self.assertEqual(len(cands), 1)
        self.assertEqual(fields(cands[0])["title"], "x")


if __name__ == "__main__":
    unittest.main()


class TestMerge(Base):
    """○にした候補を case.json に入れる（merge.py）。"""

    def accept_all(self, cands):
        out = []
        for c in cands:
            d = c.to_json()
            d["accepted"] = True
            out.append(d)
        return out

    def test_jiko_shomei_into_case(self):
        import merge
        _, cands = self.read("j.pdf", [TestJikoShomei.PAGE])
        case = {"evidence": {}}
        key, errs = merge.apply_doc(case, self.accept_all(cands))
        self.assertEqual(errs, [])
        self.assertEqual(key, "jiko_shomei")
        self.assertEqual(case["jiko"]["date"], "2024-01-01")
        self.assertEqual(case["sojou"]["plaintiff"]["name"], "鈴木花子")
        self.assertEqual(case["sojou"]["defendants"][0]["name"], "山田太郎")
        sides = {v["side"]: v["type"] for v in case["jiko"]["vehicles"]}
        self.assertEqual(sides, {"被告側": "普通乗用自動車", "原告側": "普通自動二輪車"})

    def test_receipts_twice_no_duplicate_and_needs_evidence(self):
        import merge
        _, cands = self.read("r.pdf", [TestReceipt.page(None, "令和６年１月", 31, 357800, True)])
        js = self.accept_all(cands)
        case = {"evidence": {}}
        merge.apply_doc(case, js)
        merge.apply_doc(case, js)
        self.assertEqual(case["receipts"], [["A整形外科", "R6.1", 31, None, 357800, "receipt"]])
        js[0]["accepted"] = False        # 証拠を×にすると、レセプトの行は入れられない
        _, errs = merge.apply_doc({"evidence": {}}, js)
        self.assertTrue(errs and "証拠" in errs[0])

    def test_unique_key(self):
        import merge
        case = {"evidence": {"receipt": {}, "receipt_2": {}}}
        owners = {"receipt": "a.pdf", "receipt_2": "b.pdf"}
        self.assertEqual(merge.unique_key(case, "receipt", "c.pdf", owners), "receipt_3")
        self.assertEqual(merge.unique_key(case, "receipt", "a.pdf", owners), "receipt")
        self.assertEqual(merge.unique_key(case, "Receipt-A", None, {}), "receipt_a")

    def test_payment_and_item_add(self):
        import merge
        case = {"evidence": {}}
        c = {"label": "自賠責保険金の受領", "op": {"kind": "payment"}, "accepted": True,
             "fields": [{"name": "date", "value": "2024-03-15"}, {"name": "amount", "value": "1,200,000"}]}
        merge.apply_cand(case, c, "jibai1")
        merge.apply_cand(case, c, "jibai1")
        it = case["songai"]["items"][0]
        self.assertEqual(it["payments"], [{"date": "2024-03-15", "amount": 1200000, "ev": "jibai1"}])
        self.assertEqual(it["method"], "genpon")
        a = {"label": "任意", "op": {"kind": "item_add"}, "accepted": True,
             "fields": [{"name": "key", "value": "任意保険金(治療費)"}, {"name": "amount", "value": 100}]}
        merge.apply_cand(case, a, "n1")
        merge.apply_cand(case, a, "n1")
        merge.apply_cand(case, a, "n2")
        it = [x for x in case["songai"]["items"] if x["key"] == "任意保険金(治療費)"][0]
        self.assertEqual(it["amount"], 200)
        self.assertNotIn("_src", merge.strip_private(case)["songai"]["items"][1])

    def test_bad_values(self):
        import merge
        c = {"label": "事故の日", "op": {"kind": "set", "path": ["jiko", "date"]}, "accepted": True,
             "fields": [{"name": "value", "vtype": "date", "value": "令和6年1月1日"}]}
        with self.assertRaises(merge.MergeError):
            merge.apply_cand({}, c, None)

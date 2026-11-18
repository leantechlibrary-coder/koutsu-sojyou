"""core.py：書面の生成と、合計の計算（formulas）が Excel・LibreOffice と同じになること。

期待値（expected_values.json）は scripts/lo_expected.py で LibreOffice（日本語ロケール）の全再計算から作った。
"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import core

HERE = Path(__file__).resolve().parent
EXPECTED = json.loads((HERE / "expected_values.json").read_text(encoding="utf-8"))
OFFICE = json.loads((core.SKILL_DIR / "office.example.json").read_text(encoding="utf-8"))
# 事案の概要 L68・M68（入通院の日数の表示用）は formulas が計算できない（合計には使わない）
NOT_SUPPORTED = {"事案の概要!L68", "事案の概要!M68"}


def load_case(name):
    return json.loads((core.SKILL_DIR / f"{name}.json").read_text(encoding="utf-8"))


def docx_text(p):
    with zipfile.ZipFile(p) as z:
        import re
        return re.sub(r"<[^>]+>", "", z.read("word/document.xml").decode("utf-8"))


class TestCalc(unittest.TestCase):
    """見本4件の全セルで、formulas の計算と LibreOffice の再計算が一致する。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.vals = {}
        for name in EXPECTED:
            x = Path(cls.td.name) / f"{name}.xlsx"
            core.build_xlsx(load_case(name), x)
            cls.vals[name] = core.evaluate(x)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_all_cells(self):
        for name, cells in EXPECTED.items():
            with self.subTest(case=name):
                got = self.vals[name]
                bad = []
                n = 0
                for ref, exp in cells.items():
                    if ref in NOT_SUPPORTED:
                        continue
                    sh, cell = ref.split("!")
                    g = got.get((sh.upper(), cell))
                    if g is None:
                        continue   # 数式でないセル（入力値）は formulas が返さないことがある
                    n += 1
                    if not (isinstance(g, (int, float)) and abs(g - exp) < 1e-6):
                        bad.append((ref, exp, g))
                self.assertGreater(n, 400)
                self.assertEqual(bad, [])

    def test_key_totals(self):
        want = {"case_kisairei1": (40142986, 0), "case_kisairei2": (0, 2387000),
                "case_kakutei_rei": (37237464, 0), "case_shibou_rei": (78241482, 0)}
        for name, (f74, f105) in want.items():
            v = self.vals[name]
            self.assertEqual(round(v[("損害額一覧表", "F74")]), f74, name)
            self.assertEqual(round(v[("損害額一覧表", "F105")]), f105, name)


class TestBuildAll(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_kisairei1(self):
        r = core.build_all(load_case("case_kisairei1"), OFFICE, self.out)
        self.assertEqual(r["totals"]["amount"], 40142986)
        for f in r["files"]:
            self.assertTrue(Path(f).is_file(), f)
        text = docx_text(self.out / "訴状.docx")
        self.assertIn("４０１４万２９８６円", text)
        self.assertEqual(r["evidence"][0]["no"], "甲001")

    def test_busson(self):
        r = core.build_all(load_case("case_kisairei2"), OFFICE, self.out)
        self.assertEqual((r["totals"]["amount"], r["totals"]["busson"]), (0, 2387000))
        self.assertIn("２３８万７０００円", docx_text(self.out / "訴状.docx"))

    def test_shibou_plaintiffs_match_sheet(self):
        """相続等一覧表の請求額合計（45行）と、訴状の原告ごとの請求額が一致する（core.check_plaintiffs）。"""
        r = core.build_all(load_case("case_shibou_rei"), OFFICE, self.out)
        ps = {p["name"]: p["jinson"] for p in r["totals"]["plaintiffs"]}
        self.assertEqual(ps["○○花子"], 29772360)
        self.assertEqual(ps["○○ウメ"], 990000)
        memo = (self.out / "訴状_mints入力メモ.md").read_text(encoding="utf-8")
        self.assertIn("29,772,360", memo)

    def test_plaintiff_mismatch_stops(self):
        case = load_case("case_shibou_rei")
        tot = {"amount": 78241482, "busson": 0,
               "plaintiffs": [{"name": "○○花子", "col": "F", "jinson": 1, "busson": 0}]}
        with self.assertRaises(core.BuildError):
            core.check_plaintiffs(case, tot)

    def test_skill_error_becomes_build_error(self):
        case = load_case("case_kisairei1")
        case["jiko"]["ev"] = ["no_such_key"]
        with self.assertRaises(core.BuildError) as cm:
            core.build_all(case, OFFICE, self.out)
        self.assertIn("no_such_key", str(cm.exception))

    def test_case_not_modified(self):
        case = load_case("case_kakutei_rei")
        before = json.dumps(case, sort_keys=True, ensure_ascii=False)
        core.build_all(case, OFFICE, self.out)
        self.assertEqual(json.dumps(case, sort_keys=True, ensure_ascii=False), before)


if __name__ == "__main__":
    unittest.main()

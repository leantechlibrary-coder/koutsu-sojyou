"""app.py・server.py：事件フォルダを読み、候補を確認して入れ、書面を作るまで。サーバーの合言葉も。"""
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import app as appmod
import core
import server
from tests.pdfgen import make_pdf
from tests.test_rules import TestJikoShomei, TestReceipt


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.root / "appdata")        # 設定はテスト用の場所に
        self.case_dir = self.root / "令和7年ワ第1号 鈴木"
        (self.case_dir / "証拠").mkdir(parents=True)
        make_pdf(self.case_dir / "証拠" / "甲1 交通事故証明書.pdf", [TestJikoShomei.PAGE])
        make_pdf(self.case_dir / "証拠" / "甲2 レセプト.pdf", [TestReceipt.page(None, "令和６年１月", 31, 357800, True)])
        (self.case_dir / "_マスク作業").mkdir()
        make_pdf(self.case_dir / "_マスク作業" / "x.pdf", [[(50, 50, "作業フォルダのPDFは見ない")]])

    def tearDown(self):
        if self.old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.old_appdata
        self.td.cleanup()

    def all_ok(self, view):
        return {c["id"]: {"accepted": True} for c in view["cands"] if c["op"]["kind"] != "info"}


class TestCaseApp(Base):
    def test_flow(self):
        a = appmod.CaseApp(self.case_dir)
        self.assertEqual(a.pdfs(), ["証拠/甲1 交通事故証明書.pdf", "証拠/甲2 レセプト.pdf"])
        v = a.read_file("証拠/甲1 交通事故証明書.pdf")
        self.assertEqual(v["kind"], "jiko_shomei")
        r = a.decide(v["rel"], self.all_ok(v))
        self.assertEqual(r["errors"], [])
        self.assertEqual(a.case["jiko"]["date"], "2024-01-01")
        self.assertEqual(r["file"]["evkey"], "jiko_shomei")
        v2 = a.read_file("証拠/甲2 レセプト.pdf")
        a.decide(v2["rel"], self.all_ok(v2))
        self.assertEqual(a.case["receipts"][0][5], "receipt")
        saved = json.loads((self.case_dir / "訴状" / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["evidence"]["jiko_shomei"]["date"], "2024-02-01")
        # 開き直しても、決めたことは残っている（state.json）
        b = appmod.CaseApp(self.case_dir)
        self.assertTrue(all(c.get("applied") for c in b.file_view(v["rel"])["cands"] if c["op"]["kind"] != "info"))
        self.assertEqual(b.checks()["unconfirmed"], [])

    def test_decide_twice_and_edit(self):
        a = appmod.CaseApp(self.case_dir)
        v = a.read_file("証拠/甲1 交通事故証明書.pdf")
        a.decide(v["rel"], self.all_ok(v))
        cid = next(c["id"] for c in v["cands"] if c["label"] == "事故の場所")
        a.decide(v["rel"], {cid: {"accepted": True, "fields": {"value": "東京都千代田区霞が関1丁目1番"}}})
        self.assertEqual(a.case["jiko"]["place"], "東京都千代田区霞が関1丁目1番")
        self.assertEqual(len(a.case["sojou"]["defendants"]), 1)

    def test_same_key_two_files(self):
        make_pdf(self.case_dir / "証拠" / "甲3 レセプト2.pdf", [TestReceipt.page(None, "令和６年２月", 12, 54320)])
        a = appmod.CaseApp(self.case_dir)
        for rel in ("証拠/甲2 レセプト.pdf", "証拠/甲3 レセプト2.pdf"):
            v = a.read_file(rel)
            a.decide(rel, self.all_ok(v))
        self.assertEqual(sorted(a.case["evidence"]), ["receipt", "receipt_2"])
        self.assertEqual([r[5] for r in a.case["receipts"]], ["receipt", "receipt_2"])

    def test_outside_path_refused(self):
        a = appmod.CaseApp(self.case_dir)
        with self.assertRaises(appmod.AppError):
            a.read_file("../x.pdf")

    def test_build_with_example_case(self):
        a = appmod.CaseApp(self.case_dir)
        a.save_case(json.loads((core.SKILL_DIR / "case_kisairei1.json").read_text(encoding="utf-8")))
        a.save_settings({"office": json.loads((core.SKILL_DIR / "office.example.json").read_text(encoding="utf-8"))})
        r = a.build()
        self.assertEqual(r["totals"]["amount"], 40142986)
        self.assertIn("訴状.docx", r["files"])
        self.assertTrue((self.case_dir / "訴状" / "原告一覧表.xlsx").is_file())

    def test_build_error_message(self):
        a = appmod.CaseApp(self.case_dir)
        with self.assertRaises(appmod.AppError):
            a.build()

    def test_checks(self):
        a = appmod.CaseApp(self.case_dir)
        ch = a.checks()
        self.assertIn("事故日", [m["msg"] for m in ch["missing"]])
        self.assertEqual(len(ch["unconfirmed"]), 2)

    def test_ai_read_needs_setting_and_uses_client(self):
        from types import SimpleNamespace as NS
        blocks = [NS(type="tool_use", input={"kind": "交通事故証明書", "items": [
            {"type": "jiko_date", "values": {"value": "2024-01-01"}, "page": 1, "quote": "令和6年1月1日"}]})]
        client = NS(messages=NS(create=lambda **kw: NS(content=blocks, stop_reason="tool_use")))
        a = appmod.CaseApp(self.case_dir, ai_client=client)
        with self.assertRaises(appmod.AppError):
            a.ai_read("証拠/甲1 交通事故証明書.pdf")
        a.save_settings({"ai": {"enabled": True}})
        v = a.ai_read("証拠/甲1 交通事故証明書.pdf")
        self.assertTrue(any(c["source"] == "AI" for c in v["cands"]))
        self.assertTrue((self.case_dir / "_訴状アプリ" / "AI送信記録.txt").is_file())


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.app = appmod.CaseApp(self.case_dir)
        self.srv, self.url = server.serve(self.app, 0, open_browser=False)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        return r.status, r.read(), r

    def test_token(self):
        self.assertEqual(self.req("GET", "/")[0], 403)
        self.assertEqual(self.req("GET", "/api/data")[0], 403)
        st, body, r = self.req("GET", f"/?t={self.app.token}")
        self.assertEqual(st, 200)
        self.assertIn(self.app.token, body.decode("utf-8"))
        cookie = r.getheader("Set-Cookie").split(";")[0]
        st, body, _ = self.req("GET", "/api/data", headers={"Cookie": cookie})
        self.assertEqual(st, 200)
        self.assertEqual(len(json.loads(body)["files"]), 2)
        # POST は合言葉のヘッダーも要る
        self.assertEqual(self.req("POST", "/api/read", {"rel": "証拠/甲1 交通事故証明書.pdf"},
                                  {"Cookie": cookie})[0], 403)
        st, body, _ = self.req("POST", "/api/read", {"rel": "証拠/甲1 交通事故証明書.pdf"},
                               {"Cookie": cookie, "X-Token": self.app.token, "Content-Type": "application/json"})
        self.assertEqual(st, 200)
        st, body, r = self.req("GET", "/api/page?rel=" + __import__("urllib.parse").parse.quote("証拠/甲1 交通事故証明書.pdf") + "&p=1",
                               headers={"Cookie": cookie})
        self.assertEqual((st, r.getheader("Content-Type")), (200, "image/png"))

    def test_bad_host(self):
        st, _, _ = self.req("GET", f"/?t={self.app.token}", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()

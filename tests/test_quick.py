"""quick.py・koutsu.py：「送る」メニューの bat と、入口のコマンド。"""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import koutsu
import quick


class TestQuick(unittest.TestCase):
    def test_sendto_bat(self):
        bat = quick.encode_bat(quick.sendto_bat('"C:\\koutsu\\koutsu.exe"'))
        self.assertEqual(bat, b'@echo off\r\n"C:\\koutsu\\koutsu.exe" quick %*\r\nif errorlevel 1 pause\r\n')

    def test_file_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            p = f.name
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(quick.main([p]), 1)
            self.assertIn("事件フォルダ", err.getvalue())
        finally:
            os.unlink(p)

    def test_entry_usage_and_unknown(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(koutsu.main(["--help"]), 0)
        self.assertIn("交通事故の訴状", out.getvalue())
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(koutsu.main(["nope"]), 2)

    def test_build_without_case(self):
        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(koutsu.main(["build", td]), 1)
            self.assertIn("case.json", err.getvalue())
            self.assertFalse((Path(td) / "訴状").exists())


if __name__ == "__main__":
    unittest.main()

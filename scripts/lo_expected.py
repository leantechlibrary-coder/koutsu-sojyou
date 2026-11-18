"""テストの期待値（tests/expected_values.json）を LibreOffice で作り直す（Linux の開発用。アプリでは使わない）。

    python scripts/lo_expected.py

kotsu-sojou/ の見本の case.json から共通書式xlsx を作り、LibreOffice（日本語ロケール）で全部を再計算して、
損害額一覧表・相続等一覧表・治療費等集計表・事案の概要の数値のセルを保存する。
tests/test_core.py は、アプリの計算（formulas）がこの値と一致することを確かめる。
LibreOffice は英語ロケールだと DATEVALUE("2020/3/31") が #VALUE! になるので、必ず日本語ロケールで動かす。
"""
import json
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core  # noqa: E402

CASES = ["case_kisairei1", "case_kisairei2", "case_kakutei_rei", "case_shibou_rei"]
SHEETS = ["損害額一覧表", "相続等一覧表（死亡事案で使用）", "治療費等集計表", "事案の概要"]
UNO = r'''
import sys, time, subprocess, uno
from com.sun.star.beans import PropertyValue
def pv(n, v):
    p = PropertyValue(); p.Name, p.Value = n, v; return p
src, dst, port = sys.argv[1], sys.argv[2], sys.argv[3]
proc = subprocess.Popen(["soffice", "--headless", "--norestore", "--nologo",
                         f"--accept=socket,host=localhost,port={port};urp;"])
ctx = None
for _ in range(90):
    try:
        local = uno.getComponentContext()
        res = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        ctx = res.resolve(f"uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext"); break
    except Exception:
        time.sleep(1)
desk = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
doc = desk.loadComponentFromURL(uno.systemPathToFileUrl(src), "_blank", 0, (pv("Hidden", True),))
doc.calculateAll()
doc.storeToURL(uno.systemPathToFileUrl(dst), (pv("FilterName", "Calc MS Excel 2007 XML"),))
doc.close(True)
try:
    desk.terminate()
except Exception:
    pass
proc.wait(timeout=60)
'''


def main() -> int:
    import openpyxl
    warnings.simplefilter("ignore")
    out: dict = {}
    env = dict(os.environ, LC_ALL="ja_JP.UTF-8", LANG="ja_JP.UTF-8")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "uno_recalc.py").write_text(UNO, encoding="utf-8")
        for name in CASES:
            case = json.loads((core.SKILL_DIR / f"{name}.json").read_text(encoding="utf-8"))
            x = td / f"{name}.xlsx"
            core.build_xlsx(case, x)
            y = td / f"{name}_lo.xlsx"
            # LibreOffice 同梱の Python ではなく、uno が入っているシステムの python3 で動かす
            subprocess.run(["/usr/bin/python3", str(td / "uno_recalc.py"), str(x), str(y), "2002"],
                           env=env, check=True, timeout=300, capture_output=True)
            wb = openpyxl.load_workbook(y, data_only=True)
            cells = {}
            for sh in SHEETS:
                for row in wb[sh].iter_rows():
                    for c in row:
                        if isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
                            cells[f"{sh}!{c.coordinate}"] = c.value
            out[name] = cells
            print(name, "F74 =", cells.get("損害額一覧表!F74"), " F105 =", cells.get("損害額一覧表!F105"))
    dst = ROOT / "tests" / "expected_values.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    print("書きました:", dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())

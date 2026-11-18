"""訴状アプリの入口（exe にするときもここから）。1つの exe で画面・書面の作成・「送る」を切り替える。

    koutsu.exe review 事件フォルダ        画面（証拠を読む → 確認 → 入力 → 書面を作る）
    koutsu.exe build 事件フォルダ [--full] 画面なしで、訴状\\case.json から書面を作る
    koutsu.exe quick 事件フォルダ…         右クリックの「送る」メニューから（quick.py）
    koutsu.exe install-sendto              「送る」メニューに登録（uninstall-sendto で消す）
    koutsu.exe version                     同梱したライブラリの一覧

Python で動かすときは  python koutsu.py review ...  のように使う。
"""

from __future__ import annotations

import sys
from pathlib import Path

USAGE = """koutsu — 交通事故の訴状（共通書式）

使い方:
  review.bat 事件フォルダ              画面（証拠PDFを読む → 候補を確認 → 入力 → 書面を作る）
  build.bat 事件フォルダ [--full]      画面なしで、事件フォルダの 訴状\\case.json から書面を作る

右クリックで使う（パスを打たない）:
  送るに登録.bat をダブルクリックすると、事件フォルダの右クリック →「送る」に「交通事故の訴状を作る」が出ます。

（koutsu.exe review ... のように、exe に直接 review / build を付けても同じです）
"""


def tool_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _version() -> int:
    from importlib import metadata

    print("ツールのフォルダ:", tool_dir())
    v = tool_dir() / "version.txt"
    if v.exists():
        print("版:", v.read_text(encoding="utf-8").strip())
    for pkg in ("openpyxl", "lxml", "formulas", "pymupdf", "anthropic", "boto3"):
        try:
            print(f"  {pkg} {metadata.version(pkg)}")
        except metadata.PackageNotFoundError:
            print(f"  {pkg} なし")
    import app
    import reader
    s = app.load_settings()
    ocr = reader.find_tokumeiocr(s.get("tokumeiocr_dir"))
    print("tokumeiocr:", ocr[0] if ocr else "見つかりません（テキスト層のないページは手入力）")
    print("AI（Bedrock）:", "使う" if s.get("ai", {}).get("enabled") else "使わない", f"（設定: {app.settings_path()}）")
    return 0


def _build(argv) -> int:
    import argparse

    import app
    ap = argparse.ArgumentParser(prog="koutsu build", description="事件フォルダの 訴状\\case.json から書面を作る")
    ap.add_argument("case_dir")
    ap.add_argument("--full", action="store_true", help="表紙・当事者目録つきの全体版（紙・旧方式用）")
    a = ap.parse_args(argv)
    try:
        ca = app.CaseApp(a.case_dir)
        if not ca.case_path.is_file():
            raise app.AppError(f"{ca.case_path} がありません。先に画面（review）で入力してください")
        r = ca.build(full=a.full)
    except app.AppError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    t = r["totals"]
    print(f"人身損害 合計（F74）: {t['amount']:,}円　物的損害 合計（F105）: {t['busson']:,}円")
    for p in t.get("plaintiffs", []):
        print(f"  原告 {p['name']}: {p['jinson'] + p['busson']:,}円")
    for w in r["warnings"]:
        print("知らせ:", w)
    print("出力:", ca.out_dir)
    for f in r["files"]:
        print("  ", f)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        if not argv and getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
            input("Enter キーで閉じます。")
        return 0 if argv else 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "review":
        import server
        return server.main(rest)
    if cmd == "build":
        return _build(rest)
    if cmd in ("quick", "install-sendto", "uninstall-sendto"):
        import quick
        try:
            if cmd == "quick":
                return quick.main(rest)
            if cmd == "install-sendto":
                return quick.install()
            return quick.uninstall()
        except quick.QuickError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
    if cmd == "version":
        return _version()
    print(f"知らないコマンドです: {cmd}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""右クリック（「送る」メニュー）から使う入口。パスを打たずに、事件フォルダを選ぶだけで画面を開く。

    koutsu.exe quick 事件フォルダ…   「交通事故の訴状を作る」から呼ばれる
    koutsu.exe install-sendto        「送る」メニューに登録する
    koutsu.exe uninstall-sendto      「送る」メニューから消す

何件かまとめて選んで「送る」と、1件ずつ順に画面を開く（「終了」を押すと次へ）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app import AppError, CaseApp

MENU = "交通事故の訴状を作る"


class QuickError(Exception):
    pass


def run_one(folder: Path) -> None:
    import server

    app = CaseApp(folder)
    print(f"事件フォルダ: {app.case_dir}")
    print(f"証拠PDF: {len(app.pdfs())} 件")
    server.run(app)


def main(argv: list[str]) -> int:
    if not argv:
        print("使い方: koutsu.exe quick 事件フォルダ…（ふつうは「送る」メニューから使います）", file=sys.stderr)
        return 2
    folders = [Path(a) for a in argv]
    failed = 0
    for i, f in enumerate(folders, 1):
        if len(folders) > 1:
            print(f"\n=== {i}/{len(folders)}: {f.name} ===")
        if f.is_file():
            print(f"エラー: 事件フォルダ（証拠PDFを入れたフォルダ）を右クリックして「送る」を選んでください"
                  f"（ファイルが送られました: {f.name}）", file=sys.stderr)
            failed += 1
            continue
        try:
            run_one(f)
        except (AppError, OSError) as e:
            print(f"エラー: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


# --- 「送る」メニュー ---------------------------------------------------------

def sendto_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base or os.name != "nt":
        raise QuickError("「送る」メニューは Windows だけで使えます。")
    return Path(base) / "Microsoft" / "Windows" / "SendTo"


def launcher() -> str:
    """bat から呼ぶコマンド（exe なら exe、Python なら .venv の python と koutsu.py）。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    here = Path(__file__).resolve().parent
    venv = here / ".venv" / "Scripts" / "python.exe"
    py = venv if venv.exists() else Path(sys.executable)
    return f'"{py.resolve()}" "{here / "koutsu.py"}"'


def sendto_bat(command: str) -> str:
    # 終わったら閉じる。エラーのときは読めるように止める
    return f"@echo off\r\n{command} quick %*\r\nif errorlevel 1 pause\r\n"


def encode_bat(text: str) -> bytes:
    # cmd は bat を ANSI コードページ（日本語の Windows なら cp932）で読む
    enc = "mbcs" if os.name == "nt" else "cp932"
    try:
        return text.encode(enc)
    except UnicodeEncodeError:
        raise QuickError("ツールの置き場所のパスに、bat で使えない文字があります。C:\\koutsu などに置いてください。") \
            from None


def install() -> int:
    dest = sendto_dir()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{MENU}.bat").write_bytes(encode_bat(sendto_bat(launcher())))
    print(f"「送る」メニューに登録しました: {MENU}")
    print("事件フォルダを右クリック →（Windows 11 は「その他のオプションを確認」→）「送る」から使えます。")
    return 0


def uninstall() -> int:
    (sendto_dir() / f"{MENU}.bat").unlink(missing_ok=True)
    print("「送る」メニューから消しました。")
    return 0

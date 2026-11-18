"""exe 版（Python のない PC で使う配布物）を作る。PyInstaller ＋ 動作確認 ＋ zip。

    python scripts/build_exe.py [--zip] [--skip-check]

先に、ビルドに使う Python（3.12〜3.14）にライブラリと PyInstaller を入れておく：
    python -m pip install -r requirements.txt -c constraints.txt
    python -m pip install -r requirements-build.txt

できるもの（dist/koutsu/ フォルダごと配る）：
    koutsu.exe             本体（review / build / quick を先頭の語で切り替える）
    _internal\\            Python・ライブラリ一式・スキル（kotsu-sojou）と裁判所の様式
    review.bat             画面（事件フォルダ）
    build.bat              画面なしで書面を作る（事件フォルダの 訴状\\case.json から）
    送るに登録.bat / 送るから削除.bat   右クリックの「送る」メニューへの登録
    使い方.txt、version.txt

手順：
  1. PyInstaller でビルド（koutsu.spec）
  2. 動作確認（--skip-check で省略）：
     - koutsu.exe version が動く
     - スキルの見本（記載例1・物損・死亡）で、exe と Python とで同じ合計額・同じ証拠番号になり、記載例1は 40,142,986円
     - 合成の証拠PDF（交通事故証明書）を exe で読み、画面のサーバーが立ち、合言葉なしでは応答せず、「終了」で止まる
     - 外部に接続しない（プロキシを使えない宛先に向けて動かす）
  3. --zip で dist/koutsu-<版>-<OS>.zip を作る
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
DIST = TOOL_DIR / "dist"
APP = DIST / "koutsu"
WIN = os.name == "nt"
EXE = APP / ("koutsu.exe" if WIN else "koutsu")
SKILL = TOOL_DIR / "kotsu-sojou"

BAT = '@echo off\r\n"%~dp0koutsu.exe" {cmd} %*\r\n'
PAUSE_BAT = '@echo off\r\n"%~dp0koutsu.exe" {cmd} %*\r\npause\r\n'

HOWTO = """koutsu（exe版） — 交通事故の訴状（共通書式）

Python を入れる必要はありません。

■ 置き場所

  このフォルダごと C:\\koutsu に置くのがおすすめです（C:\\koutsu\\koutsu.exe になるように）。
  OneDrive で同期されるフォルダ・ネットワークドライブ・事件フォルダの中は避けてください。
  中身（koutsu.exe・_internal）はばらばらにしないでください。

■ 右クリックで使う（おすすめ）

  1. 送るに登録.bat をダブルクリックする（1回だけ）
  2. 事件フォルダ（証拠PDFを入れたフォルダ）を右クリック →（Windows 11 は「その他のオプションを確認」→）
     「送る」→「交通事故の訴状を作る」
  3. ブラウザで画面が開く。① 証拠を読む → 候補を ○／× にして「反映」→ ②〜⑦ で足りないところを入れる →
     ⑧ 書面を作る
  4. 終わったら「終了」を押す

■ できるもの

  <事件フォルダ>\\訴状\\case.json              入力の本体（スキル kotsu-sojou と同じ形）
  <事件フォルダ>\\訴状\\原告一覧表.xlsx         共通書式（事案の概要・損害額一覧表・治療費等集計表・相続等一覧表）
  <事件フォルダ>\\訴状\\原告一覧表_証拠リスト.json   証拠説明書を作るための一覧
  <事件フォルダ>\\訴状\\訴状.docx               訴状（mints添付用）
  <事件フォルダ>\\訴状\\訴状_mints入力メモ.md    mints に入れる訴額などのメモ
  <事件フォルダ>\\_訴状アプリ\\                  読み取りのキャッシュ・確認の状態・AI送信記録

■ ネットワーク

  ふだんは接続しません。設定で「Bedrock を使う」にして、画面で「AIで読む」「AIで下書き」を押したときだけ、
  その書類を Bedrock（Claude Sonnet 4.6）に送ります。AWS の認証は、このPCの AWS の設定（プロファイル）を使います。

■ テキスト層のないページ

  スキャナでOCRしていないページは、tokumeiocr（C:\\tokumeiocr）があれば、それで読みます。

■ コマンドで使う

  review.bat 事件フォルダ
  build.bat 事件フォルダ [--full]      画面なしで書面を作る
  koutsu.exe version                   同梱したライブラリの一覧
"""


def git_version() -> str:
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=TOOL_DIR,
                             capture_output=True, text=True, check=True).stdout.strip()
        return out or "dev"
    except (OSError, subprocess.CalledProcessError):
        return "dev"


def build() -> None:
    shutil.rmtree(APP, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(DIST),
                    "--workpath", str(TOOL_DIR / "build"), str(TOOL_DIR / "koutsu.spec")],
                   cwd=TOOL_DIR, check=True)
    for name, text in {"review.bat": BAT.format(cmd="review"), "build.bat": BAT.format(cmd="build"),
                       "送るに登録.bat": PAUSE_BAT.format(cmd="install-sendto"),
                       "送るから削除.bat": PAUSE_BAT.format(cmd="uninstall-sendto")}.items():
        (APP / name).write_bytes(text.encode("ascii"))
    (APP / "使い方.txt").write_text(HOWTO, encoding="utf-8-sig")
    (APP / "version.txt").write_text(git_version() + "\n", encoding="utf-8")


# --- 動作確認 ------------------------------------------------------------------

def _env(appdata: Path) -> dict:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", APPDATA=str(appdata))
    # 外部に接続しないことを確かめるため、プロキシを使えない宛先に向けておく
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        env[k] = "http://127.0.0.1:9"
    env["NO_PROXY"] = env["no_proxy"] = ""
    return env


def _case(tmp: Path, name: str, example: str) -> Path:
    d = tmp / name
    (d / "訴状").mkdir(parents=True)
    shutil.copyfile(SKILL / f"{example}.json", d / "訴状" / "case.json")
    return d


def _run_build(cmd: list[str], d: Path, env: dict) -> dict:
    r = subprocess.run(cmd + ["build", str(d)], capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    print(r.stdout, end="")
    if r.returncode:
        raise SystemExit(f"書面の作成に失敗しました（{cmd[0]}）:\n{r.stderr}")
    ev = json.loads((d / "訴状" / "原告一覧表_証拠リスト.json").read_text(encoding="utf-8"))
    return {"out": r.stdout.split("出力:")[0], "ev": [(e["no"], e["key"]) for e in ev],
            "files": sorted(p.name for p in (d / "訴状").iterdir())}


def check_version(env: dict) -> None:
    r = subprocess.run([str(EXE), "version"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    print(r.stdout, end="")
    if r.returncode or "formulas" not in r.stdout:
        raise SystemExit("koutsu version が動きません。\n" + r.stderr)


def check_build(tmp: Path, env: dict) -> None:
    (tmp / "office").mkdir()
    for example in ("case_kisairei1", "case_kisairei2", "case_shibou_rei"):
        py = _run_build([sys.executable, str(TOOL_DIR / "koutsu.py")], _case(tmp / "py", example, example), env)
        ex = _run_build([str(EXE)], _case(tmp / "exe", example, example), env)
        if py != ex:
            raise SystemExit(f"exe と Python とで結果が違います（{example}）:\n{py}\n{ex}")
        if example == "case_kisairei1" and "40,142,986円" not in ex["out"]:
            raise SystemExit("記載例1の合計額が 40,142,986円 になりません:\n" + ex["out"])
        print(f"確認: {example} は exe と Python で同じ（{ex['out'].splitlines()[0]}）")


def check_review(tmp: Path, env: dict) -> None:
    sys.path.insert(0, str(TOOL_DIR))
    from tests.pdfgen import make_pdf
    from tests.test_rules import TestJikoShomei
    d = tmp / "review"
    (d / "証拠").mkdir(parents=True)
    make_pdf(d / "証拠" / "甲1 交通事故証明書.pdf", [TestJikoShomei.PAGE])
    p = subprocess.Popen([str(EXE), "review", str(d), "--no-browser"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env)
    try:
        url, lines, end = None, [], time.time() + 300
        while time.time() < end and url is None:
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line)
            m = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", line)
            if m:
                url = (int(m.group(1)), m.group(2))
        if url is None:
            raise SystemExit("画面のアドレスが出ません:\n" + "".join(lines))
        port, token = url

        def req(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
            c.request(method, path, body=body, headers=dict({"Host": f"127.0.0.1:{port}"}, **(headers or {})))
            r = c.getresponse()
            return r.status, r.read()

        assert req("GET", "/")[0] == 403, "合言葉なしで応答しました"
        assert req("GET", "/api/data")[0] == 403, "合言葉なしでデータを返しました"
        status, html = req("GET", f"/?t={token}")
        assert status == 200 and "交通事故の訴状".encode() in html, status
        auth = {"Cookie": f"tok={token}", "X-Token": token, "Content-Type": "application/json"}
        status, body = req("POST", "/api/read", json.dumps({"rel": "証拠/甲1 交通事故証明書.pdf"}).encode(), auth)
        view = json.loads(body)
        assert status == 200 and view["kind"] == "jiko_shomei", (status, body[:300])
        dates = [f["value"] for c in view["cands"] for f in c["fields"] if c["label"] == "事故の日"]
        assert dates == ["2024-01-01"], dates
        assert req("POST", "/api/quit", b"{}", {"Cookie": f"tok={token}"})[0] == 403, "X-Token なしで止まりました"
        assert req("POST", "/api/quit", b"{}", auth)[0] == 200
        p.wait(timeout=60)
        print("確認: 画面（交通事故証明書を読み、事故日 2024-01-01 を拾った。合言葉なしは拒否、終了で停止）")
    finally:
        if p.poll() is None:
            p.kill()


def make_zip() -> Path:
    tag = "win64" if WIN else sys.platform
    z = DIST / f"koutsu-{git_version()}-{tag}.zip"
    z.unlink(missing_ok=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(APP.rglob("*")):
            if f.is_file():
                zf.write(f, Path("koutsu") / f.relative_to(APP))
    print(f"zip: {z}（{z.stat().st_size / 1e6:.0f} MB）")
    return z


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="訴状アプリの exe 版を作る")
    ap.add_argument("--zip", action="store_true", help="dist/koutsu-<版>-<OS>.zip も作る")
    ap.add_argument("--skip-check", action="store_true", help="動作確認をしない")
    args = ap.parse_args(argv)
    build()
    if not args.skip_check:
        tmp = Path(tempfile.mkdtemp(prefix="koutsu-build-"))
        try:
            env = _env(tmp / "appdata")
            check_version(env)
            check_build(tmp, env)
            check_review(tmp, env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if args.zip:
        make_zip()
    print(f"できました: {APP}")
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

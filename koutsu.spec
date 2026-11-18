# PyInstaller の設定。直接使わず python scripts/build_exe.py でビルドする（動作確認と zip まで行う）。
#   pyinstaller --noconfirm koutsu.spec  → dist/koutsu/（koutsu.exe と _internal\）
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# スキル（kotsu-sojou/）は .py のまま _internal\kotsu-sojou\ に置き、実行時に sys.path から読む（core.py）。
# スクリプトが自分と同じフォルダの賃金センサス（csv）・様式（assets）を探すので、フォルダごと同梱する。
datas = [("ui.html", "."), ("kotsu-sojou", "kotsu-sojou")]
# 共通書式の数式を計算する formulas は、Excel の関数を動的に読み込む
datas += collect_data_files("formulas") + collect_data_files("schedula")
hiddenimports = ["app", "server", "quick", "core", "reader", "rules", "merge", "ai",
                 # スキルのスクリプトが使うもの（.py のまま読むので PyInstaller が見つけられない）
                 "lxml.etree", "lxml._elementpath", "csv", "calendar", "fractions", "zipfile"]
hiddenimports += collect_submodules("formulas") + collect_submodules("schedula")
hiddenimports += ["anthropic", "anthropic.lib.bedrock", "boto3", "botocore"]
datas += collect_data_files("botocore")   # Bedrock の API 定義
# koutsu.exe version がバージョンを表示するため（importlib.metadata）
for line in open("constraints.txt", encoding="utf-8"):
    line = line.split("#")[0].strip()
    if line:
        try:
            datas += copy_metadata(line.split("==")[0].strip())
        except Exception:  # このOSでは入らないもの
            pass

a = Analysis(
    ["koutsu.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest", "onnxruntime"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="koutsu",
    console=True,       # 進み具合やエラーを画面に出す（画面のアドレスもここに出る）
    upx=False,
    icon=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="koutsu", upx=False)

@echo off
setlocal
rem ============================================================
rem  訴状アプリの初回セットアップ（Python 版）
rem   1. Python のバージョン確認（3.12～3.14）
rem   2. .venv を作って pip install（constraints.txt で固定。wheelhouse\ があればそこから）
rem ============================================================
cd /d "%~dp0"

set PY=
for %%V in (3.13 3.14 3.12) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set PY=py -%%V
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>&1 && set PY=python
)
if not defined PY (
  echo [エラー] Python 3.12～3.14 が見つかりません。
  echo         https://www.python.org/ から Python 3.13（64bit）を入れてから、もう一度実行してください。
  echo         Python を入れたくないときは、exe 版を使ってください（README の「exe版」）。
  pause
  exit /b 1
)
echo 使う Python:
%PY% --version

if not exist .venv (
  echo .venv を作ります...
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail

if exist wheelhouse\ (
  echo ライブラリを wheelhouse\ から入れます（ネットワークは使いません）...
  python -m pip install --no-index --find-links wheelhouse -r requirements.txt -c constraints.txt || goto :fail
) else (
  echo ライブラリを入れます...
  python -m pip install --upgrade pip || goto :fail
  python -m pip install -r requirements.txt -c constraints.txt || goto :fail
)

python koutsu.py version

echo.
echo セットアップが終わりました。
echo   画面:       review.bat 事件フォルダ
echo   右クリック: 送るに登録.bat をダブルクリック → 事件フォルダを右クリック →「送る」→「交通事故の訴状を作る」
pause
exit /b 0

:fail
echo.
echo [エラー] セットアップに失敗しました。上のメッセージを確認してください。
pause
exit /b 1

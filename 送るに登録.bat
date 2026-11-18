@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [エラー] 先に setup.bat を実行してください。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0koutsu.py" install-sendto
pause

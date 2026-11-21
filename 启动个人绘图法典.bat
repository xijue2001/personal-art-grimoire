@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -B -m grimoire.app --open
if not errorlevel 1 exit /b
:failed
pause
exit /b 1

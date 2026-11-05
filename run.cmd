@echo off
setlocal
rem GCP RAG department console launcher. Usage: run.cmd [--port N] [--no-browser]
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.gui-requirements"

if exist "%PY%" goto deps
echo [run] Creating virtual environment in %VENV%
where py >nul 2>&1
if errorlevel 1 (python -m venv "%VENV%") else (py -3 -m venv "%VENV%")
if not exist "%PY%" (
  echo [run] Python 3.12+ is required. Install it and try again.
  exit /b 1
)

:deps
rem Reinstall only when requirements-gui.txt changed since the last install.
fc /b requirements-gui.txt "%STAMP%" >nul 2>&1
if not errorlevel 1 goto launch
echo [run] Installing GUI dependencies
"%PY%" -m pip install --disable-pip-version-check -q -r requirements-gui.txt
if errorlevel 1 (
  echo [run] Dependency install failed.
  exit /b 1
)
copy /y requirements-gui.txt "%STAMP%" >nul

:launch
echo [run] Starting console (default http://127.0.0.1:8765). Stop with Ctrl+C.
rem "(goto) 2>nul" leaves the batch context before Python starts, so Ctrl+C exits
rem immediately without cmd's "Terminate batch job (Y/N)?" prompt.
rem Paths are absolute because leaving the batch also restores the caller's directory.
(goto) 2>nul & "%PY%" "%~dp0scripts\dept_gui.py" %*

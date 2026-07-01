@echo off
REM Build a single, self-contained autotyper.exe (no Python needed to run it).
REM Just double-click this file, or run it from a terminal.
setlocal
cd /d "%~dp0"

REM Use the project venv if present, otherwise the system Python launcher.
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=py"
)

echo Installing the build tool (PyInstaller)...
REM The tool itself has NO runtime dependencies (pure standard library), so
REM only PyInstaller is needed, and only to build.
%PY% -m pip install --quiet --disable-pip-version-check pyinstaller

echo Building autotyper.exe ...
%PY% -m PyInstaller --onefile --console --name autotyper --clean --noconfirm autotyper.py

echo.
if exist "dist\autotyper.exe" (
    echo Done.  Your standalone program is:  dist\autotyper.exe
) else (
    echo Build FAILED - see the messages above.
)
pause

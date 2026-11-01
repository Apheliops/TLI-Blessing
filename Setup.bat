@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === TLI Blessing Helper setup ===
echo.
set PY=
set OLD=
rem Test that Python really runs: the Microsoft Store "python" shortcut is found by "where" but does not
for %%P in ("py -3" "python") do if not defined PY (
  %%~P -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 3)" >nul 2>nul
  if errorlevel 1 (if errorlevel 3 if not errorlevel 4 set OLD=1) else set "PY=%%~P"
)
if not defined PY (
  if defined OLD echo Your Python is too old: TLI Blessing Helper needs Python 3.9 or newer.
  where python 2>nul | find /i "WindowsApps" >nul && echo The "python" command on this PC is only the Microsoft Store shortcut, not a real Python install.
  echo No working Python 3.9+ was found. Install it from https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" during setup, then run this file again.
  start https://www.python.org/downloads/
  pause
  exit /b
)
echo [1/2] Installing Python packages...
%PY% -m pip install --upgrade -r requirements.txt
echo.
echo [2/2] Checking Tesseract-OCR...
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
  echo Tesseract is already installed.
) else (
  where winget >nul 2>nul && (
    echo Installing Tesseract with winget...
    winget install -e --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements
  ) || (
    echo Install Tesseract manually: download "tesseract-ocr-w64-setup" from the page that opens.
    start https://github.com/UB-Mannheim/tesseract/wiki
  )
)
echo.
echo Setup finished. Start the app with Start.bat.
pause

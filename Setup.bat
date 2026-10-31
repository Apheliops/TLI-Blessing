@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === TLI Blessing Helper setup ===
echo.
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
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

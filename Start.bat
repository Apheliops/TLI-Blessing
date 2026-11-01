@echo off
cd /d "%~dp0"
rem Find a Python that really runs (the Microsoft Store "python" shortcut does not) and is 3.9+,
rem then start the app without a console window (pythonw next to that python.exe).
set "PYC="
set "OLD="
for %%P in ("py -3" "python") do if not defined PYC (
  %%~P -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 3)" >nul 2>nul
  if errorlevel 1 (if errorlevel 3 if not errorlevel 4 set "OLD=1") else set "PYC=%%~P"
)
set "PYW="
if defined PYC for /f "delims=" %%W in ('%PYC% -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%W"
if defined PYW if exist "%PYW%" (start "" "%PYW%" tli_blessing.py & exit /b)
if defined PYC (%PYC% tli_blessing.py & exit /b)
echo.
if defined OLD echo Your Python is too old: TLI Blessing Helper needs Python 3.9 or newer.
where python 2>nul | find /i "WindowsApps" >nul && echo The "python" command on this PC is only the Microsoft Store shortcut, not a real Python install.
echo Install Python from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during setup), then run Setup.bat and try again.
pause

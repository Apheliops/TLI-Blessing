@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 tli_blessing.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw tli_blessing.py & exit /b)
where py >nul 2>nul && (py -3 tli_blessing.py & exit /b)
where python >nul 2>nul && (python tli_blessing.py & exit /b)
echo.
echo Python was not found. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during setup), then run Setup.bat and try again.
pause

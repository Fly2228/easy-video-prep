@echo off
setlocal
rem ---- UTF-8 console so the app's Chinese output shows correctly ----
rem This .bat is deliberately PURE ASCII + CRLF: cmd.exe parses batch
rem files byte-wise in the current codepage, so non-ASCII bytes here
rem would be mis-read (and can even swallow the line break).
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0"

rem ---- find a Python: local venv first, then whatever is on PATH ----
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   easy-video-prep  -  video preparation workflow
echo   ------------------------------------------------------
if "%~1"=="" goto nouser
echo   material dir : %~1
goto run
:nouser
echo   No folder given. Set the material directory in the top bar.
echo   Tip: drag a folder onto this .bat, or pass it as an argument:
echo        "%~nx0" "D:\path\to\your\videos"
:run
echo.
echo   Python : %PY%
echo   Open the URL printed below in your browser. Ctrl+C to stop.
echo.
"%PY%" app.py %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo   exited with code %RC%
pause
endlocal

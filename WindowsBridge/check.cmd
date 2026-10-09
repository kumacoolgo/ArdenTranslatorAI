@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem Double-click this to verify the setup WITHOUT opening any window.
rem Checks: model / base_url, whether the API key is readable, both log channels,
rem the installed addon version, and whether a real translation round-trip works.
rem The window stays open so you can read the result.
rem ---------------------------------------------------------------------------

call "%~dp0_find_python.cmd"
if errorlevel 1 (
  echo.
  echo [ERROR] No Python 3 with tkinter was found.
  echo Install Python 3.11+ from https://www.python.org/downloads/
  echo and keep the "tcl/tk and IDLE" option checked.
  echo.
  pause
  exit /b 1
)

echo Interpreter: %PYEXE%
echo.
%PYEXE% ArdenTranslatorAI.py --check
set RC=%errorlevel%
echo.
if "%RC%"=="0" (
  echo ---------------------------------------------------------------
  echo  OK - setup works. Now double-click run_python.cmd to start.
  echo ---------------------------------------------------------------
) else (
  echo ---------------------------------------------------------------
  echo  FAILED - see the message above.
  echo ---------------------------------------------------------------
)
echo.
pause

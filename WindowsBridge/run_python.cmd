@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem ArdenTranslator AI - Windows Bridge launcher
rem Just double-click this file.
rem
rem It opens the settings window first; press "start" there to install the
rem addon and raise the translation overlay.
rem
rem Pass --check to verify config + API without opening any window, e.g.
rem   run_python.cmd --check
rem Pass --overlay to skip the settings window and go straight to the overlay.
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

echo Using interpreter: %PYEXE%
echo Starting ArdenTranslator AI bridge...
echo.
%PYEXE% ArdenTranslatorAI.py %*
if errorlevel 1 (
  echo.
  echo The bridge exited with an error. See the message above.
  pause
)

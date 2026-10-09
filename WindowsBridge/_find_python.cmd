@echo off
rem ---------------------------------------------------------------------------
rem Shared helper. Sets PYEXE to a Python 3 interpreter that has tkinter,
rem because the overlay GUI needs it.
rem
rem Deliberately has NO setlocal: called scripts inherit the caller's
rem environment, so PYEXE stays visible to whoever ran "call _find_python.cmd".
rem
rem Exit code 0 = PYEXE set, 1 = nothing usable found.
rem ---------------------------------------------------------------------------

set "PYEXE="

rem 1) common python.org install locations
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
  "%ProgramFiles(x86)%\Python311\python.exe"
) do (
  if not defined PYEXE if exist "%%~P" (
    "%%~P" -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYEXE=%%~P"
  )
)

rem 2) the "py" launcher
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
  )
)

rem 3) whatever "python" resolves to
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
  )
)

if not defined PYEXE exit /b 1
exit /b 0

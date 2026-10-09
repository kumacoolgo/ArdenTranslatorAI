@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem ArdenTranslator AI - build the Windows bridge executables.
rem
rem Produces TWO files in dist\ :
rem   ArdenTranslatorAI.exe         GUI: settings window -> overlay
rem   ArdenTranslatorAI-check.exe   console build - for "--check" diagnostics
rem
rem Why two: a --windowed EXE has no stdout attached, so running
rem "ArdenTranslatorAI.exe --check" prints absolutely nothing. On a new machine
rem the console build is by far the fastest way to find out WHY nothing
rem translates (wrong key, wrong model, no Logs folder).
rem
rem v1.0.15: the addon folder is embedded, so the EXE can deploy a current copy
rem of the plugin into Interface\AddOns itself. If you edit the plugin, you MUST
rem rebuild the EXE or the deployed copy stays stale.
rem
rem v1.1 (V11-06): this script now drives the checked-in .spec files instead of
rem passing a --add-data path on the command line. The old version had two
rem problems:
rem   1. It deleted the .spec files ("if exist ... del"), so a build could not
rem      reproduce the settings we ship; the .spec is the source of truth now.
rem   2. The addon path was derived from %~dp0 at build time. If somebody copied
rem      just the EXE build command elsewhere, the path broke. The .spec computes
rem      the addon folder relative to ITSELF (SPECPATH) and hard-fails with a
rem      clear message if the layout is wrong, instead of silently producing an
rem      EXE with no plugin inside.
rem
rem Both .spec files share one hiddenimports whitelist. When you add or remove a
rem bridge module you must update BOTH (protocol/jobs/cache/dedupe/diagnostics/
rem transport), otherwise the packaged EXE dies with ImportError on a machine
rem that has no source tree.
rem
rem NOTE: keep this file pure ASCII. cmd.exe parses it with the OEM codepage
rem (936 on Chinese Windows) and any non-ASCII byte here corrupts the script.
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

rem Sanity check the addon source up front, so the user gets a plain message
rem here rather than a PyInstaller traceback. (The .spec re-checks this.)
set "ADDON_SRC=%~dp0..\ArdenTranslatorAI"
if not exist "%ADDON_SRC%\ArdenTranslatorAI.lua" (
  echo.
  echo [ERROR] Addon source not found: %ADDON_SRC%\ArdenTranslatorAI.lua
  echo         The EXE embeds this folder so it can install the addon itself.
  echo         Keep WindowsBridge\ and ArdenTranslatorAI\ as siblings.
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0ArdenTranslatorAI.spec" (
  echo.
  echo [ERROR] ArdenTranslatorAI.spec is missing next to this script.
  echo         The .spec files are checked in and are the build definition.
  echo.
  pause
  exit /b 1
)

echo Build interpreter: %PYEXE%
echo Addon source     : %ADDON_SRC%
echo.

%PYEXE% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

rem Refuse to build while an overlay is running. Windows keeps an exclusive lock
rem on a running image, so clearing dist\ fails with a bare "access denied" and
rem PyInstaller then dies with a long confusing traceback. Fail early instead,
rem with the actual fix.
tasklist /FI "IMAGENAME eq ArdenTranslatorAI.exe" /NH 2>nul | find /I "ArdenTranslatorAI.exe" >nul
if not errorlevel 1 (
  echo.
  echo [ERROR] ArdenTranslatorAI.exe is still running.
  echo         Windows locks the running file, so it cannot be overwritten.
  echo         Close the overlay first ^(Ctrl+Q, or click its X^), then retry.
  echo.
  pause
  exit /b 1
)
tasklist /FI "IMAGENAME eq ArdenTranslatorAI-check.exe" /NH 2>nul | find /I "ArdenTranslatorAI-check.exe" >nul
if not errorlevel 1 (
  echo.
  echo [ERROR] ArdenTranslatorAI-check.exe is still running. Close it and retry.
  echo.
  pause
  exit /b 1
)

rem Clean previous work output. --noconfirm already overwrites, but a stale
rem build\ can mask a packaging change, so start clean.
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [1/2] Building GUI overlay (windowed) from ArdenTranslatorAI.spec...
%PYEXE% -m PyInstaller --noconfirm --clean --distpath dist --workpath build ArdenTranslatorAI.spec
if errorlevel 1 goto :fail

echo.
echo [2/2] Building console diagnostic build from ArdenTranslatorAI-check.spec...
%PYEXE% -m PyInstaller --noconfirm --clean --distpath dist --workpath build ArdenTranslatorAI-check.spec
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  Build complete:
echo    %CD%\dist\ArdenTranslatorAI.exe         - double-click to run
echo    %CD%\dist\ArdenTranslatorAI-check.exe   - run with --check
echo ============================================================
echo.
echo Verifying the packaged build can find its modules...
"%CD%\dist\ArdenTranslatorAI-check.exe" --check
if errorlevel 1 (
  echo.
  echo [WARN] The --check self test reported a problem. Read the output above.
  echo.
  pause
  exit /b 1
)

echo.
echo The EXE now opens a settings window first, then installs the
echo addon into Interface\AddOns and shows the translation overlay.
echo Both read %%LOCALAPPDATA%%\ArdenTranslatorAI\config.json.
echo.
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1

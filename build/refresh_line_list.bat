@echo off
REM ============================================================
REM  ICE TB Line List - daily refresh
REM
REM  SETUP:
REM    Nothing to edit. The data URL lives in source_url.txt
REM    next to this file. Double-click this .bat to test.
REM
REM  SCHEDULE IT (once):
REM    Task Scheduler -> Create Basic Task -> Daily -> 07:00
REM    Action: Start a program -> browse to this .bat file
REM    Tick "Run whether user is logged on or not"
REM
REM  The page is only overwritten when a fresh download passes
REM  validation, so a failed run leaves yesterday's page working.
REM ============================================================

cd /d "%~dp0"

if not exist "source_url.txt" (
  echo source_url.txt is missing - cannot find the data URL.
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH. Install Python 3 from python.org
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

echo Refreshing ICE TB line list...
python build_linelist.py
if errorlevel 1 (
  echo.
  echo REFRESH FAILED - the existing page was left unchanged.
  echo See refresh_log.txt for the reason.
  if not "%1"=="/quiet" pause
  exit /b 1
)

echo.
echo Done. Open ice_tb_line_list.html
if not "%1"=="/quiet" pause

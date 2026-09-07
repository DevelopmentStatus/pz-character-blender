@echo off
REM ---------------------------------------------------------------------
REM  Run the character extractor on its own, with whatever arguments you
REM  pass through. setup.bat is the first-run wrapper around this; use
REM  this one for re-runs and for slices while iterating.
REM
REM    extract.bat                       everything (models + clips + pools)
REM    extract.bat --dry-run             list what it would do, write nothing
REM    extract.bat --only FemaleBody     one mesh, for a fast loop
REM    extract.bat --pools-only          re-mine appearance_pools.json only
REM
REM  Set PZ_GAME_DIR first if your install is not at the built-in default:
REM    set "PZ_GAME_DIR=C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid"
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo   ****  Python 3 was not found on PATH. Run setup.bat first.
  exit /b 1
)

REM setup.bat records the install it found in game_dir.txt so re-runs do not
REM have to re-find it. An explicit PZ_GAME_DIR in the environment wins.
if not defined PZ_GAME_DIR if exist "game_dir.txt" (
  for /f "usebackq delims=" %%G in ("game_dir.txt") do set "PZ_GAME_DIR=%%G"
)
if defined PZ_GAME_DIR echo   Game: !PZ_GAME_DIR!

if not exist "assets\characters" mkdir "assets\characters"

python "src\extractor\pz_characters.py" --out "assets\characters\characters.pzc" %*
if errorlevel 1 exit /b 1

REM Locomotion is a separate, ~5-second pass over media\AnimSets. The add-on
REM works without it - it only supplies the upper/lower bone masks used for
REM partial-body animation preview - so a failure here is not fatal.
python "src\extractor\pz_animscript.py" --out "assets\characters\locomotion_manifest.json"
if errorlevel 1 echo   ****  locomotion_manifest.json failed - the add-on still works without it.

echo.
echo   Wrote assets\characters\
exit /b 0

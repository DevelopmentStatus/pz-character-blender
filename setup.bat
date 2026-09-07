@echo off
REM ---------------------------------------------------------------------
REM  One-command bootstrap for pz-character.
REM
REM  Checks Python, finds a Project Zomboid Build 42 install, extracts the
REM  character models / animations / appearance data out of it into
REM  assets\characters, and then offers to install the Blender add-on that
REM  reads them.
REM
REM  The extractor needs no pip packages at all - it is stdlib-only Python.
REM  Blender ships its own Python, so the add-on needs nothing installed
REM  either.
REM
REM  Run this LOCALLY, not over a network drive: the export reads ~3,000
REM  small .x files and is file-I/O bound, so a mount turns minutes into
REM  hours.
REM
REM  Re-running is safe. It only overwrites assets\characters.
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   ================================================================
echo    PZ-CHARACTER  --  first-time setup
echo   ================================================================
echo.
echo    1. Python check
echo    2. Find your Project Zomboid install
echo    3. Extract characters  ^(~10 min, ~200 MB^)
echo    4. Install the Blender add-on  ^(optional^)
echo.
echo   pz-character is an independent fan project, not affiliated with The
echo   Indie Stone. It reads data from a Project Zomboid install you legally
echo   own and contains no PZ code, art, or other copyrighted material of its
echo   own. Full notice: LEGAL_DISCLAIMER.md
echo.
set /p PZ_EULA_ACCEPT=Type accept to continue:
if /i not "!PZ_EULA_ACCEPT!"=="accept" (
    echo Aborted.
    exit /b 1
)
echo.

REM ---- 1. Python -------------------------------------------------------
echo    [1/4] Python
where python >nul 2>nul
if errorlevel 1 goto no_python
set "PYVER="
for /f "delims=" %%V in ('python --version 2^>^&1') do set "PYVER=%%V"
echo !PYVER! | findstr /b /c:"Python 3" >nul
if errorlevel 1 goto no_python
echo           Found: !PYVER!
echo           No pip packages needed - the extractor is stdlib only.
goto have_python

:no_python
echo.
echo    ****  Python 3 was not found on PATH.
echo    ****  Install it from https://www.python.org/downloads/ -
echo    ****  tick "Add python.exe to PATH" in the installer - then
echo    ****  re-run this script.
echo.
pause
exit /b 1

:have_python

REM ---- 2. locate the game ----------------------------------------------
echo.
echo    [2/4] Project Zomboid install
set "GAME="
if defined PZ_GAME_DIR if exist "%PZ_GAME_DIR%\media" set "GAME=%PZ_GAME_DIR%"
if not defined GAME if exist "game_dir.txt" (
  for /f "usebackq delims=" %%G in ("game_dir.txt") do set "GAME=%%G"
)
if defined GAME if not exist "!GAME!\media" set "GAME="

for %%D in (
  "C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid"
  "C:\Program Files\Steam\steamapps\common\ProjectZomboid"
  "D:\SteamLibrary\steamapps\common\ProjectZomboid"
  "E:\SteamLibrary\steamapps\common\ProjectZomboid"
  "A:\SteamLibrary\steamapps\common\ProjectZomboid"
) do if not defined GAME if exist "%%~D\media" set "GAME=%%~D"

if not defined GAME (
  echo           Opening a folder picker - select your ProjectZomboid
  echo           install folder...
  for /f "usebackq delims=" %%G in (`powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\pick_folder.ps1" 2^>nul`) do set "GAME=%%G"
)

if not defined GAME (
  echo           No folder selected.
  echo.
  set /p "GAME=           Full path to your ProjectZomboid folder: "
)

REM !GAME! (delayed expansion), never %GAME%, inside a (...) block - a real
REM Steam path is commonly "C:\Program Files (x86)\...", and %GAME% expands
REM BEFORE the block's own parens are parsed, so that literal ")" closes the
REM block early and cmd dies with "... was unexpected at this time." and no
REM pause, which just looks like the window vanished.
if not exist "!GAME!\media\models_X" (
  echo.
  echo    ****  !GAME!
  echo    ****  does not look like a Project Zomboid Build 42 install -
  echo    ****  no media\models_X folder there. Re-run this script once
  echo    ****  you have the right path.
  echo.
  pause
  exit /b 1
)
echo           Using: !GAME!
> "game_dir.txt" echo !GAME!
set "PZ_GAME_DIR=!GAME!"

REM ---- 3. extract -------------------------------------------------------
echo.
echo    [3/4] Extract
echo.
echo    About to read media\models_X, media\anims_X, media\AnimSets and
echo    media\textures, and write:
echo.
echo      assets\characters\characters.pzc          meshes + animation clips
echo      assets\characters\character_manifest.json what is inside it
echo      assets\characters\appearance_pools.json   hair/beard/clothing pools
echo      assets\characters\locomotion_manifest.json  bone masks
echo      assets\characters\textures\               per-mesh PNGs, copied
echo.
echo    Nothing in your game install is modified. This takes about ten
echo    minutes; leave the window open.
echo.
set /p "ok=   Continue? [y/N] "
if /i not "%ok%"=="y" (
  echo.
  echo   Nothing extracted. Re-run setup.bat, or extract.bat directly,
  echo   whenever you're ready.
  pause
  exit /b 0
)

REM PZ_GAME_DIR is already set above and game_dir.txt already written -
REM extract.bat picks up both on its own (see pz_characters.py's GAME_DIR
REM comment), so this is a real delegation, not a second copy of the same
REM two `python` calls drifting out of sync with extract.bat's own.
call extract.bat
if errorlevel 1 ( echo.& echo   ****  The character export failed - scroll up for the error.& echo.& pause & exit /b 1 )

REM ---- 4. Blender add-on ------------------------------------------------
call :install_addon

echo.
echo   ================================================================
echo    Done.
echo   ================================================================
echo.
echo    In Blender: Edit ^> Preferences ^> Add-ons, enable
echo    "PZ Character Viewer", then press N in the 3D viewport and open
echo    the "PZ Character" tab.
echo.
echo    If the panel says "(not set)", click its folder button and pick:
echo      %~dp0assets\characters
echo.
pause
exit /b 0

REM ----------------------------------------------------------------------
REM  A subroutine, and every early exit is `exit /b` - no forward `goto`
REM  anywhere in here. cmd.exe has a real, reproducible parser bug where a
REM  goto out of a block past intervening call/if blocks lands nowhere: the
REM  same code written with forward labels reported "cannot find the batch
REM  label" for a label plainly present, and answering n silently skipped
REM  the whole step. Do not "tidy" this back into gotos.
REM ----------------------------------------------------------------------
:install_addon
echo.
echo    [4/4] Blender add-on
set "BLROOT=%APPDATA%\Blender Foundation\Blender"
if not exist "!BLROOT!" (
  echo           No Blender config found under %%APPDATA%%.
  echo           Install by hand - see README.md, "Install the add-on".
  exit /b 0
)

REM /on sorts ascending, so the last one assigned is the newest version.
set "BLVER="
for /f "delims=" %%V in ('dir /b /ad /on "!BLROOT!" 2^>nul') do set "BLVER=%%V"
if not defined BLVER (
  echo           No Blender version folder under !BLROOT!.
  echo           Install by hand - see README.md, "Install the add-on".
  exit /b 0
)

set "ADDONS=!BLROOT!\!BLVER!\scripts\addons"
echo           Newest Blender config: !BLVER!
set /p "wantaddon=   Copy the add-on into that Blender? [y/N] "
if /i not "!wantaddon!"=="y" (
  echo           Skipped. Install by hand - see README.md.
  exit /b 0
)

if not exist "!ADDONS!" mkdir "!ADDONS!"
REM /MIR so an edited copy replaces the old one exactly, deletions included -
REM a stale leftover .py inside an add-on folder is imported happily by
REM Blender and is completely invisible when it goes wrong.
robocopy "src\addon\pz_character_viewer" "!ADDONS!\pz_character_viewer" /MIR /XD __pycache__ /XF *.pyc *.import /NJH /NJS /NDL /NP >nul
REM robocopy's exit codes are a bitmask: 0-7 are success (0 = nothing to do,
REM 1 = files copied, ...), 8 and up are real failures. `errorlevel 8` means
REM ">= 8", which is exactly the test wanted here.
if errorlevel 8 (
  echo           ****  Copy failed. Install by hand - see README.md.
  exit /b 0
)
echo           Copied to !ADDONS!\pz_character_viewer
exit /b 0

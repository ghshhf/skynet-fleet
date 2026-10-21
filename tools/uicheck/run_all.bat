@echo off
rem ==========================================================================
rem  Fleet GUI acceptance suite (Windows)
rem    1) Real screenshot at 1440x900 + optional pixel diff vs baseline
rem    2) Accessibility + contrast scan (axe-core, WCAG A/AA)
rem    3) Runtime click audit (finds dead links by actually clicking)
rem  Non-zero exit = something is broken (gate turns red)
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat with the system
rem  codepage (GBK here), so UTF-8 comments turn into garbage commands.
rem ==========================================================================
setlocal
cd /d "%~dp0..\.."

echo.
echo === 1/3 screenshot + pixel diff ===
if "%1"=="--baseline" (
  node tools\uicheck\shot.js --out baseline
) else (
  node tools\uicheck\shot.js --out current --diff
)
set RC1=%errorlevel%

echo.
echo === 2/3 accessibility + contrast ===
node tools\uicheck\a11y.js --theme light
set RC2=%errorlevel%

echo.
echo === 3/3 runtime click audit (dead links) ===
node tools\uicheck\clickaudit.js --json tools\uicheck\shots\clickaudit.json
set RC3=%errorlevel%

echo.
if %RC1%%RC2%%RC3%=="000" (
  echo [uicheck] all passed
) else (
  echo [uicheck] failures: shot=%RC1% a11y=%RC2% clicks=%RC3%
)
exit /b %RC1%%RC2%%RC3%

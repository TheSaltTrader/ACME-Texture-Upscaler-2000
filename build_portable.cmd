@echo off
REM Build the portable ACMETextureUpscaler2000.exe with PyInstaller, then stage
REM a release folder (exe + upscaler engine/models + README) under .\Releases.
setlocal
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --noconsole --clean ^
  --name ACMETextureUpscaler2000 acme_upscaler.py || exit /b 1
echo === staging release ===
python stage_release.py || exit /b 1
echo === done ===

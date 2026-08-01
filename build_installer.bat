@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1"
set EXIT=%ERRORLEVEL%
if not "%EXIT%"=="0" exit /b %EXIT%
endlocal

@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALL_MLOMEGA_V18_8_WINDOWS.ps1" %*
exit /b %ERRORLEVEL%

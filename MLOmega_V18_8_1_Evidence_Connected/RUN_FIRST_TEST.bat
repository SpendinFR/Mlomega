@echo off
REM Compatibility launcher: V18.8 starts the guarded production flow.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_MLOMEGA_V18_8.ps1" %*

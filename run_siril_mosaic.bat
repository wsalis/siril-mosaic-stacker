@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py sirilmosaic_gui.py
) else (
    python sirilmosaic_gui.py
)
if errorlevel 1 pause

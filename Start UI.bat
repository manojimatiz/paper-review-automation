@echo off
REM Double-click this to open the control panel. Installs Flask on first run.
REM
REM There is deliberately no "is Flask installed?" check before launching: that
REM check started a whole extra Python interpreter and cost about three seconds
REM on every single launch, to answer a question that is only ever "yes" after the
REM first run. ui.py exits with code 2 when Flask is missing, so the install path
REM below runs only when it is actually needed.
title Paper Review Automation
cd /d "%~dp0"

py ui.py
if errorlevel 2 (
    echo.
    echo Setting up for first use, one moment...
    py -m pip install --quiet flask flask-login waitress pystray Pillow
    if errorlevel 1 (
        echo.
        echo Could not install Flask. Check your internet connection and try again.
        pause
        exit /b 1
    )
    py ui.py
)
if errorlevel 1 pause

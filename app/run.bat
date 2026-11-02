@echo off
cd /d %~dp0

echo ============================================
echo   FFmpeg -^> Icecast : control panel
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [WARNING] ffmpeg not found in PATH.
    echo Download a build from https://www.gyan.dev/ffmpeg/builds/
    echo or set the path to ffmpeg.exe later in the panel ^(section 08^).
    echo.
)

echo Installing dependencies...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo Retrying with --user...
    python -m pip install --quiet --user -r requirements.txt
)
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo Opening panel: http://127.0.0.1:8770
start "" http://127.0.0.1:8770
python app.py
pause

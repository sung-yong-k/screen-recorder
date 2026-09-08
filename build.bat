@echo off
REM Build ScreenRecorder.exe (single file, ffmpeg bundled) into dist\
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
)

call .venv\Scripts\activate.bat || goto :error

echo Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || goto :error
python -m pip install pyinstaller || goto :error

echo Building...
rmdir /s /q build 2>nul
python -m PyInstaller --noconfirm --clean ScreenRecorder.spec || goto :error

echo.
echo Done:  %CD%\dist\ScreenRecorder.exe
goto :eof

:error
echo.
echo Build failed.
exit /b 1

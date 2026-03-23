@echo off
echo Building nymeria-backend.exe...
cd /d "%~dp0"
pip install pyinstaller --quiet 2>nul
pyinstaller nymeria-backend.spec --noconfirm
echo.
if exist "dist\nymeria-backend.exe" (
    echo Build successful: dist\nymeria-backend.exe
) else (
    echo Build FAILED. Check output above for errors.
    exit /b 1
)

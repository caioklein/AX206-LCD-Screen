@echo off
REM Builds "Appotech AX206 LCD Screen Manager.exe" (single file) into the dist folder.
REM Needs Python 3.10+ for Windows (python.org) with the "py" launcher.
cd /d "%~dp0"
py -m pip install --upgrade -r requirements.txt pyinstaller || goto :err
py make_icon.py || goto :err
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "Appotech AX206 LCD Screen Manager" ^
  --icon assets\app.ico --version-file version_info.txt ^
  --add-data "assets;assets" ^
  --collect-all customtkinter --collect-all libusb_package --collect-all imageio_ffmpeg ^
  --hidden-import usb.backend.libusb1 ^
  app.py || goto :err
echo.
echo Done: dist\Appotech AX206 LCD Screen Manager.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1

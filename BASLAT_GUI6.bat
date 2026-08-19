@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo Whole Slide Uploader GUI6
echo ========================================
echo Calisan dosya:
echo %~dp0whole_slide_uploader_GUI6.py
echo.

if not exist "%~dp0whole_slide_uploader_GUI6.py" (
  echo HATA: whole_slide_uploader_GUI6.py bu klasorde bulunamadi.
  pause
  exit /b 1
)

py -3 "%~dp0whole_slide_uploader_GUI6.py" --version
if errorlevel 1 (
  echo.
  echo Surum kontrolu basarisiz. Python/py kurulumu veya dosya hatasi olabilir.
  pause
  exit /b 1
)

echo.
echo GUI aciliyor...
py -3 "%~dp0whole_slide_uploader_GUI6.py" --gui
if errorlevel 1 (
  echo.
  echo Program hata ile kapandi.
  pause
)
endlocal

@echo off
title TV Playlist Guncelle
cd /d "%~dp0"

set PY=python
where python >nul 2>nul || set PY=py
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python bulunamadi. https://www.python.org/downloads/ adresinden kurun,
    echo kurulumda "Add Python to PATH" kutusunu isaretleyin, sonra tekrar calistirin.
    pause
    exit /b
)

echo [1/3] Gerekli paketler kontrol ediliyor (ilk seferde birkac dakika surebilir)...
%PY% -m pip install -q requests playwright
%PY% -m playwright install chromium

echo.
echo [2/3] Linkler kontrol ediliyor, olu olanlar resmi sayfalardan yenileniyor...
%PY% official.py --yerel

echo.
echo [3/3] Bitti. Bu pencereyi kapatabilirsiniz.
pause


@echo off
setlocal

echo [1/3] Aktivacija virtuelnog okruzenja...
call .venv\Scripts\activate
if errorlevel 1 (
    echo GRESKA: Nije pronadjen .venv. Prvo pokrenite:
    echo python -m venv .venv
    exit /b 1
)

echo [2/3] Instalacija PyInstaller-a...
pip install pyinstaller
if errorlevel 1 (
    echo GRESKA: Instalacija PyInstaller nije uspela.
    exit /b 1
)

echo [2b/3] Generisanje ICO ikone iz ICON\PIGEONp.png...
python -c "from pathlib import Path; from PIL import Image; src=Path('ICON/PIGEONp.png'); im=Image.open(src).convert('RGBA'); sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]; imgs=[im.resize(s, Image.Resampling.LANCZOS) for s in sizes]; out=Path('ICON/app.ico'); imgs[0].save(out, format='ICO', sizes=[(i.width,i.height) for i in imgs], append_images=imgs[1:])"
if errorlevel 1 (
    echo GRESKA: Neuspelo pravljenje ICON\app.ico (proverite Pillow i fajl ICON\PIGEONp.png).
    exit /b 1
)

echo [3/3] Build EXE...
pyinstaller --noconfirm --onefile --windowed --name TaurisPdfApp --icon "ICON\app.ico" --collect-all reportlab --hidden-import reportlab.graphics.barcode.code128 --hidden-import reportlab.graphics.barcode.code39 --hidden-import reportlab.graphics.barcode.qr --add-data "ICON\\PIGEONp.png;ICON" --add-data "ICON\\pigeon.png;ICON" main.py
if errorlevel 1 (
    echo GRESKA: Build nije uspeo.
    exit /b 1
)

echo.
echo GOTOVO. EXE je kreiran u folderu dist\TaurisPdfApp.exe
endlocal

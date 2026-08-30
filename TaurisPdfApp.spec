# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

_build_flavor = os.environ.get("TAURIS_BUILD_FLAVOR", "full").strip().lower()

datas = [
    ('ICON\\PIGEONp.png', 'ICON'),
    ('ICON\\pigeon.png', 'ICON'),
    ('ICON\\Pigeon in sky.png', 'ICON'),
]
binaries = []
hiddenimports = [
    'reportlab.graphics.barcode.code128',
    'reportlab.graphics.barcode.code39',
    'reportlab.graphics.barcode.qr',
]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pywin32')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

if _build_flavor == "lite":
    # U spec fajlu nema __file__; PyInstaller daje SPECPATH (folder ili .spec — normalizuj).
    _sp = Path(SPECPATH).resolve()
    _spec_dir = _sp.parent if _sp.suffix.lower() == ".spec" else _sp
    _marker = _spec_dir / "TAURIS_LITE"
    if not _marker.is_file():
        raise FileNotFoundError(
            "Lite build: napravite prazan fajl TAURIS_LITE u korenu projekta "
            "(build_installer.ps1 to radi automatski)."
        )
    datas.append((str(_marker), "."))


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['hooks/pyi_rth_tauris_ssl.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TaurisPdfApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['ICON\\app.ico'],
)

# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import pysiril

pysiril_lib = Path(pysiril.__file__).resolve().parent / 'lib'


a = Analysis(
    ['sirilmosaic_gui.py'],
    pathex=[str(pysiril_lib)],
    binaries=[],
    datas=[('run_siril_mosaic.bat', '.')],
    hiddenimports=['changelog', 'LogMsg', 'PipeReader', 'PipeWriter', 'ThreadSiril', 'tools'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    name='SirilMosaicStacker',
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
)

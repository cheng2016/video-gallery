# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: onedir package for Windows end users.

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('vg', 'vg'),
    ],
    hiddenimports=[
        'waitress',
        'flask',
        'vg',
        'vg.main',
        'vg.web',
        'vg.scan',
        'vg.convert',
        'vg.media',
        'vg.cache',
        'vg.export',
        'vg.segments',
        'vg.series',
        'vg.streaming',
        'vg.drives',
        'vg.genres',
        'vg.util',
        'vg.state',
        'vg.config',
        'vg.trash',
        'vg.bootlog',
        'vg.lan',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoGallery',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VideoGallery',
)

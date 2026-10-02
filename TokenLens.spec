# -*- mode: python ; coding: utf-8 -*-
# TokenLens Windows 单文件打包：pyinstaller TokenLens.spec --noconfirm
# 产物 dist/TokenLens.exe：双击启动代理+仪表盘，读取 ~/.tokenlens/config.json

a = Analysis(
    ['scripts/launch_server.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('tokenlens/web', 'tokenlens/web'),
        ('tokenlens/pricing_data.json', 'tokenlens'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'anyio._backends._asyncio',
        'pystray',
        'pystray._win32',
    ],
    excludes=['tiktoken', 'pytest', 'mypy', 'pyinstaller'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TokenLens',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

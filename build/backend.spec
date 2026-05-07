# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['faster_whisper', 'pyannote.audio', 'pyannote.core', 'pyannote.metrics', 'curl_cffi', 'hltv_async_api', 'dotenv', 'bs4', 'lxml', 'fastapi', 'uvicorn', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'starlette', 'starlette.middleware', 'httpx', 'python_multipart', 'openai', 'anthropic', 'requests', 'yt_dlp', 'aiohttp', 'websockets', 'certifi', 'multipart']
hiddenimports += collect_submodules('torch')
hiddenimports += collect_submodules('pyannote.audio')
hiddenimports += collect_submodules('faster_whisper')


a = Analysis(
    ['C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python\\server.py'],
    pathex=['C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python'],
    binaries=[],
    datas=[('C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python\\services\\__init__.py', 'services\\'), ('C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python\\services\\whisper_service.py', 'services\\'), ('C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python\\services\\llm_service.py', 'services\\'), ('C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python\\services\\hltv_scraper.py', 'services\\'), ('C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\python\\services\\diarization.py', 'services\\')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['C:\\Users\\Z\\Desktop\\Auto-Translate-to-News\\build\\hook-torch-dll.py'],
    excludes=['tensorflow', 'tensorboard', 'cv2', 'pyarrow', 'onnxruntime', 'matplotlib', 'pandas', 'jupyter', 'ipython', 'notebook', 'sqlalchemy', 'PIL', 'sympy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='backend',
)

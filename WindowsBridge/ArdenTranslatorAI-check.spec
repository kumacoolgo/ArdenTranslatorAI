# -*- mode: python ; coding: utf-8 -*-
#
# 诊断版（console=True）。datas / hiddenimports 与主 spec 保持一致 ——
# 两个 EXE 必须能装同一个插件、跑同一套模块，否则「用 check 版验证过」
# 就不能代表「GUI 版没问题」。

import os

_here = SPECPATH if 'SPECPATH' in globals() else os.path.abspath(os.getcwd())
ADDON_DIR = os.path.normpath(os.path.join(_here, '..', 'ArdenTranslatorAI'))
if not os.path.isfile(os.path.join(ADDON_DIR, 'ArdenTranslatorAI.lua')):
    raise SystemExit(
        "[spec] 找不到插件源码：%s\n"
        "       构建前请确认 WindowsBridge\\ 与 ArdenTranslatorAI\\ 在同一层。"
        % ADDON_DIR)

# 白名单与主 spec 完全一致；改一处必须同步改另一处。
hiddenimports = [
    'protocol', 'jobs', 'cache', 'dedupe', 'diagnostics', 'transport',
]

a = Analysis(
    ['ArdenTranslatorAI.py'],
    pathex=[_here],
    binaries=[],
    datas=[(ADDON_DIR, 'plugin')],
    hiddenimports=hiddenimports,
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
    name='ArdenTranslatorAI-check',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

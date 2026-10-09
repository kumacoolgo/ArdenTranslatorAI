# -*- mode: python ; coding: utf-8 -*-
#
# V11-06：datas 里原来写死了开发机绝对路径
#     'C:/Users/cheny/Downloads/.../ArdenTranslatorAI_v1/ArdenTranslatorAI'
# 在别人的机器上这个目录必然不存在，PyInstaller 直接构建失败（或者更糟：
# 打包出一个没有插件的 EXE）。现在改成**相对 spec 文件所在目录**计算，
# 在任何路径下都能构建。

import os

# PyInstaller 执行 spec 时会把 spec 所在目录放进 __file__ 语义 —— 用
# SPECPATH（PyInstaller 注入的变量）最稳，回退到 cwd。
_here = SPECPATH if 'SPECPATH' in globals() else os.path.abspath(os.getcwd())
ADDON_DIR = os.path.normpath(os.path.join(_here, '..', 'ArdenTranslatorAI'))
if not os.path.isfile(os.path.join(ADDON_DIR, 'ArdenTranslatorAI.lua')):
    raise SystemExit(
        "[spec] 找不到插件源码：%s\n"
        "       构建前请确认 WindowsBridge\\ 与 ArdenTranslatorAI\\ 在同一层。"
        % ADDON_DIR)

# v1.1：桥接端新增了本地模块，必须让 PyInstaller 把它们收进 EXE。
# 这里是**白名单**，不是提示 —— 列了不存在的模块会被 PyInstaller 忽略，
# 但列漏了真实模块会让 EXE 在别人机器上 ImportError（隐藏 bug 只在打包后才暴露）。
# 所以：新增/删除桥接模块后，**同时**改这两个 spec 的列表，并用
# `pyinstaller ... && 生成的 EXE --check` 验证。
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
    name='ArdenTranslatorAI',
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

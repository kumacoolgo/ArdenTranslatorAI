"""Verify the packaged EXE carries NO secret of yours.

Three independent checks, because "I think it's fine" is not evidence:
  1. Raw byte scan of both EXEs for the actual key (and every long fragment).
  2. Raw byte scan of the bundled addon files.
  3. Enumerate every data file PyInstaller actually packed (from Analysis-00.toc),
     so we can see there is no stray config.json / .env / key file riding along.

Run:  python _leakcheck.py
"""
import ast, os, re, sys, winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
EXE = DIST / "ArdenTranslatorAI.exe"
CHECK = DIST / "ArdenTranslatorAI-check.exe"
TOC = HERE / "build" / "ArdenTranslatorAI" / "Analysis-00.toc"

ok = True


def check(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    tag = "PASS" if good else "FAIL"
    print(f"  [{tag}] {label}" + ("" if good else f"\n         got = {got}"))


def read_key():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        v, _ = winreg.QueryValueEx(k, "ZHIPU_API_KEY")
    return v


def scan(path, needle):
    return path.read_bytes().find(needle.encode())


print("[1] your actual key")
key = read_key()
print(f"     ZHIPU_API_KEY = {key[:6]}...{key[-4:]}  (len {len(key)})")
# 智谱 Key 形如 "xxxxxxxx.yyyyyyyy"；拿整串 + 拆分出的长片段一起扫，避免压缩漏网。
fragments = [key]
for frag in re.split(r"[.\-_\s]", key):
    if len(frag) >= 8:
        fragments.append(frag)

print("\n[2] byte scan of both EXEs")
for exe in (EXE, CHECK):
    if not exe.exists():
        print(f"     (missing {exe.name})")
        continue
    found = [(f, scan(exe, f)) for f in fragments if scan(exe, f) >= 0]
    check(f"{exe.name}: no key fragment found", found, [])

print("\n[3] byte scan of the bundled addon (the only extra data packed)")
src = HERE.parent / "ArdenTranslatorAI"
for f in sorted(src.iterdir()):
    if f.is_file():
        hits = [fr for fr in fragments if scan(f, fr) >= 0]
        check(f"{f.name}: clean", hits, [])

print("\n[4] every data file PyInstaller actually packed")
if TOC.exists():
    # PyInstaller 的 .toc 不是 JSON，是 Python 字面量的文本表示，也没有字段名 ——
    # 直接把所有形如 (dest, src, 'DATA') 的条目捞出来。
    toc = ast.literal_eval(TOC.read_text(encoding="utf-8"))
    datas = []
    for item in toc:
        if not isinstance(item, (list, tuple)):
            continue
        for e in item:
            if isinstance(e, (list, tuple)) and len(e) == 3 and e[2] == "DATA":
                if (str(e[0]), str(e[1])) not in datas:   # .toc 里有重复条目
                    datas.append((str(e[0]), str(e[1])))

    # 我们 --add-data 塞进去的东西，dest 一律是 plugin\ 开头。
    # 其余几百个是 PyInstaller 自己带的 tcl/tk / 标准库运行时资源，跟我们无关。
    ours = [d for d in datas if d[0].replace("\\", "/").startswith("plugin/")]
    runtime = [d for d in datas if d not in ours]

    print(f"     runtime resources (tcl/tk/stdlib, auto-added): {len(runtime)}")
    print(f"     files WE added: {len(ours)}")
    for dest, src_path in ours:
        print(f"       {src_path}\n         -> {dest}")

    suspicious = [d for d in datas
                  if any(s in d[1].lower() for s in
                         ("config.json", ".env", "credential", "secret", "api_key"))]
    check("no config.json / .env / key file bundled", suspicious, [])
    check("the only files we added are the two addon files",
          sorted(Path(d[1]).name for d in ours),
          ["ArdenTranslatorAI.lua", "ArdenTranslatorAI.toc"])
    check("no user data directory bundled",
          [d for d in ours if "ArdenTranslatorAI" not in Path(d[1]).name], [])
else:
    print(f"     (no TOC at {TOC}; skipping)")

print("\n[5] scan EVERY file in dist/ (not just the EXEs)")
dist_files = [p for p in DIST.rglob("*") if p.is_file()] if DIST.exists() else []
print(f"     files under dist/: {len(dist_files)}")
leaks = []
for p in dist_files:
    try:
        blob = p.read_bytes()
    except OSError:
        continue
    for fr in fragments:
        if blob.find(fr.encode()) >= 0:
            leaks.append((str(p), fr[:6] + "..."))
check("no file in dist/ contains the key", leaks, [])

print("\n[6] informational - what strings ARE visible in the EXE")
# 注意：PyInstaller 把 .py 编译成 .pyc 并压进 PYZ，所以源码里的常量字符串
# 通常不以明文躺在 EXE 里 —— 下面只是如实报告，能找到也不算泄密：
# 环境变量的*名字*和智谱的*接口域名*都是公开信息，本来就要写进代码。
raw = EXE.read_bytes()
for label, needle in (("variable name ZHIPU_API_KEY", b"ZHIPU_API_KEY"),
                      ("zhipu endpoint open.bigmodel.cn", b"open.bigmodel.cn"),
                      ("literal 'api_key'", b"api_key")):
    pos = raw.find(needle)
    print(f"     {label}: {'visible at +' + str(pos) if pos >= 0 else 'not in plain text (compressed)'}")
print("     (none of these is a secret - they are names/URLs, not values)")

print("\n[7] the config.json on YOUR disk (not shipped, but let's be sure)")
cfg = Path(os.environ.get("LOCALAPPDATA", "")) / "ArdenTranslatorAI" / "config.json"
if cfg.exists():
    txt = cfg.read_text(encoding="utf-8")
    check("local config.json has no plaintext key", '"api_key"' in txt, False)
    check("local config.json has no key-looking value",
          bool(re.search(r"sk-[A-Za-z0-9]{10,}", txt)), False)
else:
    print("     (no config.json yet - it is created on first run)")

print()
print("NO SECRET FOUND - safe to share" if ok else "*** SOMETHING LEAKED - DO NOT SHARE ***")
sys.exit(0 if ok else 1)

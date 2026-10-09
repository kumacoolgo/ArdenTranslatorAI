"""GUI smoke test for the v1.0.15 settings window (run with the system Python 3.11).

Verifies, without any human clicking:
  1. Launcher builds and shows every widget the six requirements asked for.
  2. The pre-filled values match the config / the requested defaults.
  3. Pressing "start" really deploys the addon into the game's AddOns folder,
     saves the config, destroys the settings window and raises the overlay.

Run:  python _gui_probe.py
"""
import importlib.util, os, sys, time
from pathlib import Path
import tkinter as tk

SRC = Path(__file__).resolve().parent / "ArdenTranslatorAI.py"
spec = importlib.util.spec_from_file_location("atai", SRC)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# 真实 Key 只从环境变量读；探测脚本绝不把它写进任何文件。
KEY_TEXT = m.get_env(m.DEFAULT_API_KEY_ENV)
# 用一个临时变量名验证「填 Key -> 写进环境变量」这条路，不去动用户真正的 Key。
SCRATCH_ENV = "ATAI_PROBE_KEY"
PROBE_VALUE = "probe-key-value-123456"
_OLD_SCRATCH = m.get_env(SCRATCH_ENV)

ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         got  = {got!r}")
        print(f"         want = {want!r}")

WOW = "C:/WOW/ArdenWoW/Wotlk"

root = tk.Tk()
root.withdraw()

started = {"app": None, "called": 0}

print("[1] Launcher builds")
launcher = m.Launcher(root)
root.update()
def start_overlay():
    started["called"] += 1
    started["app"] = m.App(root)
    root.deiconify()
launcher.on_start = start_overlay

check("settings window exists", launcher.win.winfo_exists(), 1)
# 路径分隔符不比较：filedialog 给的是反斜杠，这里给的是正斜杠，Path 一样认。
check("wow_path pre-filled from config", Path(launcher.var_wow.get()), Path(WOW))
check("model pre-filled", launcher.var_model.get(), "glm-4-flash-250414")
key_shown = launcher.var_key.get()
check("API key pre-filled from env var", len(key_shown) > 10, True)
check("key entry is masked", launcher.entry_key.cget("show"), "\u2022")
# v1.0.16：显示/隐藏按钮已按用户要求删掉（Key 从不以明文呈现）
check("no reveal button", hasattr(launcher, "btn_eye"), False)
check("no _toggle_eye method", hasattr(launcher, "_toggle_eye"), False)
check("overlay defaults pre-filled",
      {k: launcher.var_ov[k].get() for k in
       ("width", "height", "x", "y", "alpha", "font_size", "max_lines")},
      {"width": "500", "height": "200", "x": "40", "y": "520",
       "alpha": "0.88", "font_size": "9", "max_lines": "7"})
check("test button present", launcher.btn_test.cget("text"), "测试模型 + Key")

# 回归：曾经因为「先 _center() 后 attributes('-topmost')」导致 Tk 在 Windows 上
# 把窗口位置重置回级联位置（+156+156），管理界面贴在左上角。这里钉死这个行为。
sw, sh = launcher.win.winfo_screenwidth(), launcher.win.winfo_screenheight()
ww, wh = launcher.win.winfo_width(), launcher.win.winfo_height()
cx, cy = launcher.win.winfo_x(), launcher.win.winfo_y()
check("window is horizontally centred", abs(cx - (sw - ww) // 2) <= 4, True)
check("window is vertically placed", abs(cy - (sh - wh) // 3) <= 4, True)
print(f"         (screen {sw}x{sh}, window {ww}x{wh} @ {cx},{cy})")

print("[2] overlay field parsing (garbage must not crash / must clamp)")
launcher.var_ov["width"].set("abc")
launcher.var_ov["height"].set("-5")
launcher.var_ov["alpha"].set("9")
launcher.var_ov["font_size"].set("999")
ov = launcher._read_overlay()
check("bad width -> default", ov["width"], 500)
check("negative height clamped", ov["height"], 60)
check("alpha clamped to 1.0", ov["alpha"], 1.0)
check("font clamped to 40", ov["font_size"], 40)
launcher.var_ov["width"].set("640")
launcher.var_ov["height"].set("260")
launcher.var_ov["alpha"].set("0.75")
launcher.var_ov["font_size"].set("11")

print("[3] 'start' writes the key to the ENV VARIABLE, deploys, raises the overlay")
before = m.CONFIG_PATH.read_text(encoding="utf-8")
# 把变量名临时换成探针用的那个，这样「点开始 -> 写环境变量」这条路径能被真正
# 验证，又不会覆盖用户真正的 ZHIPU_API_KEY。
launcher.cfg.setdefault("compat", {})["api_key_env"] = SCRATCH_ENV
launcher.var_key.set(PROBE_VALUE)
launcher._start()
root.update()
time.sleep(0.6)
root.update()

dest = Path(WOW) / "Interface" / "AddOns" / "ArdenTranslatorAI"
check("start hook fired", started["called"], 1)
try:
    alive = launcher.win.winfo_exists()
except tk.TclError:
    alive = 0
check("settings window destroyed", alive, 0)
check("overlay created", started["app"] is not None, True)
check("addon folder deployed", dest.is_dir(), True)
check("addon .lua deployed", (dest / "ArdenTranslatorAI.lua").is_file(), True)
check("addon .toc deployed", (dest / "ArdenTranslatorAI.toc").is_file(), True)

src = m.plugin_source_dir()
check("deployed .lua == bundled .lua",
      (dest / "ArdenTranslatorAI.lua").read_bytes(),
      (src / "ArdenTranslatorAI.lua").read_bytes())
check("deployed .toc == bundled .toc",
      (dest / "ArdenTranslatorAI.toc").read_bytes(),
      (src / "ArdenTranslatorAI.toc").read_bytes())

saved = m.load_config()
check("config wow_path saved", Path(saved["wow_path"]), Path(WOW))
check("config model saved", saved["compat"]["model"], "glm-4-flash-250414")
check("config overlay saved", saved["overlay"],
      {"x": 40, "y": 520, "width": 640, "height": 260,
       "alpha": 0.75, "font_size": 11, "max_lines": 7})
check("legacy provider keys dropped",
      [k for k in saved if k in ("openai_model", "gemini_model")], [])
check("provider pinned to zhipu", saved["provider"], "zhipu")

# v1.0.16 硬要求：config.json 里绝对不能有明文 Key
raw = m.CONFIG_PATH.read_text(encoding="utf-8")
check("typed key landed in the env variable", m.get_env(SCRATCH_ENV), PROBE_VALUE)
check("real ZHIPU_API_KEY untouched", m.get_env("ZHIPU_API_KEY")[:12], KEY_TEXT[:12])
check("config.json has no api_key field", '"api_key"' in raw, False)
check("config.json contains no key text", PROBE_VALUE in raw, False)
check("only the env var NAME is stored", saved["compat"]["api_key_env"], SCRATCH_ENV)
check("key resolves back through effective_api_key",
      m.effective_api_key(saved), PROBE_VALUE)

# 收尾：删掉探针变量、把变量名恢复成 ZHIPU_API_KEY
m.set_user_env(SCRATCH_ENV, "")
restore = m.load_config()
restore.setdefault("compat", {})["api_key_env"] = m.DEFAULT_API_KEY_ENV
restore["wow_path"] = WOW
m.save_config(restore)

print("[4] overlay honours the saved geometry")
ovw = started["app"].overlay.root
root.update()
check("overlay width x height", (ovw.winfo_width(), ovw.winfo_height()), (640, 260))
check("overlay position", (ovw.winfo_x(), ovw.winfo_y()), (40, 520))
check("overlay is borderless", bool(ovw.overrideredirect()), True)
check("overlay topmost", bool(ovw.attributes("-topmost")), True)
check("overlay alpha", round(float(ovw.attributes("-alpha")), 2), 0.75)

print("[5] second start is idempotent (users click it every launch)")
ok2, msg2, _ = m.deploy_addon(WOW)
check("redeploy ok", ok2, True)
check("no extra files after redeploy",
      sorted(f.name for f in dest.iterdir() if f.is_file()),
      sorted(m.ADDON_FILES))

print("[5b] a friend's fresh PC: no key anywhere -> must ask, not silently fail")
NOKEY = "ATAI_PROBE_NO_SUCH_KEY"
os.environ.pop(NOKEY, None)
cfg2 = m.load_config()
cfg2.setdefault("compat", {})["api_key_env"] = NOKEY
m.save_config(cfg2)

asked = {"n": 0}
real_ask = m.messagebox.askyesno
m.messagebox.askyesno = lambda *a, **k: (asked.__setitem__("n", asked["n"] + 1), False)[1]
started2 = {"called": 0}
l2 = m.Launcher(root)
l2.on_start = lambda: started2.__setitem__("called", 1)
root.update()
l2.var_wow.set(WOW)
l2.var_key.set("")
l2._start()
root.update()
m.messagebox.askyesno = real_ask
check("no key -> warned the user instead of silently starting", asked["n"], 1)
check("no key -> overlay NOT launched", started2["called"], 0)
check("no key -> settings window stays open so they can paste a key",
      l2.win.winfo_exists(), 1)
l2.win.destroy()
root.update()

# 收尾：把探测前的那份配置放回去（悬浮窗几何保持用户要的 500x200 默认值）。
# v1.0.16 起 Key 不存在配置文件里，所以这里只需还原几何/路径，跟 Key 无关。
restored = m.json.loads(before)
restored.setdefault("compat", {}).pop("api_key", None)
restored.setdefault("compat", {})["api_key_env"] = m.DEFAULT_API_KEY_ENV
restored["wow_path"] = WOW
m.CONFIG_PATH.write_text(m.json.dumps(restored, ensure_ascii=False, indent=2),
                         encoding="utf-8")
print("[6] restored config:",
      {k: restored["overlay"][k] for k in ("width", "height", "font_size")},
      "| key lives in env var only, len =", len(m.get_env(m.DEFAULT_API_KEY_ENV)))

root.destroy()
print()
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)

"""Live end-to-end test: chat-log line -> parse -> real API call -> overlay row.

Reads ZHIPU_API_KEY (or whatever compat.api_key_env names) straight from the
Windows user environment, so it works even if the current shell predates the
variable being set. Uses a throwaway cache so the real one stays clean.

Run:  python _live_test.py
"""
import importlib.util, os, queue, sys, tempfile, threading, time, winreg
from pathlib import Path

SRC = Path(__file__).resolve().parent / "ArdenTranslatorAI.py"
spec = importlib.util.spec_from_file_location("atai", SRC)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

cfg = m.load_config()
provider, model, key, base_url, extra_headers, extra_body = m.resolve_provider(cfg)
env_name = (cfg.get("compat") or {}).get("api_key_env") or m.DEFAULT_API_KEY_ENV

if not key:
    # 和程序本身一样，读注册表兜底（用户级环境变量对已运行的进程不可见）。
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        key = winreg.QueryValueEx(k, env_name)[0]
        print(f"[i] {env_name} 不在当前进程环境里，已从注册表读取（长度 {len(key)}）")
    except FileNotFoundError:
        print(f"[x] 找不到 {env_name}。请在管理界面里填 Key，或先设置环境变量。")
        raise SystemExit(1)

print(f"provider = {provider}   model = {model}")
print(f"base_url = {base_url}\n")

class FakeOverlay:
    def __init__(self):
        self.rows = []
    def add(self, sender, channel, original, translation, cached=False):
        self.rows.append((sender, channel, translation, cached))
        print(f"    [悬浮窗] [{channel}] {sender}: {translation}"
              + ("  [缓存]" if cached else ""))
    def error(self, msg):
        self.rows.append(("ERROR", "", msg, False))
        print(f"    [悬浮窗][错误] {msg}")

class StubApp:
    def __init__(self):
        self.cfg = cfg
        self.overlay = FakeOverlay()
        cache_path = Path(tempfile.gettempdir()) / "atai_live_cache.sqlite3"
        cache_path.unlink(missing_ok=True)   # always exercise the network
        self.cache = m.TranslationCache(cache_path)
        self.uiq = queue.Queue()
        self.stop = threading.Event()
        self.seen_runtime = set()
        # v1.0.13：dispatch_payload 会把见过的公共频道名记到这里（屏蔽面板用）
        self.seen_channels = set()
        self.sem = threading.Semaphore(max(1, int(cfg["translation"].get("max_workers", 4))))
        # v1.0.8 加了启动追赶、v1.0.9 加了通道 B 之后，解析路径会碰到下面这些状态。
        # 以前这个桩没跟上，handle_log_line 一进来就 AttributeError。宁可在这里
        # 一次性列全，也不要每次报错补一个。
        self.stat = {
            "size": 0, "marker": 0, "accepted": 0, "translated": 0,
            "errors": 0, "muted": 0, "replayed": 0, "grew_at": time.time(),
            "miss_size": 0, "miss_marker": 0, "miss_grew_at": time.time(),
        }
        self.catching_up = False
        self.catchup_cutoff = 0.0
        self.startup_size = 0
        self.miss_path = None
        self.seen_miss = set()
    def pump(self):
        while True:
            try:
                item = self.uiq.get_nowait()
            except queue.Empty:
                return
            if item[0] == "translation":
                _, sender, ch, original, tr, cached = item
                self.overlay.add(sender, ch, original, tr, cached)
            elif item[0] == "error":
                self.overlay.error(item[1])

app = StubApp()
app.translate_one = m.App.translate_one.__get__(app)
app.call_provider = m.App.call_provider.__get__(app)
app.handle_log_line = m.App.handle_log_line.__get__(app)
app.dispatch_payload = m.App.dispatch_payload.__get__(app)
app.handle_miss_line = m.App.handle_miss_line.__get__(app)

# 通道 A：聊天日志格式（@ATAI1@，转义过的 %7C，5 段旧格式）
lines = [
    "10/8 10:40:01.100  [1. Whisper] Me: @ATAI1@|1|CHAT_MSG_GUILD|Thorgrim|LFG BWL need 1 tank 1 heal, pst BiS gear",
    "10/8 10:40:02.200  [1. Whisper] Me: @ATAI1@|2|CHAT_MSG_PARTY|Yuki|すみません、回復役が足りません",
    "10/8 10:40:03.300  [1. Whisper] Me: @ATAI1@|3|CHAT_MSG_GUILD|Ivan|привет всем, кто идёт в рейд?",
    "10/8 10:40:04.400  [1. Whisper] Me: @ATAI1@|4|CHAT_MSG_GUILD|Mei|有人在吗，来一个奶妈",
    "10/8 10:40:05.500  [1. Whisper] Me: ordinary whisper without the marker",
]

print("投喂 5 条日志行（4 条带标记，其中 1 条中文应被跳过；1 条无标记应被忽略）\n")
t0 = time.time()
for ln in lines:
    app.handle_log_line(ln)
time.sleep(10)
app.pump()
dt = time.time() - t0

print(f"\n通道 A 耗时 {dt:.1f}s")
expected = 3   # en + ja + ru; zh skipped, no-marker ignored
got = [r for r in app.overlay.rows if r[0] != "ERROR"]
errs = [r for r in app.overlay.rows if r[0] == "ERROR"]
print(f"收到翻译 {len(got)} 条（期望 {expected}），错误 {len(errs)} 条")
ok_a = len(got) == expected and not errs

# 通道 B：MissingFiles 格式（ATAI2 + 会话 nonce，客户端原样保留 %7C，末尾自带 .blp）
print("\n投喂 3 行 MissingFiles 记录（ATAI2 形态；1 条重复应被去重）\n")
miss_lines = [
    "ATAI2%7C1776000001%7C1%7CCHAT_MSG_SAY%7CBrann%7CTwo more mobs, pull the left pack.blp",
    "ATAI2%7C1776000001%7C2%7CCHAT_MSG_SAY%7CBrann%7CWait for the healer to drink first.tga",
    "ATAI2%7C1776000001%7C1%7CCHAT_MSG_SAY%7CBrann%7CTwo more mobs, pull the left pack.blp",
]
t1 = time.time()
for ln in miss_lines:
    app.handle_miss_line(ln)
time.sleep(10)
app.pump()
dt_b = time.time() - t1

print(f"\n通道 B 耗时 {dt_b:.1f}s")
got_b = [r for r in app.overlay.rows if r[0] != "ERROR"]
errs_b = [r for r in app.overlay.rows if r[0] == "ERROR"]
# 3 行里第 3 行是第 1 行的重复，去重后只应新增 2 条。
print(f"累计翻译 {len(got_b)} 条（期望 {expected + 2}），错误 {len(errs_b)} 条")
ok_b = len(got_b) == expected + 2 and not errs_b

# 通道 C：v1.0.14 的修复项 —— **带点号的频道名**。
# 插件把 "." 转义成 %2E；不转义的话客户端会在最后一个点号处把整条 payload
# 截断（频道名 "2. Newcomers" 变成 "2.blp"，只剩 4 段，被 require_six 丢弃，
# 公共频道一条都翻不出来）。这里喂的就是修好后的真实形态。
print("\n投喂 2 行带点号频道名的记录（v1.0.14 点号转义；正文里也有句号）\n")
chan_lines = [
    "ATAI2%7C1776000002%7C1%7C2%2E Newcomers%7CJacey%7CMight%2E It adds defence stuff.blp",
    "ATAI2%7C1776000002%7C2%7C2%2E Newcomers%7COgilt%7Cwhats the best stats for heirloom cache.tga",
]
t2 = time.time()
for ln in chan_lines:
    app.handle_miss_line(ln)
time.sleep(10)
app.pump()
dt_c = time.time() - t2

all_rows = [r for r in app.overlay.rows if r[0] != "ERROR"]
errs_c = [r for r in app.overlay.rows if r[0] == "ERROR"]
print(f"\n通道 C 耗时 {dt_c:.1f}s")
print(f"累计翻译 {len(all_rows)} 条（期望 {expected + 4}），错误 {len(errs_c)} 条")
print(f"最近两个标签: {[r[1] for r in all_rows[-2:]]}")
print(f"已发现的频道: {sorted(app.seen_channels)}")
ok_c = (len(all_rows) == expected + 4
        and not errs_c
        and all_rows[-1][1] == "2. Newcomers"
        and "2. Newcomers" in app.seen_channels)

ok = ok_a and ok_b and ok_c
print("\n" + ("=== 端到端通过（通道 A + B + C）===" if ok else "=== 端到端失败 ==="))
sys.exit(0 if ok else 1)

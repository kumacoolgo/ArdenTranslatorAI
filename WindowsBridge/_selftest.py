import importlib.util, json, os, queue, shutil as _shutil, sys, threading, time, tempfile
from pathlib import Path

# V11-06：原来这里写死了开发机绝对路径，换台机器跑自检会直接 ModuleNotFound。
# 改成相对本文件的目录，并在导入前把该目录塞进 sys.path —— 这样桥接模块
# (protocol/jobs/cache/dedupe/transport/diagnostics) 才能被 ArdenTranslatorAI.py
# 正常 import。
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

SRC = _here / "ArdenTranslatorAI.py"
spec = importlib.util.spec_from_file_location("atai", SRC)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)   # __name__ != "__main__" so no GUI starts

import dedupe as _dedupe
import jobs as _jobs
import transport as _transport

ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(f"    [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"           got  = {got}")
        print(f"           want = {want}")

print("[1] module imported OK")
cfg = m.load_config()
print("    wow_path      =", repr(cfg.get("wow_path")))
print("    find_chat_log =", m.find_chat_log(cfg.get("wow_path", "")))

print("[2] language detection")
for t, want in [("LFG need healer for Zul'Farrak", "en"), ("ありがとうございます", "ja"),
                ("привет как дела", "ru"), ("有人吗，来一个奶妈", "zh"),
                ("12345 !!!", "other"), ("GG WP", "en"),
                # 中英混排的边界：判定是「汉字够不够多」，不是「有没有汉字」。
                # 拉丁字母占压倒多数时判成 en 并送去翻译 —— 对真实聊天是合理的
                # （"ArdenTranslator 探测" 确实主要是英文）。但这条边界正是
                # v1.0.10 的 /atai test 翻出「假重复」的原因：它拿这条混排文本
                # 当「中文探针」用，结果被判成 en 照翻，和另一条探针撞成重复。
                ("ArdenTranslator 探测 15:19:39", "en"),
                ("这个任务boss打不过 lol", "zh"),
                ("ok 好的", "zh")]:
    check(repr(t), m.detect_language(t), want)

print("[3] protected-term round trip")
prot, mapping = m.protect_terms("ArdenWoW Runemaster need DPS for LFG", cfg["translation"]["protected_terms"])
check("restore", m.restore_terms(prot, mapping), "ArdenWoW Runemaster need DPS for LFG")

print("[4] escaped-pipe message survives the marker split")
line = "10/8 10:04:07.001  [1. Whisper] Weifeng: @ATAI1@|8|CHAT_MSG_PARTY|Ken|hehe %7C nice"
parts = line[line.find(m.MARKER):].split("|", 4)
check("part count", len(parts), 5)
check("unescaped", m.unesc(parts[4]), "hehe | nice")

print("[4b] fully-escaped payload (v1.0.2 addon protocol)")
print("     addon now escapes the separators too, so no bare | ever reaches WoW chat")

def fresh_stat():
    """Mirrors App.__init__'s counter dict; the stubs must stay in sync or
    handle_log_line / tail_loop / status_text blow up on an attribute miss.

    v1.1 增删的键（parse_rejected / dropped / queue 相关）必须补在这里 ——
    少了键，stub 上跑真实方法时会在 _ingest 里 AttributeError，测试会假红。
    """
    return {"size": 0, "marker": 0, "accepted": 0,
            "translated": 0, "errors": 0, "muted": 0, "replayed": 0, "grew_at": 0.0,
            "miss_size": 0, "miss_marker": 0, "miss_grew_at": 0.0,
            # v1.1
            "parse_rejected": 0, "dropped": 0}


def stub_channels():
    """A real ChannelHealth pair so note_line / note_error 不会 AttributeError。

    v1.1 起 handle_log_line / handle_miss_line 会往 self.health[...] 记账；
    stub 若只给个 {} 会在第一次调用就炸，掩盖真正要测的解析逻辑。
    """
    return {"A": _transport.ChannelHealth("A"),
            "B": _transport.ChannelHealth("B")}


class _Cap:
    def __init__(self):
        # v1.0.13：dispatch_payload 会往这里记见过的公共频道名
        self.seen_channels = set()
        self.cfg = cfg
        self.stat = fresh_stat()
        self.health = stub_channels()
        self.catching_up = False
        self.catchup_cutoff = 0.0
        self.startup_size = 0
        self.miss_path = None
        # v1.1：真正的去重交给 Deduper；这里给它一个够用的实例。
        self.deduper = _dedupe.Deduper()
        # v1.1：_ingest 会把 job 放进队列。stub 用一个「只收集不启动 worker」的
        # 假队列，避免测试里真的起线程调 API。
        self.jobs = _FakeJobs()
    # 真实的解析/分发逻辑，绑上来而不是在这里重写一遍
    dispatch_payload = m.App.dispatch_payload
    handle_miss_line = m.App.handle_miss_line
    _ingest = m.App._ingest

    @property
    def received(self):
        """v1.0.x 自检断言用的旧镜像视图；v1.1 起真实产物是 jobs.jobs。"""
        return self.jobs.received

    # 注意：staticmethod 直接赋给类属性会退化成普通函数（self 被当第一个参数），
    # 而 m.App._guess_source 取到的已是裸函数，故再包一层 staticmethod 即可。
    _guess_source = staticmethod(m.App._guess_source)
    _settings_snapshot = m.App._settings_snapshot


class _FakeJobs:
    """只记录被入队的 job，不起 worker、不调 API。

    真实 BoundedJobQueue 会开 2 个线程去调 translate_one；离线自检绝不能联网，
    所以这里把 put() 换成「收集」，同时保留 put() 的签名与返回语义。

    另外维护一份**旧格式的镜像** `received`：[label, sender, text]。
    v1.0.x 的自检断言全是围绕 translate_one(event, sender, msg, lang) 的回调写的；
    v1.1 把末端换成 job 之后，那些断言如果全部重写既啰嗦又容易写错。
    这里让 put() 顺手把 job 投影成旧三元组，老断言几乎不用改就能继续跑 ——
    而且它验证的仍是**真实管道产物**（job 的字段），不是另造一份假数据。
    """
    def __init__(self):
        self.jobs = []
        self.received = []          # 旧断言用的镜像视图
        self.stats = None
    def put(self, job):
        self.jobs.append(job)
        self.received.append([job.channel, job.sender, job.text])
        return True
    def start(self):
        pass
    def stop(self, timeout=1.0):
        pass

_cap = _Cap()
for raw, want_event, want_sender, want_msg in [
    ("10/8 10:41:02.500  发送给Me：@ATAI1@%7C9%7CCHAT_MSG_CHANNEL%7CJohn%7Clfg need tank %7C healer",
     "CHAT_MSG_CHANNEL", "John", "lfg need tank | healer"),
    ("10/8 10:41:03.500  发送给Me：@ATAI1@%7C10%7CCHAT_MSG_GUILD%7CBob%7Csure, 100% ok",
     "CHAT_MSG_GUILD", "Bob", "sure, 100% ok"),
]:
    _cap.jobs.jobs.clear()
    m.App.handle_log_line(_cap, raw)
    # v1.1：管道末端的产物是**入队的 job**，不再是 translate_one() 的即时回调。
    # 断言 job 里的 (频道, 发送者, 译文源文本) —— 这才是真正流过管道的证据。
    if _cap.jobs.jobs:
        j = _cap.jobs.jobs[0]
        check("event ", j.channel, want_event)
        check("sender", j.sender, want_sender)
        check("msg   ", j.text, want_msg)
    else:
        check("parsed", "nothing", want_msg)

print("[4c] WoW link markup stripped before translation")
for raw, want in [
    ("Accepted |cFFFFFF00|Hquest:453:25|h[Finding the Shadowy Figure]|h|r",
     "Accepted [Finding the Shadowy Figure]"),
    ("|cff58c7ffArdenTranslator|r hello there", "ArdenTranslator hello there"),
    ("a || b", "a | b"),
    ("|TInterface\\Icons\\Spell_Nature:16:16|tLFM BWL", "LFM BWL"),
    ("plain text, nothing to do", "plain text, nothing to do"),
]:
    check(repr(raw[:30]), m.strip_wow_markup(raw), want)

# ---------------------------------------------------------------------------
# [5] tail_loop against a real file, written in awkward chunks:
#     - a multi-byte character split across two writes
#     - a line written without its trailing newline, then completed later
# ---------------------------------------------------------------------------
print("[5] tail_loop chunking / buffering")

class Stub:
    def __init__(self, log_path):
        self.log_path = Path(log_path)
        self.cfg = cfg
        self.stop = threading.Event()
        self.uiq = queue.Queue()
        self.seen_channels = set()
        self.stat = fresh_stat()
        self.health = stub_channels()      # v1.1
        self.catching_up = False
        self.catchup_cutoff = 0.0
        self.startup_size = 0
        self.miss_path = None
        self.deduper = _dedupe.Deduper()   # v1.1
        self.jobs = _FakeJobs()            # v1.1
    dispatch_payload = m.App.dispatch_payload
    _ingest = m.App._ingest
    # 注意：staticmethod 直接赋给类属性会退化成普通函数（self 被当第一个参数），
    # 而 m.App._guess_source 取到的已是裸函数，故再包一层 staticmethod 即可。
    _guess_source = staticmethod(m.App._guess_source)
    _settings_snapshot = m.App._settings_snapshot

tmp = Path(tempfile.gettempdir()) / "atai_test_chatlog.txt"
tmp.write_bytes(b"")
stub = Stub(tmp)
# handle_log_line is an App method; bind it to the stub so the real parsing
# path is exercised rather than reimplemented here.
stub.handle_log_line = lambda line: m.App.handle_log_line(stub, line)
threading.Thread(target=m.App.tail_loop, args=(stub,), daemon=True).start()
time.sleep(0.6)   # let first_open latch onto the current size

# 中 = e4 b8 ad, deliberately split between the two writes。
# 注意分隔符必须是**编码形态** %7C：v1.1 起 parse_payload 只在未解码串上找 %7C，
# 裸 | 不再被当作分隔符（那正是 V11-01 要修的 bug）。这里若仍写裸 |，
# 解析会全灭，测出来的就不是「分块读取」而是「格式不对」。
tmp.open("ab").write(b"10/8 10:05:00.001  [1. Whisper] Me: @ATAI1@%7C1776000001%7C1%7CCHAT_MSG_GUILD%7CBob%7Chello \xe4\xb8")
time.sleep(0.4)
tmp.open("ab").write(b"\xad\xe6\x96\x87 world\n")
time.sleep(0.4)

# a line that is still being written must not be dispatched early
tmp.open("ab").write(b"10/8 10:05:01.001  [1. Whisper] Me: @ATAI1@%7C1776000002%7C2%7CCHAT_MSG_PARTY%7CKen%7Cpartial message")
time.sleep(0.5)
mid = len(stub.jobs.jobs)
tmp.open("ab").write(b" done\n")
time.sleep(0.5)

stub.stop.set()
time.sleep(0.3)

_j0 = stub.jobs.jobs[0] if stub.jobs.jobs else None
check("multibyte char split across reads",
      (_j0.sender, _j0.text) if _j0 else None,
      ("Bob", "hello 中文 world"))
check("no premature dispatch of half a line", mid, 1)
_j1 = stub.jobs.jobs[1] if len(stub.jobs.jobs) > 1 else None
check("completed line dispatched", (_j1.sender, _j1.text) if _j1 else None,
      ("Ken", "partial message done"))

tmp.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# [6] compat provider: OpenAI-compatible /chat/completions against a local
#     mock server, so we verify the real request shape and response parsing.
# ---------------------------------------------------------------------------
print("[6] compat provider (OpenAI-compatible endpoint)")

import http.server, json as _json, socketserver, os

captured = []

class MockHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 + explicit Content-Length keeps connections reusable and stops
    # the client seeing spurious "Remote end closed connection" errors.
    protocol_version = "HTTP/1.1"

    def _reply(self, code, obj):
        body = _json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        captured.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": _json.loads(raw.decode("utf-8")),
        })
        if "/fail/" in self.path:
            self._reply(400, {"error": {"message": "bad model"}})
            return
        if "/reasoning/" in self.path:
            # a reasoning model that spent the whole token budget thinking
            self._reply(200, {"choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "",
                            "reasoning_content": "Let me think about this..."},
            }]})
            return
        self._reply(200, {"choices": [{"message": {"role": "assistant",
                                                   "content": "  需要坦克打黑翼之巢  "}}]})

    def log_message(self, *a):
        pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}/v1"

out = m.compat_translate("test-key", "glm-4.7-flash", "LFG need tank for BWL", 5, base)
check("returns and strips content", out, "需要坦克打黑翼之巢")
check("hits /chat/completions", captured[0]["path"], "/v1/chat/completions")
check("sends bearer token", captured[0]["auth"], "Bearer test-key")
check("model forwarded", captured[0]["body"]["model"], "glm-4.7-flash")
check("system prompt first", captured[0]["body"]["messages"][0]["role"], "system")
check("user text second", captured[0]["body"]["messages"][1]["content"], "LFG need tank for BWL")
check("no stray extra_body by default", "thinking" in captured[0]["body"], False)

m.compat_translate("k", "m", "hi", 5, base, extra_body={"thinking": {"type": "disabled"}})
check("extra_body merged into payload",
      captured[1]["body"].get("thinking"), {"type": "disabled"})
check("extra_body does not clobber model", captured[1]["body"]["model"], "m")

m.compat_translate("", "qwen2.5:7b", "hi", 5, f"http://127.0.0.1:{port}/v1")
check("no key -> no auth header", captured[2]["auth"], None)

try:
    m.compat_translate("k", "m", "hi", 5, f"http://127.0.0.1:{port}/v1/fail")
    check("http error raises", "no exception", "RuntimeError")
except RuntimeError as e:
    check("http error surfaces body", "400" in str(e) and "bad model" in str(e), True)

try:
    m.compat_translate("k", "m", "hi", 5, f"http://127.0.0.1:{port}/v1/reasoning")
    check("empty reasoning-only reply raises", "no exception", "RuntimeError")
except RuntimeError as e:
    check("reasoning-only reply explained",
          "思考" in str(e) and "glm-4-flash-250414" in str(e), True)

srv.shutdown()

print("[7] resolve_provider (zhipu only)")
def rp(**extra):
    # 用内置默认值当基底，测试才不受本机 config.json 影响。
    c = json.loads(json.dumps(m.DEFAULT_CONFIG))
    c.update(extra)
    return m.resolve_provider(c)

check("model defaults to glm-4-flash-250414", rp()[1], "glm-4-flash-250414")
check("base_url defaults to zhipu",
      rp()[3], "https://open.bigmodel.cn/api/paas/v4")
check("provider defaults to zhipu", rp()[0], "zhipu")
check("compat.model wins over default",
      rp(compat={"model": "glm-4.5-flash"})[1], "glm-4.5-flash")
check("empty model falls back to default",
      rp(compat={"model": "   "})[1], "glm-4-flash-250414")
check("local endpoint detection", m.is_local_base_url("http://localhost:11434/v1"), True)
check("remote endpoint detection",
      m.is_local_base_url("https://open.bigmodel.cn/api/paas/v4"), False)
check("extra_body resolved",
      rp(compat={"model": "m", "extra_body": {"thinking": {"type": "disabled"}}})[5],
      {"thinking": {"type": "disabled"}})
check("zhipu url assembles correctly",
      "https://open.bigmodel.cn/api/paas/v4".rstrip("/") + "/chat/completions",
      "https://open.bigmodel.cn/api/paas/v4/chat/completions")

print("[7b] API key: environment variable is the ONLY source")
_old_env = os.environ.get("ZHIPU_API_KEY")
try:
    os.environ["ZHIPU_API_KEY"] = "from-env"
    check("reads the env var",
          m.effective_api_key({"compat": {"api_key_env": "ZHIPU_API_KEY"}}),
          "from-env")
    # v1.0.16：配置里的 compat.api_key **不再被理会** —— 明文 Key 不许落盘，
    # 所以就算配置文件里有，也只能靠环境变量。
    check("config api_key ignored (no plaintext on disk)",
          m.effective_api_key({"compat": {"api_key": "ignored-me",
                                          "api_key_env": "ZHIPU_API_KEY"}}),
          "from-env")
    check("api_key_env selects a different variable",
          m.effective_api_key({"compat": {"api_key_env": "MY_CUSTOM_KEY"}})
          == (os.environ.get("MY_CUSTOM_KEY") or m._read_user_env("MY_CUSTOM_KEY")),
          True)
    os.environ.pop("ZHIPU_API_KEY")
    # 注意：不能用 ZHIPU_API_KEY 来测「哪儿都没有 Key」—— 这台机器上它真的
    # 设在用户注册表里（get_env 会读注册表），所以换一个肯定不存在的名字。
    check("no key anywhere -> empty",
          m.effective_api_key({"compat": {"api_key_env": "ATAI_SELFTEST_NO_SUCH_KEY"}}),
          "")
    check("api_key_env_name default", m.api_key_env_name({}), "ZHIPU_API_KEY")
finally:
    if _old_env is None:
        os.environ.pop("ZHIPU_API_KEY", None)
    else:
        os.environ["ZHIPU_API_KEY"] = _old_env

print("[7c] config.json must never contain a plaintext key")
_cfg_plain = json.loads(json.dumps(m.DEFAULT_CONFIG))
_cfg_plain["compat"]["api_key"] = "sk-should-be-stripped"
_saved = m.CONFIG_PATH.read_text(encoding="utf-8") if m.CONFIG_PATH.exists() else None
try:
    _tmp_cfg = tempfile.mkdtemp(prefix="atai_cfg_")
    m.CONFIG_PATH = Path(_tmp_cfg) / "config.json"
    m.save_config(_cfg_plain)
    _on_disk = m.CONFIG_PATH.read_text(encoding="utf-8")
    check("save_config strips compat.api_key", "sk-should-be-stripped" in _on_disk, False)
    check("round-tripped config has no api_key field",
          "api_key" in json.loads(_on_disk)["compat"], False)
    check("no other plaintext leak", _on_disk.find("sk-"), -1)
finally:
    m.CONFIG_PATH = m.DATA_DIR / "config.json"
    _shutil.rmtree(_tmp_cfg, ignore_errors=True)
    del _cfg_plain, _saved

# ---------------------------------------------------------------------------
# [8] retry: free tiers rate-limit bursts, so a 429 must not lose the message
# ---------------------------------------------------------------------------
print("[8] retry on transient errors")

for msg, want in [("HTTP 429: rate limited", True),
                  ("HTTP 500: oops", True),
                  ("HTTP 502: bad gateway", True),
                  ("HTTP 503: busy", True),
                  ("HTTP 504: gateway timeout", True),
                  ("The read operation timed out", True),
                  ("HTTP 400: bad model", False),
                  ("HTTP 401: invalid token", False),
                  ("未设置 ZHIPU_API_KEY", False)]:
    check(f"is_retryable({msg[:22]!r})", m.is_retryable(RuntimeError(msg)), want)

flaky_hits = []

class FlakyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        flaky_hits.append(1)
        n = len(flaky_hits)
        if n < 3:
            body = b'{"error":{"code":"1302","message":"rate limited"}}'
            code = 429
        else:
            body = _json.dumps({"choices": [{"message": {"content": "重试成功"}}]}).encode("utf-8")
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
port2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()

class CfgStub:
    def __init__(self, retry_attempts):
        self.cfg = {
            "translation": {"request_timeout_seconds": 5,
                            "retry_attempts": retry_attempts,
                            "retry_backoff_seconds": 0.05},
            "compat": {"api_key_env": "COMPAT_API_KEY"},
        }

flaky_base = f"http://127.0.0.1:{port2}/v1"
out = m.App.call_provider(CfgStub(3), "compat", "m", "", flaky_base, {}, {}, "hi")
check("retries 429 then succeeds", out, "重试成功")
check("made exactly 3 attempts", len(flaky_hits), 3)

# with retries disabled the very first 429 must surface immediately
flaky_hits.clear()
try:
    m.App.call_provider(CfgStub(1), "compat", "m", "", flaky_base, {}, {}, "hi")
    check("no retry -> raises", "no exception", "RuntimeError")
except RuntimeError as e:
    check("no retry -> raises", "429" in str(e), True)
check("no retry -> single attempt", len(flaky_hits), 1)

srv2.shutdown()

# ---------------------------------------------------------------------------
# [9] live status line + counters.
#     The whole point of this line is to make "nobody is chatting" visually
#     distinguishable from "the in-game half stopped flushing" -- the failure
#     mode that wasted an hour, where the overlay is simply blank either way.
#     v1.1 起这条线改由**通道健康状态机**驱动（不再看文件 idle 秒数），
#     所以断言也跟着改成新语义：能区分「未配置」「有数据」「解析一直失败」。
# ---------------------------------------------------------------------------
print("[9] live status line / counters")

class StatusApp:
    def __init__(self, log_path):
        self.log_path = Path(log_path)
        self.miss_path = None
        # v1.1.1：status_text() 现在会读 cfg["wow_path"] 与 logs_dir_found
        # 来区分「目录没选对」和「游戏还没跑过」两种 NOT_CONFIGURED。
        self.cfg = {"wow_path": str(log_path.parent.parent) if log_path else ""}
        self.logs_dir_found = True
        self.stat = fresh_stat()
        self.stat["size"] = 105288
        self.stat["marker"] = 325
        self.stat["miss_marker"] = 41
        self.stat["translated"] = 12
        self.stat["grew_at"] = time.time()
        # v1.1：status_text() 需要 health / jobs / queue_capacity
        self.health = stub_channels()
        self.jobs = _FakeJobs()
        self.jobs.depth = 0
        self.jobs.stats = _jobs.QueueStats()
        self.queue_capacity = 100
    status_text = m.App.status_text

# [5] removes its own temp log, so make a fresh one for the status checks
status_log = Path(tempfile.gettempdir()) / "atai_status_probe.txt"
status_log.write_bytes(b"probe\n")

sa = StatusApp(status_log)
# 通道都还没配置 -> 明确提示（这是「游戏没开」这一档，和「没人说话」必须分开）
_before = sa.status_text()
# v1.1.1：NOT_CONFIGURED 的提示分三种（没选目录 / 找不到 Logs / 等游戏写日志），
# 不再一律甩「未找到 Logs\」把用户引向重选目录。
check("unconfigured channels explain themselves",
      any(k in _before for k in ("未设置客户端目录", "未找到 Logs", "等待游戏写入")), True)

# 让两条通道都“配置好”并喂进有效行 -> 状态线应给出计数与活跃标记
sa.health["A"].set_configured(True)
sa.health["B"].set_configured(True)
sa.health["A"].note_valid()
sa.health["B"].note_valid()
txt = sa.status_text()
check("shows realtime count", "B:" in txt and "41" in txt, True)
check("shows marker count", "A:" in txt and "325" in txt, True)
check("shows translated count", "已译 12" in txt, True)
check("health label present", "正常" in txt, True)

# 通道 A 从未成功 + 大量无效行 -> 明确报 ERROR（解析坏了 vs 没人说话）
sa2 = StatusApp(status_log)
sa2.health["A"].set_configured(True)
sa2.health["B"].set_configured(True)
for _ in range(25):
    sa2.health["A"].note_invalid("bad payload")
check("persistent parse failures surfaced", "异常" in sa2.status_text(), True)

# 队列深度/丢弃只有非零才出现，避免正常时噪声
sa.stat["grew_at"] = time.time() - 30
check("no queue noise when idle", "队列" in sa.status_text(), False)
sa.jobs.depth = 3
sa.jobs.stats.dropped = 2
check("queue depth shown when non-empty", "队列 3/100" in sa.status_text(), True)
check("dropped count shown", "丢弃 2" in sa.status_text(), True)

# v1.1.1：把两条通道标回「未配置」+ logs_dir_found=False 才算真的找不到目录。
sa.health["A"].set_configured(False)
sa.health["B"].set_configured(False)
sa.logs_dir_found = False
check("reports missing log", "未找到 Logs" in sa.status_text(), True)

# 目录对、只是游戏还没跑 -> 不该说「未找到」
sa.logs_dir_found = True
check("configured-but-silent is not blamed on the path",
      "未找到" not in sa.status_text(), True)
sa.logs_dir_found = False

# the counters must actually move when a real line goes through
_cap.stat = fresh_stat()
_cap.jobs.jobs.clear()
_cap.deduper = _dedupe.Deduper()      # 全新去重器，别让前面的用例把这条当重复
m.App.handle_log_line(_cap, "10/8 10:41:02.500  发送给Me：@ATAI1@%7C9%7CCHAT_MSG_CHANNEL%7CJohn%7Clfg need tank")
check("marker counter moved", _cap.stat["marker"], 1)
check("accepted counter moved", _cap.stat["accepted"], 1)

# and a line without the marker must not move anything
m.App.handle_log_line(_cap, "10/8 10:41:03.000  你获得了396点经验值。")
check("non-marker line ignored", _cap.stat["marker"], 1)

# 坏包只计数、不抛异常（tail 线程绝不能被一条脏行杀掉）
_before_rej = _cap.stat["parse_rejected"]
m.App.dispatch_payload(_cap, "@ATAI1@%7Cnotanumber%7CCHAT_MSG_GUILD%7CBob%7Cx")
check("bad payload counted, not raised", _cap.stat["parse_rejected"] > _before_rej, True)

# ---------------------------------------------------------------------------
# [10] startup catch-up. The bridge gets restarted mid-session constantly;
#      jumping straight to EOF used to drop everything that arrived while it
#      was down, which looks exactly like "the translation stopped working".
# ---------------------------------------------------------------------------
print("[10] startup catch-up (parse_log_time + replay window)")

_now = time.time()
def stamped(offset_seconds, text):
    return time.strftime("%m/%d %H:%M:%S", time.localtime(_now + offset_seconds)) + ".000  " + text

check("parses a current stamp", abs(m.parse_log_time(stamped(0, "x")) - _now) < 2, True)
check("no stamp -> None", m.parse_log_time("no timestamp here"), None)
check("bogus date -> None", m.parse_log_time("13/45 99:99:99.000  x"), None)

cap = _Cap()
cap.catching_up = True
cap.catchup_cutoff = _now - 600
m.App.handle_log_line(cap, stamped(-30, "发送给Me：@ATAI1@%7C1%7CCHAT_MSG_GUILD%7CBob%7Crecent line"))
m.App.handle_log_line(cap, stamped(-3600, "发送给Me：@ATAI1@%7C2%7CCHAT_MSG_GUILD%7CBob%7Cold line"))
got = [j.text for j in cap.jobs.jobs]
check("recent line replayed", "recent line" in got, True)
check("stale line dropped", "old line" in got, False)
check("replay counter", cap.stat["replayed"], 1)

cap2 = _Cap()
cap2.catching_up = False
m.App.handle_log_line(cap2, stamped(-3600, "发送给Me：@ATAI1@%7C3%7CCHAT_MSG_GUILD%7CBob%7Cold but live"))
check("after catch-up age is ignored",
      cap2.jobs.jobs[0].text if cap2.jobs.jobs else None, "old but live")

# and the real tail_loop path, against a file that already has history
replay_log = Path(tempfile.gettempdir()) / "atai_replay_test.txt"
replay_log.write_bytes(
    (stamped(-3600, "发送给Me：@ATAI1@%7C1%7CCHAT_MSG_GUILD%7CBob%7Cold history") + "\n").encode("utf-8")
    + (stamped(-20, "发送给Me：@ATAI1@%7C2%7CCHAT_MSG_GUILD%7CBob%7Cfresh history") + "\n").encode("utf-8")
)
stub2 = Stub(replay_log)
stub2.handle_log_line = lambda line: m.App.handle_log_line(stub2, line)
threading.Thread(target=m.App.tail_loop, args=(stub2,), daemon=True).start()
time.sleep(1.2)
stub2.stop.set()
time.sleep(0.3)
got2 = [j.text for j in stub2.jobs.jobs]
check("catch-up replayed the fresh line", "fresh history" in got2, True)
check("catch-up skipped the stale line", "old history" in got2, False)
check("catch-up flag cleared", stub2.catching_up, False)
check("catch-up reports to the overlay",
      any(i[0] == "info" for i in list(stub2.uiq.queue)), True)
replay_log.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# [11] 通道 B：MissingFiles.txt。插件把 payload 当贴图路径丢给 SetTexture，
#      客户端会把它（连带自动补的 .blp / .tga）实时写进 Logs\MissingFiles.txt。
#      这是目前唯一能把延迟从「几十分钟」压到「秒级」的通道。
# ---------------------------------------------------------------------------
print("[11] MissingFiles channel (handle_miss_line)")

mcap = _Cap()
body = (f"{m.MISS_MARKER}%7C1760000000%7C1%7CCHAT_MSG_CHANNEL%7CJohn"
        f"%7Clfg need tank %7C healer")

mcap.received.clear()
check("blp line accepted", m.App.handle_miss_line(mcap, body + ".blp"), True)
time.sleep(0.15)
check("parsed event", mcap.received[0][0] if mcap.received else None, "CHAT_MSG_CHANNEL")
check("parsed sender", mcap.received[0][1] if mcap.received else None, "John")
check("parsed msg (escaped pipe restored)",
      mcap.received[0][2] if mcap.received else None, "lfg need tank | healer")

# 客户端会给同一个 payload 再记一条 .tga，去掉后缀去重后必须被丢掉
check("tga duplicate dropped", m.App.handle_miss_line(mcap, body + ".tga"), False)
check("exact duplicate dropped", m.App.handle_miss_line(mcap, body + ".blp"), False)
# v1.1：miss_marker 统计的是「带 ATAI2 的行数」，重复行也计入（它们确实到达了
# 通道），真正去重在 deduper。所以这里断言 >=1，并单列断言「只入队 1 条」。
check("miss marker counter counted arrivals", mcap.stat["miss_marker"] >= 1, True)

# 非本插件的行必须无视 —— 这个文件里绝大多数是客户端自己的缺失资源
check("foreign line ignored",
      m.App.handle_miss_line(mcap, "Interface\\ATAI\\P134401.blp"), False)
check("blank line ignored", m.App.handle_miss_line(mcap, "   "), False)
check("GameFont line ignored", m.App.handle_miss_line(mcap, "GameFontHighlightSmall"), False)
check("foreign/blank lines add no marker", mcap.stat["miss_marker"] >= 1, True)

# 新会话 = 新 nonce，同样的文本必须能再次通过。
# MissingFiles 是「集合」语义，没有 nonce 的话这条会被客户端直接不记。
body2 = (f"{m.MISS_MARKER}%7C1760009999%7C1%7CCHAT_MSG_CHANNEL%7CJohn"
         f"%7Clfg need tank %7C healer")
mcap.received.clear()
check("new session nonce accepted", m.App.handle_miss_line(mcap, body2 + ".blp"), True)
time.sleep(0.15)
check("new session translated", len(mcap.received), 1)

# 中文消息按 skip_chinese 丢掉，别浪费一次 API 调用
mcap.received.clear()
zh = f"{m.MISS_MARKER}%7C1760000001%7C2%7CCHAT_MSG_SAY%7C玩家甲%7C这个Boss门是不是出bug了"
# v1.1：中文按 skip_chinese 跳过 -> 未入队，返回 False（但绝不进 parse_rejected）。
_rej_before = mcap.stat["parse_rejected"]
check("chinese line is well-formed (not a parse reject)",
      m.App.handle_miss_line(mcap, zh + ".blp"), False)
check("chinese did not count as bad packet", mcap.stat["parse_rejected"], _rej_before)
time.sleep(0.15)
check("chinese not translated", len(mcap.received), 0)

# 旧格式（5 段、没有 nonce）仍要能解析 —— 历史日志还要能读
mcap.received.clear()
m.App.handle_log_line(
    mcap, "10/8 12:00:00.000  发送给Me：@ATAI1@%7C7%7CCHAT_MSG_GUILD%7CBob%7Clegacy payload still works")
time.sleep(0.15)
check("legacy 5-field payload parsed", len(mcap.received), 1)
check("legacy msg", mcap.received[0][2] if mcap.received else None, "legacy payload still works")

# ---------------------------------------------------------------------------
# [12] 通道 B 的格式门禁。通道 B 只会发 6 段格式，所以那边 require_six=True：
#      格式对不上宁可丢掉，也不能按 5 段错位解析 —— 错位不报错，但会把 seq
#      当事件名、事件名当发送者，整条消息全错，比丢掉难查得多。
# ---------------------------------------------------------------------------
print()
print("[12] channel B strict 6-field gate")

# 6 段但 nonce/seq 不是数字 —— 坏包，通道 B 必须丢掉而不是错位解析
bad = f"{m.MISS_MARKER}%7Cnotanumber%7C1%7CCHAT_MSG_SAY%7CBob%7Chi there"
mcap.received.clear()
# v1.1：坏包返回 False（未成功入队）。旧断言写 True 是 v1.0 的语义 ——
# 那时「到达」和「被接受」混为一谈，现在明确区分。
check("channel B rejects non-numeric nonce", m.App.handle_miss_line(mcap, bad + ".blp"), False)
time.sleep(0.15)
check("channel B bad packet not translated", len(mcap.received), 0)

# 只有 5 段 —— 通道 B 不接受旧格式
short = f"{m.MISS_MARKER}%7C5%7CCHAT_MSG_SAY%7CBob%7Chi there"
mcap.received.clear()
m.App.handle_miss_line(mcap, short + ".blp")
time.sleep(0.15)
check("channel B rejects 5-field legacy shape", len(mcap.received), 0)

# 同一段内容走通道 A（聊天日志）时，5 段旧格式仍然要能读
mcap.received.clear()
m.App.handle_log_line(
    mcap, "10/8 12:05:00.000  发送给Me：@ATAI1@%7C5%7CCHAT_MSG_SAY%7CBob%7Chi there")
time.sleep(0.15)
check("channel A still reads legacy 5-field", len(mcap.received), 1)
check("channel A legacy sender", mcap.received[0][1] if mcap.received else None, "Bob")

# ---------------------------------------------------------------------------
# [13] 频道名 + 屏蔽（v1.0.13）
#   插件把 CHAT_MSG_CHANNEL 的 arg4（如 "2. Newcomers"）直接填进 payload 的
#   第 4 段（标签段）。**段数不变**，所以旧版 EXE 靠 EVENT_LABELS 的兜底也能
#   正确显示 —— 这里把新旧两种写法都验证一遍。
# ---------------------------------------------------------------------------
print()
print("[13] channel name in the label field + mute")

check("channel name is not an event", m.is_channel_event("2. Newcomers"), True)
check("CHAT_MSG_ prefix is an event", m.is_channel_event("CHAT_MSG_GUILD"), False)
check("empty is not a channel", m.is_channel_event(""), False)
check("label: channel name shown as-is", m.event_label("2. Newcomers"), "2. Newcomers")
check("label: guild -> chinese", m.event_label("CHAT_MSG_GUILD"), "公会")
check("label: unknown event falls back", m.event_label("CHAT_MSG_WEIRD"), "CHAT_MSG_WEIRD")
# 旧插件（<=1.0.12）发的仍是 CHAT_MSG_CHANNEL —— 必须还能显示「频道」
check("label: legacy CHAT_MSG_CHANNEL", m.event_label("CHAT_MSG_CHANNEL"), "频道")

# 频道名要能从通道 B 的 payload 里解析出来，并记进 seen_channels
mcap.seen_channels.clear()
mcap.received.clear()
chan_payload = (f"{m.MISS_MARKER}%7C1760007777%7C11%7C2. Newcomers%7CKen"
                f"%7Clfg need a tank for Zul'Farrak")
check("channel payload accepted", m.App.handle_miss_line(mcap, chan_payload + ".blp"), True)
time.sleep(0.15)
check("channel payload translated", len(mcap.received), 1)
check("label field is the channel name",
      mcap.received[0][0] if mcap.received else None, "2. Newcomers")
check("channel recorded for the settings panel", mcap.seen_channels, {"2. Newcomers"})

saved_mute = cfg.get("mute")

# 屏蔽：按频道名
cfg["mute"] = {"labels": [], "channels": ["2. Newcomers"]}
mcap.received.clear()
muted_payload = (f"{m.MISS_MARKER}%7C1760007778%7C12%7C2. Newcomers%7CKen"
                 f"%7Canother lfg message here")
before_muted = mcap.stat["muted"]
m.App.handle_miss_line(mcap, muted_payload + ".blp")
time.sleep(0.15)
check("muted channel not translated", len(mcap.received), 0)
check("muted counter bumped", mcap.stat["muted"], before_muted + 1)

# 屏蔽：按分类标签
cfg["mute"] = {"labels": ["公会"], "channels": []}
mcap.received.clear()
guild_payload = (f"{m.MISS_MARKER}%7C1760007779%7C13%7CCHAT_MSG_GUILD%7CBob"
                 f"%7Cneed dps for raid tonight")
m.App.handle_miss_line(mcap, guild_payload + ".blp")
time.sleep(0.15)
check("muted label not translated", len(mcap.received), 0)

# 同样内容换到没被屏蔽的分类，必须照常翻译（别把屏蔽做成全局静音）
mcap.received.clear()
party_payload = (f"{m.MISS_MARKER}%7C1760007780%7C14%7CCHAT_MSG_PARTY%7CBob"
                 f"%7Cneed dps for raid tonight")
m.App.handle_miss_line(mcap, party_payload + ".blp")
time.sleep(0.15)
check("unmuted label still translated", len(mcap.received), 1)

# 通配 "*" = 屏蔽所有公共频道，但不影响公会/小队等分类
cfg["mute"] = {"labels": [], "channels": ["*"]}
mcap.received.clear()
any_chan = (f"{m.MISS_MARKER}%7C1760007781%7C15%7C5. World%7CAnn"
            f"%7Canyone selling bags cheap")
m.App.handle_miss_line(mcap, any_chan + ".blp")
time.sleep(0.15)
check("wildcard mutes every channel", len(mcap.received), 0)
check("wildcard leaves guild alone", m.is_muted("CHAT_MSG_GUILD", cfg["mute"]), False)

# 还原，别污染后续（也保证脚本可重复运行）
if saved_mute is None:
    cfg.pop("mute", None)
else:
    cfg["mute"] = saved_mute

# ---------------------------------------------------------------------------
# [14] 点号转义（v1.0.14）—— 修「公共频道完全翻译不了」
#
#   客户端把 SetTexture 的路径当文件名：在**最后一个点号**处切分，把后半段当
#   扩展名，替换成 .blp / .tga 各记一行。所以 payload 里任何点号后面的内容都会
#   被吃掉。v1.0.13 把频道名 "2. Newcomers" 放进标签段，于是整条被截成 "2.blp"
#   （只剩 4 段），被 require_six 静默丢弃 —— 公共频道一条都翻不出来。
#
#   修法：插件 esc() 把 "." 转成 "%2E"，这里验证 unesc() 能精确还原，
#   并且转义顺序不会互相破坏。
# ---------------------------------------------------------------------------
print()
print("[14] dot escaping round trip (%2E)")

check("unesc %2E", m.unesc("2%2E Newcomers"), "2. Newcomers")
check("unesc %2e lowercase", m.unesc("2%2e Newcomers"), "2. Newcomers")
# 顺序保证：用户原文里的字面 "%2E" 被插件转义成 "%252E"。必须先还原 %2E、
# 最后才还原 %25，否则 "%252E" 会被错解成 "."（把玩家的原文改掉）。
check("literal %2E survives", m.unesc("%252E"), "%2E")
check("literal % survives", m.unesc("100%25 off"), "100% off")
check("mixed %2E + %7C + %25", m.unesc("100%25 off %7C nice%2E ok"), "100% off | nice. ok")

# 端到端：完全按修好后插件实际发出的形态（标签段 = 2%2E Newcomers）
mcap.seen_channels.clear()
mcap.received.clear()
fixed_payload = (f"{m.MISS_MARKER}%7C1760008888%7C21%7C2%2E Newcomers%7CJacey"
                 f"%7CMight%2E It adds defence stuff")
check("dotted channel payload accepted",
      m.App.handle_miss_line(mcap, fixed_payload + ".blp"), True)
time.sleep(0.15)
check("dotted channel payload translated", len(mcap.received), 1)
check("label decoded back to '2. Newcomers'",
      mcap.received[0][0] if mcap.received else None, "2. Newcomers")
check("message keeps its full stop",
      mcap.received[0][2] if mcap.received else None, "Might. It adds defence stuff")
check("dotted channel recorded for the panel", mcap.seen_channels, {"2. Newcomers"})

# 反面用例：被客户端截断的残片（旧插件发的 / 未转义的点号）必须被丢弃，
# 绝不能错位解析成一条假消息。
mcap.received.clear()
truncated = f"{m.MISS_MARKER}%7C1760008889%7C22%7C2"
m.App.handle_miss_line(mcap, truncated + ".blp")
time.sleep(0.15)
check("truncated 4-field payload not translated", len(mcap.received), 0)
check("truncated payload adds no channel", mcap.seen_channels, {"2. Newcomers"})

# ---------------------------------------------------------------------------
# [15] 插件一键部署（v1.0.15）
#
#     「点开始 -> 删掉旧的 AddOns\ArdenTranslatorAI -> 重建 -> 写入内置的
#     .lua/.toc」是这一版最关键的新动作，而且它会**删目录**。所以既要证明
#     正常路径能装对，也要证明指错目录时它什么都不碰。
# ---------------------------------------------------------------------------
print()
print("[15] addon deployment")

src = m.plugin_source_dir()
check("bundled/dev plugin source found", src is not None and src.is_dir(), True)
check("source has the .lua", (src / "ArdenTranslatorAI.lua").exists(), True)
check("source has the .toc", (src / "ArdenTranslatorAI.toc").exists(), True)

check("empty path rejected", m.check_wow_path("")[0], False)
check("nonexistent dir rejected", m.check_wow_path(str(Path(tempfile.gettempdir()) / "no-such-xyz"))[0], False)

_notwow = Path(tempfile.mkdtemp(prefix="atai_notwow_"))
check("dir without Interface rejected", m.check_wow_path(str(_notwow))[0], False)
check("rejection message mentions Interface",
      "Interface" in m.check_wow_path(str(_notwow))[1], True)

_fake = Path(tempfile.mkdtemp(prefix="atai_wow_"))
(_fake / "Interface").mkdir()
check("real wow root accepted", m.check_wow_path(str(_fake))[0], True)
check("addons_dir shape", m.addons_dir(_fake),
      _fake / "Interface" / "AddOns")

# 先造一个「旧版本」现场：多一个陈旧文件，且 .lua 内容是错的。
dest = _fake / "Interface" / "AddOns" / "ArdenTranslatorAI"
dest.mkdir(parents=True)
(dest / "ArdenTranslatorAI.lua").write_text("-- STALE OLD ADDON", encoding="utf-8")
(dest / "stale_leftover.txt").write_text("junk", encoding="utf-8")

ok_deploy, msg_deploy, dest_deploy = m.deploy_addon(str(_fake))
check("deploy succeeds", ok_deploy, True)
check("deploy returns dest", dest_deploy, dest)
check("lua replaced with the bundled one",
      (dest / "ArdenTranslatorAI.lua").read_bytes(),
      (src / "ArdenTranslatorAI.lua").read_bytes())
check("toc written", (dest / "ArdenTranslatorAI.toc").exists(), True)
check("stale leftover removed", (dest / "stale_leftover.txt").exists(), False)
check("no .blp junk created", sorted(f.name for f in dest.iterdir() if f.is_file()),
      sorted(f.name for f in src.iterdir() if f.is_file()))

# 再装一次必须幂等（用户每次点开始都会走这条路）。
ok2, msg2, _ = m.deploy_addon(str(_fake))
check("second deploy also succeeds", ok2, True)
check("still exactly the bundled files",
      (dest / "ArdenTranslatorAI.lua").read_bytes(),
      (src / "ArdenTranslatorAI.lua").read_bytes())

# 安全闸：wow_path 指错目录时绝不能删任何东西。
_bad = Path(tempfile.mkdtemp(prefix="atai_bad_"))
(_bad / "Interface" / "AddOns" / "PreciousData").mkdir(parents=True)
(_bad / "Interface" / "AddOns" / "PreciousData" / "keep.txt").write_text("keep", encoding="utf-8")
check("bogus path refused", m.deploy_addon(str(_bad / "nope"))[0], False)
check("unrelated folder untouched",
      (_bad / "Interface" / "AddOns" / "PreciousData" / "keep.txt").exists(), True)

_shutil.rmtree(_notwow, ignore_errors=True)
_shutil.rmtree(_fake, ignore_errors=True)
_shutil.rmtree(_bad, ignore_errors=True)

# 配置层：默认值就是用户要的那一组
check("overlay defaults match the spec",
      m.DEFAULT_CONFIG["overlay"],
      {"x": 40, "y": 520, "width": 500, "height": 200,
       "alpha": 0.88, "font_size": 9, "max_lines": 7})
check("default model", m.DEFAULT_CONFIG["compat"]["model"], "glm-4-flash-250414")
check("default base_url", m.DEFAULT_CONFIG["compat"]["base_url"],
      "https://open.bigmodel.cn/api/paas/v4")
check("no openai/gemini keys left in defaults",
      [k for k in m.DEFAULT_CONFIG if "openai" in k or "gemini" in k], [])
check("version bumped", m.VERSION, "1.1.1")
check("defaults carry no api_key field", "api_key" in m.DEFAULT_CONFIG["compat"], False)

print()
print("ALL PASS" if ok else "SOME CHECKS FAILED")

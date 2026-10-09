# ArdenTranslator AI Windows Bridge -- translation cache (v1.1)
#
# V11-03：缓存 key 版本化，并且**不破坏老用户的库**。
#
# 旧 key（v1.0.17）：sha256(provider \0 model \0 text.lower())
#   * 少了 target 语言 —— 用户把目标语言从简体中文改成英文（将来支持时），
#     会命中旧译文，显示错误语言的「缓存结果」。
#   * 少了 prompt_version —— 改了 SYSTEM 提示词后，旧译文继续被复用，
#     新提示词等于没生效，而且完全没有迹象。
#   * 少了术语表版本 —— 改了 protected_terms 后，旧译文里术语是旧的处理方式。
#   * 少了 base_url —— 换端点（同模型名不同服务商）会错误命中。
#   * 强制 .lower() —— 大小写折叠会把 "LFG" 和 "lfg" 当同一句，而它们
#     （对术语保护和模型输出而言）并不总是等价的。
#
# 新 key 在**同一张表**里直接写入，只是 k 的算法变了。旧行自然不再被命中，
# 但它们还在库里、不占内存、也不影响正确性。规范明确要求「不要破坏老用户
# 数据库」，所以这里不建新表、不迁移、不删除。

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from typing import Optional

# 提示词版本。**改动 TRANSLATOR_INSTRUCTIONS 时必须 +1**，否则旧译文会被
# 错误复用。桥接启动时会把这个值和缓存 key 一起算进去。
PROMPT_VERSION = 2

# 缓存 key 的算法版本。将来若改变 key 的构成方式，也 +1。
CACHE_KEY_VERSION = 2

# 缓存条数上限（保守清理）。0 = 不限制。
DEFAULT_MAX_ROWS = 200_000


def normalize_text(text: str) -> str:
    """折叠空白，但**不做**大小写折叠。

    V11-03 第 4 条：不能用 .lower()，因为 "ArdenWoW" 和 "ardenwow" 在
    术语保护和模型输出上不保证等价（专有名词首字母大小写是有意义的）。
    这里只压缩连续空白 + 去首尾。
    """
    return re.sub(r"\s+", " ", (text or "").strip())


def terms_fingerprint(terms) -> str:
    """术语表的短哈希。顺序无关（排序后再哈希），内容变一点就变。"""
    if not terms:
        return "-"
    norm = sorted({str(t) for t in terms if str(t).strip()})
    raw = "\x1f".join(norm).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def normalize_base_url(url: str) -> str:
    """规范化 base_url，避免 "https://x/v4/" 和 "https://x/v4" 生成两个 key。"""
    u = (url or "").strip()
    if not u:
        return "-"
    u = u.rstrip("/")
    return u.lower()


def cache_key(*, provider, base_url, model, target, text, terms=None,
              prompt_version=PROMPT_VERSION, extra_version=""):
    """新 key：把所有**会影响译文**的因素都纳进来。

    `text` 必须已经过 normalize_text()。这里不再做任何折叠。
    """
    parts = [
        "v%d" % CACHE_KEY_VERSION,
        str(provider or "-"),
        normalize_base_url(base_url),
        str(model or "-"),
        str(target or "-"),
        "p%d" % int(prompt_version),
        "t" + terms_fingerprint(terms),
        str(extra_version or "-"),
        normalize_text(text),
    ]
    raw = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class TranslationCache:
    """SQLite 缓存 + 内存 inflight 合并（singleflight）。

    线程安全：所有 DB 访问走 self.lock；inflight 表走 self._flight_lock。
    """

    def __init__(self, path, *, max_rows: int = DEFAULT_MAX_ROWS):
        self.path = path
        self.max_rows = max_rows
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS cache(
                k TEXT PRIMARY KEY,
                translation TEXT NOT NULL,
                created INTEGER NOT NULL
            )""")
            # 元数据表：记录 key 算法版本，便于将来迁移时给出提示。
            self.db.execute("""CREATE TABLE IF NOT EXISTS meta(
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )""")
            self.db.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES('cache_key_version',?)",
                (str(CACHE_KEY_VERSION),))
        # 计数器（供诊断导出）。
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.errors = 0
        self.evictions = 0

    # ---- 基础读写 --------------------------------------------------------
    def get(self, key: str) -> Optional[str]:
        try:
            with self.lock:
                row = self.db.execute(
                    "SELECT translation FROM cache WHERE k=?", (key,)).fetchone()
        except Exception:
            self.errors += 1
            return None
        if row and row[0]:
            self.hits += 1
            return row[0]
        self.misses += 1
        return None

    def put(self, key: str, translation: str):
        if not translation:
            return
        try:
            with self.lock, self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO cache(k,translation,created) "
                    "VALUES(?,?,?)", (key, translation, int(time.time())))
            self.writes += 1
        except Exception:
            # sqlite3 异常只计数，绝不抛给调用方 —— 缓存写不进去不该让翻译失败。
            self.errors += 1

    def prune(self):
        """保守清理：超过上限时删掉最旧的一批（保留最近的 90%）。"""
        if not self.max_rows or self.max_rows <= 0:
            return 0
        try:
            with self.lock:
                n = self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                if n <= self.max_rows:
                    return 0
                cutoff = int(n * 0.10)
                with self.db:
                    self.db.execute(
                        "DELETE FROM cache WHERE k IN ("
                        "  SELECT k FROM cache ORDER BY created ASC LIMIT ?)",
                        (cutoff,))
            self.evictions += cutoff
            return cutoff
        except Exception:
            self.errors += 1
            return 0

    def close(self):
        try:
            with self.lock:
                self.db.close()
        except Exception:
            pass


class SingleFlight:
    """把「同一内容同时在途的多个请求」合并成一次。

    V11-03 第 1 条：第一条成为 owner 去真正请求，其余等待者共享它的结果或异常。
    后续消息**都要各自显示**（那是上层的事），但**只发一次 HTTP**。

    异常时必须释放 inflight，否则后续同内容的消息会永远等在一个已经死掉的
    future 上 —— 那是「永久挂起」，比失败更糟。
    """

    class _Flight:
        __slots__ = ("event", "value", "error", "waiters")

        def __init__(self):
            self.event = threading.Event()
            self.value = None
            self.error = None
            self.waiters = 0

    def __init__(self):
        self._lock = threading.Lock()
        self._flights = {}
        # 计数：供诊断显示「待合并请求数」。
        self.merged = 0        # 因为有同内容在途而被合并的次数
        self.started = 0       # 真正发起的请求数

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._flights)

    def run(self, key: str, fn):
        """返回 (value, is_owner)。

        owner 负责真正执行 fn；其它调用等待同一个结果。异常会原样抛给**所有**
        等待者（每条消息各自走自己的错误路径），但 fn 只被调用一次。
        """
        with self._lock:
            existing = self._flights.get(key)
            if existing is not None:
                existing.waiters += 1
                self.merged += 1
                flight = existing
                owner = False
            else:
                flight = SingleFlight._Flight()
                self._flights[key] = flight
                self.started += 1
                owner = True

        if not owner:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value, False

        # ---- owner：真正执行，然后把结果广播出去 ----
        try:
            value = fn()
            flight.value = value
            return value, True
        except BaseException as e:            # noqa: BLE001 - 原样转发给等待者
            flight.error = e
            raise
        finally:
            # 无论成功、失败还是被取消，都必须摘掉并叫醒等待者。
            with self._lock:
                self._flights.pop(key, None)
            flight.event.set()

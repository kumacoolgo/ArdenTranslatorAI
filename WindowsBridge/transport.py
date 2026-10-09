# ArdenTranslator AI Windows Bridge -- channel health (v1.1)
#
# V12-03 要求给每条通道一个真实状态机。v1.1 先落地状态机本身并接进状态行，
# 完整的自动降级/退避在 v1.2 细化。
#
# 为什么要状态机而不是「文件在不在」
# ---------------------------------------------------------------------------
# 旧实现的状态行只有「idle 秒数」，无法区分这些**完全不同的**故障：
#   * 插件根本没装           -> 永远不会有数据，等多久都没用
#   * 装了但游戏没开         -> 文件不存在
#   * 游戏开着但没人说话     -> 文件存在、只是没新行（这是正常！）
#   * 通道真的断了           -> 以前有数据，现在长时间没有
#   * 解析一直失败           -> 有行但全是垃圾（协议不匹配、编码坏了）
# 用户看到的都是「无新行」，没法行动。状态机把这些分开。

from __future__ import annotations

import threading
import time

# 状态取值（V12-03 规定的五个）
NOT_CONFIGURED = "NOT_CONFIGURED"   # 路径都没配好
WAITING = "WAITING"                 # 配好了但还没见过有效数据
HEALTHY = "HEALTHY"                 # 最近收到过**有效**消息
STALE = "STALE"                     # 曾经健康，但已超过阈值没动静
ERROR = "ERROR"                     # 持续读/解析失败

# 秒。超过这个时间没有有效消息就从 HEALTHY 掉到 STALE。
STALE_AFTER = 90.0


class ChannelHealth:
    """一条通道的健康状态。

    关键设计（V12-03 明确要求）：
      * **不能**仅凭文件 mtime 更新就断言「端到端实时」—— 文件被重写
        （MissingFiles 是集合语义，大小会涨也会跌）也会刷新 mtime，但那不代表
        有我们的消息。所以健康判据是**实际解析出一条属于当前会话的有效消息**。
      * 解析失败要聚合计数，不能让每 0.4 秒刷一次同样的错误。
    """

    def __init__(self, name: str, *, stale_after: float = STALE_AFTER):
        self.name = name
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self.state = NOT_CONFIGURED
        self.last_change_at = 0.0        # 文件最后一次变动
        self.last_valid_at = 0.0         # 最后一条**有效**消息
        self.last_parse_error = ""
        self.last_error_at = 0.0
        self.counters = {
            "lines": 0,          # 扫过的行数
            "markers": 0,        # 带我们 marker 的行
            "valid": 0,          # 解析成功且属于当前会话
            "invalid": 0,        # 解析失败
            "rejected": 0,       # 解析成功但被去重/水位拒收
            "errors": 0,         # I/O 或其它异常次数
        }

    def set_configured(self, ok: bool):
        with self._lock:
            self.state = WAITING if ok else NOT_CONFIGURED

    def note_file_change(self):
        with self._lock:
            self.last_change_at = time.time()

    def note_line(self, has_marker: bool):
        with self._lock:
            self.counters["lines"] += 1
            if has_marker:
                self.counters["markers"] += 1

    def note_valid(self):
        with self._lock:
            self.counters["valid"] += 1
            self.last_valid_at = time.time()
            self.state = HEALTHY

    def note_rejected(self):
        with self._lock:
            self.counters["rejected"] += 1

    def note_invalid(self, reason: str = ""):
        """解析失败。原因只在**变化时**记录，避免每轮刷同样的错误。"""
        now = time.time()
        with self._lock:
            self.counters["invalid"] += 1
            if reason and reason != self.last_parse_error:
                self.last_parse_error = reason
                self.last_error_at = now
            # 有大量无效行又从未成功过 -> 明显是 ERROR（协议不匹配之类）
            if self.counters["valid"] == 0 and self.counters["invalid"] >= 20:
                self.state = ERROR

    def note_error(self, reason: str = ""):
        now = time.time()
        with self._lock:
            self.counters["errors"] += 1
            if reason and reason != self.last_parse_error:
                self.last_parse_error = reason
                self.last_error_at = now
            self.state = ERROR

    def tick(self):
        """周期性重算状态（由状态刷新调用）。"""
        now = time.time()
        with self._lock:
            if self.state in (NOT_CONFIGURED, ERROR):
                return self.state
            if self.last_valid_at == 0.0:
                self.state = WAITING
            elif now - self.last_valid_at > self.stale_after:
                self.state = STALE
            else:
                self.state = HEALTHY
            return self.state

    def snapshot(self):
        now = time.time()
        with self._lock:
            return {
                "name": self.name,
                "state": self.state,
                "since_valid": (round(now - self.last_valid_at, 1)
                                if self.last_valid_at else None),
                "counters": dict(self.counters),
                "last_error": self.last_parse_error[-120:],
            }

    def label(self) -> str:
        """给状态行用的短标签（中文，一眼能懂）。"""
        st = self.tick()
        if st == NOT_CONFIGURED:
            return "未配置"
        if st == WAITING:
            return "等待数据"
        if st == HEALTHY:
            return "正常"
        if st == STALE:
            with self._lock:
                idle = time.time() - self.last_valid_at
            return ("%.0fs 无消息" % idle) if idle < 120 else ("%.0f 分钟无消息" % (idle / 60))
        if st == ERROR:
            return "读取/解析异常"
        return st

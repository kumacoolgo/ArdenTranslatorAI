# ArdenTranslator AI Windows Bridge -- diagnostics & counters (v1.1)
#
# V11-06 要求「支持安装包显示各自版本和协议兼容范围」；V12-06 要求脱敏诊断导出。
# 两者共用这里的计数与版本信息。v1.1 先把骨架建好并接进状态行，
# 完整的仪表盘（p50/p95 延迟等）留给 v1.2。
#
# 脱敏原则（规范硬要求，也是 V11-06 第 3 条「不得宣传为加密存储」的配套）：
#   * 绝不包含 API Key / Authorization / 任何环境变量的值；
#   * 默认不包含聊天原文、玩家名；
#   * 路径只保留**最后一级目录名**，不暴露用户的完整目录结构。

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
from collections import deque

# 桥接端版本。v1.1 起按规范升到 1.1.x。
BRIDGE_VERSION = "1.1.1"
# 插件端版本（内置那份；运行时从 .toc 读到的实际值另算）。
ADDON_VERSION = "1.1.0"
# 支持的 wire protocol。范围写清楚，方便用户判断「我的插件会不会太旧」。
PROTOCOL_SUPPORTED = (1, 2)      # 1 = @ATAI1@ 5 段；2 = @ATAI1@/ATAI2 6 段


class LatencyTracker:
    """滚动窗口的耗时统计（给 V12-06 的 p50/p95 用）。

    v1.1 先接上「桥接侧耗时」，但**不**声称是端到端延迟 —— 插件时间戳不可得。
    """

    def __init__(self, window: int = 512):
        self._d = deque(maxlen=window)
        self._lock = None
        try:
            import threading
            self._lock = threading.Lock()
        except Exception:
            pass

    def add(self, seconds: float):
        if seconds is None or seconds < 0:
            return
        if self._lock:
            with self._lock:
                self._d.append(float(seconds))
        else:
            self._d.append(float(seconds))

    def _snapshot(self):
        if self._lock:
            with self._lock:
                return sorted(self._d)
        return sorted(self._d)

    def percentile(self, p: float):
        vals = self._snapshot()
        if not vals:
            return None
        k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
        return vals[k]

    def stats(self):
        vals = self._snapshot()
        if not vals:
            return {"n": 0, "p50": None, "p95": None, "avg": None}
        return {
            "n": len(vals),
            "p50": round(self.percentile(50), 3),
            "p95": round(self.percentile(95), 3),
            "avg": round(sum(vals) / len(vals), 3),
        }


def _basename_safe(path) -> str:
    """只保留路径的最后一级（脱敏）。"""
    if not path:
        return ""
    try:
        return os.path.basename(str(path).rstrip("\\/")) or ""
    except Exception:
        return ""


def build_report(*, bridge_version=BRIDGE_VERSION, addon_version=ADDON_VERSION,
                 protocol_supported=PROTOCOL_SUPPORTED,
                 counters=None, channel_health=None, latencies=None,
                 extra=None) -> dict:
    """生成脱敏诊断报告（纯 dict，方便 JSON 化）。

    调用方负责保证 counters / channel_health 里不含敏感内容 —— 这里的约定是
    「只放数字和枚举，不放文本」。
    """
    rep = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bridge_version": bridge_version,
        "addon_version": addon_version,
        "protocol_supported": list(protocol_supported),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "host": socket.gethostname(),        # 主机名不算敏感，便于区分机器
        "counters": dict(counters or {}),
        "channel_health": dict(channel_health or {}),
        "latency_seconds": dict(latencies or {}),
        "notes": [
            "已脱敏：不含 API Key、不含聊天原文、不含玩家名。",
            "路径仅保留最后一级目录名。",
            "latency_seconds 是**桥接侧**耗时（收到 → 展示），"
            "不含 Lua 到日志的传输时间，所以不等于端到端延迟。",
        ],
    }
    if extra:
        rep["extra"] = extra
    return rep


def dumps_report(rep: dict) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=2)

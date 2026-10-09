# ArdenTranslator AI Windows Bridge -- de-duplication state (v1.1)
#
# V11-04：把「超阈值就 set.clear()」换成有界且**联动扫描策略**的去重。
#
# 旧实现的问题
# ---------------------------------------------------------------------------
#   self.seen_miss.add(line)
#   if len(self.seen_miss) > 50000:
#       self.seen_miss.clear()          # <-- 定时炸弹
#
# MissingFiles.txt 是**集合语义**：客户端每次都整体重写，文件里长期保留着
# 一大批历史 payload。一旦 seen_miss 被清空，下一轮整文件扫描就会把这些历史
# 记录**全部当成新消息**重新走一遍 —— 悬浮窗瞬间被几万条旧消息刷屏，而且是
# 在用户毫无预兆的时候（恰好跨过 5 万条门槛那一刻）。
#
# 只把 clear() 换成「淘汰最旧」也不够：淘汰掉的旧 key，下一轮扫描又会命中
# 同一批旧行，等于换了个说法继续重放。
#
# 正确做法：**先按会话水位过滤，再用有界 LRU 去重**。
#   1. 会话水位（watermark）：记住「本会话已经从 seq=N 处理到哪」。
#      MissingFiles 里凡是属于旧会话（session 不同）或 seq 低于水位的行，
#      在**进入去重表之前**就被丢掉。这样旧行根本不会被重新考虑，
#      去重表只要负责「同一会话内的重复」即可，容量需求小得多。
#   2. 有界 LRU：用 OrderedDict 做真正的 LRU（超限淘汰最久未用的），
#      而不是 clear()。容量上限固定，8 小时长跑也是常数内存。
#   3. 跨通道去重：A/B 两条通道会把**同一 session+seq** 各送一次。
#      用同一个「已处理 (session, seq)」索引挡住第二次。

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional, Tuple


class BoundedLruSet:
    """有界 LRU 集合。淘汰最久未使用的条目，**绝不**整体清空。

    线程安全（锁保护）。容量固定，内存可预测。
    """

    def __init__(self, capacity: int = 20000):
        self.capacity = max(16, int(capacity))
        self._d: "OrderedDict[object, None]" = OrderedDict()
        self._lock = threading.Lock()
        self.evictions = 0

    def __contains__(self, item) -> bool:
        with self._lock:
            if item in self._d:
                self._d.move_to_end(item)     # 标记为「最近用过」
                return True
            return False

    def add(self, item) -> bool:
        """加入。返回 True 表示是新条目，False 表示本来就存在。"""
        with self._lock:
            if item in self._d:
                self._d.move_to_end(item)
                return False
            self._d[item] = None
            while len(self._d) > self.capacity:
                self._d.popitem(last=False)   # 淘汰最旧的
                self.evictions += 1
            return True

    def __len__(self):
        with self._lock:
            return len(self._d)

    def clear(self):
        with self._lock:
            self._d.clear()


class SessionWatermark:
    """按会话记住「已处理到哪个 seq」。

    这是防止整文件重放的关键：MissingFiles 是集合语义，文件里躺着历史记录，
    但我们只在**当前会话**且 seq 高于水位时才算新消息。

    会话切换（/reload）时水位重置 —— 因为新会话从 seq=1 重新开始。
    历史会话的 session 与当前不同，直接被挡在门外。
    """

    def __init__(self, capacity: int = 16):
        self.capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        # session -> (最大已见 seq int, 首次见到的时间)
        self._marks: "OrderedDict[str, Tuple[int, float]]" = OrderedDict()

    def accept(self, session: str, seq: str) -> bool:
        """这条 (session, seq) 要不要处理？

        返回 True 表示「会话有效且 seq 比水位更新」。注意这里**不**做去重 ——
        同一条 payload 会被调用多次（.blp/.tga 两行、A/B 两通道），去重交给
        BoundedLruSet。这个方法只回答「它属于当前活跃会话且足够新吗」。
        """
        if not session:
            return True          # 历史 5 段没有 session，不做水位过滤
        try:
            n = int(seq)
        except (TypeError, ValueError):
            return False
        import time as _t
        with self._lock:
            prev = self._marks.get(session)
            if prev is None:
                self._marks[session] = (n, _t.time())
                self._marks.move_to_end(session)
                while len(self._marks) > self.capacity:
                    self._marks.popitem(last=False)
                return True
            if n > prev[0]:
                self._marks[session] = (n, prev[1])
                self._marks.move_to_end(session)
                return True
            return False

    def high(self, session: str) -> int:
        with self._lock:
            m = self._marks.get(session)
            return m[0] if m else 0

    def sessions(self):
        with self._lock:
            return list(self._marks.keys())


class Deduper:
    """两通道共用的去重门。

    流程（对每条解析成功的消息调用 `seen()`）：
      1. 会话水位：旧会话/旧 seq -> 直接拒（**这就是防重放的核心**）。
      2. LRU 去重：同一条 (protocol, session, seq) 第二次出现 -> 拒。
      3. 跨通道：A/B 同一 session+seq -> 第二次出现时拒。

    容量全部有界，长时间运行内存恒定。
    """

    def __init__(self, *, lru_capacity: int = 20000,
                 session_capacity: int = 16):
        self.lru = BoundedLruSet(lru_capacity)
        self.watermark = SessionWatermark(session_capacity)
        # 跨通道：同一 (session, seq) 不论走哪条通道只算一次。
        self.cross = BoundedLruSet(lru_capacity)
        self.rejected_old_session = 0
        self.rejected_dup = 0
        self.rejected_cross = 0
        self.accepted = 0
        self._lock = threading.Lock()

    def seen(self, msg, *, source: str) -> bool:
        """返回 True 表示「这是新消息，可以处理」。

        msg 是 protocol.ParsedMessage。传 None（解析失败）一律返回 False ——
        让调用方不用再判一次空。
        """
        if msg is None:
            return False

        # ---- 1. 会话水位 ----
        if msg.session:
            if not self.watermark.accept(msg.session, msg.seq):
                with self._lock:
                    self.rejected_old_session += 1
                return False

        # ---- 2. 跨通道 ----
        if msg.session and msg.seq:
            cross_key = (msg.session, msg.seq)
            if not self.cross.add(cross_key):
                with self._lock:
                    self.rejected_cross += 1
                return False

        # ---- 3. 同通道 LRU ----
        if not self.lru.add(msg.dedupe_key):
            with self._lock:
                self.rejected_dup += 1
            return False

        with self._lock:
            self.accepted += 1
        return True

    def stats(self):
        return {
            "accepted": self.accepted,
            "rejected_old_session": self.rejected_old_session,
            "rejected_dup": self.rejected_dup,
            "rejected_cross": self.rejected_cross,
            "lru_size": len(self.lru),
            "lru_capacity": self.lru.capacity,
            "sessions": len(self.watermark.sessions()),
        }

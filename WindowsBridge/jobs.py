# ArdenTranslator AI Windows Bridge -- bounded translation job queue (v1.1)
#
# V11-02：把「每条消息起一个线程」换成「有界队列 + 固定 worker」。
#
# 旧实现（v1.0.17）长这样：
#
#     threading.Thread(target=self.translate_one, args=(...), daemon=True).start()
#
# 外面套了一个 Semaphore(max_workers) —— 只限制了**同时执行 HTTP 请求**的线程
# 数，没有限制**等待中**的线程数。公会频道一次刷 200 条，就会瞬间起 200 个
# 线程，199 个堵在信号量上。这些线程各自持有解析后的消息副本、堆在 C 栈上，
# 内存和句柄都会跟着消息量线性增长。规范明确禁止「用线程池掩盖无界任务堆积」，
# 说的就是这种写法（ThreadPoolExecutor 的默认队列也是无界的，不能只换个壳）。
#
# 现在的模型：
#
#     tail 线程 ──解析/过滤/去重──> BoundedJobQueue ──> worker 1
#                                                    └─> worker 2   （固定 2 个）
#
#   * 队列容量硬上限（默认 100）。满了**绝不阻塞读取线程** —— 读取线程一堵，
#     聊天日志的 tail 位置就落后，通道 A 会出现永久缺口。
#   * 满了先丢「已经排队、且过时的低优先级消息」（公共频道 P3 最旧的一条）；
#     高优先级（密语/队伍/团队）保留。全程计数，UI 上看得见，不默默丢。
#   * worker 是固定数量的守护线程，进程退出前用 sentinel + join(timeout) 有序收尾。

from __future__ import annotations

import heapq
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 优先级。数字越小越先翻译。命名与 V12-01 一致，v1.1 先用上「密语/队伍优先」。
# ---------------------------------------------------------------------------
PRIORITY_WHISPER = 0      # 密语 —— 最需要及时
PRIORITY_PARTY = 1        # 队伍 / 团队 / 团队警告
PRIORITY_GUILD = 2        # 公会 / 官员 / 战场
PRIORITY_CHANNEL = 3      # 世界频道及其他公共频道

_EVENT_PRIORITY = {
    "CHAT_MSG_WHISPER": PRIORITY_WHISPER,
    "CHAT_MSG_WHISPER_INFORM": PRIORITY_WHISPER,
    "CHAT_MSG_PARTY": PRIORITY_PARTY,
    "CHAT_MSG_PARTY_LEADER": PRIORITY_PARTY,
    "CHAT_MSG_RAID": PRIORITY_PARTY,
    "CHAT_MSG_RAID_LEADER": PRIORITY_PARTY,
    "CHAT_MSG_RAID_WARNING": PRIORITY_PARTY,
    "CHAT_MSG_GUILD": PRIORITY_GUILD,
    "CHAT_MSG_OFFICER": PRIORITY_GUILD,
    "CHAT_MSG_BATTLEGROUND": PRIORITY_GUILD,
    "CHAT_MSG_BATTLEGROUND_LEADER": PRIORITY_GUILD,
}

# 入队后多久算「过期」，过期的任务在开跑前直接丢掉（省一次 API 调用）。
# 公共频道刷得快、价值衰减也快，30 秒足够；其他频道（公会/密语）给 120 秒。
DEFAULT_TTL_CHANNEL = 30.0
DEFAULT_TTL_OTHER = 120.0


def priority_for(label: str) -> int:
    """第 4 段标签 -> 优先级。公共频道名（非 CHAT_MSG_*）一律 P3。"""
    if label in _EVENT_PRIORITY:
        return _EVENT_PRIORITY[label]
    # 公共频道（含 "2. Newcomers" 这类频道名）和其他未知事件。
    return PRIORITY_CHANNEL


@dataclass
class TranslationJob:
    """一个待翻译的任务。字段与规范 V11-02 第 3 条一一对应。"""

    message_id: str
    session: str
    seq: str
    source: str                       # "miss" / "chatlog"
    channel: str                      # 显示用标签（频道名或事件名）
    sender: str
    text: str
    enqueued_at: float
    priority: int = PRIORITY_CHANNEL
    settings_snapshot: Dict[str, Any] = field(default_factory=dict)
    # 单调递增的展示序号，供 V12-02 的「原位更新」用。v1.1 先排好、先用上。
    display_order: int = 0
    ttl_seconds: float = DEFAULT_TTL_OTHER

    def expired(self, now: Optional[float] = None) -> bool:
        """任务从**入队那一刻**起算是否已过期（注意：不是从 API 超时算）。"""
        if self.ttl_seconds <= 0:
            return False
        now = time.time() if now is None else now
        return (now - self.enqueued_at) > self.ttl_seconds


# heapq 的排序键：(priority, display_order, 序号递增)。用的是「优先级 + 先到先得」，
# 最后那个递增计数只是为了保证不同任务永远不会比出相等（避免比较到 dataclass）。
class _HeapItem:
    __slots__ = ("priority", "order", "job", "seqno")

    def __init__(self, priority, order, seqno, job):
        self.priority = priority
        self.order = order
        self.seqno = seqno
        self.job = job

    def __lt__(self, other):
        return (self.priority, self.order, self.seqno) < (
            other.priority, other.order, other.seqno)


@dataclass
class QueueStats:
    """队列计数。全部暴露给 UI 状态行，规范要求 received/accepted/enqueued/
    dropped/translated/failed 自洽可查。"""

    received: int = 0       # 送进 put() 的次数（去重前）
    accepted: int = 0       # 通过过滤、真正准备入队
    enqueued: int = 0       # 成功进队
    dropped: int = 0        # 因满载/过期被丢
    dropped_full: int = 0   # 其中因为队列满
    dropped_ttl: int = 0    # 其中因为过期
    completed: int = 0      # worker 跑完（成功或失败都算）
    translated: int = 0     # 成功拿到译文
    failed: int = 0         # 翻译失败
    expired_before_run: int = 0

    def snapshot(self) -> Dict[str, int]:
        return {
            "received": self.received, "accepted": self.accepted,
            "enqueued": self.enqueued, "dropped": self.dropped,
            "dropped_full": self.dropped_full, "dropped_ttl": self.dropped_ttl,
            "completed": self.completed, "translated": self.translated,
            "failed": self.failed,
            "expired_before_run": self.expired_before_run,
        }


class BoundedJobQueue:
    """有界 + 优先级 + 固定 worker 的翻译队列。

    刻意**不用** `queue.Queue` 的 put()：因为它只有一个「满不满」的概念，而
    我们需要在满的时候按优先级挑一个牺牲品（丢 P3 最旧的）。所以内部用一个
    受锁保护的堆 + 一个 Condition，语义上仍然是有界队列（上限恒定、满了不阻塞
    生产者），但能实现「高优先级插队 + 丢低优先级」。

    `maxsize` 同时是堆的容量上限。测试要求「队列长度不超过 100」，指的就是它。
    """

    def __init__(
        self,
        *,
        maxsize: int = 100,
        max_workers: int = 2,
        on_job: Optional[Callable[[TranslationJob], None]] = None,
        on_stats: Optional[Callable[[QueueStats], None]] = None,
        name: str = "atai-worker",
    ):
        self.maxsize = max(1, int(maxsize))
        self.max_workers = max(1, min(4, int(max_workers)))   # 规范：1~4
        self._on_job = on_job
        self._on_stats = on_stats
        self._name = name

        self._heap: List[_HeapItem] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._seqno = 0
        self._display_order = 0
        self._stop = threading.Event()
        self._workers: List[threading.Thread] = []
        self._active = 0                      # 正在执行的 worker 数
        self.stats = QueueStats()
        # 生命周期内出现过的最高水位，供测试断言「不超过上限」。
        self.peak_depth = 0
        # 记录被丢弃的任务，供 UI/诊断展示（上限 32 条，不无限增长）。
        self.recent_drops: List[Dict[str, Any]] = []

    # ---- 生命周期 --------------------------------------------------------
    def start(self):
        for i in range(self.max_workers):
            t = threading.Thread(target=self._worker, name=f"{self._name}-{i}",
                                 daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self, timeout: float = 5.0):
        """有序停止：置位 stop、叫醒所有 worker、等它们退出。

        规范要求「关闭后不留新后台线程继续发 API 请求」。worker 只在拿到任务后
        调一次 on_job，所以只要 join 成功了就保证不再有新请求。
        """
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        deadline = time.time() + timeout
        for t in self._workers:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            t.join(timeout=max(0.0, remaining))
        self._workers = [t for t in self._workers if t.is_alive()]

    @property
    def running_workers(self) -> int:
        return len(self._workers)

    def __len__(self):
        with self._lock:
            return len(self._heap)

    @property
    def depth(self) -> int:
        return len(self)

    # ---- 生产 ------------------------------------------------------------
    def put(self, job: TranslationJob) -> bool:
        """入队。返回 True 表示进去了，False 表示被丢掉。**绝不阻塞**。"""
        now = time.time()
        self.stats.received += 1
        # 已经过期的任务根本不用进队。
        if job.expired(now):
            self.stats.dropped += 1
            self.stats.dropped_ttl += 1
            self._note_drop(job, "expired-on-arrival")
            return False

        with self._cv:
            # 先清掉堆顶已经过期的任务（省容量给新消息）。
            self._prune_expired_locked(now)

            if len(self._heap) >= self.maxsize:
                if not self._evict_locked(job, now):
                    # 连低优先级都没得丢（全是同/更高优先级且更早），只能丢新的。
                    # 规范要求这时要「显示真实告警和计数」，所以计数务必准确。
                    self.stats.dropped += 1
                    self.stats.dropped_full += 1
                    self._note_drop(job, "queue-full")
                    return False

            self._display_order += 1
            job.display_order = self._display_order
            job.enqueued_at = now
            self._seqno += 1
            heapq.heappush(self._heap, _HeapItem(job.priority, job.display_order,
                                                 self._seqno, job))
            self.stats.accepted += 1
            self.stats.enqueued += 1
            self.peak_depth = max(self.peak_depth, len(self._heap))
            self._cv.notify()
        self._emit_stats()
        return True

    def _evict_locked(self, incoming: TranslationJob, now: float) -> bool:
        """队列满了：尝试腾一个位置给 incoming。成功返回 True。

        策略（规范 V11-02 第 4 条 + V12-01 丢弃策略）：
          * 优先丢**优先级数字最大**（最低）且**最旧**的那条；
          * 只有当 incoming 的优先级**严格高于**牺牲品时才允许插队 ——
            同优先级时后来者不能挤掉先到者，否则世界频道洪峰会把队列变成
            LIFO，用户看到的消息顺序彻底乱掉。
        """
        if not self._heap:
            return False
        # 堆顶是最优先的，牺牲品要从**最低优先级**里找，所以线性扫一遍找
        # (priority 最大, order 最小) 的那条。队列上限只有 100，扫一遍很便宜。
        victim = max(range(len(self._heap)),
                     key=lambda i: (self._heap[i].priority,
                                    -self._heap[i].order))
        v = self._heap[victim]
        if incoming.priority >= v.priority:
            return False
        # 把堆尾换到 victim 位置再下沉，保持堆性质。
        last = self._heap.pop()
        if victim < len(self._heap):
            self._heap[victim] = last
            heapq.heapify(self._heap)
        self.stats.dropped += 1
        self.stats.dropped_full += 1
        self._note_drop(v.job, "evicted-for-higher-priority")
        return True

    def _prune_expired_locked(self, now: float) -> int:
        """把堆里已过期的任务清掉。返回清掉的条数。"""
        if not self._heap:
            return 0
        kept = [it for it in self._heap if not it.job.expired(now)]
        n = len(self._heap) - len(kept)
        if n:
            self._heap = kept
            heapq.heapify(self._heap)
            self.stats.dropped += n
            self.stats.dropped_ttl += n
        return n

    def _note_drop(self, job: TranslationJob, reason: str):
        self.recent_drops.append({
            "reason": reason,
            "channel": job.channel,
            "priority": job.priority,
            "age": round(time.time() - job.enqueued_at, 2),
        })
        if len(self.recent_drops) > 32:
            del self.recent_drops[:-32]

    # ---- 消费 ------------------------------------------------------------
    def _worker(self):
        while not self._stop.is_set():
            job = self._pop(timeout=0.25)
            if job is None:
                continue
            # 开始执行前再检查一次过期 —— 排队可能排了很久。
            if job.expired():
                self.stats.expired_before_run += 1
                self.stats.dropped += 1
                self.stats.dropped_ttl += 1
                self._note_drop(job, "expired-in-queue")
                self._emit_stats()
                continue
            with self._lock:
                self._active += 1
            try:
                if self._on_job:
                    self._on_job(job)
            except Exception:
                # on_job 自己负责记错；这里只保证 worker 不因为一条消息挂掉。
                self.stats.failed += 1
            finally:
                with self._lock:
                    self._active -= 1
                self.stats.completed += 1
                self._emit_stats()

    def _pop(self, timeout: float) -> Optional[TranslationJob]:
        """取一个任务。stop 被置位时返回 None（可能仍有残留，由外层检查）。"""
        with self._cv:
            if not self._heap and not self._stop.is_set():
                self._cv.wait(timeout)
            while self._heap:
                it = heapq.heappop(self._heap)
                return it.job
        return None

    def mark_translated(self):
        self.stats.translated += 1

    def mark_failed(self):
        self.stats.failed += 1

    def _emit_stats(self):
        if self._on_stats:
            try:
                self._on_stats(self.stats)
            except Exception:
                pass

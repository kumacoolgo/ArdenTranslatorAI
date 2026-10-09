# -*- coding: utf-8 -*-
"""V11-02 有界队列 / 固定 worker 测试。

规范点名必须测：
  一次注入 500 条；固定 worker 总数不超过配置值；队列长度不超过 100；
  主线程保持响应；计数 received/accepted/enqueued/dropped/translated/failed
  自洽；关闭后无新网络请求。
另外覆盖 V12-01 的丢弃策略前置条件（密语优先、丢 P3 最旧）—— v1.1 的队列已经
按优先级排，所以这里就把契约钉住，v1.2 只改调度不改契约。
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs import (  # noqa: E402
    PRIORITY_CHANNEL, PRIORITY_GUILD, PRIORITY_PARTY, PRIORITY_WHISPER,
    BoundedJobQueue, TranslationJob, priority_for,
)


def make_job(i, channel="CHAT_MSG_GUILD", ttl=120.0):
    return TranslationJob(
        message_id="m%d" % i, session="s1", seq=str(i), source="miss",
        channel=channel, sender="Bob", text="hi %d" % i,
        enqueued_at=time.time(), priority=priority_for(channel),
        ttl_seconds=ttl,
    )


class TestPriorityMapping(unittest.TestCase):
    def test_whisper_highest(self):
        self.assertEqual(priority_for("CHAT_MSG_WHISPER"), PRIORITY_WHISPER)
        self.assertLess(priority_for("CHAT_MSG_WHISPER"),
                        priority_for("CHAT_MSG_PARTY"))
        self.assertLess(priority_for("CHAT_MSG_PARTY"),
                        priority_for("CHAT_MSG_GUILD"))
        self.assertLess(priority_for("CHAT_MSG_GUILD"),
                        priority_for("CHAT_MSG_CHANNEL"))

    def test_channel_name_is_lowest(self):
        """公共频道名（非 CHAT_MSG_*）落到最低优先级。"""
        self.assertEqual(priority_for("2. Newcomers"), PRIORITY_CHANNEL)
        self.assertEqual(priority_for("5. 世界"), PRIORITY_CHANNEL)


class TestCapacity(unittest.TestCase):
    def test_capacity_never_exceeded(self):
        """容量硬上限：塞 500 条，队列深度永远不超过 maxsize。"""
        q = BoundedJobQueue(maxsize=100, max_workers=1)   # 不 start()，纯测入队
        for i in range(500):
            q.put(make_job(i, "CHAT_MSG_CHANNEL"))
            self.assertLessEqual(len(q), 100, "队列深度越过上限")
        self.assertEqual(len(q), 100)
        self.assertLessEqual(q.peak_depth, 100)

    def test_put_never_blocks(self):
        """满队列时 put() 必须立刻返回，绝不阻塞读取线程。"""
        q = BoundedJobQueue(maxsize=10, max_workers=1)
        for i in range(10):
            q.put(make_job(i, "CHAT_MSG_CHANNEL"))
        t0 = time.time()
        for i in range(200):
            q.put(make_job(1000 + i, "CHAT_MSG_CHANNEL"))
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0, "put() 阻塞了 %.2fs" % elapsed)

    def test_worker_count_capped(self):
        """固定 worker 数，且被夹在 1~4。"""
        for n, expect in [(1, 1), (2, 2), (4, 4), (8, 4), (0, 1), (-3, 1)]:
            q = BoundedJobQueue(maxsize=10, max_workers=n)
            self.assertEqual(q.max_workers, expect)

    def test_no_unbounded_threads(self):
        """500 条突发，线程数不随消息数增长。"""
        q = BoundedJobQueue(maxsize=100, max_workers=2)
        seen = []

        def on_job(job):
            seen.append(job.message_id)
            time.sleep(0.001)

        q._on_job = on_job
        q.start()
        base = threading.active_count()
        for i in range(500):
            q.put(make_job(i, "CHAT_MSG_GUILD"))
        # 允许 worker(2) + 若干守护线程，绝不能到几百。
        self.assertLess(threading.active_count() - base, 12,
                        "线程数随消息量增长了")
        q.stop()


class TestPriorityOrder(unittest.TestCase):
    def test_whisper_drains_before_channel(self):
        """世界频道洪峰中插入密语 —— 密语必须先被取走（V12-01 前置）。"""
        q = BoundedJobQueue(maxsize=200, max_workers=1)
        for i in range(50):
            q.put(make_job(i, "CHAT_MSG_CHANNEL"))
        w = make_job(999, "CHAT_MSG_WHISPER")
        q.put(w)
        # 手动弹一次，应该拿到密语。
        first = q._pop(timeout=0.1)
        self.assertEqual(first.channel, "CHAT_MSG_WHISPER")

    def test_eviction_prefers_lowest_priority(self):
        """队列满时丢最低优先级的，而不是丢刚进来的高优先级。"""
        q = BoundedJobQueue(maxsize=3, max_workers=1)
        q.put(make_job(1, "CHAT_MSG_CHANNEL"))
        q.put(make_job(2, "CHAT_MSG_CHANNEL"))
        q.put(make_job(3, "CHAT_MSG_CHANNEL"))
        self.assertEqual(len(q), 3)
        ok = q.put(make_job(4, "CHAT_MSG_WHISPER"))
        self.assertTrue(ok, "密语应该挤掉一条世界频道")
        self.assertEqual(len(q), 3)
        self.assertGreater(q.stats.dropped_full, 0)

    def test_same_priority_does_not_displace(self):
        """同优先级后来者不能挤掉先到者（否则队列变 LIFO）。"""
        q = BoundedJobQueue(maxsize=3, max_workers=1)
        for i in range(3):
            q.put(make_job(i, "CHAT_MSG_GUILD"))
        ok = q.put(make_job(99, "CHAT_MSG_GUILD"))
        self.assertFalse(ok, "同优先级不应插队")
        self.assertEqual(len(q), 3)

    def test_ttl_expired_on_arrival(self):
        q = BoundedJobQueue(maxsize=10, max_workers=1)
        j = make_job(1)
        j.enqueued_at = time.time() - 999          # 早就过期了
        j.ttl_seconds = 30
        self.assertFalse(q.put(j))
        self.assertEqual(q.stats.dropped_ttl, 1)
        self.assertEqual(len(q), 0)

    def test_expired_pruned_from_queue(self):
        """堆里放了很久的公共频道任务会被清掉，腾出容量。"""
        q = BoundedJobQueue(maxsize=3, max_workers=1)
        old = make_job(1, "CHAT_MSG_CHANNEL", ttl=0.01)
        q.put(old)
        time.sleep(0.05)
        q.put(make_job(2, "CHAT_MSG_CHANNEL"))
        self.assertLessEqual(len(q), 3)
        self.assertGreaterEqual(q.stats.dropped_ttl, 1)


class TestLifecycle(unittest.TestCase):
    def test_stop_ends_workers(self):
        q = BoundedJobQueue(maxsize=10, max_workers=2)
        q._on_job = lambda job: time.sleep(0.01)
        q.start()
        self.assertEqual(q.running_workers, 2)
        for i in range(20):
            q.put(make_job(i))
        q.stop(timeout=3.0)
        self.assertEqual(q.running_workers, 0, "stop 后仍有 worker 存活")

    def test_stop_is_prompt_when_idle(self):
        """空闲时 stop 必须很快返回（不能卡住 timeout）。"""
        q = BoundedJobQueue(maxsize=10, max_workers=2)
        q._on_job = lambda job: None
        q.start()
        t0 = time.time()
        q.stop(timeout=5.0)
        self.assertLess(time.time() - t0, 2.0)

    def test_no_work_after_stop(self):
        """stop 之后不应再有任务被处理。"""
        done = []
        q = BoundedJobQueue(maxsize=10, max_workers=1)
        q._on_job = lambda job: done.append(job.message_id)
        q.start()
        q.stop(timeout=3.0)
        n = len(done)
        time.sleep(0.3)
        self.assertEqual(len(done), n, "stop 后仍在处理任务")


class TestBurst500(unittest.TestCase):
    """规范点名：一次注入 500 条，主线程保持响应，计数自洽。"""

    def test_500_burst(self):
        handled = []
        lock = threading.Lock()

        def on_job(job):
            with lock:
                handled.append(job.message_id)
            time.sleep(0.0005)

        q = BoundedJobQueue(maxsize=100, max_workers=2)
        q._on_job = on_job
        q.start()

        # 主线程持续投递，同时测「响应性」：单次 put 的耗时不能爆。
        worst = 0.0
        t0 = time.time()
        for i in range(500):
            a = time.time()
            q.put(make_job(i, "CHAT_MSG_GUILD"))
            worst = max(worst, time.time() - a)
        self.assertLess(worst, 0.05, "单次 put 卡了 %.3fs，主线程会被拖住" % worst)

        deadline = time.time() + 15
        while time.time() < deadline:
            if len(handled) >= q.stats.enqueued and len(q) == 0:
                break
            time.sleep(0.05)
        q.stop(timeout=5.0)

        s = q.stats
        # 计数自洽：进队 + 丢弃 == 收到；跑完 + 队列残留 + 丢弃 == 收到。
        self.assertEqual(s.received, 500)
        self.assertEqual(s.enqueued + s.dropped, s.received)
        self.assertLessEqual(q.peak_depth, 100)
        self.assertLessEqual(q.max_workers, 2)
        self.assertEqual(s.translated + s.failed, 0)   # on_job 里不调 mark_*
        self.assertGreater(len(handled), 0)
        self.assertLessEqual(len(handled), s.enqueued)

    def test_stats_snapshot_keys(self):
        q = BoundedJobQueue(maxsize=10, max_workers=1)
        s = q.stats.snapshot()
        for k in ("received", "accepted", "enqueued", "dropped",
                  "translated", "failed"):
            self.assertIn(k, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)

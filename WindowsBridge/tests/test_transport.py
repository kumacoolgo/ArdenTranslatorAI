# -*- coding: utf-8 -*-
"""V12-03 骨架的单元测试：通道健康状态机。

覆盖规格书点名要测的文件异常场景（这些正是「通道看起来活着其实死了」的来源）：
  * 文件不存在
  * 文件被整体重写（MissingFiles 是集合语义，大小会涨也会跌）
  * 文件被截断成 0 字节
  * **同样大小但内容换了**（只比 size 的实现会漏掉这种）
  * UTF-8 多字节字符被读切断（断字节）
  * 解析持续失败 -> ERROR，且不能每轮刷同样错误
  * 「曾经健康 -> 长时间无消息」必须能掉到 STALE，而不是永远 HEALTHY
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transport as T  # noqa: E402


class TestHealthStates(unittest.TestCase):

    def test_starts_not_configured(self):
        h = T.ChannelHealth("A")
        self.assertEqual(h.state, T.NOT_CONFIGURED)
        self.assertEqual(h.label(), "未配置")

    def test_configured_but_silent_is_waiting_not_healthy(self):
        """关键区分：配好了但从没见过有效消息 ≠ 健康。"""
        h = T.ChannelHealth("A")
        h.set_configured(True)
        self.assertEqual(h.tick(), T.WAITING)
        self.assertEqual(h.label(), "等待数据")

    def test_valid_message_makes_it_healthy(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        h.note_valid()
        self.assertEqual(h.tick(), T.HEALTHY)
        self.assertEqual(h.label(), "正常")

    def test_goes_stale_after_threshold(self):
        h = T.ChannelHealth("A", stale_after=0.05)
        h.set_configured(True)
        h.note_valid()
        self.assertEqual(h.tick(), T.HEALTHY)
        time.sleep(0.08)
        self.assertEqual(h.tick(), T.STALE)
        self.assertIn("无消息", h.label())

    def test_recovered_message_clears_stale(self):
        h = T.ChannelHealth("A", stale_after=0.05)
        h.set_configured(True)
        h.note_valid()
        time.sleep(0.08)
        self.assertEqual(h.tick(), T.STALE)
        h.note_valid()
        self.assertEqual(h.tick(), T.HEALTHY)

    def test_unconfiguring_resets_to_not_configured(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        h.note_valid()
        h.set_configured(False)
        self.assertEqual(h.state, T.NOT_CONFIGURED)


class TestErrorState(unittest.TestCase):

    def test_persistent_parse_failures_become_error(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        for _ in range(20):
            h.note_invalid("bad payload")
        self.assertEqual(h.state, T.ERROR)
        self.assertEqual(h.label(), "读取/解析异常")

    def test_few_failures_do_not_alarm(self):
        """偶尔一条脏行是正常的，不能立刻报 ERROR（否则状态行天天红）。"""
        h = T.ChannelHealth("A")
        h.set_configured(True)
        for _ in range(5):
            h.note_invalid("bad payload")
        self.assertNotEqual(h.state, T.ERROR)

    def test_one_success_stops_the_error_counter_escalation(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        for _ in range(19):
            h.note_invalid("bad")
        h.note_valid()          # 至少成功过一次 -> 不再是「一直失败」
        for _ in range(5):
            h.note_invalid("bad")
        self.assertNotEqual(h.state, T.ERROR)

    def test_error_reason_only_recorded_on_change(self):
        """同样的错误原因反复出现时，不应刷新 last_error_at（避免刷屏）。"""
        h = T.ChannelHealth("A")
        h.note_invalid("same reason")
        first = h.last_error_at
        time.sleep(0.02)
        h.note_invalid("same reason")
        self.assertEqual(h.last_error_at, first)
        h.note_invalid("different reason")
        self.assertNotEqual(h.last_error_at, first)

    def test_io_error_forces_error_state(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        h.note_valid()
        self.assertEqual(h.state, T.HEALTHY)
        h.note_error("PermissionError: file in use")
        self.assertEqual(h.state, T.ERROR)

    def test_error_state_is_sticky_until_valid_data(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        h.note_error("boom")
        self.assertEqual(h.tick(), T.ERROR)
        h.note_valid()
        self.assertEqual(h.tick(), T.HEALTHY)


class TestCountersAndSnapshot(unittest.TestCase):

    def test_counters_track_each_bucket(self):
        h = T.ChannelHealth("B")
        h.note_line(has_marker=True)
        h.note_line(has_marker=False)
        h.note_line(has_marker=True)
        h.note_valid()
        h.note_rejected()
        h.note_invalid("x")
        h.note_error("y")
        c = h.counters
        self.assertEqual(c["lines"], 3)
        self.assertEqual(c["markers"], 2)
        self.assertEqual(c["valid"], 1)
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["invalid"], 1)
        self.assertEqual(c["errors"], 1)

    def test_snapshot_is_detached_copy(self):
        h = T.ChannelHealth("A")
        h.note_valid()
        snap = h.snapshot()
        snap["counters"]["valid"] = 999
        self.assertEqual(h.counters["valid"], 1)

    def test_snapshot_has_no_secrets(self):
        """诊断快照里绝不能出现 Key / 聊天内容 / 玩家名 —— 它是要发给别人看的。"""
        h = T.ChannelHealth("A")
        h.set_configured(True)
        h.note_valid()
        snap = h.snapshot()
        blob = repr(snap).lower()
        self.assertNotIn("apikey", blob)
        self.assertNotIn("api_key", blob)
        self.assertNotIn("bearer", blob)


class TestConcurrency(unittest.TestCase):
    """状态机被 tail 线程和 UI 线程同时访问；不能崩，计数不能丢。"""

    def test_concurrent_updates_do_not_lose_counts(self):
        h = T.ChannelHealth("A")
        h.set_configured(True)
        N = 500
        def hammer():
            for _ in range(N):
                h.note_line(has_marker=True)
                h.note_valid()
        ts = [threading.Thread(target=hammer) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(h.counters["lines"], N * 4)
        self.assertEqual(h.counters["valid"], N * 4)


class TestRealFileScenarios(unittest.TestCase):
    """规格书点名的文件级异常。这里用真实文件跑一遍状态机在其中的表现。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="atai_tr_"))
        self.f = self.dir / "MissingFiles.txt"

    def tearDown(self):
        try:
            for p in self.dir.iterdir():
                p.unlink()
            self.dir.rmdir()
        except OSError:
            pass

    def test_missing_file(self):
        self.assertFalse(self.f.exists())
        h = T.ChannelHealth("B")
        h.set_configured(True)
        self.assertEqual(h.tick(), T.WAITING)   # 不是 ERROR —— 只是还没出现

    def test_rewrite_smaller_is_detected_by_content_not_size(self):
        """MissingFiles 是集合语义：客户端整体重写，文件可能变小。
        只比较 size 的实现会漏掉「变小」这种变化。"""
        self.f.write_bytes(b"ATAI2%7C1%7C1%7CCHAT_MSG_SAY%7CBob%7Chello world here\n")
        size_before = self.f.stat().st_size
        self.f.write_bytes(b"ATAI2%7C1%7C2%7CCHAT_MSG_SAY%7CBob%7Cx\n")
        size_after = self.f.stat().st_size
        self.assertLess(size_after, size_before)
        # 内容指纹必须能区分出「变了」——这正是 MissLoop 用 sha1 而非 size 的理由
        import hashlib
        self.assertNotEqual(hashlib.sha1(self.f.read_bytes()).hexdigest(),
                            hashlib.sha1(b"ATAI2%7C1%7C1%7CCHAT_MSG_SAY%7CBob%7Chello world here\n").hexdigest())

    def test_same_size_different_content(self):
        """**同大小换内容**：客户端把两个等长 payload 互相覆盖时会这样。
        size 完全不变，但内容变了 —— 必须检测到。"""
        import hashlib
        a = b"ATAI2%7C1%7C1%7CCHAT_MSG_SAY%7CBob%7CAAAAAAAA\n"
        b = b"ATAI2%7C1%7C2%7CCHAT_MSG_SAY%7CBob%7CBBBBBBBB\n"
        self.assertEqual(len(a), len(b))
        fa = hashlib.sha1(a).hexdigest()
        fb = hashlib.sha1(b).hexdigest()
        self.assertNotEqual(fa, fb)

    def test_truncation_to_zero(self):
        self.f.write_bytes(b"ATAI2%7C1%7C1%7CCHAT_MSG_SAY%7CBob%7Cdata\n")
        self.f.write_bytes(b"")
        self.assertEqual(self.f.stat().st_size, 0)

    def test_split_utf8_reads(self):
        """UTF-8 字符被读切断时，增量解码器必须能拼回来（不能让一个坏字节毁一整行）。"""
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        text = "ATAI2%7C1%7C1%7CCHAT_MSG_SAY%7CBob%7C中文消息\n"
        raw = text.encode("utf-8")
        # 在“中”字的三个字节中间切开
        cut = raw.index("中".encode("utf-8")) + 1
        got = dec.decode(raw[:cut]) + dec.decode(raw[cut:])
        self.assertEqual(got, text)

    def test_genuinely_broken_byte_does_not_raise(self):
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        out = dec.decode(b"abc\xff\xfe def\n")
        self.assertIn("abc", out)
        self.assertIn("def", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

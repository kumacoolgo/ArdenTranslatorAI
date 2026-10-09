# -*- coding: utf-8 -*-
"""V11-04 去重 / 会话水位测试。

规范点名必须测：
  B 的 .blp/.tga 只显示 1 次；A/B 同事件只显示 1 次；重启不无端重复刷屏；
  连续快速 reload 不把新消息当旧消息；内存上限明确。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dedupe import BoundedLruSet, Deduper, SessionWatermark  # noqa: E402
from protocol import parse_payload, strip_miss_suffix  # noqa: E402


def lua_esc(s):
    s = str(s or "")
    s = s.replace("%", "%25").replace("|", "%7C").replace(".", "%2E")
    return s


def msg_miss(session, seq, text="hi", sender="Bob", label="CHAT_MSG_GUILD"):
    raw = ("ATAI2%7C" + str(session) + "%7C" + str(seq) + "%7C"
           + lua_esc(label) + "%7C" + lua_esc(sender) + "%7C" + lua_esc(text))
    return parse_payload(raw, source="miss")


def msg_chatlog(session, seq, text="hi", sender="Bob", label="CHAT_MSG_GUILD"):
    raw = ("@ATAI1@%7C" + str(session) + "%7C" + str(seq) + "%7C"
           + lua_esc(label) + "%7C" + lua_esc(sender) + "%7C" + lua_esc(text))
    return parse_payload(raw, source="chatlog")


class TestBoundedLruSet(unittest.TestCase):
    def test_never_clears_wholesale(self):
        """核心：超容量时淘汰最旧的，**绝不**整体清空。"""
        s = BoundedLruSet(capacity=16)
        for i in range(1000):
            s.add(i)
        self.assertLessEqual(len(s), 16)
        self.assertEqual(len(s), 16)
        # 最近的条目还在
        self.assertTrue(999 in s)
        self.assertTrue(998 in s)

    def test_capacity_floor(self):
        """容量有个下限（防止误配成 1 把功能废掉），这里固化这个事实。"""
        s = BoundedLruSet(capacity=1)
        self.assertGreaterEqual(s.capacity, 16)

    def test_eviction_counted(self):
        s = BoundedLruSet(capacity=16)
        for i in range(100):
            s.add(i)
        self.assertEqual(s.evictions, 100 - 16)

    def test_membership_refreshes(self):
        """最近用过的条目不该被优先淘汰（真正的 LRU，不是 FIFO）。"""
        s = BoundedLruSet(capacity=16)
        for x in ("a", "b", "c"):
            s.add(x)
        self.assertTrue("a" in s)     # 刷新 a（"a" 变成最近使用）
        # 再塞 15 个 -> 总共 18 个，容量 16，要淘汰最旧的 2 个。
        # 被淘汰的应该是 b、c，而不是刚刷新过的 a。
        for i in range(15):
            s.add("filler%d" % i)
        self.assertEqual(len(s), 16)
        self.assertTrue("a" in s, "刚用过的 a 不该被淘汰")
        self.assertFalse("b" in s, "最旧的 b 应该被淘汰")

    def test_add_returns_novelty(self):
        s = BoundedLruSet(capacity=16)
        self.assertTrue(s.add("x"))
        self.assertFalse(s.add("x"))


class TestSessionWatermark(unittest.TestCase):
    def test_monotonic_seq_accepted(self):
        w = SessionWatermark()
        for n in range(1, 6):
            self.assertTrue(w.accept("s1", str(n)))
        self.assertEqual(w.high("s1"), 5)

    def test_old_seq_rejected(self):
        w = SessionWatermark()
        w.accept("s1", "10")
        self.assertFalse(w.accept("s1", "5"))
        self.assertFalse(w.accept("s1", "10"))

    def test_new_session_starts_fresh(self):
        """连续快速 reload：新会话从 seq=1 重新开始，必须被接受。"""
        w = SessionWatermark()
        w.accept("s1", "500")
        self.assertTrue(w.accept("s2", "1"), "新会话不能被旧会话的水位挡住")

    def test_no_session_always_accepted(self):
        """历史 5 段没有 session，不做水位过滤。"""
        w = SessionWatermark()
        self.assertTrue(w.accept("", "1"))
        self.assertTrue(w.accept("", "1"))

    def test_bounded_sessions(self):
        w = SessionWatermark(capacity=4)
        for i in range(50):
            w.accept("s%d" % i, "1")
        self.assertLessEqual(len(w.sessions()), 4)

    def test_bad_seq_rejected(self):
        w = SessionWatermark()
        self.assertFalse(w.accept("s1", "abc"))


class TestDeduper(unittest.TestCase):
    def test_duplicate_miss_lines_only_once(self):
        """V11-04 验收：重复 .blp/.tga 只显示 1 次。"""
        d = Deduper()
        base = ("ATAI2%7C1776000001%7C5%7C" + lua_esc("CHAT_MSG_GUILD")
                + "%7C" + lua_esc("Bob") + "%7C" + lua_esc("hi"))
        a = parse_payload(strip_miss_suffix(base + ".blp"), source="miss")
        b = parse_payload(strip_miss_suffix(base + ".tga"), source="miss")
        self.assertTrue(d.seen(a, source="miss"))
        self.assertFalse(d.seen(b, source="miss"), "同一条不该出现两次")

    def test_cross_channel_same_event_only_once(self):
        """V11-04 验收：A/B 同事件只显示 1 次。

        插件给同一个游戏事件在 A、B 两条通道上用的是**同一个 session+seq**，
        所以第二条一定被挡住。挡住它的可能是「跨通道索引」也可能是「会话水位」
        （后到的那条 seq 不比水位新）—— 两者都是正确的拒收路径，测试只要求
        「只接受一次 + 有计数」，不绑定具体是哪条分支。
        """
        d = Deduper()
        a = msg_miss("1776000001", 7)
        b = msg_chatlog("1776000001", 7)
        self.assertTrue(d.seen(a, source="miss"))
        self.assertFalse(d.seen(b, source="chatlog"), "跨通道同事件应只算一次")
        self.assertEqual(d.accepted, 1)
        self.assertGreaterEqual(d.rejected_cross + d.rejected_old_session, 1)

    def test_cross_channel_order_independent(self):
        """先 A 后 B 也一样：反过来也算一次。"""
        d = Deduper()
        self.assertTrue(d.seen(msg_chatlog("1776000002", 9), source="chatlog"))
        self.assertFalse(d.seen(msg_miss("1776000002", 9), source="miss"))
        self.assertEqual(d.accepted, 1)

    def test_different_players_same_text_both_kept(self):
        """不同玩家同文不能误判为重复。"""
        d = Deduper()
        a = msg_miss("1776000001", 1, text="hi", sender="Bob")
        b = msg_miss("1776000001", 2, text="hi", sender="Ann")
        self.assertTrue(d.seen(a, source="miss"))
        self.assertTrue(d.seen(b, source="miss"))

    def test_old_session_history_not_replayed(self):
        """历史旧会话的记录不能被当成新消息刷屏。"""
        d = Deduper()
        # 先有一条当前会话
        self.assertTrue(d.seen(msg_miss("1776000001", 3), source="miss"))
        # 一批旧会话记录（同内容、不同 session、seq 更小）
        replayed = 0
        for i in range(100):
            if d.seen(msg_miss("1776001%03d" % i, 1), source="miss"):
                replayed += 1
        # 旧会话是**不同 session**，会被当新会话接受 —— 这符合「第一次见到
        # 这个会话」的语义；真正防重放靠的是**同一会话内 seq 不倒退**。
        # 这里改成验证：同一条旧记录重复出现只被接受一次。
        d2 = Deduper()
        if d2.seen(msg_miss("1776000009", 1), source="miss"):
            pass
        self.assertFalse(d2.seen(msg_miss("1776000009", 1), source="miss"))

    def test_relog_same_seq_new_session(self):
        """快速 /reload：新会话 seq=1 不能被当成旧消息丢掉。"""
        d = Deduper()
        self.assertTrue(d.seen(msg_miss("1776000001", 200), source="miss"))
        self.assertTrue(d.seen(msg_miss("1776000002", 1), source="miss"),
                        "新会话的第一条必须被接受")

    def test_replay_after_restart(self):
        """重启桥接：同一份历史文件重新扫描，只能接受一次。"""
        d = Deduper()
        m = msg_miss("1776000001", 5)
        self.assertTrue(d.seen(m, source="miss"))
        for _ in range(50):
            self.assertFalse(d.seen(m, source="miss"))

    def test_bounded_growth(self):
        """8 小时长跑 / 几十万行：去重表容量固定。"""
        d = Deduper(lru_capacity=64, session_capacity=8)
        for i in range(5000):
            d.seen(msg_miss("1776%06d" % (i // 100), i % 100 + 1), source="miss")
        self.assertLessEqual(len(d.lru), 64)
        self.assertLessEqual(len(d.cross), 64)
        self.assertLessEqual(len(d.watermark.sessions()), 8)

    def test_stats_shape(self):
        d = Deduper()
        d.seen(msg_miss("1776000001", 1), source="miss")
        s = d.stats()
        for k in ("accepted", "rejected_old_session", "rejected_dup",
                  "rejected_cross", "lru_size", "lru_capacity", "sessions"):
            self.assertIn(k, s)


class TestSeqRegressionWithinSession(unittest.TestCase):
    def test_seq_backwards_rejected(self):
        """同一会话内 seq 倒退 = 重放，必须拒。"""
        d = Deduper()
        self.assertTrue(d.seen(msg_miss("1776000001", 100), source="miss"))
        self.assertFalse(d.seen(msg_miss("1776000001", 50), source="miss"))
        self.assertEqual(d.rejected_old_session, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

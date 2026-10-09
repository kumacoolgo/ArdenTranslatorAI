# -*- coding: utf-8 -*-
"""V11-01 协议解析测试。

覆盖规范第 3 节「协议 / 解析鲁棒性」两行，以及 V11-01 正文点名的全部用例：
    hello.world / Might. It adds defence stuff / The code is %2E / foo%7Cbar /
    a|b / 100%25 / 100% / 空消息 / 日文 / 俄文 / 中文 / 2. Newcomers /
    错误前缀 / 缺段 / 历史 5 段 / 两通道同一消息

判定：所有字段 roundtrip 与原始输入完全一致（Lua 会把换行换成空格，属已知例外）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (  # noqa: E402
    MARKER_CHATLOG, MARKER_MISS, MAX_TEXT_LEN, parse_payload, strip_miss_suffix,
    unesc,
)


# ---------------------------------------------------------------------------
# 复刻插件端的 esc()。**必须逐字对得上 Lua**，否则测试就成了自说自话。
# Lua:
#   s = gsub(s, "%%", "%%25")
#   s = gsub(s, "|",  "%%7C")
#   s = gsub(s, "%.", "%%2E")
#   s = gsub(s, "\r", " "); gsub(s, "\n", " ")
# ---------------------------------------------------------------------------
def lua_esc(s):
    s = str(s or "")
    s = s.replace("%", "%25")
    s = s.replace("|", "%7C")
    s = s.replace(".", "%2E")
    s = s.replace("\r", " ").replace("\n", " ")
    return s


SESSION = "1776000001"
SEQ = 7


def wire_miss(label, sender, text, session=SESSION, seq=SEQ):
    """通道 B 线上形态。注意分隔符也是 %7C。"""
    return (MARKER_MISS + "%7C" + str(session) + "%7C" + str(seq) + "%7C"
            + lua_esc(label) + "%7C" + lua_esc(sender) + "%7C" + lua_esc(text))


def wire_chatlog(label, sender, text, session=SESSION, seq=SEQ):
    return (MARKER_CHATLOG + "%7C" + str(session) + "%7C" + str(seq) + "%7C"
            + lua_esc(label) + "%7C" + lua_esc(sender) + "%7C" + lua_esc(text))


def wire_chatlog_v5(label, sender, text, seq=SEQ):
    """历史 5 段格式（没有 session），只在通道 A 的历史日志里出现。"""
    return (MARKER_CHATLOG + "%7C" + str(seq) + "%7C"
            + lua_esc(label) + "%7C" + lua_esc(sender) + "%7C" + lua_esc(text))


class TestRoundTrip(unittest.TestCase):
    """V11-01 点名必须测试的文本，逐条 roundtrip。"""

    CASES = [
        ("hello.world", "hello.world"),
        ("Might. It adds defence stuff", "Might. It adds defence stuff"),
        # 关键：字面 %2E 必须仍是 %2E，不能变成 "."
        ("The code is %2E", "The code is %2E"),
        ("foo%7Cbar", "foo%7Cbar"),
        ("a|b", "a|b"),
        ("100%25", "100%25"),
        ("100%", "100%"),
        ("", ""),
        ("LFG need a healer for Zul'Farrak, whisper me",
         "LFG need a healer for Zul'Farrak, whisper me"),
    ]

    def test_miss_roundtrip(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                m = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", text),
                                  source="miss")
                self.assertIsNotNone(m, "解析失败：%r" % text)
                self.assertEqual(m.text, expected)
                self.assertEqual(m.sender, "Bob")
                self.assertEqual(m.label, "CHAT_MSG_GUILD")
                self.assertEqual(m.version, 6)

    def test_chatlog_roundtrip(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                m = parse_payload(wire_chatlog("CHAT_MSG_WHISPER", "Ann", text),
                                  source="chatlog")
                self.assertIsNotNone(m)
                self.assertEqual(m.text, expected)

    def test_literal_marker_like_text(self):
        """原文里出现 %252E —— 也就是用户字面打了 "%2E"（% 被再编码一层）。

        这里最容易出的错是「解两次」：解一次得 %2E（正确），
        解两次得 "."（错误，用户原文被改写）。
        """
        m = parse_payload(wire_miss("CHAT_MSG_SAY", "X", "The code is %2E"),
                          source="miss")
        self.assertEqual(m.text, "The code is %2E")
        self.assertNotEqual(m.text, "The code is .")


class TestUnicode(unittest.TestCase):
    """日文 / 俄文 / 中文。"""

    def test_japanese(self):
        m = parse_payload(wire_miss("CHAT_MSG_GUILD", "太郎", "こんにちは、元気ですか？"),
                          source="miss")
        self.assertEqual(m.text, "こんにちは、元気ですか？")
        self.assertEqual(m.sender, "太郎")

    def test_russian(self):
        m = parse_payload(wire_miss("CHAT_MSG_GUILD", "Иван", "Привет, друг! Го в данж."),
                          source="miss")
        self.assertEqual(m.text, "Привет, друг! Го в данж.")

    def test_chinese(self):
        m = parse_payload(wire_miss("CHAT_MSG_SAY", "玩家甲", "这个Boss门是不是出bug了没开？"),
                          source="miss")
        self.assertEqual(m.text, "这个Boss门是不是出bug了没开？")

    def test_mixed_with_pipes_and_percent(self):
        """混合：WoW 链接 + 百分号 + 竖线，全都要活下来。"""
        text = "|Hitem:12345|h[测试物品]|h 100% a|b"
        m = parse_payload(wire_miss("CHAT_MSG_WHISPER", "张三", text), source="miss")
        self.assertIsNotNone(m)
        self.assertEqual(m.text, text)


class TestChannelLabel(unittest.TestCase):
    """公共频道名作为标签（不是 CHAT_MSG_*）。"""

    def test_dotted_channel_name(self):
        wire = wire_miss("2. Newcomers", "Testguy",
                         "Might. It adds defence stuff")
        m = parse_payload(wire, source="miss")
        self.assertIsNotNone(m, "带点号的频道名必须能解析（v1.0.14 的回归点）")
        self.assertEqual(m.label, "2. Newcomers")
        self.assertEqual(m.text, "Might. It adds defence stuff")
        self.assertTrue(m.is_channel)

    def test_cjk_channel_name(self):
        m = parse_payload(wire_miss("5. 世界", "玩家乙", "组队打本吗"), source="miss")
        self.assertEqual(m.label, "5. 世界")
        self.assertTrue(m.is_channel)

    def test_event_label_is_not_channel(self):
        m = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi"), source="miss")
        self.assertFalse(m.is_channel)


class TestRejects(unittest.TestCase):
    """坏输入只拒收，不抛异常、不崩线程。"""

    BAD = [
        None,
        "",
        "   ",
        "hello world",
        "ATAI",
        "ATAI3%7C1%7C2%7CCHAT_MSG_SAY%7CBob%7Chi",       # 未知 marker
        "XATAI2%7C1%7C2%7CCHAT_MSG_SAY%7CBob%7Chi",      # 前缀不干净
        "@ATAI1@nonsep%7C1%7C2%7CCHAT_MSG_SAY%7CBob%7Chi",
        MARKER_MISS,                                       # 只有前缀
        MARKER_MISS + "%7C",                               # 前缀 + 一个分隔符
        # 缺段：只有 4 段
        MARKER_MISS + "%7C1%7C2%7CCHAT_MSG_SAY",
        # session 不是数字
        MARKER_MISS + "%7Cabc%7C2%7CCHAT_MSG_SAY%7CBob%7Chi",
        # seq 不是数字
        MARKER_MISS + "%7C1%7Cxyz%7CCHAT_MSG_SAY%7CBob%7Chi",
        # label 为空
        MARKER_MISS + "%7C1%7C2%7C%7CBob%7Chi",
        # 超长（超过 MAX_WIRE_LEN）
        MARKER_MISS + "%7C1%7C2%7CCHAT_MSG_SAY%7CBob%7C" + ("x" * 9000),
    ]

    def test_rejects_cleanly(self):
        for raw in self.BAD:
            with self.subTest(raw=(raw[:60] if isinstance(raw, str) else raw)):
                self.assertIsNone(parse_payload(raw, source="miss"),
                                  "本应拒收：%r" % (raw,))

    def test_miss_requires_six(self):
        """通道 B **只认 6 段** —— 5 段必须丢掉，不能错位解析。"""
        self.assertIsNone(parse_payload(wire_chatlog_v5("CHAT_MSG_SAY", "B", "hi"),
                                        source="miss"))

    def test_chatlog_accepts_five(self):
        """通道 A 兼容历史 5 段。"""
        m = parse_payload(wire_chatlog_v5("CHAT_MSG_SAY", "B", "hi"),
                          source="chatlog")
        self.assertIsNotNone(m, "通道 A 必须兼容历史 5 段")
        self.assertEqual(m.version, 5)
        self.assertEqual(m.session, "")
        self.assertEqual(m.text, "hi")

    def test_text_too_long(self):
        raw = wire_miss("CHAT_MSG_SAY", "B", "x" * (MAX_TEXT_LEN + 10))
        self.assertIsNone(parse_payload(raw, source="miss"))

    def test_non_string(self):
        for v in (123, b"bytes", [], {}):
            self.assertIsNone(parse_payload(v, source="miss"))


class TestDedupeKey(unittest.TestCase):
    """去重主键：6 段用 (protocol, session, seq)；不同玩家同文不算重复。"""

    def test_same_session_seq_is_dup(self):
        a = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi"), source="miss")
        b = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi"), source="miss")
        self.assertEqual(a.dedupe_key, b.dedupe_key)

    def test_same_text_different_player_not_dup(self):
        """V11-04 明确要求：不同玩家说了同一句话，**不能**判成重复。

        真实插件里 seq 是**全局单调自增**的，所以两个不同玩家不可能拿到同一个
        seq —— 测试必须用真实的 seq 形态（连续递增），否则测的是不可能发生的
        状态。
        """
        a = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi", seq=100), source="miss")
        b = parse_payload(wire_miss("CHAT_MSG_GUILD", "Ann", "hi", seq=101), source="miss")
        self.assertNotEqual(a.dedupe_key, b.dedupe_key)

    def test_same_seq_different_session_not_dup(self):
        """两个会话（快速 /reload）可以有相同 seq，靠 session 区分。"""
        a = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi",
                                    session="1776000001", seq=1), source="miss")
        b = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi",
                                    session="1776000002", seq=1), source="miss")
        self.assertNotEqual(a.dedupe_key, b.dedupe_key)

    def test_same_text_different_seq_not_dup(self):
        a = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi", seq=1), source="miss")
        b = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi", seq=2), source="miss")
        self.assertNotEqual(a.dedupe_key, b.dedupe_key)

    def test_cross_channel_same_message_key_differs_by_protocol(self):
        """两通道同一事件：插件给的是**同一个** session+seq，但 protocol 不同。

        所以它们落在**不同**的 dedupe_key 上 —— 这正好让上层可以做「跨通道
        去重」：只要看到同 session+seq 在另一条通道已处理过，就不再重复显示。
        测试在这里固化「两条 key 形状一致、只差 protocol」这个事实。
        """
        a = parse_payload(wire_miss("CHAT_MSG_GUILD", "Bob", "hi"), source="miss")
        b = parse_payload(wire_chatlog("CHAT_MSG_GUILD", "Bob", "hi"), source="chatlog")
        self.assertEqual(a.session, b.session)
        self.assertEqual(a.seq, b.seq)
        self.assertEqual(a.dedupe_key[1:], b.dedupe_key[1:])
        self.assertNotEqual(a.dedupe_key[0], b.dedupe_key[0])

    def test_legacy_key_uses_content(self):
        a = parse_payload(wire_chatlog_v5("CHAT_MSG_SAY", "B", "hi", seq=1), source="chatlog")
        b = parse_payload(wire_chatlog_v5("CHAT_MSG_SAY", "B", "hi", seq=2), source="chatlog")
        # 5 段没有 session，只能靠内容 —— 同内容同玩家同频道算一条。
        self.assertEqual(a.dedupe_key, b.dedupe_key)


class TestOnePassDecode(unittest.TestCase):
    """直接钉住「只解一次」这个核心不变量。"""

    def test_pipe_in_body_does_not_split(self):
        """正文里的字面竖线不能把字段切错位。"""
        m = parse_payload(wire_miss("CHAT_MSG_SAY", "Bob", "a|b|c|d"), source="miss")
        self.assertEqual(m.text, "a|b|c|d")

    def test_percent_2e_survives_in_body(self):
        m = parse_payload(wire_miss("CHAT_MSG_SAY", "Bob", "%2E"), source="miss")
        self.assertEqual(m.text, "%2E")

    def test_percent_7c_literal_survives(self):
        """用户字面打 "%7C" 四个字符 —— 插件编码成 %257C，解码后必须是 %7C。"""
        m = parse_payload(wire_miss("CHAT_MSG_SAY", "Bob", "%7C"), source="miss")
        self.assertEqual(m.text, "%7C")

    def test_unesc_idempotence_boundary(self):
        """固化 unesc 的语义，防止有人「顺手」再加一层。"""
        self.assertEqual(unesc("%252E"), "%2E")   # 一次 -> %2E
        self.assertEqual(unesc(unesc("%252E")), ".")   # 两次 -> 错，所以只能解一次
        self.assertEqual(unesc("%7C"), "|")
        self.assertEqual(unesc("%257C"), "%7C")   # 字面 %7C
        self.assertEqual(unesc("100%25"), "100%")


class TestMissSuffix(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(strip_miss_suffix("ATAI2%7C1%7C2%7Cx.blp"), "ATAI2%7C1%7C2%7Cx")
        self.assertEqual(strip_miss_suffix("ATAI2%7C1%7C2%7Cx.tga"), "ATAI2%7C1%7C2%7Cx")
        self.assertEqual(strip_miss_suffix("ATAI2%7C1%7C2%7Cx.BLP"), "ATAI2%7C1%7C2%7Cx")
        # 正文里自己的 .blp 不受影响（只剥结尾）
        self.assertEqual(strip_miss_suffix("a.blp.b"), "a.blp.b")

    def test_both_suffixes_parse_identically(self):
        """客户端给一条 payload 记两行（.blp + .tga），去后缀后必须一致。"""
        base = "ATAI2%7C1776000001%7C5%7CCHAT_MSG_GUILD%7CBob%7Chi there"
        a = strip_miss_suffix(base + ".blp")
        b = strip_miss_suffix(base + ".tga")
        self.assertEqual(a, b)
        self.assertEqual(parse_payload(a, source="miss").dedupe_key,
                         parse_payload(b, source="miss").dedupe_key)


if __name__ == "__main__":
    unittest.main(verbosity=2)

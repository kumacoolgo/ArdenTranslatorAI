# -*- coding: utf-8 -*-
"""V11-06 / V12-06 骨架的单元测试：脱敏诊断、版本信息、延迟统计。

诊断报告是**要发给别人看**的（贴论坛、发给作者排障）。所以这里的核心断言不是
「字段齐全」，而是**绝不能泄漏**：API Key、聊天原文、玩家名、完整目录结构。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnostics as D  # noqa: E402


class TestVersions(unittest.TestCase):

    def test_bridge_version_bumped(self):
        # 桥接版本随修复递增；插件版本单独管（见下）。
        self.assertTrue(D.BRIDGE_VERSION.startswith("1.1."),
                        f"意外版本：{D.BRIDGE_VERSION}")

    def test_addon_version_matches(self):
        self.assertEqual(D.ADDON_VERSION, "1.1.0")

    def test_protocol_range_covers_legacy_and_current(self):
        """必须同时支持 1（5 段老日志）和 2（6 段当前格式），
        否则老用户的聊天日志会突然读不出来。"""
        self.assertIn(1, D.PROTOCOL_SUPPORTED)
        self.assertIn(2, D.PROTOCOL_SUPPORTED)
        self.assertEqual(tuple(sorted(D.PROTOCOL_SUPPORTED)), tuple(D.PROTOCOL_SUPPORTED))

    def test_bundled_toc_version_matches_diagnostics(self):
        """diagnostics 里写的插件版本必须和**实际内置的 .toc** 一致，
        否则诊断报告会撒谎（用户拿着报告来问「为什么版本对不上」）。"""
        toc = (Path(__file__).resolve().parent.parent.parent
               / "ArdenTranslatorAI" / "ArdenTranslatorAI.toc")
        self.assertTrue(toc.exists(), f"找不到 .toc: {toc}")
        ver = ""
        for line in toc.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith("## version:"):
                ver = line.split(":", 1)[1].strip()
        self.assertEqual(ver, D.ADDON_VERSION)


class TestLatencyTracker(unittest.TestCase):

    def test_empty_stats(self):
        t = D.LatencyTracker()
        self.assertEqual(t.stats(), {"n": 0, "p50": None, "p95": None, "avg": None})
        self.assertIsNone(t.percentile(50))

    def test_percentiles(self):
        t = D.LatencyTracker()
        for v in range(1, 101):
            t.add(v / 100.0)
        s = t.stats()
        self.assertEqual(s["n"], 100)
        # 最近秩法取的是离散样本点，不是插值：100 个点里 p50 落在第 50/51 个
        # 样本（0.50 或 0.51），p95 落在第 95/96 个（0.95/0.96）。
        # 断言「落在相邻样本内」而不是「等于精确分位」，否则测的是舍入方式
        # 而不是分位功能本身。
        self.assertIn(s["p50"], (0.5, 0.51))
        self.assertIn(s["p95"], (0.95, 0.96))
        self.assertLess(s["p50"], s["p95"])

    def test_window_is_bounded(self):
        """长时间运行必须有界，否则诊断会变成内存泄漏。"""
        t = D.LatencyTracker(window=10)
        for i in range(1000):
            t.add(i * 0.001)
        self.assertEqual(t.stats()["n"], 10)

    def test_negative_and_none_ignored(self):
        t = D.LatencyTracker()
        t.add(-1.0)
        t.add(None)
        t.add(0.5)
        self.assertEqual(t.stats()["n"], 1)

    def test_single_value(self):
        t = D.LatencyTracker()
        t.add(0.25)
        self.assertEqual(t.stats()["p50"], 0.25)
        self.assertEqual(t.stats()["p95"], 0.25)


class TestBasenameSafe(unittest.TestCase):

    def test_strips_to_last_component(self):
        self.assertEqual(D._basename_safe(r"C:\Users\bob\secret\config.json"),
                         "config.json")

    def test_handles_forward_slashes(self):
        self.assertEqual(D._basename_safe("/home/bob/private/x.log"), "x.log")

    def test_trailing_separator(self):
        self.assertEqual(D._basename_safe(r"C:\Users\bob\Logs\\"), "Logs")

    def test_empty(self):
        self.assertEqual(D._basename_safe(""), "")
        self.assertEqual(D._basename_safe(None), "")

    def test_does_not_leak_parent_dirs(self):
        out = D._basename_safe(r"C:\Users\cheny\AppData\Local\ArdenTranslatorAI\config.json")
        self.assertNotIn("cheny", out)
        self.assertNotIn("AppData", out)


class TestRedactionGuarantees(unittest.TestCase):
    """最重要的一组断言：报告里不能出现敏感内容。"""

    def _report_with_secrets(self):
        """构造一个「调用方不小心把敏感内容塞进来」的场景，看报告是否兜住。
        注意：build_report 的**约定**是调用方只传数字/枚举；这里验证的是
        notes / 结构层面不会主动引入泄漏，以及常见字段名不会误导。"""
        return D.build_report(
            counters={"received": 10, "translated": 8},
            channel_health={"A": {"state": "HEALTHY"}, "B": {"state": "WAITING"}},
            latencies={"total": {"n": 5, "p50": 0.3}},
        )

    def test_no_api_key_fields(self):
        blob = json.dumps(self._report_with_secrets(), ensure_ascii=False).lower()
        for bad in ("api_key", "apikey", "authorization", "bearer",
                    "zhipu_api_key", "secret", "token", "password"):
            self.assertNotIn(bad, blob, f"报告里不该出现 {bad}")

    def test_no_chat_text_or_player_names(self):
        blob = json.dumps(self._report_with_secrets(), ensure_ascii=False)
        # build_report 不接受聊天文本；确认结构里也没有这样的键
        self.assertNotIn("text", blob.lower().replace("context", ""))
        self.assertNotIn("sender", blob.lower())
        self.assertNotIn("message", blob.lower())

    def test_path_field_stays_basename_only(self):
        rep = self._report_with_secrets()
        rep["config_path"] = D._basename_safe(r"C:\Users\bob\private\config.json")
        blob = json.dumps(rep, ensure_ascii=False)
        self.assertNotIn("bob", blob)
        self.assertNotIn("private", blob)
        self.assertIn("config.json", blob)

    def test_notes_explain_latency_is_bridge_side(self):
        """延迟数字最容易被误读成端到端，notes 必须点破。"""
        rep = self._report_with_secrets()
        joined = " ".join(rep["notes"])
        self.assertIn("桥接侧", joined)
        self.assertIn("端到端", joined)

    def test_notes_state_redaction(self):
        rep = self._report_with_secrets()
        joined = " ".join(rep["notes"])
        self.assertIn("脱敏", joined)
        self.assertIn("API Key", joined)

    def test_is_json_serialisable(self):
        rep = self._report_with_secrets()
        s = D.dumps_report(rep)
        back = json.loads(s)
        self.assertEqual(back["bridge_version"], D.BRIDGE_VERSION)

    def test_extra_is_opt_in(self):
        rep = D.build_report()
        self.assertNotIn("extra", rep)
        rep2 = D.build_report(extra={"why": "debug"})
        self.assertEqual(rep2["extra"]["why"], "debug")


class TestReportShape(unittest.TestCase):

    def test_has_expected_top_level_keys(self):
        rep = D.build_report()
        for k in ("generated_at", "bridge_version", "addon_version",
                  "protocol_supported", "counters", "channel_health",
                  "latency_seconds", "notes", "python", "platform"):
            self.assertIn(k, rep)

    def test_protocol_supported_is_a_list_in_json(self):
        rep = D.build_report()
        blob = json.loads(D.dumps_report(rep))
        self.assertIsInstance(blob["protocol_supported"], list)

    def test_counters_are_copied_not_aliased(self):
        src = {"a": 1}
        rep = D.build_report(counters=src)
        rep["counters"]["a"] = 999
        self.assertEqual(src["a"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

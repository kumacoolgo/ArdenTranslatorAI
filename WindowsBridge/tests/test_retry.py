# -*- coding: utf-8 -*-
"""V12-05 骨架的单元测试：错误分层 / 重试 / 429。

规格书点名要覆盖的响应：
    401 / 429 / 500 / 超时 / 空 choices

全部用**打桩**（monkeypatch compat_translate），绝不真调 API —— 离线可跑、
不花钱、不受网络影响。测的是「什么该重试、什么该立刻放弃、什么该给出人话」。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location("atai", _HERE / "ArdenTranslatorAI.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class TestRetryableClassification(unittest.TestCase):
    """哪些错误值得重试 —— 判错的代价是双向的：
    把 401 当可重试 -> 每次都白等三倍超时；把 429 当永久 -> 直接丢消息。"""

    def test_rate_limit_is_retryable(self):
        self.assertTrue(m.is_retryable(RuntimeError("HTTP 429: rate limited")))

    def test_server_errors_are_retryable(self):
        for code in (500, 502, 503, 504):
            self.assertTrue(m.is_retryable(RuntimeError(f"HTTP {code}: oops")),
                            f"HTTP {code} 应该可重试")

    def test_timeouts_are_retryable(self):
        self.assertTrue(m.is_retryable(RuntimeError("The read operation timed out")))
        self.assertTrue(m.is_retryable(RuntimeError("connection timeout")))
        self.assertTrue(m.is_retryable(RuntimeError("Request TIMED OUT")))

    def test_auth_error_is_not_retryable(self):
        """401 是配置问题，重试三次只是让用户多等 36 秒然后看到同一个错误。"""
        self.assertFalse(m.is_retryable(RuntimeError("HTTP 401: invalid api key")))

    def test_bad_request_is_not_retryable(self):
        self.assertFalse(m.is_retryable(RuntimeError("HTTP 400: bad model")))

    def test_missing_key_is_not_retryable(self):
        self.assertFalse(m.is_retryable(RuntimeError("未设置 ZHIPU_API_KEY")))

    def test_not_found_is_not_retryable(self):
        self.assertFalse(m.is_retryable(RuntimeError("HTTP 404: no such model")))


class _FakeApp:
    """只带 call_provider 需要的最小状态，避免起真的 App（会开线程、读配置）。"""

    def __init__(self, attempts=3, backoff=0.0):
        self.cfg = {"translation": {
            "request_timeout_seconds": 1,
            "retry_attempts": attempts,
            "retry_backoff_seconds": backoff,
        }}


def _make_app(attempts=3, backoff=0.0):
    app = _FakeApp(attempts=attempts, backoff=backoff)
    app.call_provider = m.App.call_provider.__get__(app, _FakeApp)
    return app


class TestCallProviderRetry(unittest.TestCase):

    def setUp(self):
        self._orig = m.compat_translate

    def tearDown(self):
        m.compat_translate = self._orig

    def _patch(self, fn):
        m.compat_translate = fn

    def test_retries_429_then_succeeds(self):
        hits = []
        def flaky(*a, **k):
            hits.append(1)
            if len(hits) < 3:
                raise RuntimeError("HTTP 429: too many requests")
            return "译文"
        self._patch(flaky)
        app = _make_app(attempts=3)
        out = app.call_provider("zhipu", "glm", "k", "https://x/v1", None, None, "hi")
        self.assertEqual(out, "译文")
        self.assertEqual(len(hits), 3)

    def test_gives_up_after_attempts(self):
        hits = []
        def always_429(*a, **k):
            hits.append(1)
            raise RuntimeError("HTTP 429: rate limited")
        self._patch(always_429)
        app = _make_app(attempts=3)
        with self.assertRaises(RuntimeError) as cm:
            app.call_provider("zhipu", "glm", "k", "https://x/v1", None, None, "hi")
        self.assertIn("429", str(cm.exception))
        self.assertEqual(len(hits), 3, "应恰好尝试 3 次")

    def test_401_is_not_retried(self):
        """关键：401 必须**一次就放弃**，不能白等三次。"""
        hits = []
        def auth_fail(*a, **k):
            hits.append(1)
            raise RuntimeError("HTTP 401: invalid api key")
        self._patch(auth_fail)
        app = _make_app(attempts=3)
        with self.assertRaises(RuntimeError):
            app.call_provider("zhipu", "glm", "bad", "https://x/v1", None, None, "hi")
        self.assertEqual(len(hits), 1, "401 不该重试")

    def test_500_retried_then_raises(self):
        hits = []
        def boom(*a, **k):
            hits.append(1)
            raise RuntimeError("HTTP 500: internal error")
        self._patch(boom)
        app = _make_app(attempts=2)
        with self.assertRaises(RuntimeError):
            app.call_provider("zhipu", "glm", "k", "https://x/v1", None, None, "hi")
        self.assertEqual(len(hits), 2)

    def test_timeout_retried(self):
        hits = []
        def slow(*a, **k):
            hits.append(1)
            raise RuntimeError("The read operation timed out")
        self._patch(slow)
        app = _make_app(attempts=2)
        with self.assertRaises(RuntimeError):
            app.call_provider("zhipu", "glm", "k", "https://x/v1", None, None, "hi")
        self.assertEqual(len(hits), 2)

    def test_missing_key_fails_without_network(self):
        """没有 Key 且不是本地端点 -> 应立刻抛「未设置」，不发起任何请求。"""
        called = []
        self._patch(lambda *a, **k: called.append(1) or "x")
        app = _make_app(attempts=3)
        with self.assertRaises(RuntimeError) as cm:
            app.call_provider("zhipu", "glm", "", "https://open.bigmodel.cn/api/paas/v4",
                              None, None, "hi")
        self.assertIn("未设置", str(cm.exception))
        self.assertEqual(called, [], "缺 Key 时不该发起请求")


class TestEmptyChoices(unittest.TestCase):
    """空 choices / 只有 reasoning_content —— 这两条是最难查的静默失败。"""

    def setUp(self):
        self._orig = m.http_json

    def tearDown(self):
        m.http_json = self._orig

    def _patch_http(self, obj):
        m.http_json = lambda url, headers, payload, timeout: obj

    def test_empty_choices_raises_with_diagnostic(self):
        self._patch_http({"choices": []})
        with self.assertRaises(RuntimeError) as cm:
            m.compat_translate("k", "glm", "hi", 5, "https://x/v1")
        self.assertIn("choices", str(cm.exception))

    def test_reasoning_only_reply_explains_itself(self):
        """推理模型把 token 全花在 reasoning_content 上、content 为空时，
        报错必须点破「是 token 预算被思考吃了」，否则用户只会看到「没有文本」。"""
        self._patch_http({"choices": [{
            "finish_reason": "length",
            "message": {"content": "", "reasoning_content": "让我想想……"},
        }]})
        with self.assertRaises(RuntimeError) as cm:
            m.compat_translate("k", "glm-4.7-flash", "hi", 5, "https://x/v1")
        self.assertIn("思考", str(cm.exception))

    def test_empty_content_without_reasoning(self):
        self._patch_http({"choices": [{"message": {"content": ""}}]})
        with self.assertRaises(RuntimeError):
            m.compat_translate("k", "glm", "hi", 5, "https://x/v1")

    def test_missing_message_key(self):
        self._patch_http({"choices": [{}]})
        with self.assertRaises(RuntimeError):
            m.compat_translate("k", "glm", "hi", 5, "https://x/v1")

    def test_good_reply_is_stripped(self):
        self._patch_http({"choices": [{"message": {"content": "  你好  \n"}}]})
        self.assertEqual(m.compat_translate("k", "glm", "hi", 5, "https://x/v1"), "你好")

    def test_no_base_url_raises_before_request(self):
        called = []
        m.http_json = lambda *a, **k: called.append(1) or {}
        with self.assertRaises(RuntimeError) as cm:
            m.compat_translate("k", "glm", "hi", 5, "")
        self.assertIn("base_url", str(cm.exception))
        self.assertEqual(called, [])

    def test_no_model_raises_before_request(self):
        called = []
        m.http_json = lambda *a, **k: called.append(1) or {}
        with self.assertRaises(RuntimeError) as cm:
            m.compat_translate("k", "", "hi", 5, "https://x/v1")
        self.assertIn("模型名", str(cm.exception))
        self.assertEqual(called, [])


class TestHttpStatusMapping(unittest.TestCase):
    """http_json 把 HTTPError 折叠成 `HTTP <code>: <body>` —— 分类就靠这个前缀。"""

    def test_http_error_text_carries_the_code(self):
        import urllib.error
        orig_urlopen = m.request.urlopen
        try:
            def raise_401(req, timeout=None):
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", {}, None)
            # HTTPError.read() 需要 body；用一个最小可读对象
            class _Body:
                def read(self):
                    return b'{"error":"bad key"}'
            def raise_401b(req, timeout=None):
                e = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
                e.read = lambda: b'{"error":"bad key"}'
                raise e
            m.request.urlopen = raise_401b
            with self.assertRaises(RuntimeError) as cm:
                m.http_json("https://x/v1/chat/completions", {}, {}, 5)
            self.assertIn("HTTP 401", str(cm.exception))
            self.assertIn("bad key", str(cm.exception))
        finally:
            m.request.urlopen = orig_urlopen

    def test_network_error_becomes_runtimeerror(self):
        orig_urlopen = m.request.urlopen
        try:
            def boom(req, timeout=None):
                raise OSError("connection refused")
            m.request.urlopen = boom
            with self.assertRaises(RuntimeError) as cm:
                m.http_json("https://x/v1/chat/completions", {}, {}, 5)
            self.assertIn("connection refused", str(cm.exception))
        finally:
            m.request.urlopen = orig_urlopen


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""V11-03 缓存 key 版本化 + singleflight 测试。

规范点名必须测：
  同文本 20 次同时到达 -> 1 次翻译请求、20 条独立显示；
  更换 target 语言 / 术语表 / 提示词版本后必须重新请求；
  API 失败后下一次可重试。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cache import (  # noqa: E402
    CACHE_KEY_VERSION, SingleFlight, TranslationCache, cache_key,
    normalize_base_url, normalize_text, terms_fingerprint,
)


class TestKeyComposition(unittest.TestCase):
    BASE = dict(provider="zhipu", base_url="https://x/v4", model="glm-4-flash",
                target="Simplified Chinese", text="hello")

    def k(self, **over):
        d = dict(self.BASE)
        d.update(over)
        return cache_key(**d)

    def test_identical_inputs_same_key(self):
        self.assertEqual(self.k(), self.k())

    def test_target_changes_key(self):
        """V11-03 第 2 条：改目标语言必须重新翻译。"""
        self.assertNotEqual(self.k(), self.k(target="Japanese"))

    def test_model_changes_key(self):
        self.assertNotEqual(self.k(), self.k(model="glm-4.5-flash"))

    def test_provider_changes_key(self):
        self.assertNotEqual(self.k(), self.k(provider="other"))

    def test_base_url_changes_key(self):
        self.assertNotEqual(self.k(), self.k(base_url="https://y/v4"))

    def test_base_url_trailing_slash_normalized(self):
        """尾斜杠不该生成两个 key。"""
        self.assertEqual(self.k(base_url="https://x/v4/"),
                         self.k(base_url="https://x/v4"))

    def test_prompt_version_changes_key(self):
        self.assertNotEqual(self.k(prompt_version=1), self.k(prompt_version=2))

    def test_terms_changes_key(self):
        """V11-03 第 2 条：术语表变了必须重新翻译。"""
        self.assertNotEqual(self.k(terms=["ArdenWoW"]),
                            self.k(terms=["ArdenWoW", "PvP"]))
        self.assertNotEqual(self.k(terms=["ArdenWoW"]), self.k(terms=None))

    def test_terms_order_irrelevant(self):
        self.assertEqual(self.k(terms=["a", "b"]), self.k(terms=["b", "a"]))

    def test_text_changes_key(self):
        self.assertNotEqual(self.k(text="hello"), self.k(text="hello!"))

    def test_key_version_is_embedded(self):
        self.assertEqual(CACHE_KEY_VERSION, 2)
        # key 是 sha256 十六进制，长度固定。
        self.assertEqual(len(self.k()), 64)


class TestNoCaseFolding(unittest.TestCase):
    """V11-03 第 4 条：不做全文小写。"""

    def test_case_preserved(self):
        self.assertEqual(normalize_text("  LFG   Need  A "), "LFG Need A")

    def test_case_matters_for_key(self):
        a = cache_key(provider="p", base_url="u", model="m", target="t",
                      text="ArdenWoW")
        b = cache_key(provider="p", base_url="u", model="m", target="t",
                      text="ardenwow")
        self.assertNotEqual(a, b, "大小写不同的术语不能被合并")

    def test_whitespace_folded(self):
        a = cache_key(provider="p", base_url="u", model="m", target="t",
                      text="a  b")
        b = cache_key(provider="p", base_url="u", model="m", target="t",
                      text="a b")
        self.assertEqual(a, b, "连续空白折叠后应命中同一 key")


class TestCacheDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.sqlite3"
        self.c = TranslationCache(self.path)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_put_get(self):
        k = cache_key(provider="p", base_url="u", model="m", target="t", text="hi")
        self.assertIsNone(self.c.get(k))
        self.c.put(k, "你好")
        self.assertEqual(self.c.get(k), "你好")

    def test_old_table_preserved(self):
        """老库里的行必须还在（规范：不要破坏老用户数据库）。"""
        import sqlite3
        k_old = "deadbeef" * 8
        with self.c.db:
            self.c.db.execute(
                "INSERT OR REPLACE INTO cache(k,translation,created) VALUES(?,?,?)",
                (k_old, "老译文", 1))
        self.c.close()
        c2 = TranslationCache(self.path)
        try:
            self.assertEqual(c2.get(k_old), "老译文")
        finally:
            c2.close()

    def test_empty_translation_not_cached(self):
        k = "x" * 64
        self.c.put(k, "")
        self.assertIsNone(self.c.get(k))

    def test_prune_bounded(self):
        c = TranslationCache(self.path, max_rows=10)
        for i in range(40):
            c.put("%064d" % i, "t%d" % i)
        removed = c.prune()
        self.assertGreater(removed, 0)
        n = c.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        self.assertLessEqual(n, 40)
        c.close()

    def test_db_error_does_not_raise(self):
        """DB 出问题只计数，不把异常抛给翻译流程。"""
        self.c.close()          # 关掉底层连接，后续访问会抛
        self.assertIsNone(self.c.get("k" * 64))
        self.c.put("k" * 64, "v")     # 不应抛
        self.assertGreater(self.c.errors, 0)


class TestSingleFlight(unittest.TestCase):
    def test_20_concurrent_same_text_one_request(self):
        """规范点名：同文本 20 次同时到达 -> 1 次请求、20 个结果。"""
        sf = SingleFlight()
        calls = {"n": 0}
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def fn():
            calls["n"] += 1
            time.sleep(0.15)          # 模拟网络
            return "translated"

        def worker():
            barrier.wait()            # 20 个线程同时冲
            v, owner = sf.run("samekey", fn)
            with lock:
                results.append(v)

        ts = [threading.Thread(target=worker) for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        self.assertEqual(calls["n"], 1, "应该只发 1 次请求，实际 %d" % calls["n"])
        self.assertEqual(len(results), 20, "20 条消息各自都要拿到结果")
        self.assertTrue(all(r == "translated" for r in results))
        self.assertEqual(sf.started, 1)
        self.assertEqual(sf.merged, 19)

    def test_different_keys_not_merged(self):
        sf = SingleFlight()
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "x"
        sf.run("a", fn)
        sf.run("b", fn)
        self.assertEqual(calls["n"], 2)

    def test_error_shared_to_all_waiters(self):
        """异常必须共享给所有等待者，且 inflight 要被释放。"""
        sf = SingleFlight()
        errs = []
        lock = threading.Lock()
        started = threading.Event()

        def fn():
            started.set()
            time.sleep(0.1)
            raise RuntimeError("HTTP 500")

        def owner():
            try:
                sf.run("k", fn)
            except Exception as e:
                with lock:
                    errs.append(("owner", str(e)))

        def waiter():
            started.wait()
            try:
                sf.run("k", fn)
            except Exception as e:
                with lock:
                    errs.append(("waiter", str(e)))

        ts = [threading.Thread(target=owner)] + \
             [threading.Thread(target=waiter) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(errs), 4, "所有等待者都应拿到异常")
        # inflight 已释放
        self.assertEqual(sf.pending, 0)

    def test_next_call_after_failure_succeeds(self):
        """规范点名：API 失败后下一次可重试。"""
        sf = SingleFlight()
        state = {"fail": True}

        def fn():
            if state["fail"]:
                raise RuntimeError("boom")
            return "ok"

        with self.assertRaises(RuntimeError):
            sf.run("k", fn)
        state["fail"] = False
        v, owner = sf.run("k", fn)
        self.assertEqual(v, "ok")
        self.assertTrue(owner)
        self.assertEqual(sf.pending, 0)

    def test_no_permanent_hang(self):
        """失败后不能留下永久挂起的 flight。"""
        sf = SingleFlight()

        def boom():
            raise ValueError("x")

        for _ in range(5):
            with self.assertRaises(ValueError):
                sf.run("k", boom)
        self.assertEqual(sf.pending, 0)


class TestHelpers(unittest.TestCase):
    def test_terms_fingerprint_stable(self):
        self.assertEqual(terms_fingerprint(["a", "b"]), terms_fingerprint(["b", "a"]))
        self.assertNotEqual(terms_fingerprint(["a"]), terms_fingerprint(["a", "b"]))

    def test_normalize_base_url(self):
        self.assertEqual(normalize_base_url("HTTPS://X/V4/"), "https://x/v4")
        self.assertEqual(normalize_base_url(""), "-")


if __name__ == "__main__":
    unittest.main(verbosity=2)

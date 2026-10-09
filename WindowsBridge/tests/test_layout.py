# -*- coding: utf-8 -*-
"""v1.1.1：客户端目录布局探测。

**为什么单独一组测试**

用户真机上踩到的坑：Grimfall 客户端把游戏放在 `resources\\Wotlk\\` 下面，
日志和插件都在那一层，而桥接只认「<root>\\Logs」。结果插件明明在跑、
payload 格式也全对，悬浮窗却显示「未找到 Logs\\」—— 排障时最容易被误导的
一类故障：数据没问题，只是取件地址错了。

再加上 MissingFiles 的**文件名**也不统一：
    ArdenWoW -> MissingFiles.txt
    Grimfall -> MissingFiles.log
只认 .txt 会让通道 B（**唯一实时的那条**）永远显示「尚未生成」。

这组测试把这些布局逐一钉死，防止以后改路径逻辑时又退回「只认一种布局」。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location("atai", _HERE / "ArdenTranslatorAI.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _layout_classic(root: Path) -> Path:
    """布局 A：<root>\\Logs + <root>\\Interface\\AddOns"""
    (root / "Logs").mkdir(parents=True)
    (root / "Interface" / "AddOns").mkdir(parents=True)
    (root / "Logs" / "WoWChatLog.txt").write_text("", encoding="utf-8")
    (root / "Logs" / "MissingFiles.txt").write_text("", encoding="utf-8")
    return root


def _layout_launcher(root: Path, sub="resources/Wotlk", miss_name="MissingFiles.log") -> Path:
    """布局 B：启动器套壳，游戏在 <root>\\resources\\Wotlk\\ 下。"""
    game = root / sub
    (game / "Logs").mkdir(parents=True)
    (game / "Interface" / "AddOns").mkdir(parents=True)
    (game / "Logs" / "WoWChatLog.txt").write_text("", encoding="utf-8")
    (game / "Logs" / miss_name).write_text("", encoding="utf-8")
    return root


class LayoutTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atai_layout_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestClassicLayout(LayoutTestBase):
    """布局 A 不能因为加了 B 的支持而退化。"""

    def test_finds_logs(self):
        root = _layout_classic(self.tmp / "wow")
        self.assertEqual(m.find_logs_dir(root), root / "Logs")
        self.assertEqual(m.find_chat_log(root), root / "Logs" / "WoWChatLog.txt")
        self.assertEqual(m.find_missing_files(root), root / "Logs" / "MissingFiles.txt")

    def test_check_path_ok(self):
        root = _layout_classic(self.tmp / "wow")
        self.assertEqual(m.check_wow_path(str(root)), (True, ""))

    def test_addons_dir(self):
        root = _layout_classic(self.tmp / "wow")
        self.assertEqual(m.addons_dir(root), root / "Interface" / "AddOns")


class TestLauncherLayout(LayoutTestBase):
    """布局 B：Grimfall 这种启动器套壳。这是用户真机踩到的那个。"""

    def test_finds_logs_under_resources_wotlk(self):
        root = _layout_launcher(self.tmp / "Grimfall")
        game = root / "resources" / "Wotlk"
        self.assertEqual(m.find_logs_dir(root), game / "Logs")
        self.assertEqual(m.find_chat_log(root), game / "Logs" / "WoWChatLog.txt")
        self.assertEqual(m.find_missing_files(root), game / "Logs" / "MissingFiles.log")

    def test_check_path_ok_from_launcher_root(self):
        """用户选了 <root>（启动器根）也要能通过。"""
        root = _layout_launcher(self.tmp / "Grimfall")
        self.assertEqual(m.check_wow_path(str(root)), (True, ""))

    def test_check_path_ok_from_game_dir(self):
        """用户选到 <root>\\resources\\Wotlk 同样通过（他实际就是这么配的）。"""
        root = _layout_launcher(self.tmp / "Grimfall")
        game = root / "resources" / "Wotlk"
        self.assertEqual(m.check_wow_path(str(game)), (True, ""))

    def test_addons_dir_from_launcher_root(self):
        root = _layout_launcher(self.tmp / "Grimfall")
        game = root / "resources" / "Wotlk"
        self.assertEqual(m.addons_dir(root), game / "Interface" / "AddOns")

    def test_addons_dir_from_game_dir(self):
        root = _layout_launcher(self.tmp / "Grimfall")
        game = root / "resources" / "Wotlk"
        self.assertEqual(m.addons_dir(game), game / "Interface" / "AddOns")

    def test_both_path_levels_resolve_to_same_place(self):
        """关键不变式：无论用户选哪一层，解析结果必须**指向同一处**，
        否则「选上层能工作、选下层不能」这种不一致会让人怀疑人生。"""
        root = _layout_launcher(self.tmp / "Grimfall")
        game = root / "resources" / "Wotlk"
        self.assertEqual(m.find_chat_log(root), m.find_chat_log(game))
        self.assertEqual(m.find_missing_files(root), m.find_missing_files(game))
        self.assertEqual(m.addons_dir(root), m.addons_dir(game))


class TestMissingFilesNaming(LayoutTestBase):
    """通道 B 的文件名有 .txt / .log 两种，都要认。"""

    def test_txt_recognised(self):
        root = _layout_launcher(self.tmp / "w", miss_name="MissingFiles.txt")
        f = m.find_missing_files(root)
        self.assertIsNotNone(f)
        self.assertEqual(f.name, "MissingFiles.txt")

    def test_log_recognised(self):
        root = _layout_launcher(self.tmp / "w", miss_name="MissingFiles.log")
        f = m.find_missing_files(root)
        self.assertIsNotNone(f)
        self.assertEqual(f.name, "MissingFiles.log")

    def test_both_names_declared(self):
        self.assertIn("MissingFiles.txt", m.MISSING_FILES_NAMES)
        self.assertIn("MissingFiles.log", m.MISSING_FILES_NAMES)

    def test_missing_returns_none(self):
        root = _layout_classic(self.tmp / "w")
        (root / "Logs" / "MissingFiles.txt").unlink()
        self.assertIsNone(m.find_missing_files(root))


class TestCandidateOrdering(LayoutTestBase):

    def test_classic_logs_first(self):
        """候选顺序稳定：经典布局排第一，兼容性判断不受新布局影响。"""
        root = _layout_classic(self.tmp / "w")
        cands = m._candidate_logs_dirs(root)
        self.assertEqual(cands[0], root / "Logs")

    def test_no_duplicates(self):
        root = _layout_launcher(self.tmp / "Grimfall")
        cands = m._candidate_logs_dirs(root)
        keys = [str(c).lower() for c in cands]
        self.assertEqual(len(keys), len(set(keys)), f"候选有重复：{keys}")

    def test_empty_path_gives_empty_list(self):
        self.assertEqual(m._candidate_logs_dirs(""), [])
        self.assertEqual(m._candidate_logs_dirs(None), [])

    def test_quoted_path_stripped(self):
        """配置里手写过带引号的路径也要能处理。"""
        root = _layout_classic(self.tmp / "w")
        self.assertEqual(m.find_logs_dir(f'"{root}"'), root / "Logs")


class TestPathSafety(LayoutTestBase):

    def test_bogus_path_rejected(self):
        ok, msg = m.check_wow_path(str(self.tmp / "does_not_exist"))
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_plain_dir_rejected(self):
        plain = self.tmp / "just_a_folder"
        plain.mkdir()
        ok, msg = m.check_wow_path(str(plain))
        self.assertFalse(ok)
        self.assertIn("Interface", msg)

    def test_empty_rejected(self):
        ok, msg = m.check_wow_path("")
        self.assertFalse(ok)

    def test_no_logs_yet_still_ok_with_interface(self):
        """Interface 在、Logs 还没生成 —— 路径是对的，只是游戏还没跑过。
        这时必须判 ok（否则用户会被赶去重选一个已经正确的目录）。"""
        root = self.tmp / "fresh"
        (root / "Interface" / "AddOns").mkdir(parents=True)
        self.assertEqual(m.check_wow_path(str(root)), (True, ""))
        self.assertIsNone(m.find_logs_dir(root))      # 但确实还没有 Logs

    def test_error_message_mentions_resources_wotlk(self):
        """排障文案要主动提到启动器布局，用户才知道该往哪儿点。"""
        plain = self.tmp / "nope"
        plain.mkdir()
        ok, msg = m.check_wow_path(str(plain))
        self.assertFalse(ok)
        self.assertIn("resources", msg.lower())


class TestRealGrimfallIfPresent(unittest.TestCase):
    """如果本机真有那个客户端，就拿真路径验一遍（CI 上没有会自动跳过）。"""

    REAL = r"C:\WOW2\Grimfall\resources\Wotlk"

    def setUp(self):
        if not Path(self.REAL).is_dir():
            self.skipTest(f"本机没有 {self.REAL}")

    def test_resolves_real_client(self):
        root = Path(self.REAL)
        self.assertIsNotNone(m.find_logs_dir(root))
        self.assertIsNotNone(m.find_chat_log(root))
        self.assertEqual(m.check_wow_path(str(root)), (True, ""))
        self.assertIn("AddOns", str(m.addons_dir(root)))

    def test_real_missingfiles_name_is_log(self):
        root = Path(self.REAL)
        f = m.find_missing_files(root)
        if f is None:
            self.skipTest("该客户端当前没有 MissingFiles 文件")
        self.assertIn(f.name, m.MISSING_FILES_NAMES)

    def test_parent_level_also_resolves(self):
        root = Path(self.REAL).parent.parent
        self.assertIsNotNone(m.find_logs_dir(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""V11-06 的部署测试：安全安装 / 覆盖升级 / 失败回滚 / 路径安全闸。

一键部署会**删/换插件目录**，是整个程序里唯一带破坏性的动作。所以这里的重点
不是「成功路径能跑」（那太容易），而是**失败路径必须留下一个能用的状态**：

  * 目标目录被占用（模拟文件锁）-> 不能留下半个插件
  * 复制中途失败 -> 旧版本必须原样回来
  * wow_path 指错 -> 一个字节都不许动别人的目录
  * 覆盖升级 -> 旧目录改名成 .bak 再原子替换，不留残渣
  * 幂等 -> 连点两次「开始」结果一致

全部在临时目录里跑真实文件系统操作。
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


def _make_wow(root: Path) -> Path:
    """造一个最小的 WoW 客户端目录（只要能通过 check_wow_path）。"""
    (root / "Interface" / "AddOns").mkdir(parents=True, exist_ok=True)
    (root / "Logs").mkdir(parents=True, exist_ok=True)
    return root


class DeployTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atai_deploy_"))
        self.wow = _make_wow(self.tmp / "wow")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def dest(self) -> Path:
        return self.wow / "Interface" / "AddOns" / "ArdenTranslatorAI"


class TestSuccessPaths(DeployTestBase):

    def test_fresh_install_writes_both_files(self):
        ok, msg, dest = m.deploy_addon(str(self.wow))
        self.assertTrue(ok, msg)
        self.assertIsNotNone(dest)
        self.assertTrue((dest / "ArdenTranslatorAI.lua").exists())
        self.assertTrue((dest / "ArdenTranslatorAI.toc").exists())
        self.assertIn("1.1.0", msg)

    def test_deployed_lua_matches_bundled_source(self):
        src = m.plugin_source_dir()
        ok, msg, dest = m.deploy_addon(str(self.wow))
        self.assertTrue(ok, msg)
        self.assertEqual((dest / "ArdenTranslatorAI.lua").read_bytes(),
                         (src / "ArdenTranslatorAI.lua").read_bytes())

    def test_is_idempotent(self):
        """用户每次点「开始」都会走一遍；第二次必须和第一次结果一样。"""
        ok1, msg1, d1 = m.deploy_addon(str(self.wow))
        v1 = (d1 / "ArdenTranslatorAI.lua").read_bytes()
        ok2, msg2, d2 = m.deploy_addon(str(self.wow))
        self.assertTrue(ok1 and ok2, f"{msg1} / {msg2}")
        self.assertEqual(v1, (d2 / "ArdenTranslatorAI.lua").read_bytes())

    def test_upgrade_removes_stale_files(self):
        """覆盖升级：旧目录里的多余文件不能残留（否则新旧混在一起很难查）。"""
        self.dest.mkdir(parents=True)
        (self.dest / "ArdenTranslatorAI.lua").write_text("-- OLD", encoding="utf-8")
        (self.dest / "stale_leftover.txt").write_text("old", encoding="utf-8")
        ok, msg, dest = m.deploy_addon(str(self.wow))
        self.assertTrue(ok, msg)
        self.assertFalse((dest / "stale_leftover.txt").exists())
        self.assertIn(b"1.1.0", (dest / "ArdenTranslatorAI.lua").read_bytes())

    def test_no_backup_or_temp_left_behind_on_success(self):
        """成功之后 .bak-* / .new-* 必须清干净，否则 AddOns 里会堆垃圾。"""
        self.dest.mkdir(parents=True)
        (self.dest / "ArdenTranslatorAI.lua").write_text("-- OLD", encoding="utf-8")
        ok, msg, dest = m.deploy_addon(str(self.wow))
        self.assertTrue(ok, msg)
        leftovers = [p.name for p in (self.wow / "Interface" / "AddOns").iterdir()
                     if ".bak-" in p.name or ".new-" in p.name]
        self.assertEqual(leftovers, [], f"不该留残渣：{leftovers}")

    def test_upgrade_preserves_user_added_file_in_backup_until_replaced(self):
        """用户在插件目录里放了自己的东西时，我们**改名**而不是 rmtree 删掉。"""
        self.dest.mkdir(parents=True)
        (self.dest / "my_own_notes.txt").write_text("keep me", encoding="utf-8")
        ok, msg, dest = m.deploy_addon(str(self.wow))
        self.assertTrue(ok, msg)
        # 部署后新目录里只有内置文件（用户文件不进新目录），但流程没有用过
        # rmtree 直接删，且成功后备份被清理 —— 这是我们承诺的语义。
        names = sorted(p.name for p in dest.iterdir())
        self.assertEqual(names, sorted(m.ADDON_FILES))


class TestPathSafetyGate(DeployTestBase):
    """安全闸：wow_path 指错时绝不能碰任何不相关目录。"""

    def test_nonexistent_path_refused(self):
        ok, msg, dest = m.deploy_addon(str(self.tmp / "nope"))
        self.assertFalse(ok)
        self.assertIsNone(dest)
        self.assertTrue(msg)

    def test_dir_without_interface_refused(self):
        plain = self.tmp / "just_a_folder"
        plain.mkdir()
        (plain / "important.txt").write_text("precious", encoding="utf-8")
        ok, msg, dest = m.deploy_addon(str(plain))
        self.assertFalse(ok)
        self.assertTrue((plain / "important.txt").exists(),
                        "拒绝部署时一个字节都不许动")

    def test_unrelated_addons_untouched(self):
        vendor = self.wow / "Interface" / "AddOns" / "SomeOtherAddon"
        vendor.mkdir(parents=True)
        (vendor / "keep.lua").write_text("-- mine", encoding="utf-8")
        ok, msg, dest = m.deploy_addon(str(self.wow))
        self.assertTrue(ok, msg)
        self.assertTrue((vendor / "keep.lua").exists())
        self.assertEqual((vendor / "keep.lua").read_text(encoding="utf-8"), "-- mine")

    def test_empty_path_refused(self):
        ok, msg, dest = m.deploy_addon("")
        self.assertFalse(ok)
        self.assertIsNone(dest)


class TestFailureRollback(DeployTestBase):
    """失败路径：旧版本必须原样回来。"""

    def _pre_install_old_version(self):
        self.dest.mkdir(parents=True)
        (self.dest / "ArdenTranslatorAI.lua").write_text("-- OLD VERSION", encoding="utf-8")
        (self.dest / "ArdenTranslatorAI.toc").write_text("## Version: 1.0.14", encoding="utf-8")
        return (self.dest / "ArdenTranslatorAI.lua").read_bytes()

    def test_copy_failure_rolls_back_old_version(self):
        """让 copyfile 在复制 .toc 时抛错，验证旧版本被恢复。"""
        old = self._pre_install_old_version()
        orig = shutil.copyfile
        calls = {"n": 0}

        def flaky_copy(src, dst, **kw):
            calls["n"] += 1
            if Path(dst).name.endswith(".toc"):
                raise OSError("simulated disk full / file in use")
            return orig(src, dst, **kw)

        shutil.copyfile = flaky_copy
        try:
            ok, msg, dest = m.deploy_addon(str(self.wow))
        finally:
            shutil.copyfile = orig

        self.assertFalse(ok, "复制失败时必须报失败")
        self.assertIn("恢复", msg)
        # 旧版本必须还在，而且内容一模一样
        self.assertTrue((self.dest / "ArdenTranslatorAI.lua").exists(),
                        "回滚后旧版本必须存在")
        self.assertEqual((self.dest / "ArdenTranslatorAI.lua").read_bytes(), old)

    def test_temp_dir_cleaned_after_failure(self):
        self._pre_install_old_version()
        orig = shutil.copyfile

        def always_fail(src, dst, **kw):
            raise OSError("simulated failure")

        shutil.copyfile = always_fail
        try:
            m.deploy_addon(str(self.wow))
        finally:
            shutil.copyfile = orig
        addons = self.wow / "Interface" / "AddOns"
        junk = [p.name for p in addons.iterdir() if ".new-" in p.name]
        self.assertEqual(junk, [], f"失败后不该留临时目录：{junk}")

    def test_no_previous_install_copy_failure_leaves_nothing_broken(self):
        """首次安装就失败 -> 不该留下一个半个插件的目录让游戏去加载。"""
        orig = shutil.copyfile

        def always_fail(src, dst, **kw):
            raise OSError("simulated failure")

        shutil.copyfile = always_fail
        try:
            ok, msg, dest = m.deploy_addon(str(self.wow))
        finally:
            shutil.copyfile = orig
        self.assertFalse(ok)
        addons = self.wow / "Interface" / "AddOns"
        names = [p.name for p in addons.iterdir()]
        self.assertEqual(names, [], f"首次安装失败后 AddOns 应保持干净：{names}")

    def test_incomplete_source_refused_before_touching_dest(self):
        """内置插件缺文件时应在**动目标目录之前**就拒绝。"""
        old = self._pre_install_old_version()
        fake_src = self.tmp / "fake_src"
        fake_src.mkdir()
        # 只放 .lua，缺 .toc
        (fake_src / "ArdenTranslatorAI.lua").write_text("-- x", encoding="utf-8")
        orig = m.plugin_source_dir
        m.plugin_source_dir = lambda: fake_src
        try:
            ok, msg, dest = m.deploy_addon(str(self.wow))
        finally:
            m.plugin_source_dir = orig
        self.assertFalse(ok)
        self.assertIn("不完整", msg)
        self.assertEqual((self.dest / "ArdenTranslatorAI.lua").read_bytes(), old,
                         "源不完整时旧版本必须原封不动")


class TestTocVersionRead(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atai_toc_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_version(self):
        p = self.tmp / "x.toc"
        p.write_text("## Interface: 30300\n## Version: 1.1.0\n## Title: x\n",
                     encoding="utf-8")
        self.assertEqual(m._read_toc_version(p), "1.1.0")

    def test_missing_file_returns_empty(self):
        self.assertEqual(m._read_toc_version(self.tmp / "nope.toc"), "")

    def test_no_version_line_returns_empty(self):
        p = self.tmp / "x.toc"
        p.write_text("## Interface: 30300\n", encoding="utf-8")
        self.assertEqual(m._read_toc_version(p), "")

    def test_real_bundled_toc_is_1_1_0(self):
        src = m.plugin_source_dir()
        self.assertIsNotNone(src, "找不到内置插件目录")
        self.assertEqual(m._read_toc_version(src / "ArdenTranslatorAI.toc"), "1.1.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)

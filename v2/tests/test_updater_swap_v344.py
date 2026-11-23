#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.4 更新链路修复回归（用户第 19 轮：更新失败「找不到主程序」）

复现的两个真实故障：
  A. D:/Programs/_update_staging/ 残留历次解压的旧程序目录，旧版
     unpack_zip() 只在「恰好一个子目录」时返回该目录，见到两个目录就
     返回暂存根 → 找不到 ProductSuite_v2.2.exe。
  B. 更新器进程本身运行在 staging 内，_internal/*.dll 被占用，
     shutil.move(staging → target) 触发 WinError 5（历史日志 4 次失败）。

本文件锁定：脏暂存智能挑选、prepare 清空暂存、copytree 交换、失败回滚
清理半成品 target、旧下载缓存回收、主 exe 发现优先级。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from v2 import EXE_NAME, LEGACY_EXE_NAME, __version__
from v2.core import updater

ROOT = Path(__file__).resolve().parents[2]
UPDATER_SRC = ROOT / "v2" / "core" / "updater.py"


def _make_zip(path: Path) -> Path:
    """造一个与真实发布包同构的小 zip：顶层 订单管理工作台/ + 两枚 exe"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"订单管理工作台/{EXE_NAME}", b"NEW-MAIN")
        z.writestr(f"订单管理工作台/{LEGACY_EXE_NAME}", b"NEW-ALIAS")
        z.writestr("订单管理工作台/_internal/placeholder.dll", b"DLL")
    return path


class DirtyStagingTest(unittest.TestCase):
    """故障 A：暂存区有历史残留，仍须正确定位本次解压出的程序目录"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v344_stage_"))
        self.zip_path = _make_zip(self.tmp / "release.zip")
        self.staging = self.tmp / updater.STAGING_NAME

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_version_is_344(self):
        self.assertEqual(__version__, "3.5.4")

    def test_02_clean_staging_returns_program_dir(self):
        program = updater.unpack_zip(self.zip_path, self.staging)
        self.assertEqual(program.name, "订单管理工作台")
        self.assertTrue((program / EXE_NAME).is_file())
        self.assertTrue((program / LEGACY_EXE_NAME).is_file())

    def test_03_dirty_staging_picks_newest_dir_with_main_exe(self):
        # 模拟上次失败/回滚遗留的旧程序目录（含同名别名 exe），mtime 更早
        old = self.staging / "ProductSuite_v2.2"
        old.mkdir(parents=True)
        (old / LEGACY_EXE_NAME).write_bytes(b"OLD-ALIAS")
        (old / "_internal").mkdir()
        (old / "_internal" / "old.dll").write_bytes(b"OLD")
        past = time.time() - 7200
        os.utime(old, (past, past))
        (self.staging / "README.txt").write_text("残留杂物", encoding="utf-8")

        program = updater.unpack_zip(self.zip_path, self.staging)
        self.assertEqual(program.name, "订单管理工作台",
                         "必须指向本次解压的新目录，而不是暂存根或旧残留")
        self.assertEqual((program / LEGACY_EXE_NAME).read_bytes(), b"NEW-ALIAS")
        # 旧版 3.4.2 拿这个目录去找别名 exe，必须命中
        found = updater.find_main_exe(program, LEGACY_EXE_NAME)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, LEGACY_EXE_NAME)

    def test_04_flat_zip_without_top_dir(self):
        flat = self.tmp / "flat.zip"
        with zipfile.ZipFile(flat, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(EXE_NAME, b"MAIN")
            z.writestr(LEGACY_EXE_NAME, b"ALIAS")
            z.writestr("_internal/x.dll", b"X")
        staging = self.tmp / "flat_staging"
        program = updater.unpack_zip(flat, staging)
        self.assertEqual(program, staging)
        self.assertTrue((program / LEGACY_EXE_NAME).is_file())

    def test_05_prepare_update_clears_staging_before_unpack(self):
        app = self.tmp / "ProductSuite_v2.2"
        app.mkdir()
        # 残留：旧程序目录 + 上次下载的一半内容
        old = self.staging / "ProductSuite_v2.2"
        old.mkdir(parents=True)
        (old / LEGACY_EXE_NAME).write_bytes(b"OLD")
        (self.staging / "junk.tmp").write_text("junk", encoding="utf-8")

        manifest = {"version": "3.4.4", "zip": "unused.zip",
                    "sha256": "0" * 64, "note": "", "date": ""}
        with mock.patch.object(updater, "app_dir", return_value=app), \
             mock.patch.object(updater, "download_zip",
                               return_value=self.zip_path):
            res = updater.prepare_update(manifest)
        self.assertEqual(res["dir"].name, "订单管理工作台")
        # 源码模式 running_name 默认正式中文名；冻结旧版 3.4.2 会传兼容别名，
        # 两种口径都必须能在同一个程序目录里定位到主 exe
        self.assertEqual(res["staging_exe"].name, EXE_NAME)
        alias = updater.find_main_exe(res["dir"], LEGACY_EXE_NAME)
        self.assertIsNotNone(alias)
        self.assertEqual(alias.name, LEGACY_EXE_NAME)
        self.assertFalse((self.staging / "junk.tmp").exists())
        self.assertFalse((self.staging / "ProductSuite_v2.2").exists(),
                         "prepare 必须先清空暂存区里的历史残留")
        marker = json.loads((self.tmp / "_update_marker.json").read_text(
            encoding="utf-8"))
        self.assertEqual(marker["launch_exe"], EXE_NAME)
        self.assertEqual(marker["staging_dir"], str(res["dir"]))


class CopySwapTest(unittest.TestCase):
    """故障 B：更新器进程占用 staging，必须复制而非移动"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="v344_swap_"))
        self.target = self.base / "ProductSuite_v2.2"
        self.target.mkdir()
        (self.target / LEGACY_EXE_NAME).write_bytes(b"OLD-EXE")
        (self.target / "old.txt").write_text("old", encoding="utf-8")
        (self.target / "data").mkdir()
        (self.target / "data" / "erp.db").write_bytes(b"PRECIOUS-DB")
        (self.target / "data" / "排产附件").mkdir()

        self.staging = self.base / updater.STAGING_NAME / "订单管理工作台"
        self.staging.mkdir(parents=True)
        (self.staging / EXE_NAME).write_bytes(b"NEW-MAIN")
        (self.staging / LEGACY_EXE_NAME).write_bytes(b"NEW-ALIAS")
        (self.staging / "_internal").mkdir()
        (self.staging / "_internal" / "MSVCP140_1.dll").write_bytes(b"LOCKED-DLL")
        self.marker = {
            "target_dir": str(self.target), "staging_dir": str(self.staging),
            "exe_name": LEGACY_EXE_NAME, "base_dir": str(self.base),
            "log": str(self.base / "_update_apply.log"), "launch": False,
        }

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_06_source_uses_copytree_not_move(self):
        src = UPDATER_SRC.read_text(encoding="utf-8")
        self.assertIn("shutil.copytree(staging, target)", src)
        self.assertNotIn("_move(staging, target)", src,
                         "移动自身运行目录会触发 WinError 5")
        self.assertIn("shutil.rmtree(staging_root, ignore_errors=True)", src)

    def test_07_swap_success_keeps_staging_and_data(self):
        res = updater.apply_update_from_marker(self.marker, launch=False)
        self.assertTrue(res["ok"], res["message"])
        self.assertEqual((self.target / EXE_NAME).read_bytes(), b"NEW-MAIN")
        self.assertEqual((self.target / LEGACY_EXE_NAME).read_bytes(), b"NEW-ALIAS")
        self.assertEqual((self.target / "data" / "erp.db").read_bytes(),
                         b"PRECIOUS-DB")
        self.assertTrue((self.target / "data" / "排产附件").is_dir())
        self.assertFalse((self.target / "old.txt").exists())
        # 复制语义：staging 仍完整保留（更新器进程还在其中运行）
        self.assertTrue(self.staging.is_dir())
        self.assertEqual((self.staging / "_internal" / "MSVCP140_1.dll").read_bytes(),
                         b"LOCKED-DLL")
        olds = list(self.base.glob("_old_*"))
        self.assertEqual(len(olds), 1)
        self.assertEqual((olds[0] / LEGACY_EXE_NAME).read_bytes(), b"OLD-EXE")

    def test_08_copytree_failure_rolls_back_without_partial_target(self):
        def half_copy(src, dst):
            Path(dst).mkdir()
            (Path(dst) / "half.txt").write_text("half", encoding="utf-8")
            raise OSError("[WinError 5] 拒绝访问")

        with mock.patch.object(updater.shutil, "copytree",
                               side_effect=half_copy):
            res = updater.apply_update_from_marker(self.marker, launch=False)
        self.assertFalse(res["ok"])
        self.assertEqual((self.target / LEGACY_EXE_NAME).read_bytes(), b"OLD-EXE")
        self.assertFalse((self.target / "half.txt").exists(),
                         "半成品 target 必须先删再还原，不能嵌套进旧目录")
        self.assertEqual((self.target / "data" / "erp.db").read_bytes(),
                         b"PRECIOUS-DB")

    def test_09_missing_staging_still_rolls_back(self):
        shutil.rmtree(self.staging)
        res = updater.apply_update_from_marker(self.marker, launch=False)
        self.assertFalse(res["ok"])
        self.assertEqual((self.target / LEGACY_EXE_NAME).read_bytes(), b"OLD-EXE")
        self.assertEqual((self.target / "data" / "erp.db").read_bytes(),
                         b"PRECIOUS-DB")

    def test_10_launch_prefers_chinese_name(self):
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            marker = dict(self.marker, launch=True, launch_exe=EXE_NAME)
            res = updater.apply_update_from_marker(marker, launch=True)
        self.assertTrue(res["ok"], res["message"])
        self.assertTrue(res["launched"])
        exe = popen.call_args[0][0][0]
        self.assertEqual(Path(exe).name, EXE_NAME)

    def test_11_cleanup_old_downloads(self):
        folder = self.base / updater.DOWNLOADS_NAME
        folder.mkdir()
        for i in range(4):
            p = folder / f"update_3.4.{i}.zip"
            p.write_bytes(f"z{i}".encode())
            past = time.time() - (4 - i) * 3600
            os.utime(p, (past, past))
        n = updater.cleanup_old_downloads(self.base, keep=2)
        self.assertEqual(n, 2)
        left = sorted(p.name for p in folder.glob("update_*.zip"))
        self.assertEqual(left, ["update_3.4.2.zip", "update_3.4.3.zip"])


class MainExeDiscoveryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v344_find_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_12_preferred_then_official_then_alias(self):
        (self.tmp / EXE_NAME).write_bytes(b"M")
        (self.tmp / LEGACY_EXE_NAME).write_bytes(b"A")
        self.assertEqual(updater.find_main_exe(self.tmp, LEGACY_EXE_NAME).name,
                         LEGACY_EXE_NAME)
        self.assertEqual(updater.find_main_exe(self.tmp).name, EXE_NAME)
        self.assertIsNone(updater.find_main_exe(self.tmp / "missing"))

    def test_13_hash_helper_stable(self):
        p = self.tmp / "x.zip"
        p.write_bytes(b"abc")
        self.assertEqual(updater.sha256_of(p),
                         hashlib.sha256(b"abc").hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)

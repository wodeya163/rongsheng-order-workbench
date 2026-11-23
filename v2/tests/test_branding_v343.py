#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.3 回归（2026-11-10 · 用户第 17 轮）：品牌与程序集更名 + 更新链路兼容

用户口径：
  1. 「产品全链路管理工作台」→「榕盛商贸订单管理工作台」；
  2. 打包后的程序集名称为「订单管理工作台」。

锁定五条硬口径：
  ① 名称集中在 v2/__init__.py 四个常量，UI / 文档 / 打包脚本同源；
  ② 新包必须是中文名主 exe + ProductSuite_v2.2.exe 兼容别名双入口；
  ③ 旧版 3.4.2 升级时能被自己的更新器定位（兼容别名存在），
     目录交换完成后自动拉起中文名主 exe（launch_exe / find_main_exe）；
  ④ data/ 原位保留、schema 仍 v6、单实例互斥体名不变（新旧 exe 同一把锁）；
  ⑤ Windows 版本资源写入公司「榕盛商贸」与产品名，可被 PyInstaller 解析。

    python -m pytest v2/tests/test_branding_v343.py -q
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import (APP_TITLE, ASSEMBLY_NAME, EXE_NAME, LEGACY_EXE_NAME,  # noqa: E402
                __version__)
from v2.core import updater                                            # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database                 # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OLD_TITLE = "产品全链路管理工作台"

try:
    from PySide6.QtWidgets import QApplication, QLabel
    PYSIDE = True
except ImportError:                                             # pragma: no cover
    PYSIDE = False

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(_APP)
    return _APP


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class BrandConstantsTest(unittest.TestCase):
    """① 名称单一数据源与更名覆盖面"""

    def test_01_version_and_constants(self):
        self.assertEqual(__version__, "3.5.4")
        self.assertEqual(APP_TITLE, "榕盛商贸订单管理工作台")
        self.assertEqual(ASSEMBLY_NAME, "订单管理工作台")
        self.assertEqual(EXE_NAME, "订单管理工作台.exe")
        self.assertEqual(LEGACY_EXE_NAME, "ProductSuite_v2.2.exe")

    def test_02_ui_and_service_sources_are_renamed(self):
        for rel in ("v2/ui/main_window_v2.py", "v2/ui/panels/due_actions.py",
                    "v2/core/worddoc.py", "v2/app_v2.py",
                    "tools/release.py", "tools/verify.py"):
            with self.subTest(rel=rel):
                s = _src(rel)
                self.assertNotIn(OLD_TITLE, s, f"{rel} 仍残留旧展示名")
        self.assertIn("APP_TITLE", _src("v2/ui/main_window_v2.py"))
        self.assertIn("APP_TITLE", _src("v2/ui/panels/due_actions.py"))
        self.assertIn("APP_TITLE", _src("v2/core/worddoc.py"))
        self.assertIn("setApplicationName(APP_TITLE)", _src("v2/app_v2.py"))

    def test_03_build_scripts_share_assembly_name(self):
        for rel in ("build_v2.py", "tools/release.py", "tools/verify.py"):
            with self.subTest(rel=rel):
                s = _src(rel)
                self.assertIn("ASSEMBLY_NAME", s)
                self.assertNotIn('NAME = "ProductSuite_v2.2"', s)
        self.assertIn('"--version-file"', _src("build_v2.py"))
        self.assertIn("shutil.copy2(exe, alias)", _src("build_v2.py"))
        self.assertIn("ALIAS_EXE.is_file()", _src("tools/verify.py"))

    def test_04_version_info_carries_brand(self):
        text = _src("version_info.txt")
        self.assertIn(APP_TITLE, text)          # ProductName
        self.assertIn(ASSEMBLY_NAME, text)      # FileDescription / InternalName
        self.assertIn("榕盛商贸", text)          # CompanyName
        self.assertIn(", ".join(__version__.split(".")) + ", 0", text)
        self.assertIn("080404B0", text)          # 简体中文 + Unicode
        try:
            from PyInstaller.utils.win32.versioninfo import (
                load_version_info_from_text_file as load)
        except ImportError:                                     # pragma: no cover
            return
        self.assertIn(APP_TITLE, str(load(str(ROOT / "version_info.txt"))))

    def test_05_single_instance_mutex_unchanged(self):
        s = _src("v2/core/single_instance.py")
        self.assertIn("Local\\\\ProductSuite_", s,
                      "互斥体名不能随品牌改，否则新旧 exe 会同时写同一个库")
        self.assertIn(f'title: str = "{APP_TITLE}"', s)

    def test_06_schema_v7_contract_items(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)


@unittest.skipUnless(PYSIDE, "需要 PySide6")
class BrandUITest(unittest.TestCase):
    """窗口 / 品牌栏 / 关于 / 托盘的真实文案"""

    def setUp(self):
        _app()
        self.db = Database(":memory:")
        from v2.ui.main_window_v2 import MainWindowV2
        self.w = MainWindowV2(self.db)

    def tearDown(self):
        self.w.close()
        self.db.close()

    def test_07_window_title(self):
        t = self.w.windowTitle()
        self.assertIn(APP_TITLE, t)
        self.assertIn(f"v{__version__}", t)
        self.assertNotIn(OLD_TITLE, t)

    def test_08_brand_bar(self):
        labels = [x.text() for x in self.w.findChildren(QLabel)
                  if x.objectName() == "PageTitle"]
        self.assertEqual(labels, [APP_TITLE])

    def test_09_about_dialog_text(self):
        from v2.ui import theme
        with mock.patch.object(theme, "notify") as notify:
            self.w._show_about()
        self.assertTrue(notify.called)
        self.assertEqual(notify.call_args[0][1], "关于")
        self.assertIn(APP_TITLE, notify.call_args[0][2])
        self.assertNotIn(OLD_TITLE, notify.call_args[0][2])

    def test_10_tray_tooltip(self):
        self.assertIn('f"{APP_TITLE} · 交期提醒"',
                      _src("v2/ui/panels/due_actions.py"))


class WordFooterTest(unittest.TestCase):
    """三类 Word 文档页脚落的是新名"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brand_docx_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_11_three_templates_footer(self):
        from docx import Document
        from v2.core import worddoc
        ctx = {"schedule_no": "DDPS-TEST", "order_no": "DD-TEST",
               "project_no": "SO-343-01", "customer": "国能沈西热电厂",
               "order_type": "外部订单", "status": "已确认",
               "buyer": "国能沈西热电厂", "supplier": "华北耐磨件有限公司",
               "doc_no": "CG-20261110-001", "amount": 21000,
               "items": [{"line_no": 1, "item_name": "耐磨弯头",
                          "spec": "DN250", "unit": "件", "qty_ordered": 2,
                          "unit_price": 100, "amount": 200}]}
        cases = (("approval.docx", worddoc.build_approval_docx),
                 ("whole.docx", worddoc.build_order_approval_docx),
                 ("contract.docx", worddoc.build_purchase_contract_docx))
        for name, fn in cases:
            with self.subTest(template=name):
                p = self.tmp / name
                fn(str(p), ctx)
                foot = Document(str(p)).sections[0].footer.paragraphs[0].text
                self.assertIn(APP_TITLE, foot)
                self.assertNotIn(OLD_TITLE, foot)


class UpdaterCompatTest(unittest.TestCase):
    """② 兼容别名 + ③ 升级后拉起新名 exe"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brand_upd_"))
        self.program = self.tmp / "package" / ASSEMBLY_NAME
        self.program.mkdir(parents=True)
        (self.program / EXE_NAME).write_bytes(b"NEW")
        (self.program / LEGACY_EXE_NAME).write_bytes(b"ALIAS")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_12_old_version_preferred_finds_alias(self):
        self.assertEqual(updater.find_main_exe(self.program, LEGACY_EXE_NAME).name,
                         LEGACY_EXE_NAME)

    def test_13_new_version_preferred_finds_main(self):
        self.assertEqual(updater.find_main_exe(self.program, EXE_NAME).name,
                         EXE_NAME)

    def test_14_default_prefers_official_name(self):
        self.assertEqual(updater.find_main_exe(self.program).name, EXE_NAME)

    def test_15_only_alias_package_still_works(self):
        (self.program / EXE_NAME).unlink()
        self.assertEqual(updater.find_main_exe(self.program).name,
                         LEGACY_EXE_NAME)

    def test_16_empty_package_returns_none(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIsNone(updater.find_main_exe(empty))

    def test_17_prepare_update_writes_launch_exe(self):
        base = self.tmp / "install"
        target = base / ASSEMBLY_NAME
        (target / "data").mkdir(parents=True)
        manifest = {"version": "3.4.3", "zip": "x.zip", "sha256": "0" * 64}
        with mock.patch.object(updater, "app_dir", return_value=target), \
             mock.patch.object(updater, "download_zip",
                               return_value=self.tmp / "update.zip"), \
             mock.patch.object(updater, "unpack_zip",
                               return_value=self.program):
            result = updater.prepare_update(manifest)
        marker = json.loads((base / "_update_marker.json").read_text(
            encoding="utf-8"))
        self.assertEqual(marker["launch_exe"], EXE_NAME)
        self.assertEqual(result["staging_exe"].name, EXE_NAME)
        self.assertEqual(marker["exe_name"], EXE_NAME)

    def test_18_legacy_marker_launches_new_exe_and_keeps_data(self):
        base = self.tmp / "swap"
        target = base / ASSEMBLY_NAME
        (target / "data").mkdir(parents=True)
        (target / "data" / "erp.db").write_bytes(b"PRECIOUS-DB")
        staging = base / "_update_staging" / ASSEMBLY_NAME
        staging.mkdir(parents=True)
        (staging / EXE_NAME).write_bytes(b"NEW")
        (staging / LEGACY_EXE_NAME).write_bytes(b"ALIAS")
        # 模拟 v3.4.2 写出的 marker：只有 exe_name，没有 launch_exe
        marker = {"target_dir": str(target), "staging_dir": str(staging),
                  "exe_name": LEGACY_EXE_NAME, "base_dir": str(base),
                  "log": str(base / "apply.log"), "launch": True}
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            res = updater.apply_update_from_marker(marker, launch=True)
        self.assertTrue(res["ok"], res["message"])
        self.assertTrue(res["launched"])
        launched = Path(popen.call_args[0][0][0])
        self.assertEqual(launched.name, EXE_NAME,
                         "旧版升级后必须拉起中文名主 exe")
        self.assertTrue((target / LEGACY_EXE_NAME).is_file(),
                        "旧桌面快捷方式不能失效")
        self.assertEqual((target / "data" / "erp.db").read_bytes(),
                         b"PRECIOUS-DB")


class ReleaseSideTest(unittest.TestCase):
    """发布脚本文案与自检项"""

    def test_19_release_default_note_uses_brand(self):
        self.assertIn('f"{APP_TITLE} v{__version__}"', _src("tools/release.py"))

    def test_20_verify_alias_is_mandatory(self):
        s = _src("tools/verify.py")
        self.assertIn("缺少兼容别名 exe", s)
        self.assertIn("EXE = ROOT / \"dist\" / ASSEMBLY_NAME / EXE_NAME", s)


if __name__ == "__main__":
    unittest.main()

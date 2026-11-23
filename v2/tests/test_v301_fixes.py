#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.0.1 缺陷修复回归（用户第 7 轮反馈的 4 个 bug + 图标 + 版本号统一）：
  1  导入生产排产审批打不开文件框 —— APPROVAL_FILTER 尾逗号成元组，
     setNameFilter(元组) 抛 ValueError 被 Qt 吞掉 → 常量必须是 str，
     且 make_file_dialog 对元组过滤器做归一防御
  2  订单物资勾选后看不清 —— 勾选列挂自绘委托，QSS 指示器带白色对勾图
  3  新建询配码项目的「询配码文件路径」支持「浏览…」选文件（FormDialog type=path）
  4  应用图标贯通（v2/ui/app.ico + apply_theme 全局挂载）
  5  所有弹窗版本徽标统一取 theme.BADGE = f"v{v2.__version__}"
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.domains import schedule as sched_mod


class FilterConstantsTest(unittest.TestCase):
    """bug 1 根因回归：过滤器常量必须是纯字符串"""

    def test_01_filters_are_plain_strings(self):
        for name in ("APPROVAL_FILTER", "IMAGE_FILTER", "DRAWING_FILTER"):
            value = getattr(sched_mod, name)
            self.assertIsInstance(value, str, f"{name} 应为 str，收到 {type(value)}")
            self.assertIn("*.", value)

    def test_02_badge_matches_package_version(self):
        import v2
        from v2.ui import theme
        self.assertEqual(theme.BADGE, f"v{v2.__version__}")


class _UiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("未安装 PySide6")
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)


class ThemeFixTest(_UiBase):
    def test_03_qss_checkbox_indicator_has_check_image(self):
        from PySide6.QtGui import QImage
        from v2.ui import theme
        url = theme._check_png_url()
        if url:  # 临时目录可写时：裸路径（非 file://）且 QImage 能真实加载
            self.assertIn('image: url("', theme.QSS)
            self.assertFalse(url.startswith("file:"))
            self.assertFalse(QImage(url).isNull())
        self.assertIn("QCheckBox::indicator:checked", theme.QSS)

    def test_04_app_icon_loaded_from_package(self):
        from v2.ui import theme
        icon = theme.app_icon()
        ico = Path(theme.__file__).resolve().parent / "app.ico"
        if ico.is_file():
            self.assertIsNotNone(icon)
            self.assertFalse(icon.isNull())

    def test_05_dialog_shell_default_badge_is_theme_badge(self):
        from v2.ui import theme
        from v2.ui.base_dialog import ThemedDialog
        dlg = ThemedDialog(None, "测试窗口")
        badges = [w for w in dlg.findChildren(type(theme.label("", "")))
                  if w.objectName() == "ViewBadge"]
        self.assertTrue(badges)
        self.assertEqual(badges[0].text(), theme.BADGE)

    def test_06_dialog_has_app_window_icon(self):
        from v2.ui import theme
        from v2.ui.base_dialog import ThemedDialog
        dlg = ThemedDialog(None, "测试窗口")
        if Path(theme.__file__).resolve().parent.joinpath("app.ico").is_file():
            self.assertFalse(dlg.windowIcon().isNull())

    def test_07_make_file_dialog_accepts_tuple_filters(self):
        """防御回归：再传元组过滤器也不能抛异常"""
        from v2.ui import theme
        from PySide6.QtWidgets import QFileDialog
        dlg = theme.make_file_dialog(None, "t", "multi", "", ("a (*.x)",))
        self.assertIsInstance(dlg, QFileDialog)


class FormDialogPathFieldTest(_UiBase):
    def test_08_path_field_renders_lineedit_and_browse_button(self):
        from PySide6.QtWidgets import QLineEdit, QPushButton
        from v2.ui.dialogs import FormDialog
        fields = [{"key": "file_path", "label": "询配码文件路径", "type": "path"},
                  {"key": "project_name", "label": "名称", "required": True}]
        dlg = FormDialog(None, "测试路径字段", fields)
        edit, typ = dlg.widgets["file_path"]
        self.assertIsInstance(edit, QLineEdit)
        self.assertEqual(typ, "text")
        btns = [b for b in dlg.findChildren(QPushButton) if b.text() == "浏览…"]
        self.assertEqual(len(btns), 1)

    def test_09_value_returns_lineedit_text(self):
        from v2.ui.dialogs import FormDialog
        fields = [{"key": "file_path", "label": "路径", "type": "path",
                   "default": r"D:\data\询配码.xlsx"},
                  {"key": "project_name", "label": "名称", "required": True}]
        dlg = FormDialog(None, "测试路径取值", fields)
        self.assertEqual(dlg.value("file_path"), r"D:\data\询配码.xlsx")

    def test_10_code_project_fields_use_path_type(self):
        from v2.ui.dialogs import code_project_fields
        fp = next(f for f in code_project_fields() if f["key"] == "file_path")
        self.assertEqual(fp.get("type"), "path")


class ScheduleCheckDelegateTest(_UiBase):
    """复用 scope 测试的三物资订单夹具，验证勾选列委托就位"""

    def test_11_check_column_has_custom_delegate(self):
        import tempfile
        import shutil
        from v2.core.db import Database
        from v2.core import models
        from v2.domains import OrderService, OrderScheduleService
        from v2.ui.schedule_dialog import OrderScheduleDialog, _CheckDelegate

        tmp = tempfile.mkdtemp(prefix="v301_fix_")
        try:
            db = Database(":memory:")
            orders = OrderService(db)
            svc = OrderScheduleService(db, tmp)
            order = orders.create_order(
                customer="华东电建", project_no="SO-2026-110",
                status=models.OrderStatus.CONFIRMED)
            orders.add_item(order["id"], item_name="耐磨弯头",
                            spec="DN250", unit="件", qty_ordered=10)
            orders.add_item(order["id"], item_name="落煤管",
                            spec="DN400", unit="根", qty_ordered=3)
            dlg = OrderScheduleDialog(None, svc, order_id=order["id"])
            self.assertIsInstance(dlg.item_table.itemDelegateForColumn(0),
                                  _CheckDelegate)
            # 默认整单预勾选 → 委托画选中态；清空 → 未选中态
            self.assertEqual(len(dlg._checked_item_ids()), 2)
            dlg.close()
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

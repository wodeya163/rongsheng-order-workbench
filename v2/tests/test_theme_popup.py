#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.9.4 下拉弹层主题回归（2026-11-07 用户反馈：下拉选项黑底选不中）

覆盖：
  · apply_theme 装浅色调色板（Base 白 / Text 深墨 / Highlight 浅蓝）
  · QSS 含 QComboBox QAbstractItemView 弹层与 item:hover/:selected 规则
  · 展开状态下拉弹层视图 Base 为白色、Text 为深色
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.ui import theme                                            # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QComboBox
    PYSIDE = True
except ImportError:
    PYSIDE = False

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class PopupThemeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if PYSIDE:
            theme.apply_theme(_app())

    def test_01_qss_has_popup_rules(self):
        self.assertIn("QComboBox QAbstractItemView", theme.QSS)
        self.assertIn("QComboBox QAbstractItemView::item:hover", theme.QSS)
        self.assertIn("QComboBox QAbstractItemView::item:selected", theme.QSS)
        self.assertIn("QCalendarWidget QAbstractItemView", theme.QSS)

    @unittest.skipUnless(PYSIDE, "PySide6 不可用")
    def test_02_light_palette_installed(self):
        app = _app()
        pal = app.palette()
        role = pal.ColorRole
        self.assertEqual(pal.color(role.Base).name().lower(), "#ffffff")
        self.assertEqual(pal.color(role.Text).name().lower(), "#1e293b")
        self.assertEqual(pal.color(role.Highlight).name().lower(), "#dbeafe")

    @unittest.skipUnless(PYSIDE, "PySide6 不可用")
    def test_03_popup_view_is_light(self):
        app = _app()
        combo = QComboBox()
        combo.addItems(["招投标", "北电电力", "厦门龙净", "京东平台"])
        combo.showPopup()
        app.processEvents()
        view = combo.view()
        pal = view.palette()
        self.assertEqual(pal.color(pal.ColorRole.Base).name().lower(), "#ffffff")
        self.assertEqual(pal.color(pal.ColorRole.Text).name().lower(), "#1e293b")
        combo.hidePopup()
        combo.deleteLater()


if __name__ == "__main__":
    unittest.main(verbosity=2)

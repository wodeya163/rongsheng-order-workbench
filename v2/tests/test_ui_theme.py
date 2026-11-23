#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面主题一致性回归（v2.2 主题统一版）

    python -m v2.tests.test_ui_theme          # 零依赖环境自动跳过 Qt 相关用例
    D:\\Program Files\\Python313\\python.exe -m v2.tests.test_ui_theme   # 完整跑

覆盖：QSS 选择器、单一主题入口、四类业务弹窗外壳、按钮中文与主按钮样式、
消息框/文件框封装、源码里不再残留硬编码颜色与 Qt 静态弹窗调用。
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.ui import theme  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QMessageBox
    PYSIDE = True
except ImportError:
    PYSIDE = False

UI_DIR = Path(__file__).resolve().parents[1] / "ui"


class QssContractTest(unittest.TestCase):
    """纯字符串契约，零依赖环境也必须通过"""

    def test_01_qss_covers_all_top_level_windows(self):
        for sel in ("QDialog", "QMessageBox", "QFileDialog", "QAbstractSpinBox",
                    "QFrame#DialogHeader", "QFrame#DialogBody", "QFrame#DialogFooter",
                    "QLabel#DialogTitle", "QPushButton#Primary"):
            self.assertIn(sel, theme.QSS, f"QSS 缺少选择器 {sel}")

    def test_02_palette_is_single_source(self):
        for name in ("BRAND", "BRAND_DARK", "INK", "MUTED", "LINE", "BG", "CARD",
                     "OK", "WARN", "DANGER"):
            self.assertTrue(getattr(theme, name).startswith("#"), name)
        self.assertIn(theme.BG, theme.QSS)
        self.assertIn(theme.CARD, theme.QSS)
        self.assertIn(theme.BRAND, theme.QSS)

    def test_03_apply_theme_handles_none(self):
        self.assertFalse(theme.apply_theme(None))

    def test_04_no_hardcoded_colors_or_native_dialogs(self):
        for name in ("dialogs.py", "main_window_v2.py"):
            src = (UI_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("#777", src, f"{name} 残留旧灰色")
            self.assertNotIn("#C0392B", src, f"{name} 残留旧红色")
            self.assertNotIn("setStyleSheet(\"color:#", src, f"{name} 残留硬编码样式")
            for legacy in ("QMessageBox.information(", "QMessageBox.warning(",
                           "QMessageBox.question(", "QFileDialog.getSaveFileName(",
                           "QFileDialog.getOpenFileName(",
                           "QFileDialog.getExistingDirectory("):
                self.assertNotIn(legacy, src, f"{name} 未收口到主题工厂：{legacy}")


@unittest.skipUnless(PYSIDE, "PySide6 未安装，跳过 Qt 用例")
class ThemedWindowTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)
        from PySide6.QtGui import QFontDatabase
        for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
            if Path(p).is_file():
                QFontDatabase.addApplicationFont(p)
        from v2.core.db import Database
        from v2 import demo_data
        from v2.ui.main_window_v2 import MainWindowV2
        cls.db = Database(":memory:")
        demo_data.seed(cls.db)
        cls.win = MainWindowV2(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.db.close()

    def _dialogs(self):
        from v2.ui import dialogs
        return [
            dialogs.FormDialog(self.win, "测试表单", dialogs.bid_item_fields()),
            dialogs.TableViewDialog(self.win, "测试表格", ["A", "B"], [[1, 2], [3, 4]]),
            dialogs.NewOrderDialog(self.win, self.db),
            dialogs.ImportOrdersDialog(self.win, self.db),
            dialogs.LinkOrderItemDialog(self.win, self.db, self.win.orders),
        ]

    def test_05_global_qss_applied(self):
        self.assertEqual(self.app.styleSheet(), theme.QSS)
        self.assertTrue(theme.apply_theme(self.app))       # 幂等
        self.assertEqual(self.app.styleSheet(), theme.QSS)

    def test_06_all_dialogs_share_shell(self):
        from v2.ui.dialogs import ThemedDialog
        from v2.ui import dialogs
        # 2026-11-06：下单向导已取消，订单管理改为「新建订单 + 导入订单」
        self.assertFalse(hasattr(dialogs, "OrderWizardDialog"))
        self.assertFalse(hasattr(self.win, "_order_wizard"))
        for dlg in self._dialogs():
            with self.subTest(dlg=type(dlg).__name__):
                self.assertIsInstance(dlg, ThemedDialog)
                self.assertEqual(dlg.objectName(), "ThemedDialog")
                self.assertEqual(dlg.header.objectName(), "DialogHeader")
                self.assertEqual(dlg.body.objectName(), "DialogBody")
                self.assertEqual(dlg.footer.objectName(), "DialogFooter")
                self.assertGreater(dlg.body_layout.count(), 0)
                self.assertGreater(dlg.footer_layout.count(), 0)
                dlg.close()

    def test_07_buttons_are_chinese_and_primary(self):
        for dlg in self._dialogs():
            with self.subTest(dlg=type(dlg).__name__):
                boxes = dlg.findChildren(QDialogButtonBox)
                self.assertEqual(len(boxes), 1)
                btns = boxes[0].buttons()
                self.assertTrue(all(b.text() not in ("OK", "Cancel") for b in btns))
                primary = [b for b in btns if b.objectName() == "Primary"]
                self.assertEqual(len(primary), 1, f"{type(dlg).__name__} 缺主按钮")
                # ID 选择器必须真的渲染成品牌蓝（防 QSS objectName 缓存问题）
                self.assertEqual(primary[0].palette().button().color().name(),
                                 theme.BRAND.lower())
                dlg.close()

    def test_08_dialog_tables_use_theme_style(self):
        from v2.ui import dialogs
        t = dialogs.TableViewDialog(self.win, "表格", ["A", "B"], [[1, 2]])
        self.assertEqual(t.table.objectName(), "ResultTable")
        self.assertTrue(t.table.alternatingRowColors())
        self.assertFalse(t.table.verticalHeader().isVisible())
        t.close()
        nd = dialogs.NewOrderDialog(self.win, self.db)
        self.assertEqual(nd.table.objectName(), "ResultTable")
        self.assertEqual(nd.table.columnCount(), 9)   # v2.3 明细 9 字段
        nd.close()
        imp = dialogs.ImportOrdersDialog(self.win, self.db)
        self.assertEqual(imp.table.objectName(), "ImportPreview")
        imp.close()

    def test_09_form_required_hint_uses_theme_color(self):
        from v2.ui import dialogs
        d = dialogs.FormDialog(self.win, "必填校验", [
            {"key": "name", "label": "名称", "required": True}])
        d.value("name")                       # 字段可读
        d.widgets["name"][0].clear()
        d._validate()                         # 只走失败分支，不 exec
        self.assertIn("请填写", d.hint.text())
        self.assertIn(theme.DANGER, d.hint.styleSheet())
        d.close()

    def test_10_message_boxes(self):
        box = theme.make_message(self.win, "提示", "内容", kind="warn")
        self.assertEqual(box.objectName(), "ThemedMessageBox")
        self.assertEqual([b.text() for b in box.buttons()], ["知道了"])
        ok = box.button(QMessageBox.Ok)
        self.assertEqual(ok.objectName(), "Primary")
        self.assertEqual(ok.palette().button().color().name(), theme.BRAND.lower())
        box.close()
        cf = theme.make_message(self.win, "确认", "继续？", yes_no=True)
        self.assertEqual(sorted(b.text() for b in cf.buttons()), ["取消", "确定"])
        self.assertEqual(cf.button(QMessageBox.Yes).objectName(), "Primary")
        cf.close()

    def test_11_file_dialogs_are_qt_drawn(self):
        for mode, accept in (("open", QFileDialog.AcceptOpen),
                             ("save", QFileDialog.AcceptSave),
                             ("dir", QFileDialog.AcceptOpen)):
            with self.subTest(mode=mode):
                d = theme.make_file_dialog(self.win, "选择", mode=mode,
                                           filters="CSV (*.csv)")
                self.assertEqual(d.objectName(), "ThemedFileDialog")
                self.assertEqual(d.acceptMode(), accept)
                self.assertTrue(d.testOption(QFileDialog.DontUseNativeDialog))
                self.assertIn("CSV", d.nameFilters()[0])
                d.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

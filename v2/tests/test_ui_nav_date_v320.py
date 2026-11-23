#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.2.0 界面升级回归（2026-11-08 · 用户第 12 轮）

五项验收：
  1 所有日期输入框 → 只读 DatePicker + 弹出日历（FormDialog / 查询窗口 /
    审计查询 / 新建订单 / 编辑订单），禁止手工输入
  2 操作导航每个分组可单独折叠，支持全部展开 / 全部折叠
  3 整个导航可隐藏，隐藏后有恢复条，菜单勾选同步，状态持久化
  4 导航动作名称去编号/星号/口语括号，短名 + 完整说明 tooltip
  5 重要操作在右侧主操作界面有快捷按钮 + 「更多操作」完整菜单

运行：python -m pytest v2/tests/test_ui_nav_date_v320.py -q
      python -m v2.tests.test_ui_nav_date_v320
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from v2.app_v2 import ensure_font
from v2.core import models, uistate
from v2.core.db import Database
from v2.domains import MaintenanceService, OrderService
from v2.ui import datefield, dialogs, theme
from v2.ui.datefield import DatePicker, parse_date
from v2.ui.main_window_v2 import ACTION_GROUPS, MainWindowV2, VIEW_KEYS
from v2.ui.panels.common import PRIMARY_ACTIONS, find_action
from v2.ui.query_panel import QueryPanel


def _app():
    app = QApplication.instance() or QApplication([])
    ensure_font(app)
    theme.apply_theme(app)
    return app


class _FakeCalendarDialog:
    """替身日历弹窗：避免测试里真的进模态事件循环"""

    def __init__(self, chosen: QDate, cleared: bool = False):
        self.cleared = cleared
        self._chosen = chosen

    def exec(self):
        return int(QDialog.Accepted)

    def chosen(self):
        return None if self.cleared else self._chosen

    def deleteLater(self):
        pass


class DatePickerTest(unittest.TestCase):
    """① 日期控件：解析、只读、弹窗、清空"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_01_parse_common_formats(self):
        self.assertEqual(parse_date("2026-11-08").toString("yyyy-MM-dd"),
                         "2026-11-08")
        self.assertEqual(parse_date("2026/3/7").toString("yyyy-MM-dd"),
                         "2026-03-07")
        self.assertEqual(parse_date("2026.3.7").toString("yyyy-MM-dd"),
                         "2026-03-07")
        self.assertEqual(parse_date("2026-11-08 12:30:00").toString("yyyy-MM-dd"),
                         "2026-11-08")
        self.assertEqual(parse_date(dt.date(2026, 1, 2)).toString("yyyy-MM-dd"),
                         "2026-01-02")
        self.assertEqual(parse_date(QDate(2026, 5, 6)).toString("yyyy-MM-dd"),
                         "2026-05-06")
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date("不是日期"))

    def test_02_picker_is_readonly_and_normalizes(self):
        p = DatePicker(value="2026/11/8")
        self.assertTrue(p.isReadOnly())
        self.assertTrue(p.edit.isReadOnly())
        self.assertEqual(p.text(), "2026-11-08")
        self.assertTrue(p.btn_clear.isEnabled())
        p.setText("2026-11-08 12:30:00")
        self.assertEqual(p.text(), "2026-11-08")
        p.close()

    def test_03_dirty_value_is_kept_not_silently_dropped(self):
        p = DatePicker()
        p.setText("历史脏数据")
        self.assertEqual(p.text(), "历史脏数据")
        self.assertIsNone(p.date())
        p.close()

    def test_04_clear_and_placeholder_api(self):
        p = DatePicker(value="2026-11-08")
        p.setPlaceholderText("起始日期")
        self.assertEqual(p.placeholderText(), "起始日期")
        p.clear()
        self.assertEqual(p.text(), "")
        self.assertFalse(p.btn_clear.isEnabled())
        p.close()

    def test_05_calendar_selection_writes_iso_date(self):
        p = DatePicker()
        with patch.object(datefield, "_DateCalendarDialog",
                          return_value=_FakeCalendarDialog(QDate(2026, 12, 1))):
            p.open_calendar()
        self.assertEqual(p.text(), "2026-12-01")
        p.close()

    def test_06_calendar_clear_empties_field(self):
        p = DatePicker(value="2026-11-08")
        with patch.object(datefield, "_DateCalendarDialog",
                          return_value=_FakeCalendarDialog(QDate.currentDate(),
                                                           cleared=True)):
            p.open_calendar()
        self.assertEqual(p.text(), "")
        p.close()

    def test_07_formdialog_date_field_is_picker(self):
        d = dialogs.FormDialog(None, "日期表单", [
            {"key": "d1", "label": "投标日期", "type": "date",
             "default": "2026-11-08"},
            {"key": "t1", "label": "名称", "type": "text"}])
        w = d.widgets["d1"][0]
        self.assertIsInstance(w, DatePicker)
        self.assertTrue(w.isReadOnly())
        self.assertEqual(d.value("d1"), "2026-11-08")
        d.close()

    def test_08_query_panel_date_range_is_picker(self):
        db = Database(":memory:")
        qp = QueryPanel(db, on_query=lambda: None, on_reset=lambda: None)
        self.assertIsInstance(qp.widgets["date_from"], DatePicker)
        self.assertIsInstance(qp.widgets["date_to"], DatePicker)
        qp.apply_filters({"date_from": "2026-01-01", "date_to": "2026-12-31"})
        self.assertEqual(qp.values()["date_from"], "2026-01-01")
        self.assertEqual(qp.values()["date_to"], "2026-12-31")
        qp.clear()
        self.assertEqual(qp.values()["date_from"], "")
        db.close()

    def test_09_order_dialogs_and_audit_use_picker(self):
        db = Database(":memory:")
        orders = OrderService(db)
        o = orders.create_order(customer="国能沈西热电厂")
        orders.add_item(o["id"], item_name="耐磨弯头", qty_ordered=3,
                        unit_price=100.0)
        new = dialogs.NewOrderDialog(None, db)
        edit = dialogs.OrderEditDialog(None, db, o["id"])
        audit = dialogs.AuditQueryDialog(None, db, MaintenanceService(db))
        for w in (new.edit_delivery, new.edit_date, edit.edit_delivery,
                  edit.edit_date, audit.ed_from, audit.ed_to):
            with self.subTest(w=type(w).__name__):
                self.assertIsInstance(w, DatePicker)
                self.assertTrue(w.isReadOnly())
        self.assertEqual(new.edit_date.text(), dt.date.today().isoformat())
        self.assertEqual(audit._query_args()["date_from"], "")
        for d in (new, edit, audit):
            d.close()
        db.close()


class NavigationNameTest(unittest.TestCase):
    """④ 名称简化与正式化"""

    def test_10_no_decorations_or_colloquial_suffixes(self):
        titles = [g[0] for g in ACTION_GROUPS]
        labels = [a[0] for _, _, acts in ACTION_GROUPS for a in acts]
        self.assertEqual(len(titles), 6)
        self.assertFalse(any("①" in t or "★" in t or "·" in t for t in titles))
        for bad in ("★", "一键", "给用户", "（", "(", "/", "右键", "≥"):
            with self.subTest(bad=bad):
                self.assertFalse(any(bad in x for x in labels),
                                 f"动作名仍含非正规片段 {bad!r}")
        self.assertTrue(all(len(x) <= 8 for x in labels), "动作名应短")

    def test_11_every_action_has_full_tooltip(self):
        for title, key, acts in ACTION_GROUPS:
            for act in acts:
                with self.subTest(action=act[0]):
                    self.assertEqual(len(act), 4)
                    self.assertTrue(act[3].strip())
                    self.assertGreaterEqual(len(act[3]), 8)

    def test_12_find_action_and_primary_specs_valid(self):
        self.assertEqual(find_action("_new_order", "orders")[0], "新增订单")
        self.assertEqual(find_action("_order_schedule", "production")[2], "orders")
        for view, specs in PRIMARY_ACTIONS.items():
            self.assertIn(view, VIEW_KEYS)
            self.assertTrue(specs)
            for fn, _override in specs:
                with self.subTest(view=view, fn=fn):
                    self.assertTrue(find_action(fn, view))


class NavigationUiTest(unittest.TestCase):
    """②③ 折叠 / 隐藏 / ⑤ 快捷栏"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.db = Database(":memory:")
        cls.w = MainWindowV2(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.w.close()
        cls.db.close()

    def setUp(self):
        self.w._expand_all(True)
        self.w.set_nav_visible(True)
        self.w.tree_search.clear()

    def test_13_group_titles_have_fold_indicator(self):
        tree = self.w.action_tree
        self.assertEqual(tree.topLevelItemCount(), 6)
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            self.assertTrue(g.text(0).startswith("▼ "))
            self.assertTrue(g.isExpanded())

    def test_14_single_group_toggle(self):
        g = self.w.action_tree.topLevelItem(0)
        self.w._toggle_group(g)
        self.assertFalse(g.isExpanded())
        self.assertTrue(g.text(0).startswith("→ "))
        self.w._toggle_group(g)
        self.assertTrue(g.isExpanded())
        self.assertTrue(g.text(0).startswith("▼ "))

    def test_15_expand_all_and_fold_all(self):
        self.w._expand_all(False)
        self.assertFalse(any(self.w.action_tree.topLevelItem(i).isExpanded()
                             for i in range(6)))
        self.w._expand_all(True)
        self.assertTrue(all(self.w.action_tree.topLevelItem(i).isExpanded()
                            for i in range(6)))

    def test_16_search_auto_expands_matching_group(self):
        self.w._expand_all(False)
        self.w.tree_search.setText("整单排产")
        orders = self.w.action_tree.topLevelItem(2)
        self.assertFalse(orders.isHidden())
        self.assertTrue(orders.isExpanded())
        self.w.tree_search.clear()

    def test_17_hide_show_navigation_and_menu_sync(self):
        self.w.set_nav_visible(False)
        self.assertTrue(self.w.left_panel.isHidden())
        self.assertFalse(self.w.nav_restore.isHidden())
        self.assertFalse(self.w.act_left.isChecked())
        self.w.nav_restore.click()
        self.assertFalse(self.w.left_panel.isHidden())
        self.assertTrue(self.w.nav_restore.isHidden())
        self.assertTrue(self.w.act_left.isChecked())

    def test_18_view_menu_has_fold_and_nav_actions(self):
        acts = []
        for a in self.w.menuBar().actions():
            menu = a.menu()
            if menu is not None:
                acts.extend(x.text() for x in menu.actions())
        for text in ("显示操作导航", "展开全部导航分组", "折叠全部导航分组"):
            self.assertIn(text, acts)
        # v3.3.0：新增「提醒(&R)」菜单组
        self.assertEqual(len(self.w.menuBar().actions()), 8)
        self.assertEqual(self.w.act_left.shortcut().toString(), "Ctrl+Shift+N")

    def test_19_action_bar_buttons_follow_view(self):
        seen = {}
        for key in VIEW_KEYS:
            self.w.switch_view(key)
            btns = [b.text() for b in
                    self.w.action_bar_host.findChildren(QPushButton)]
            with self.subTest(view=key):
                self.assertGreaterEqual(len(btns), 3)
                self.assertIn("更多操作 ▼", btns)
            seen[key] = btns
        self.assertIn("新增订单", seen["orders"])
        self.assertIn("订单排产", seen["orders"])
        self.assertNotIn("新增订单", seen["bids"])
        self.w.switch_view("bids")

    def test_20_more_menu_contains_current_and_maintenance_actions(self):
        self.w.switch_view("orders")
        more = [b for b in self.w.action_bar_host.findChildren(QPushButton)
                if b.objectName() == "MoreActions"][0]
        labels = [a.text() for a in more.menu().actions()]
        for name in ("新增订单", "编辑订单", "整单排产", "编辑记录", "审计留痕"):
            self.assertIn(name, labels)
        self.assertNotIn("新增招标项目", labels)
        self.w.switch_view("bids")

    def test_21_shortcut_button_runs_action(self):
        self.w.switch_view("bids")
        btn = [b for b in self.w.action_bar_host.findChildren(QPushButton)
               if b.text() == "登记中标"][0]
        with patch.object(self.w, "_run") as run:
            btn.click()
        self.assertTrue(run.called)
        self.assertEqual(run.call_args[0][0], "登记中标")
        self.assertTrue(callable(run.call_args[0][1]))

    def test_22_more_menu_entry_switches_view_when_needed(self):
        self.w.switch_view("production")
        more = [b for b in self.w.action_bar_host.findChildren(QPushButton)
                if b.objectName() == "MoreActions"][0]
        act = [a for a in more.menu().actions() if a.text() == "订单排产"][0]
        with patch.object(self.w, "_run") as run:
            act.trigger()
        self.assertEqual(self.w.current_view, "orders")
        self.assertEqual(run.call_args[0][0], "订单排产")
        self.w.switch_view("bids")


class NavigationPersistenceTest(unittest.TestCase):
    """③ 折叠/隐藏状态落到 data/ui_state.json，升级后仍在"""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v320_uistate_"))
        self.db = Database(str(self.tmp / "erp.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_23_uistate_roundtrip_and_corrupt_fallback(self):
        self.assertEqual(uistate.load(self.tmp)["nav_visible"], True)
        self.assertTrue(uistate.save(self.tmp, {
            "nav_visible": False, "nav_expanded": {"订单管理": False}}))
        st = uistate.load(self.tmp)
        self.assertFalse(st["nav_visible"])
        self.assertFalse(st["nav_expanded"]["订单管理"])
        (self.tmp / uistate.FILENAME).write_text("{坏掉的 JSON",
                                                 encoding="utf-8")
        self.assertEqual(uistate.load(self.tmp)["nav_visible"], True)
        self.assertFalse(uistate.save(None, {}))

    def test_24_new_window_restores_hidden_and_folded_state(self):
        w1 = MainWindowV2(self.db)
        w1.set_nav_visible(False)
        w1._expand_all(False)
        w1.close()
        w2 = MainWindowV2(self.db)
        self.assertTrue(w2.left_panel.isHidden())
        self.assertFalse(w2.nav_restore.isHidden())
        self.assertFalse(w2.action_tree.topLevelItem(0).isExpanded())
        self.assertTrue(w2.action_tree.topLevelItem(0).text(0).startswith("→ "))
        w2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

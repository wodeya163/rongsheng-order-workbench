#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.3.0 交期自动提醒专项回归（2026-11-08）

用户口径：
  · 距交货期 10 天开始自动提醒；
  · 状态改为「已发货」后不再提醒。

覆盖：
  A 纯逻辑：日期解析 / 10 天边界 / 终态过滤 / 逾期上限 / 排序 / 指纹 /
            状态机与审计 / 导入归一 / 交期口径一致性 / 偏好去重
  B 界面：清单弹窗 / 一键发货 / 设置弹窗 / 状态栏徽标 / 菜单与导航 /
          离屏不弹窗 / 关闭开关
"""
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import duereminder as dr
from v2.core import models, query as qb, reminder_prefs as rp
from v2.core import order_fields as ofdefs
from v2.core.db import Database
from v2.domains import OrderService

TODAY = datetime.date(2026, 11, 8)


def _iso(delta):
    return (TODAY + datetime.timedelta(days=delta)).isoformat()


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.svc = OrderService(self.db)
        self.addCleanup(self.db.close)

    def mk(self, project_no, delta, status="已确认", **kw):
        return self.svc.create_order(
            customer=kw.pop("customer", "国能沈西热电厂"),
            project_no=project_no,
            required_date=_iso(delta) if delta is not None else "",
            status=status, **kw)

    def scan(self, **kw):
        kw.setdefault("today", TODAY)
        return dr.scan(self.db, **kw)


class DateParsingTest(unittest.TestCase):

    def test_01_parse_date_formats(self):
        cases = {
            "2026-11-08": TODAY,
            "2026/11/8": TODAY,
            "2026/11/08 00:00:00": TODAY,
            "20261108": TODAY,
            "2026-11-08 12:30:00": TODAY,
            datetime.date(2026, 11, 8): TODAY,
            datetime.datetime(2026, 11, 8, 9, 30): TODAY,
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dr.parse_date(raw), want)

    def test_02_parse_date_dirty(self):
        for raw in ("", None, "脏数据", "2026-13-45", "交货期待定", "08/11/2026"):
            with self.subTest(raw=raw):
                self.assertIsNone(dr.parse_date(raw))

    def test_03_days_left_and_level(self):
        self.assertEqual(dr.days_left(_iso(10), TODAY), 10)
        self.assertEqual(dr.days_left(_iso(0), TODAY), 0)
        self.assertEqual(dr.days_left(_iso(-3), TODAY), -3)
        self.assertEqual(dr.level_of(-1), "overdue")
        self.assertEqual(dr.level_of(0), "today")
        self.assertEqual(dr.level_of(9), "soon")
        self.assertEqual(dr.label_of(-3), "已逾期 3 天")
        self.assertEqual(dr.label_of(0), "今天到期")
        self.assertEqual(dr.label_of(7), "剩余 7 天")

    def test_04_quiet_statuses(self):
        for s in ("已发货", "发货中", "已交付", "已验收", "关闭", "作废"):
            with self.subTest(status=s):
                self.assertTrue(dr.is_quiet(s))
        for s in ("草稿", "已确认", "待发货", "已排产", "生产中", ""):
            with self.subTest(status=s):
                self.assertFalse(dr.is_quiet(s))


class ScanTest(_Fixture):

    def test_05_ten_day_boundary(self):
        self.mk("D9", 9)
        self.mk("D10", 10)
        self.mk("D11", 11)
        rep = self.scan()
        self.assertEqual([r["days_left"] for r in rep["rows"]], [9, 10])
        self.assertEqual(rep["summary"]["soon"], 2)
        self.assertEqual(rep["days"], 10)

    def test_06_custom_days(self):
        self.mk("D5", 5)
        self.mk("D20", 20)
        self.assertEqual(len(self.scan(days=5)["rows"]), 1)
        self.assertEqual(len(self.scan(days=20)["rows"]), 2)

    def test_07_shipped_and_history_are_quiet(self):
        self.mk("SHIPPED", 3, "已发货")
        self.mk("SHIPPING", 3, "发货中")       # 历史库里等价于已发货
        self.mk("ACCEPTED", 3, "已验收")
        self.mk("CLOSED", 3, "关闭")
        # 「作废」只能走维护通道，不能直接建单（models.VOID_STATUS 不在 CREATABLE）
        void_order = self.mk("VOID", 3, "已确认")
        self.db.update("orders", {"status": models.VOID_STATUS},
                       "id = ?", (void_order["id"],))
        self.mk("WAIT", 3, "待发货")
        rep = self.scan()
        self.assertEqual([r["project_no"] for r in rep["rows"]], ["WAIT"])

    def test_08_overdue_still_reminded(self):
        self.mk("OD1", -1)
        self.mk("OD9", -9)
        self.mk("TODAY", 0)
        rep = self.scan()
        self.assertEqual([r["days_left"] for r in rep["rows"]], [-9, -1, 0])
        self.assertEqual(rep["summary"]["overdue"], 2)
        self.assertEqual(rep["summary"]["today"], 1)

    def test_09_long_overdue_split_and_cap_zero(self):
        self.mk("OD20", -20)
        self.mk("OD40", -40)
        self.mk("OD900", -900)
        rep = self.scan()
        self.assertEqual([r["days_left"] for r in rep["rows"]], [-20])
        self.assertEqual([r["days_left"] for r in rep["long_overdue"]],
                         [-900, -40])
        self.assertEqual(rep["summary"]["long_overdue"], 2)
        allin = self.scan(overdue_max_days=0)
        self.assertEqual(len(allin["rows"]), 3)
        self.assertEqual(allin["long_overdue"], [])

    def test_10_missing_or_dirty_date_skipped(self):
        self.mk("NONE", 3)
        o = self.db.get_by_id("orders", self.svc.create_order(
            project_no="DIRTY")["id"])
        self.db.update("orders", {"required_date": "交货期待定"},
                       "id = ?", (o["id"],))
        self.assertEqual(len(self.scan()["rows"]), 1)

    def test_11_sorted_by_urgency(self):
        for p, d in (("P10", 10), ("P-5", -5), ("P0", 0), ("P3", 3)):
            self.mk(p, d)
        self.assertEqual([r["days_left"] for r in self.scan()["rows"]],
                         [-5, 0, 3, 10])

    def test_12_signature_and_notice(self):
        self.mk("A", 3)
        self.mk("B", -2)
        rep = self.scan()
        self.assertTrue(rep["signature"])
        self.assertEqual(rep["signature"], dr.signature(rep["rows"]))
        self.assertIn("逾期 1 单", dr.notice_text(rep))
        self.assertIn("10 天内到期 1 单", dr.notice_text(rep))
        self.assertEqual(dr.signature([]), "")


class MarkShippedTest(_Fixture):

    def test_13_mark_shipped_writes_status_and_audit(self):
        o = self.mk("A", 5, "待发货")
        out = dr.mark_shipped(self.db, o["id"], "张哥")
        self.assertEqual(out["status"], models.OrderStatus.SHIPPED)
        log = self.db.query(
            "SELECT * FROM audit_log WHERE action='mark_shipped'")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["operator"], "张哥")

    def test_14_mark_shipped_removes_from_reminder(self):
        a = self.mk("A", 5, "待发货")
        self.mk("B", 6, "已确认")
        before = self.scan()
        self.assertEqual(len(before["rows"]), 2)
        dr.mark_shipped(self.db, a["id"], "张哥")
        after = self.scan()
        self.assertEqual(len(after["rows"]), 1)
        self.assertNotEqual(before["signature"], after["signature"])

    def test_15_idempotent_and_terminal_rejected(self):
        a = self.mk("A", 5, "待发货")
        dr.mark_shipped(self.db, a["id"], "张哥")
        dr.mark_shipped(self.db, a["id"], "张哥")
        self.assertEqual(len(self.db.query(
            "SELECT * FROM audit_log WHERE action='mark_shipped'")), 1)
        b = self.mk("B", 5, "已验收")
        with self.assertRaises(Exception):
            dr.mark_shipped(self.db, b["id"], "张哥")

    def test_16_missing_order(self):
        with self.assertRaises(Exception):
            dr.mark_shipped(self.db, 999999, "张哥")

    def test_16b_shipped_order_cannot_reopen_scheduling(self):
        """已发货单不得再建排产单，否则 auto_fill_if_ready 会把状态打回「已排产」重新提醒"""
        from v2.domains import OrderScheduleService

        with tempfile.TemporaryDirectory(prefix="due_sched_") as root:
            o = self.mk("A", 5, "待发货")
            item = self.svc.add_item(o["id"], item_name="耐磨弯头",
                                     qty_ordered=3, unit_price=100.0)
            dr.mark_shipped(self.db, o["id"], "张哥")
            sched = OrderScheduleService(self.db, root)
            with self.assertRaises(Exception):
                sched.ensure(item["id"], "张哥")
            self.assertEqual(
                self.db.get_by_id("orders", o["id"])["status"],
                models.OrderStatus.SHIPPED)

    def test_17_status_machine_and_alias(self):
        self.assertIn(models.OrderStatus.SHIPPED,
                      models.OrderStatus.CREATABLE)
        self.assertIn(models.OrderStatus.SHIPPED,
                      models.OrderStatus.NO_REMIND)
        for old in ("草稿", "已确认", "待发货", "已排产", "生产中", "发货中"):
            with self.subTest(old=old):
                models.assert_transition("order", old,
                                         models.OrderStatus.SHIPPED)
        self.assertEqual(ofdefs._status("已发货")[0],
                         models.OrderStatus.SHIPPED)
        status_spec = [f for f in ofdefs.form_head_fields()
                       if f["key"] == "status"][0]
        self.assertIn("已发货", [o[0] for o in status_spec["options"]])

    def test_18_query_due_state_consistent(self):
        self.assertEqual(qb.due_state(_iso(0), "已发货", TODAY), "normal")
        self.assertEqual(qb.due_state(_iso(0), "发货中", TODAY), "normal")
        self.assertEqual(qb.due_state(_iso(0), "待发货", TODAY), "soon")
        self.assertEqual(qb.due_state(_iso(-1), "待发货", TODAY), "overdue")
        sql, params = qb.due_clause("soon")
        self.assertIn("NOT IN", sql)
        self.assertIn("已发货", params)
        self.assertIn("发货中", params)


class PrefsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="due_prefs_")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_19_defaults_and_roundtrip(self):
        s = rp.load(self.dir)
        self.assertEqual((s["days"], s["enabled"], s["popup"], s["tray"]),
                         (10, True, True, True))
        s["days"] = 21
        s["tray"] = False
        self.assertTrue(rp.save(self.dir, s))
        t = rp.load(self.dir)
        self.assertEqual((t["days"], t["tray"]), (21, False))
        self.assertTrue(Path(self.dir, "reminder.json").is_file())

    def test_20_corrupt_or_memory_dir(self):
        Path(self.dir, "reminder.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(rp.load(self.dir)["days"], 10)
        self.assertEqual(rp.load(None)["days"], 10)
        self.assertFalse(rp.save(None, {}))

    def test_21_clamp_out_of_range(self):
        Path(self.dir, "reminder.json").write_text(
            '{"days": 9999, "overdue_max_days": -5}', encoding="utf-8")
        s = rp.load(self.dir)
        self.assertEqual(s["days"], 365)
        self.assertEqual(s["overdue_max_days"], 0)

    def test_22_popup_dedup_and_snooze(self):
        s = rp.load(self.dir)
        self.assertTrue(rp.should_popup(s, "sig1", TODAY))
        rp.mark_popped(s, "sig1", TODAY)
        self.assertFalse(rp.should_popup(s, "sig1", TODAY))
        self.assertTrue(rp.should_popup(s, "sig2", TODAY))
        self.assertTrue(rp.should_popup(s, "sig1", TODAY + datetime.timedelta(days=1)))
        rp.snooze_today(s, TODAY)
        self.assertFalse(rp.should_popup(s, "sig2", TODAY))
        self.assertTrue(rp.should_popup(s, "sig2", TODAY + datetime.timedelta(days=1)))
        s["enabled"] = False
        self.assertFalse(rp.should_popup(s, "sig3", TODAY))
        self.assertFalse(rp.should_popup(s, "", TODAY))


class DueUiTest(_Fixture):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from v2.app_v2 import ensure_font
        from v2.ui import theme
        cls.app = QApplication.instance() or QApplication([])
        ensure_font(cls.app)
        theme.apply_theme(cls.app)

    def setUp(self):
        super().setUp()
        from v2.ui.main_window_v2 import MainWindowV2
        self.mk("D10", 10, "待发货")
        self.mk("D0", 0, "已确认")
        self.mk("D-5", -5, "生产中")
        self.mk("D-SHP", 3, "已发货")
        self.mk("D-OLD", -500, "待发货")
        self.w = MainWindowV2(self.db)
        self.addCleanup(self.w.close)

    def test_23_badge_and_menu(self):
        self.assertIn("交期提醒：3 单", self.w.due_badge.text())
        self.assertIn("长期逾期 1", self.w.due_badge.text())
        self.assertEqual(self.w.due_badge.property("level"), "danger")
        self.assertIn("提醒(&R)", [m.text() for m in self.w.menuBar().actions()])
        self.assertTrue(hasattr(self.w, "_auto_due_check"))
        self.assertTrue(hasattr(self.w, "_due_mark_shipped"))
        from v2.ui.panels.common import ACTION_GROUPS
        names = [a[0] for g in ACTION_GROUPS
                 if g[1] == "orders" for a in g[2]]
        for n in ("临期订单", "标记已发货", "交期提醒设置"):
            self.assertIn(n, names)

    def test_24_dialog_rows_and_buttons(self):
        from v2.ui.due_dialog import DueReminderDialog
        d = DueReminderDialog(self.w, self.w._due_report)
        self.addCleanup(d.close)
        self.assertEqual(d.table.rowCount(), 3)
        self.assertEqual(d.table.columnCount(), 6)
        self.assertIn("共 3 单", d.hint.text())
        self.assertEqual([b.text() for b in (d.btn_mark, d.btn_view,
                                              d.btn_snooze, d.btn_settings,
                                              d.btn_close)],
                         ["标记已发货", "查看订单", "今日不再提醒",
                          "提醒设置", "关闭"])
        self.assertIsNotNone(d.selected_order_id())

    def test_25_dialog_mark_button_removes_row(self):
        from v2.ui.due_dialog import DueReminderDialog
        d = DueReminderDialog(self.w, self.w._due_report,
                              on_mark_shipped=self.w._due_mark_shipped)
        self.addCleanup(d.close)
        oid = d.selected_order_id()
        d.btn_mark.click()
        self.assertEqual(d.table.rowCount(), 2)
        self.assertEqual(
            self.db.get_by_id("orders", oid)["status"],
            models.OrderStatus.SHIPPED)
        self.assertIn("交期提醒：2 单", self.w.due_badge.text())

    def test_26_settings_dialog_values(self):
        from v2.ui.due_dialog import ReminderSettingsDialog
        s = ReminderSettingsDialog(self.w, self.w._due_prefs)
        self.addCleanup(s.close)
        self.assertEqual(s.values()["days"], 10)
        s.spin_days.setValue(15)
        s.chk_tray.setChecked(False)
        v = s.values()
        self.assertEqual((v["days"], v["tray"]), (15, False))

    def test_27_snooze_and_view_order(self):
        oid = self.w._due_report["rows"][0]["id"]
        doc = self.db.get_by_id("orders", oid)["doc_no"]
        self.w._due_view_order(oid)
        self.assertEqual(self.w.current_view, "orders")
        self.assertEqual(self.w.query_panel.values()["keyword"], doc)
        self.w._due_snooze()
        self.assertEqual(self.w._due_prefs["snooze_date"],
                         datetime.date.today().isoformat())

    def test_28_disabled_badge(self):
        self.w._due_prefs["enabled"] = False
        self.w._refresh_due_badge()
        self.assertIn("已关闭", self.w.due_badge.text())
        self.assertEqual(self.w.due_badge.property("level"), "off")
        self.assertEqual(self.w._due_report["signature"], "")

    def test_29_headless_auto_check_never_pops(self):
        with mock.patch.object(self.w, "_show_due_reminders") as pop:
            self.w._auto_due_check()
            pop.assert_not_called()
        self.assertIn("交期提醒：3 单", self.w.due_badge.text())

    def test_30_show_dialog_via_badge(self):
        from v2.ui.due_dialog import DueReminderDialog
        with mock.patch.object(DueReminderDialog, "exec", return_value=0):
            self.assertEqual(self.w._show_due_reminders(), 0)

    def test_31_main_window_stays_slim(self):
        lines = len(Path("v2/ui/main_window_v2.py").read_text(
            encoding="utf-8").splitlines())
        self.assertLess(lines, 900)


if __name__ == "__main__":
    unittest.main(verbosity=2)

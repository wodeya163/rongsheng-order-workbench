#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.9.2 订单编辑回归（更改 + 删除）

覆盖：
  1. 订单头字段编辑（含订单来源）落库 + 审计留痕；
  2. 明细数量/单价改动 → 总价与订单头价税合计自动重算；
  3. 新增明细行（行号追加、头合计同步）、删除明细行；
  4. 硬约束：已有排产的明细不能删、数量不能改到低于已排量、作废订单不可编辑；
  5. 价税合计清空 = 解除手工锁 → 回到自动汇总；
  6. 整单删除（级联明细 + 审计）；有排产关联时禁止删除；
  7. 离屏对话框：加载头/明细、改动被正确采集为变更集。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.core.db import Database, now
from v2.core.errors import ReferencedError, StateError, ValidationError
from v2.domains import MaintenanceService, OrderService

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class OrderEditServiceTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.maint = MaintenanceService(self.db)
        self.oid = int(self.orders.create_from_spec({
            "customer": "国能九江", "order_type": "外部订单", "status": "已确认",
            "project_no": "U-100", "order_source": "招投标",
            "required_date": "2026-12-31",
            "items": [
                {"item_name": "耐磨弯头", "spec": "DN250", "unit": "件",
                 "qty_ordered": 10, "unit_price": 1000},
                {"item_name": "三通", "spec": "DN200", "unit": "件",
                 "qty_ordered": 5, "unit_price": 200},
            ],
        })["id"])
        self.items = self.orders.list_items(self.oid)

    def tearDown(self):
        self.db.close()

    # ── 头字段 ────────────────────────────────────────────
    def test_01_head_edit_and_audit(self):
        res = self.orders.apply_edits(
            self.oid,
            head={"customer": "国能沈西", "order_source": "厦门龙净",
                  "erp_order_no": "4507723757", "remark": "改过"},
            operator="测试员")
        o = res["order"]
        self.assertEqual(o["customer"], "国能沈西")
        self.assertEqual(o["order_source"], "厦门龙净")
        self.assertEqual(o["erp_order_no"], "4507723757")
        rows = self.maint.audit_trail("order", self.oid)
        self.assertTrue(any(r["action"] == "edit" for r in rows))
        self.assertIn("厦门龙净", str(rows[0]["after_json"]))

    def test_02_illegal_head_field_rejected(self):
        with self.assertRaises(ValidationError):
            self.orders.apply_edits(self.oid, head={"status": "已交付"})

    # ── 明细 ─────────────────────────────────────────────
    def test_03_item_qty_change_recalcs_amount_and_total(self):
        iid = int(self.items[0]["id"])
        before_total = float(self.db.get_by_id("orders", self.oid)["amount_total"])
        self.assertAlmostEqual(before_total, 11_000.0, places=2)
        self.orders.apply_edits(
            self.oid, item_updates=[(iid, {"qty_ordered": 12})], operator="测试员")
        item = self.db.get_by_id("order_items", iid)
        self.assertEqual(float(item["qty_ordered"]), 12.0)
        self.assertAlmostEqual(float(item["amount"]), 12_000.0, places=2)
        o = self.db.get_by_id("orders", self.oid)
        self.assertAlmostEqual(float(o["amount_total"]), 13_000.0, places=2)

    def test_04_add_item_appends_line_and_syncs(self):
        res = self.orders.apply_edits(
            self.oid,
            new_items=[{"item_name": "法兰", "spec": "DN100", "unit": "片",
                        "qty_ordered": 4, "unit_price": 50}],
            operator="测试员")
        self.assertEqual(res["added"], 1)
        items = self.orders.list_items(self.oid)
        self.assertEqual(len(items), 3)
        new = items[-1]
        self.assertEqual(new["item_name"], "法兰")
        self.assertEqual(new["line_no"], 3)
        o = self.db.get_by_id("orders", self.oid)
        self.assertAlmostEqual(float(o["amount_total"]), 11_200.0, places=2)
        self.assertAlmostEqual(float(o["item_qty_total"]), 3.0, places=2)

    def test_05_delete_item(self):
        iid = int(self.items[1]["id"])
        res = self.orders.apply_edits(
            self.oid, deleted_item_ids=[iid], operator="测试员")
        self.assertEqual(res["deleted"], 1)
        self.assertIsNone(self.db.get_by_id("order_items", iid))
        o = self.db.get_by_id("orders", self.oid)
        self.assertAlmostEqual(float(o["amount_total"]), 10_000.0, places=2)

    def test_06_new_item_validation(self):
        with self.assertRaises(ValidationError):
            self.orders.apply_edits(
                self.oid, new_items=[{"item_name": "", "qty_ordered": 1}])
        with self.assertRaises(ValidationError):
            self.orders.apply_edits(
                self.oid, new_items=[{"item_name": "X", "qty_ordered": 0}])

    # ── 硬约束 ────────────────────────────────────────────
    def test_07_link_blocks_delete_and_qty_reduction(self):
        iid = int(self.items[0]["id"])
        pid = self.db.insert("production_projects", {
            "doc_no": "SC-1", "title": "排产项目", "status": "待排产",
            "created_at": now(), "updated_at": now()})
        self.db.insert("production_links", {
            "project_id": pid, "order_item_id": iid, "allocated_qty": 6,
            "order_no_snapshot": "DD-1", "order_line_snapshot": "L1",
            "order_type_snapshot": "外部订单", "created_at": now()})
        with self.assertRaises(ValidationError):
            self.orders.apply_edits(self.oid, deleted_item_ids=[iid])
        self.assertIsNotNone(self.db.get_by_id("order_items", iid))
        with self.assertRaises(ValidationError):
            self.orders.apply_edits(self.oid, item_updates=[(iid, {"qty_ordered": 5})])
        # 不低于已排量则允许
        self.orders.apply_edits(self.oid, item_updates=[(iid, {"qty_ordered": 8})])
        self.assertEqual(float(self.db.get_by_id("order_items", iid)["qty_ordered"]), 8.0)

    def test_08_void_order_not_editable(self):
        self.maint.void("order", self.oid, reason="测试作废")
        with self.assertRaises(StateError):
            self.orders.apply_edits(self.oid, head={"customer": "x"})

    # ── 手工锁清除 ────────────────────────────────────────
    def test_09_clear_amount_returns_to_auto(self):
        self.orders.apply_edits(self.oid, head={"amount_total": "99999.00"})
        o = self.db.get_by_id("orders", self.oid)
        self.assertAlmostEqual(float(o["amount_total"]), 99_999.0, places=2)
        self.assertEqual(int(o["amount_manual"]), 1)
        res = self.orders.apply_edits(self.oid, head={"amount_total": ""})
        o = res["order"]
        self.assertAlmostEqual(float(o["amount_total"]), 11_000.0, places=2)
        self.assertEqual(int(o["amount_manual"]), 0)
        self.assertIn("amount", res["cleared"])

    # ── 整单删除 ──────────────────────────────────────────
    def test_10_delete_order_cascades_items(self):
        res = self.maint.delete("order", self.oid, operator="测试员")
        self.assertEqual(res["deleted_children"].get("order_items"), 2)
        self.assertIsNone(self.db.get_by_id("orders", self.oid))
        self.assertEqual(len(self.orders.list_items(self.oid)), 0)
        actions = [r["action"] for r in self.maint.audit_trail("order", self.oid)]
        self.assertIn("delete", actions)
        self.assertIn("delete_child", actions)

    def test_11_delete_order_blocked_by_links(self):
        iid = int(self.items[0]["id"])
        pid = self.db.insert("production_projects", {
            "doc_no": "SC-2", "title": "排产项目", "status": "待排产",
            "created_at": now(), "updated_at": now()})
        self.db.insert("production_links", {
            "project_id": pid, "order_item_id": iid, "allocated_qty": 1,
            "order_no_snapshot": "DD-2", "order_line_snapshot": "L1",
            "order_type_snapshot": "外部订单", "created_at": now()})
        with self.assertRaises(ReferencedError):
            self.maint.delete("order", self.oid)
        self.assertIsNotNone(self.db.get_by_id("orders", self.oid))


class OrderEditDialogTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.oid = int(self.orders.create_from_spec({
            "customer": "国能九江", "order_type": "外部订单", "status": "已确认",
            "project_no": "U-200", "order_source": "北电电力",
            "items": [{"item_name": "弯头", "spec": "DN100", "unit": "件",
                       "qty_ordered": 3, "unit_price": 100}],
        })["id"])

    def tearDown(self):
        self.db.close()

    def _dialog(self):
        from v2.ui.dialogs import OrderEditDialog
        return OrderEditDialog(None, self.db, self.oid)

    def test_21_loads_head_and_items(self):
        dlg = self._dialog()
        self.assertIn("U-200", dlg.edit_project_no.text())
        self.assertEqual(dlg.combo_source.currentText(), "北电电力")
        self.assertEqual(dlg.table.rowCount(), 1)
        self.assertEqual(dlg._cell(dlg.table, 0, 2), "弯头")
        self.assertEqual(dlg._row_id(0), int(self.orders.list_items(self.oid)[0]["id"]))
        dlg.close()

    def test_22_collect_changes(self):
        dlg = self._dialog()
        dlg.edit_customer.setText("国能沈西")
        dlg.table.item(0, 6).setText("6")          # 数量 3 → 6
        dlg._collect()
        head, updates, news, deleted = dlg.result_changes()
        self.assertEqual(head.get("customer"), "国能沈西")
        self.assertEqual(len(updates), 1)
        self.assertAlmostEqual(float(updates[0][1]["qty_ordered"]), 6.0)
        self.assertEqual((news, deleted), ([], []))
        dlg.close()

    def test_23_add_and_remove_rows(self):
        dlg = self._dialog()
        dlg._append_row()
        r = dlg.table.rowCount() - 1
        dlg.table.item(r, 2).setText("法兰")
        dlg.table.item(r, 6).setText("2")
        dlg.table.item(r, 7).setText("30")
        lines = [dlg._cell(dlg.table, i, 2) for i in range(dlg.table.rowCount())]
        self.assertIn("法兰", lines)
        dlg.table.setCurrentCell(0, 2)
        dlg._remove_row()
        dlg._collect()
        _head, _updates, news, deleted = dlg.result_changes()
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["item_name"], "法兰")
        self.assertEqual(len(deleted), 1)         # 原有行被移除
        dlg.close()

    def test_24_empty_row_ignored_and_no_change_quiet(self):
        dlg = self._dialog()
        _h, updates, news, deleted = dlg._diff_items()
        self.assertEqual((updates, news, deleted), ([], [], []))
        dlg.edit_project_no.setText("U-200")      # 未变
        self.assertEqual(dlg._diff_head(), {})
        dlg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

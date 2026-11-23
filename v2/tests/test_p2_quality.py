#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 工程质量回归（2026-11-07）

覆盖四件事：
  1. 表名白名单（tables.py）与 schema.sql 一一对应，非法表名在拼 SQL 前被拦；
  2. 查询方案（presets.py）：保存 / 覆盖 / 删除 / 读取，条件不落空字段；
  3. 交期预警：SQL 口径与 Python 口径一致，已验收订单不再催交；
  4. 主窗口拆分不回归：模块职责边界（panels 不反向 import 主窗口）、
     对外符号仍在原路径、主文件行数受控。
"""
import json
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.core import query as qbuild
from v2.core import tables as tbl
from v2.core.db import Database
from v2.core.errors import DuplicateError, ValidationError
from v2.core.presets import QueryPresetService
from v2.domains import BidService, OrderService

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TableWhitelistTest(unittest.TestCase):
    def test_01_declaration_matches_schema(self):
        schema = Path(__file__).resolve().parents[1] / "core" / "schema.sql"
        self.assertEqual(tbl.missing_from_declaration(schema), set(),
                         "schema.sql 里的表必须在 core/tables.py 登记")
        self.assertEqual(tbl.undeclared_in_ddl(schema), set(),
                         "core/tables.py 登记的表必须在 schema.sql 里有 DDL")

    def test_02_rejects_unknown_table(self):
        db = Database(":memory:")
        for bad in ("orders; DROP TABLE orders", "sqlite_master", "", None, "ORDERS"):
            with self.assertRaises((tbl.TableNameError, ValueError)):
                db.insert(bad, {"id": 1})
        db.close()

    def test_03_rejects_bad_column_name(self):
        db = Database(":memory:")
        with self.assertRaises(ValueError):
            db.insert("orders", {"doc_no) VALUES (1); --": 1})
        db.close()

    def test_04_valid_crud_still_works(self):
        db = Database(":memory:")
        pid = db.insert("bid_projects", {"doc_no": "BX-1", "project_name": "测试",
                                         "customer": "甲", "status": "草稿",
                                         "created_at": "2026-11-07T10:00:00",
                                         "updated_at": "2026-11-07T10:00:00"})
        db.update("bid_projects", {"status": "中标"}, "id = ?", (pid,))
        self.assertEqual(db.get_by_id("bid_projects", pid)["status"], "中标")
        db.close()


class QueryPresetTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.svc = QueryPresetService(self.db)
        self.orders = OrderService(self.db)

    def tearDown(self):
        self.db.close()

    def test_11_defaults_seeded(self):
        """迁移 v4 预置 4 个方案，方便开箱即用"""
        names = [p["name"] for p in self.svc.list()]
        self.assertEqual(names, ["待发货订单", "逾期未交", "7 天内到期", "还有可排量"])
        self.assertEqual(self.svc.list("orders")[0]["view_key"], "orders")
        self.assertEqual(self.svc.list("bids"), [])

    def test_12_save_and_apply(self):
        p = self.svc.save("我的逾期单", "orders",
                          {"status": "待发货", "due": "overdue",
                           "keyword": "", "won_only": False})
        self.assertEqual(p["name"], "我的逾期单")
        # 空条件不落库，避免方案里存一堆空串
        self.assertEqual(p["filters"], {"status": "待发货", "due": "overdue"})
        self.assertEqual(self.svc.get(p["id"])["filters"]["due"], "overdue")
        self.assertIn("我的逾期单", [x["name"] for x in self.svc.list("orders")])

    def test_13_duplicate_needs_overwrite(self):
        self.svc.save("同名", "orders", {"status": "待发货"})
        with self.assertRaises(DuplicateError):
            self.svc.save("同名", "orders", {"status": "已验收"})
        p = self.svc.save("同名", "orders", {"status": "已验收"}, overwrite=True)
        self.assertEqual(p["filters"], {"status": "已验收"})
        self.assertEqual(len([x for x in self.svc.list() if x["name"] == "同名"]), 1)

    def test_14_name_validation_and_delete(self):
        with self.assertRaises(ValidationError):
            self.svc.save("   ", "orders", {})
        with self.assertRaises(ValidationError):
            self.svc.save("x" * 41, "orders", {})
        p = self.svc.save("待删", "orders", {"due": "soon"})
        self.svc.delete(p["id"])
        self.assertIsNone(self.svc.find_by_name("待删"))
        self.svc.delete(p["id"])                       # 幂等：删不存在的方案不报错

    def test_15_filters_json_roundtrip(self):
        filters = {"keyword": "CG2024", "date_from": "2026-01-01",
                   "date_to": "2026-12-31", "has_capacity": True}
        p = self.svc.save("复杂条件", "orders", filters)
        row = self.db.get_by_id("query_presets", p["id"])
        self.assertEqual(json.loads(row["filters"])["keyword"], "CG2024")
        self.assertEqual(self.svc.get(p["id"])["filters"]["has_capacity"], True)

    def test_16_broken_json_falls_back_to_empty(self):
        pid = self.db.insert("query_presets", {
            "name": "坏数据", "view_key": "orders", "filters": "{not json",
            "sort_order": 1, "created_at": "2026-11-07T10:00:00"})
        self.assertEqual(self.svc.get(pid)["filters"], {})


class DueWarningTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.today = date.today()

    def tearDown(self):
        self.db.close()

    def _mk(self, project_no, required, status="已确认"):
        return self.orders.create_from_spec({
            "customer": "甲", "order_type": "外部订单", "status": status,
            "project_no": project_no, "required_date": required.isoformat(),
            "items": [{"item_name": "弯头", "spec": "DN100", "unit": "件",
                       "qty_ordered": 1, "unit_price": 10}],
        })

    def test_21_python_side_states(self):
        self.assertEqual(qbuild.due_state("", "待发货"), "none")
        self.assertEqual(qbuild.due_state("2026-13-99", "待发货"), "none")
        self.assertEqual(qbuild.due_state((self.today - timedelta(days=1)).isoformat(),
                                          "待发货"), "overdue")
        self.assertEqual(qbuild.due_state((self.today + timedelta(days=3)).isoformat(),
                                          "待发货"), "soon")
        self.assertEqual(qbuild.due_state((self.today + timedelta(days=30)).isoformat(),
                                          "待发货"), "normal")
        # 已验收 = 已交付，不再催交
        self.assertEqual(qbuild.due_state((self.today - timedelta(days=30)).isoformat(),
                                          "已验收"), "normal")

    def test_22_sql_matches_python(self):
        self._mk("D-OVER", self.today - timedelta(days=5))
        self._mk("D-SOON", self.today + timedelta(days=2))
        self._mk("D-OK", self.today + timedelta(days=60))
        self._mk("D-DONE", self.today - timedelta(days=9), status="已验收")

        def ids(state):
            sql, params = qbuild.due_clause(state)
            if not sql:
                return set()
            rows = self.db.query(f"SELECT o.project_no FROM orders o WHERE {sql}",
                                 tuple(params))
            return {r["project_no"] for r in rows}

        self.assertEqual(ids("overdue"), {"D-OVER"})
        self.assertEqual(ids("soon"), {"D-SOON"})
        self.assertEqual(ids(""), set())              # 空条件不过滤（返回空片段）
        self.assertEqual(qbuild.due_clause(""), ("", []))

    def test_23_status_not_finished_excluded(self):
        """已交付 / 已验收 / 关闭 / 作废 都不再催交（作废只能走维护通道，这里直接改库）"""
        self._mk("D-CLOSED", self.today - timedelta(days=3), status="关闭")
        o = self._mk("D-VOID", self.today - timedelta(days=3))
        self.db.update("orders", {"status": "作废"}, "id = ?", (o["id"],))
        sql, params = qbuild.due_clause("overdue")
        rows = self.db.query(f"SELECT o.project_no FROM orders o WHERE {sql}", tuple(params))
        self.assertEqual([r["project_no"] for r in rows], [])
        for status in ("已交付", "已验收", "关闭", "作废"):
            self.assertEqual(qbuild.due_state(
                (self.today - timedelta(days=9)).isoformat(), status), "normal")


class SplitStructureTest(unittest.TestCase):
    """P2-2 拆分后的结构约束：以后改代码别把边界又糊回去"""

    UI = Path(__file__).resolve().parents[1] / "ui"

    def test_31_panels_do_not_import_main_window(self):
        for p in (self.UI / "panels").glob("*.py"):
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("from ..main_window_v2 import", text,
                             f"{p.name} 反向依赖主窗口会造成循环导入")
            self.assertNotIn("import main_window_v2", text, f"{p.name} 反向依赖主窗口")

    def test_32_main_window_kept_slim(self):
        lines = len((self.UI / "main_window_v2.py").read_text(encoding="utf-8").splitlines())
        self.assertLess(lines, 900, "主窗口应只保留布局与入口，拆分前是 2,118 行")

    def test_33_legacy_symbols_still_exported(self):
        from v2.ui import main_window_v2 as mw

        for name in ("MainWindowV2", "ACTION_GROUPS", "VIEW_NAMES", "VIEW_KEYS",
                     "TABLE_ENTITIES", "fill_table", "selected_id", "select_id"):
            self.assertTrue(hasattr(mw, name), f"旧路径应仍可 import {name}")

    def test_34_mixins_have_no_duplicate_methods(self):
        from v2.ui.panels import bid_code_actions, maint_actions, order_actions, views

        seen = {}
        for mod in (views, bid_code_actions, order_actions, maint_actions):
            cls = [v for k, v in vars(mod).items() if k.endswith("Mixin")][0]
            for name in vars(cls):
                if name.startswith("__"):
                    continue
                self.assertNotIn(name, seen, f"{name} 在 {seen.get(name)} 与 {mod.__name__} 重复定义")
                seen[name] = mod.__name__
        self.assertGreater(len(seen), 60, "拆分后方法总数应与拆分前相当")


class UiSmokeTest(unittest.TestCase):
    """离屏构建：交期列、方案下拉、筛选下推都要真的能用"""

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        today = date.today()
        for i, day in enumerate((-3, 2, 90)):
            self.orders.create_from_spec({
                "customer": f"客户{i}", "order_type": "外部订单", "status": "已确认",
                "project_no": f"U-{i}", "required_date": (today + timedelta(days=day)).isoformat(),
                "items": [{"item_name": "弯头", "spec": "DN100", "unit": "件",
                           "qty_ordered": 2, "unit_price": 10}],
            })
        from v2.ui.main_window_v2 import MainWindowV2
        self.w = MainWindowV2(self.db)

    def tearDown(self):
        self.w = None
        self.db.close()

    def test_41_due_column_and_badge(self):
        self.w.switch_view("orders")
        headers = [self.w.tbl_orders.horizontalHeaderItem(i).text()
                   for i in range(self.w.tbl_orders.columnCount())]
        self.assertIn("交期状态", headers)
        idx = headers.index("交期状态")
        states = {self.w.tbl_orders.item(r, idx).text()
                  for r in range(self.w.tbl_orders.rowCount())}
        self.assertEqual(states, {"已逾期", "临期", "正常"})
        self.assertIn("逾期 1", self.w.count_badge.text())
        self.assertIn("临期 1", self.w.count_badge.text())

    def test_42_due_filter_pushed_down(self):
        self.w.switch_view("orders")
        self.w.filters = {"due": "overdue"}
        self.w.page_offsets = {k: 0 for k in self.w.page_offsets}
        self.w.refresh_all()
        self.assertEqual(self.w.page_state["orders"][2], 1)
        self.assertEqual(self.w.tbl_orders.rowCount(), 1)

    def test_43_preset_apply_fills_controls(self):
        svc = QueryPresetService(self.db)
        p = svc.save("只看逾期", "orders", {"due": "overdue"})
        self.w.query_panel.reload_presets()
        self.w._apply_query_preset(p["id"])
        self.assertEqual(self.w.filters.get("due"), "overdue")
        self.assertEqual(self.w.page_state["orders"][2], 1)
        self.assertEqual(self.w.query_panel.widgets["due"].currentData(), "overdue")

    def test_44_preset_visible_only_with_service(self):
        self.assertTrue(self.w.query_panel.preset_row.isVisibleTo(self.w.query_panel))


class QueryPanelV291Test(unittest.TestCase):
    """v2.9.1 订单查询窗口收敛：六字段 + 关键字模糊，顶部/底部说明文字移除"""

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        for i in range(3):
            self.orders.create_from_spec({
                "customer": f"客户{i}", "order_type": "外部订单", "status": "已确认",
                "project_no": f"U-{i}", "required_date": "2026-12-01",
                "items": [{"item_name": "弯头", "spec": "DN100", "unit": "件",
                           "qty_ordered": 2, "unit_price": 10}],
            })
        from v2.ui.main_window_v2 import MainWindowV2
        self.w = MainWindowV2(self.db)

    def tearDown(self):
        self.w = None
        self.db.close()

    def _row_label(self, key):
        from PySide6.QtWidgets import QLabel
        return self.w.query_panel.rows[key].findChild(QLabel).text()

    def test_51_orders_fields_and_labels(self):
        self.w.switch_view("orders")
        qp = self.w.query_panel
        visible = {k for k, row in qp.rows.items() if row.isVisibleTo(qp)}
        self.assertEqual(visible, {"keyword", "project_no", "customer",
                                   "date", "order_type", "status", "due"})
        for key, label in (("project_no", "订单项目编号"), ("customer", "客户名称"),
                           ("date", "下单时间"), ("order_type", "订单类型"),
                           ("status", "订单状态"), ("due", "交货期")):
            self.assertEqual(self._row_label(key), label, key)

    def test_52_panel_texts_removed(self):
        qp = self.w.query_panel
        self.assertFalse(hasattr(qp, "desc"), "顶部说明文字应已移除")
        self.assertFalse(hasattr(qp, "policy"), "底部业务口径长文应已移除")
        qp.set_hit(3, 3)
        self.assertEqual(qp.hit.text(), "命中 3 条 / 全库 3 条")

    def test_53_project_no_filter_pushed_down(self):
        self.w.switch_view("orders")
        self.w.filters = {"project_no": "U-1"}
        self.w.page_offsets = {k: 0 for k in self.w.page_offsets}
        self.w.refresh_all()
        self.assertEqual(self.w.page_state["orders"][2], 1)
        self.assertEqual(self.w.tbl_orders.rowCount(), 1)
        self.assertIn("U-1", self.w.tbl_orders.item(0, 0).text())

    def test_54_values_and_summary_carry_project_no(self):
        self.w.switch_view("orders")
        qp = self.w.query_panel
        qp.apply_filters({"project_no": "U-2", "status": "已确认"})
        self.assertEqual(qp.values()["project_no"], "U-2")
        self.assertIn("项目编号「U-2」", qp.summary())

    def test_55_build_where_project_column(self):
        where, params = qbuild.build_where(
            {"project_no": "U-1"}, ["o.doc_no"], project_column="o.project_no")
        self.assertIn("o.project_no", where)
        self.assertIn("LIKE", where)
        self.assertEqual(params, ["%U-1%"])
        # 不支持 project_column 时（旧调用方）不产生该条件
        where2, params2 = qbuild.build_where({"project_no": "U-1"}, ["o.doc_no"])
        self.assertEqual((where2, params2), ("", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.5.3 回归：两段式建单（先建订单头，再选中订单录入 / 导入明细）

锁定三条硬口径：
  1. 新建订单允许 0 明细；批量导入订单仍必须带明细（默认口径不变）；
  2. 「导入订单明细」只解析 9 列明细、不分组、不建订单头 —— 根除
     「一张 30 行物资表被拆成 30 张空壳订单」；
  3. 两个动作与「新增订单」同处导航 / 菜单 / 快捷栏，且订单主表右键可达。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTableWidgetItem
from PySide6.QtCore import QTimer

from v2.core import excelio
from v2.core import order_fields as ofdefs
from v2.core.db import CURRENT_SCHEMA_VERSION, Database
from v2.domains import OrderService
from v2.ui import dialogs, theme

ROOT = Path(__file__).resolve().parents[2]
REAL_WORKBOOK = ROOT / "销售订单汇总（24年7月-26年）.xlsx"


class OrderItemsV353Test(unittest.TestCase):
    """服务层 / 解析层 / UI / 接线四层回归"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)
        cls._notify = theme.notify
        theme.notify = lambda *a, **k: None       # 离屏：屏蔽模态提示

    @classmethod
    def tearDownClass(cls):
        theme.notify = cls._notify

    def setUp(self):
        self.db = Database(":memory:")
        self.db.init_schema()
        self.svc = OrderService(self.db)
        self.tmp = tempfile.mkdtemp(prefix="v353t_")
        self.addCleanup(self.db.close)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ── 工具 ────────────────────────────────────────────────
    def _order(self, **head):
        spec = {"customer": "测试客户", "project_no": "V353-TEST",
                "order_type": "外部订单"}
        spec.update(head)
        return self.svc.create_from_spec(spec, operator="测试",
                                          allow_empty_items=True)

    def _write(self, name, matrix):
        """matrix[0] 写进工作表首行，其余为数据行"""
        p = str(Path(self.tmp) / name)
        excelio.export_workbook(p, {"明细": (matrix[0], matrix[1:])})
        return p

    # ── 1. 服务层 ──────────────────────────────────────────
    def test_01_schema_version_unchanged_v7(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        self.assertEqual(self.db.scalar(
            "SELECT version FROM schema_meta ORDER BY rowid DESC LIMIT 1"), 7)

    def test_02_empty_items_only_allowed_with_explicit_flag(self):
        from v2.core.errors import ValidationError
        spec = {"customer": "测试客户", "order_type": "外部订单", "items": []}
        with self.assertRaises(ValidationError):
            self.svc.create_from_spec(spec, "测试")
        o = self.svc.create_from_spec(spec, "测试", allow_empty_items=True)
        self.assertEqual(self.svc.summary(o["id"])["item_count"], 0)
        self.assertEqual(float(o["amount_total"]), 0.0)

    def test_03_add_items_bulk_writes_rows_and_recomputes_totals(self):
        o = self._order()
        res = self.svc.add_items_bulk(o["id"], [
            {"item_name": "耐磨弯头", "qty_ordered": 3, "unit_price": 3690},
            {"item_name": "波纹补偿器", "qty_ordered": 2, "unit_price": 4980},
        ], operator="测试")
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["item_count"], 2)
        self.assertEqual(res["amount_total"], 11070 + 9960)
        rows = self.svc.list_items(o["id"])
        self.assertEqual([r["line_no"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["production_name"], "耐磨弯头")
        self.assertEqual(rows[0]["unit"], "件")
        acts = [dict(r) for r in self.db.query(
            "SELECT * FROM audit_log WHERE entity='order' AND action='add_items_bulk'")]
        self.assertEqual(len(acts), 1)
        self.assertIn('"added": 2', acts[0]["after_json"])

    def test_04_add_items_bulk_is_single_transaction(self):
        from v2.core.errors import ValidationError
        o = self._order()
        self.svc.add_item(o["id"], item_name="原有明细", qty_ordered=1,
                          unit_price=100)
        with self.assertRaises(ValidationError):
            self.svc.add_items_bulk(o["id"], [
                {"item_name": "正常行", "qty_ordered": 2, "unit_price": 50},
                {"item_name": "坏行", "qty_ordered": 0},
            ])
        self.assertEqual(len(self.svc.list_items(o["id"])), 1)
        self.assertEqual(self.svc.summary(o["id"])["amount_total"], 100.0)

    def test_05_add_items_bulk_rejects_void_order(self):
        from v2.core.errors import StateError
        o = self._order()
        self.db.update("orders", {"status": "作废"}, "id = ?", (o["id"],))
        with self.assertRaises(StateError):
            self.svc.add_items_bulk(o["id"], [
                {"item_name": "弯头", "qty_ordered": 1}])

    def test_06_add_items_bulk_keeps_manual_totals_by_default(self):
        o = self._order(amount_total=999.0)
        self.assertTrue(int(self.db.get_by_id("orders", o["id"])["amount_manual"]))
        self.svc.add_items_bulk(o["id"], [
            {"item_name": "弯头", "qty_ordered": 2, "unit_price": 100}])
        self.assertEqual(self.svc.summary(o["id"])["amount_total"], 999.0)
        res = self.svc.add_items_bulk(o["id"], [
            {"item_name": "三通", "qty_ordered": 1, "unit_price": 50}],
            reset_manual_totals=True)
        self.assertEqual(res["amount_total"], 250.0)

    # ── 2. 解析层 ──────────────────────────────────────────
    def test_07_item_headers_and_template_are_9_columns(self):
        self.assertEqual(ofdefs.item_headers(), [
            "物资代码", "物资名称", "实际生产物资名称", "规格", "单位",
            "数量", "单价", "总价", "参数描述"])
        p = str(Path(self.tmp) / "模板.xlsx")
        excelio.export_template(p, "order_item")
        rep = excelio.parse_order_item_file(p)
        self.assertEqual(rep["item_count"], 2)
        self.assertEqual(rep["header_row"], 1)
        self.assertEqual(len(ofdefs.item_sample_rows()[0]), 9)

    def test_08_only_item_columns_never_split_into_orders(self):
        """★ 根因复现：旧 17 列口径把 2 行明细拆成 2 张空壳订单"""
        p = self._write("只有明细列.xlsx", [
            ["序号", "物资名称", "规格型号", "单位", "数量", "单价"],
            [1, "耐磨弯头", "DN250", "件", 30, 1850],
            [2, "波纹补偿器", "DN300", "件", 20, 4980],
        ])
        new = excelio.parse_order_item_file(p)
        old = excelio.parse_order_file(p)
        self.assertEqual(old["group_count"], 2)        # 旧口径确实会拆成 2 单
        self.assertEqual(new["item_count"], 2)
        self.assertNotIn("group_count", new)            # 新口径根本不分组
        self.assertEqual(new["items"][0]["qty_ordered"], 30.0)
        self.assertEqual(new["items"][1]["unit_price"], 4980.0)
        self.assertIn("序号", new["ignored"])
        o = self._order()
        self.svc.add_items_bulk(o["id"], new["items"])
        self.assertEqual(self.svc.summary(o["id"])["item_count"], 2)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 1)

    def test_09_header_not_on_first_row_is_located(self):
        p = self._write("标题行.xlsx", [
            ["2026 年度采购明细表", "", "", "", "", ""],
            ["物资名称", "规格", "单位", "数量", "单价", "总价"],
            ["耐磨弯头", "DN250", "件", 3, 3690, 11070],
            ["三通", "DN254", "件", 2, 3552, 7104],
        ])
        rep = excelio.parse_order_item_file(p)
        self.assertEqual(rep["header_row"], 2)
        self.assertEqual(rep["skipped_preamble_rows"], 1)
        self.assertTrue(rep["header_found"])
        self.assertEqual(rep["item_count"], 2)
        self.assertEqual(rep["items"][0]["amount"], 11070.0)
        old = excelio.parse_order_file(p)
        self.assertTrue(any(i.get("fatal") for i in old["issues"]),
                        "旧口径遇到标题行会整表失败")

    def test_10_dirty_rows_are_reported_not_silently_dropped(self):
        p = self._write("脏数据.csv", [
            ["物资名称", "数量", "单价", "总价"],
            ["耐磨弯头", 2, "1,850", "3,700"],
            ["", 3, 100, ""],                 # 名称空 → 跳过
            ["三通", 0, 200, ""],             # 数量 0 → 跳过
            ["补偿器", 4, "面议", ""],         # 单价脏 → 提示，按 0
            ["合计", "", "", ""],             # 汇总行 → 提示跳过
            ["法兰", "5件", 300, ""],          # 带单位 → 5
        ])
        rep = excelio.parse_order_item_file(p)
        self.assertEqual(rep["item_count"], 3)
        fatal = [i for i in rep["issues"] if i.get("fatal")]
        self.assertEqual(len(fatal), 2)
        self.assertEqual(rep["items"][0]["unit_price"], 1850.0)
        self.assertEqual(rep["items"][1]["unit_price"], 0.0)
        self.assertEqual(rep["items"][2]["qty_ordered"], 5.0)

    def test_11_missing_required_columns_gives_clear_diagnosis(self):
        p = self._write("缺列.csv", [
            ["产品名称", "规格", "备注"],
            ["耐磨弯头", "DN250", "无数量列"],
        ])
        rep = excelio.parse_order_item_file(p)
        self.assertEqual(rep["item_count"], 0)
        self.assertIn("数量", rep["issues"][0]["reason"])
        self.assertIn("下载明细导入模板", rep["issues"][0]["reason"])

    def test_12_real_22_column_workbook_reads_as_items(self):
        if not REAL_WORKBOOK.is_file():
            self.skipTest("真实工作簿不在项目根目录")
        rep = excelio.parse_order_item_file(str(REAL_WORKBOOK))
        self.assertEqual(rep["item_count"], 734)
        self.assertEqual(rep["header_row"], 1)
        first = rep["items"][0]
        self.assertIn("密封圆顶阀", first["item_name"])
        self.assertEqual(first["qty_ordered"], 2.0)
        self.assertEqual(first["unit_price"], 16533.0)
        self.assertEqual(first["amount"], 33066.0)   # 价税合计 = 行金额
        self.assertEqual(excelio.parse_order_file(str(REAL_WORKBOOK))["group_count"],
                         306, "旧的整单导入行为不得被改坏")

    # ── 3. UI ──────────────────────────────────────────────
    def test_13_new_order_dialog_defaults_to_head_only(self):
        d = dialogs.NewOrderDialog(None, self.db)
        self.addCleanup(d.close)
        self.assertFalse(d.items_box.isChecked(), "明细区默认收起")
        self.assertFalse(d.items_content.isVisible())
        self.assertEqual(d.table.columnCount(), 9)
        self.assertEqual(d.table.rowCount(), 3)
        self.assertEqual(d.result_spec()["items"], [])
        d._validate()
        self.assertEqual(d.result_spec()["items"], [])

    def test_14_new_order_dialog_optional_items_still_work(self):
        d = dialogs.NewOrderDialog(None, self.db)
        self.addCleanup(d.close)
        d.items_box.setChecked(True)
        d.table.item(0, 1).setText("耐磨弯头")
        d.table.item(0, 5).setText("7.5")
        d.table.item(0, 6).setText("1850")
        items, errors = d._collect_items()
        self.assertEqual((len(items), len(errors)), (1, 0))
        self.assertEqual(d.result_spec()["items"][0]["qty_ordered"], 7.5)

    def test_15_add_items_dialog_saves_multiple_rows(self):
        o = self._order()
        d = dialogs.AddOrderItemsDialog(None, self.db, o["id"])
        self.addCleanup(d.close)
        d.table.setItem(0, 1, QTableWidgetItem("耐磨弯头"))
        d.table.setItem(0, 5, QTableWidgetItem("3"))
        d.table.setItem(0, 6, QTableWidgetItem("3690"))
        d.table.setItem(1, 1, QTableWidgetItem("波纹补偿器"))
        d.table.setItem(1, 5, QTableWidgetItem("2"))
        d.table.setItem(1, 6, QTableWidgetItem("4980"))
        d._do_save()
        self.assertIsInstance(d.result, dict)
        self.assertEqual(d.result["count"], 2)
        self.assertEqual(len(self.svc.list_items(o["id"])), 2)
        self.assertEqual(self.svc.summary(o["id"])["amount_total"], 21030.0)

    def test_16_import_items_dialog_previews_and_imports(self):
        o = self._order()
        p = self._write("导入.xlsx", [
            ["2026 明细表", "", "", "", "", ""],
            ["物资名称", "规格", "单位", "数量", "单价", "总价"],
            ["七段合金衬弯头", "DN250", "件", 3, 3690, 11070],
            ["五段弧三通", "DN254", "件", 2, 3552, 7104],
        ])
        d = dialogs.ImportOrderItemsDialog(None, self.db, o["id"])
        self.addCleanup(d.close)
        self.assertEqual(d.combo.currentData(), o["id"])
        d._load(p)
        self.assertEqual(d.table.columnCount(), 9)
        self.assertEqual(d.table.rowCount(), 2)
        self.assertEqual(d.report["header_row"], 2)
        d._do_import()
        self.assertIsInstance(d.result, dict)
        self.assertEqual(d.result["count"], 2)
        self.assertEqual(len(self.svc.list_items(o["id"])), 2)
        self.assertEqual(self.svc.summary(o["id"])["amount_total"], 18174.0)

    # ── 4. 接线 ────────────────────────────────────────────
    def _window(self):
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        self.addCleanup(w.close)
        return w

    def test_17_action_groups_and_primary_actions(self):
        from v2.ui.panels.common import ACTION_GROUPS, PRIMARY_ACTIONS, find_action
        group = [g for g in ACTION_GROUPS if g[1] == "orders"][0]
        names = [a[0] for a in group[2]]
        self.assertLess(names.index("录入订单明细"), names.index("编辑订单"))
        self.assertIn("导入订单明细", names)
        for fn in ("_add_order_items", "_import_order_items"):
            self.assertEqual(find_action(fn, "orders")[2], "orders")
            self.assertIn(fn, [x[0] for x in PRIMARY_ACTIONS["orders"]])
        self.assertTrue(all(len(a) >= 4 and len(a[3]) >= 8 for a in group[2]))

    def test_18_menus_and_context_menu_are_wired(self):
        w = self._window()
        self.assertTrue(hasattr(w, "_add_order_items"))
        self.assertTrue(hasattr(w, "_import_order_items"))
        src = Path("v2/ui/main_window_v2.py").read_text(encoding="utf-8")
        self.assertIn('("录入订单明细", self._add_order_items)', src)
        self.assertIn('("导入订单明细", self._import_order_items)', src)
        o = self._order()
        menu = w._build_orders_menu(int(o["id"]), o["doc_no"])
        labels = [a.text() for a in menu.actions()]
        self.assertTrue(any("录入订单明细" in x for x in labels))
        self.assertTrue(any("导入订单明细" in x for x in labels))
        # 快捷栏真实按钮
        w.switch_view("orders")
        from PySide6.QtWidgets import QPushButton
        btns = [b.text() for b in w.action_bar_host.findChildren(QPushButton)]
        self.assertIn("录入订单明细", btns)
        self.assertIn("导入订单明细", btns)

    def test_19_new_order_action_creates_head_without_items(self):
        w = self._window()
        before = self.db.scalar("SELECT COUNT(*) FROM orders")

        def _click():
            m = QApplication.activeModalWidget()
            if m is None or not hasattr(m, "items_box"):
                return
            m.edit_project_no.setText("V353-ACTION")
            box = m.findChild(QDialogButtonBox)
            for b in box.buttons():
                if b.objectName() == "Primary":
                    b.click()
                    return

        QTimer.singleShot(60, _click)
        msg = w._new_order()
        self.assertIn("新建 DD-", msg)
        self.assertIn("明细待录入", msg)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), before + 1)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM order_items"), 0)

    def test_20_import_orders_dialog_still_requires_items(self):
        """旧「导入订单」17 列整单导入保持原行为，未被两段式改坏"""
        self.assertIn("class ImportOrdersDialog", Path(
            "v2/ui/dialogs.py").read_text(encoding="utf-8"))
        rows = [self._head_row("耐磨弯头", "30")]
        p = self._write("整单.csv", [ofdefs.headers()] + rows)
        rep = excelio.parse_order_file(p)
        self.assertEqual(rep["group_count"], 1)
        self.assertEqual(rep["item_count"], 1)

    @staticmethod
    def _head_row(name, qty):
        return ["CG-V353-001", "测试客户", "外部订单", "草稿", "", "",
                "", "", "6001", name, "", "DN250", "件", qty,
                "100", "", ""]


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.5.0 回归（2026-11-18 · 用户第 22 轮）：订单盈亏与多供应商采购归集

张哥拍板的三条口径（本文件逐条锁定，不许回退）：
  1. 销售口径：取【明细为准】、与订单表头差额 > 0.01 时告警；
  2. 含税口径：统一【含税】，混用不含税合同时只出金额、利润率不出数；
  3. 未覆盖物资：利润率标【不完整】，绝不按 0 采购成本算虚高利润。

架构硬口径：
  · schema v7 + purchase_contract_items 桥表；不在 order_items 上加采购单价列；
  · v7 迁移只对「唯一明细 + 已签 + 金额>0」的历史合同安全回填，多明细不猜；
  · 一订单多物资 → 多供应商合同；一份合同 → 可跨多张订单（合并采购）；
  · 数量守恒 Σ qty ≤ order_items.qty_ordered；
  · Word 合同模板只输出本合同分配明细 + 含税采购单价。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                       # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database           # noqa: E402
from v2.core import integrity, tables                             # noqa: E402
from v2.core.errors import ValidationError                        # noqa: E402
from v2.domains import OrderService                               # noqa: E402
from v2.domains.contract import (                                 # noqa: E402
    PurchaseContractService, SIGNED_STATUSES, STATUS_DRAFT,
)
from v2.domains.profit import OrderProfitService                  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QPushButton
    PYSIDE = True
except ImportError:                                              # pragma: no cover
    PYSIDE = False

_APP = None


def _app():
    global _APP
    if _APP is None:
        from v2.ui import theme
        _APP = QApplication.instance() or QApplication([])
        theme.apply_theme(_APP)
    return _APP


class Profit350Test(unittest.TestCase):
    """服务层 / 迁移 / 口径（无需 Qt）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v350_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.profit = OrderProfitService(self.db)
        self.order = self.orders.create_order(
            customer="国能沈西热电厂", project_no="SO-350-001")
        self.i1 = self.orders.add_item(self.order["id"], "耐磨弯头", 2,
                                       unit_price=10000)
        self.i2 = self.orders.add_item(self.order["id"], "斜三通", 3,
                                       unit_price=8000)
        self.i3 = self.orders.add_item(self.order["id"], "波纹补偿器", 4,
                                       unit_price=6000)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _contract(self, name, supplier, amount=0, status="已签订",
                  tax_included=1, order_id=None):
        return self.svc.create({
            "contract_name": name, "supplier": supplier,
            "order_id": order_id if order_id is not None else self.order["id"],
            "amount": amount, "status": status,
            "tax_included": tax_included, "amount_source": "manual",
        }, "测试")

    def _assign(self, cid, pairs):
        return self.svc.set_contract_items(
            int(cid), [{"order_item_id": int(i["id"]), "qty": q,
                        "unit_price": p} for i, q, p in pairs], "测试")

    # ── ① 版本 / schema ────────────────────────────────────

    def test_01_version_and_schema_v7(self):
        self.assertEqual(__version__, "3.5.4")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        self.assertEqual(self.db.schema_version(), 7)
        names = [r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("purchase_contract_items", names)
        self.assertNotIn("purchase_price",
                         [r["name"] for r in self.db.query(
                             "PRAGMA table_info(order_items)")])
        self.assertEqual(tables.missing_from_declaration(
            Path("v2/core/schema.sql")), set())
        self.assertEqual(tables.undeclared_in_ddl(
            Path("v2/core/schema.sql")), set())

    # ── ② v7 迁移（老库副本语义）───────────────────────────

    def test_02_migration_backfill_only_unambiguous(self):
        p = self.tmp / "legacy.db"
        self.db.close()
        old = Database(str(p))
        svc = OrderService(old)
        o1 = svc.create_order(customer="历史客户", project_no="SO-OLD-1")
        it = svc.add_item(o1["id"], "单一物资", 2, unit_price=5000)
        cs = PurchaseContractService(old, str(self.tmp / "olddata"))
        c1 = cs.create({"contract_name": "唯一明细历史合同",
                        "order_id": o1["id"], "supplier": "老供应商",
                        "amount": 5692, "status": "已签订"}, "测试")
        o2 = svc.create_order(customer="历史客户", project_no="SO-OLD-2")
        svc.add_item(o2["id"], "甲物资", 1, unit_price=1000)
        svc.add_item(o2["id"], "乙物资", 1, unit_price=2000)
        cs.create({"contract_name": "多明细不猜", "order_id": o2["id"],
                   "supplier": "老供应商", "amount": 2500,
                   "status": "已签订"}, "测试")
        old.execute("DELETE FROM purchase_contract_items")
        old.execute("UPDATE schema_meta SET version = 6")
        old.close()

        upgraded = Database(str(p))
        self.assertEqual(upgraded.schema_version(), 7)
        rows = upgraded.query("SELECT * FROM purchase_contract_items")
        self.assertEqual(len(rows), 1, "只有唯一明细的已签合同可安全回填")
        self.assertEqual(int(rows[0]["contract_id"]), int(c1["id"]))
        self.assertEqual(int(rows[0]["order_item_id"]), int(it["id"]))
        self.assertEqual(float(rows[0]["amount"]), 5692.0)
        self.assertEqual(float(rows[0]["qty"]), 2.0)
        self.assertEqual(float(rows[0]["unit_price"]), 2846.0)
        self.assertEqual(integrity.check(upgraded)["ok"], True)
        upgraded.close()
        # 幂等：再打开一次不会重复插行
        again = Database(str(p))
        self.assertEqual(
            len(again.query("SELECT * FROM purchase_contract_items")), 1)
        again.close()
        self.db = Database(":memory:")

    # ── ③ 一单多明细 / 多供应商 ────────────────────────────

    def test_03_multi_supplier_rollup(self):
        a = self._contract("甲供合同", "甲供应商", 19000)
        b = self._contract("乙供合同", "乙供应商", 5500)
        self._assign(a["id"], [(self.i1, 2, 7000), (self.i2, 1, 5000)])
        self._assign(b["id"], [(self.i2, 1, 5500)])
        s = self.profit.order_summary(self.order["id"])
        self.assertEqual(s["sales_total"], 68000.0)
        self.assertEqual(s["purchase_total"], 24500.0)
        self.assertEqual(s["gross_profit"], 43500.0)
        self.assertAlmostEqual(s["profit_rate"], 63.97, places=2)
        self.assertEqual(s["item_count"], 3)
        self.assertEqual(s["covered_items"], 2)
        self.assertAlmostEqual(s["coverage"], 66.7, places=1)
        self.assertEqual(s["complete"], False)
        self.assertEqual(s["supplier_count"], 2)
        self.assertEqual({x["supplier"] for x in s["by_supplier"]},
                         {"甲供应商", "乙供应商"})
        self.assertTrue(any("不完整" in w for w in s["warnings"]))

    def test_04_coverage_complete_when_all_items_signed(self):
        c = self._contract("全单合同", "甲供应商", 40000)
        self._assign(c["id"], [(self.i1, 2, 7000), (self.i2, 3, 6000),
                               (self.i3, 4, 3000)])
        s = self.profit.order_summary(self.order["id"])
        self.assertTrue(s["complete"])
        self.assertEqual(s["coverage"], 100.0)
        self.assertFalse(any("不完整" in w for w in s["warnings"]))

    # ── ④ 一份合同跨多张订单 ─────────────────────────────

    def test_05_contract_crossing_orders_no_double_count(self):
        other = self.orders.create_order(customer="另一客户",
                                         project_no="SO-350-002")
        j1 = self.orders.add_item(other["id"], "阀门", 5, unit_price=2000)
        c = self._contract("合并采购", "甲供应商", 20000)
        self._assign(c["id"], [(self.i1, 2, 7000), (j1, 5, 1200)])
        s1 = self.profit.order_summary(self.order["id"])
        s2 = self.profit.order_summary(other["id"])
        self.assertEqual(s1["purchase_total"], 14000.0)
        self.assertEqual(s2["purchase_total"], 6000.0)
        self.assertEqual(s1["purchase_total"] + s2["purchase_total"], 20000.0)
        self.assertEqual(s2["sales_total"], 10000.0)

    # ── ⑤ 状态 / 含税 / 差额口径 ──────────────────────────

    def test_06_draft_and_void_excluded(self):
        draft = self._contract("草稿合同", "未定供方", 99999, STATUS_DRAFT)
        self._assign(draft["id"], [(self.i3, 4, 1000)])
        s = self.profit.order_summary(self.order["id"])
        self.assertEqual(s["purchase_total"], 0.0)
        self.assertEqual(s["covered_items"], 0)
        self.assertFalse(s["complete"])
        self.assertNotIn("草稿", "".join(s["warnings"]))

    def test_07_tax_mixed_suppresses_rate(self):
        c = self._contract("不含税合同", "甲供应商", 14000, tax_included=0)
        self._assign(c["id"], [(self.i1, 2, 7000)])
        s = self.profit.order_summary(self.order["id"])
        self.assertIsNone(s["profit_rate"])
        self.assertEqual(s["purchase_total"], 14000.0)
        self.assertTrue(any("不含税" in w for w in s["warnings"]))
        self.assertTrue(any("不出数" in w for w in s["warnings"]))

    def test_08_sales_head_gap_warns_but_uses_items(self):
        self.db.update("orders", {"amount_total": 99999.0},
                       "id = ?", (self.order["id"],))
        s = self.profit.order_summary(self.order["id"])
        self.assertEqual(s["sales_total"], 68000.0)
        self.assertEqual(s["sales_head"], 99999.0)
        self.assertEqual(s["sales_head_gap"], -31999.0)
        self.assertTrue(any("明细为准" in w for w in s["warnings"]))

    def test_09_zero_amount_falls_back_to_qty_times_price(self):
        self.db.update("order_items", {"amount": 0},
                       "id = ?", (self.i1["id"],))
        s = self.profit.order_summary(self.order["id"])
        self.assertEqual(s["sales_total"], 68000.0)

    # ── ⑥ 守恒 / 分配服务 / 模板 ─────────────────────────

    def test_10_over_purchase_blocked(self):
        a = self._contract("甲", "甲供应商", 14000)
        self._assign(a["id"], [(self.i1, 2, 7000)])
        b = self._contract("乙", "乙供应商", 0)
        with self.assertRaises(ValidationError):
            self._assign(b["id"], [(self.i1, 1, 7000)])

    def test_11_set_items_is_idempotent_replace(self):
        c = self._contract("甲", "甲供应商", 14000)
        self._assign(c["id"], [(self.i1, 2, 7000), (self.i2, 1, 5000)])
        self._assign(c["id"], [(self.i1, 2, 7000)])
        rows = self.svc.contract_items(int(c["id"]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["amount"]), 14000.0)

    def test_12_context_uses_purchase_price_only(self):
        c = self._contract("甲", "甲供应商", 14000)
        self._assign(c["id"], [(self.i1, 2, 7000)])
        ctx = self.svc.context(int(c["id"]))
        self.assertEqual(ctx["items_source"], "contract_items")
        self.assertEqual(len(ctx["items"]), 1)
        self.assertEqual(ctx["items"][0]["unit_price"], 7000.0)
        self.assertEqual(ctx["items"][0]["qty_ordered"], 2.0)
        self.assertNotEqual(ctx["items"][0]["unit_price"], 10000.0)

    def test_13_context_fallback_marked(self):
        c = self._contract("未分配", "甲供应商", 0)
        ctx = self.svc.context(int(c["id"]))
        self.assertEqual(ctx["items_source"], "order_items_fallback")
        self.assertEqual(len(ctx["items"]), 3)

    def test_14_sync_amount_from_items(self):
        c = self._contract("甲", "甲供应商", 0)
        self._assign(c["id"], [(self.i1, 2, 7000)])
        out = self.svc.sync_amount_from_items(int(c["id"]), "测试")
        self.assertEqual(float(out["amount"]), 14000.0)
        self.assertEqual(out["amount_source"], "items")

    def test_15_integrity_amount_gap_is_warning_only(self):
        c = self._contract("甲", "甲供应商", 14000)
        self._assign(c["id"], [(self.i1, 2, 7000)])
        self.svc.update(int(c["id"]), {"amount": 18000}, "测试")
        rep = integrity.check(self.db)
        self.assertTrue(rep["ok"], "金额偏差只能告警，不能判不一致")
        self.assertEqual(len(rep["contract_amount_gap"]), 1)

    # ── ⑦ 维度 / 导出 ────────────────────────────────────

    def test_16_breakdown_dimensions_do_not_double_count(self):
        other = self.orders.create_order(customer="另一客户",
                                         project_no="SO-350-003")
        j1 = self.orders.add_item(other["id"], "阀门", 5, unit_price=2000)
        a = self._contract("甲", "甲供应商", 14000)
        b = self._contract("乙", "乙供应商", 6000, order_id=other["id"])
        self._assign(a["id"], [(self.i1, 2, 7000)])
        self._assign(b["id"], [(j1, 5, 1200)])
        sup = self.profit.breakdown("supplier")
        self.assertEqual(len(sup), 2)
        self.assertAlmostEqual(
            sum(x["purchase_total"] for x in sup), 20000.0, places=2)
        self.assertAlmostEqual(sum(x["sales_total"] for x in sup),
                               30000.0, places=2)   # 20000 + 10000
        self.assertEqual(len(self.profit.breakdown("month")) >= 1, True)
        self.assertEqual(len(self.profit.breakdown("order")), 2)

    def test_17_export_sheets_has_three_sheets(self):
        c = self._contract("甲", "甲供应商", 14000)
        self._assign(c["id"], [(self.i1, 2, 7000)])
        sheets = self.profit.export_sheets(self.profit.overview())
        self.assertEqual(set(sheets),
                         {"订单盈亏汇总", "物资盈亏明细", "供方采购分析"})
        self.assertEqual(len(sheets["物资盈亏明细"][1]), 3)
        self.assertEqual(len(sheets["订单盈亏汇总"][1]), 1)


@unittest.skipUnless(PYSIDE, "PySide6 未安装")
class Profit350UITest(unittest.TestCase):
    """UI 接线 / 离屏图 / 分配页保存"""

    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="v350ui_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.order = self.orders.create_order(
            customer="国能沈西热电厂", project_no="SO-350-UI")
        self.i1 = self.orders.add_item(self.order["id"], "耐磨弯头", 2,
                                       unit_price=10000)
        self.i2 = self.orders.add_item(self.order["id"], "斜三通", 3,
                                       unit_price=8000)
        self.c = self.svc.create({
            "contract_name": "甲供合同", "supplier": "甲供应商",
            "order_id": self.order["id"], "amount": 14000,
            "status": "已签订"}, "测试")
        self.svc.set_contract_items(
            int(self.c["id"]),
            [{"order_item_id": int(self.i1["id"]), "qty": 2,
              "unit_price": 7000}], "测试")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_18_main_window_has_profit_columns(self):
        from v2.ui.main_window_v2 import MainWindowV2, PYSIDE_AVAILABLE
        self.assertTrue(PYSIDE_AVAILABLE, "主窗口 import 不得静默降级")
        w = MainWindowV2(self.db)
        w.switch_view("orders")
        w.refresh_all()
        headers = [w.tbl_orders.horizontalHeaderItem(i).text()
                   for i in range(w.tbl_orders.columnCount())]
        self.assertEqual(w.tbl_orders.columnCount(), 20)
        self.assertEqual(headers[-4:],
                         ["采购合计(含税)", "毛利(含税)", "利润率", "覆盖度"])
        self.assertEqual(headers.index("采购合同编号"),
                         headers.index("汇总口径") - 1)
        self.assertIn("不完整", w.tbl_orders.item(0, 18).text())
        self.assertTrue(hasattr(w, "profit"))
        w.close()

    def test_19_profit_dialog_three_dimensions_and_png(self):
        from v2.ui.profit_dialog import ProfitDialog
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        d = ProfitDialog(w, self.db)
        self.assertEqual(d.table.columnCount(), 11)
        self.assertEqual(len(d.chart.rows), 1)
        self.assertIn("不完整", d.hint.text())
        png = str(self.tmp / "chart.png")
        d.chart.export_png(png)
        self.assertGreater(Path(png).stat().st_size, 2000)
        for idx, cols in ((1, 5), (2, 5)):
            d.dim_box.setCurrentIndex(idx)
            self.assertEqual(d.table.columnCount(), cols)
            self.assertGreaterEqual(len(d.chart.rows), 1)
        self.assertIn("经营分析", d.summary_text())
        d.close()
        w.close()

    def test_20_items_dialog_save_and_cross_order_kept(self):
        from v2.ui.contract_items_dialog import ContractItemsDialog
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        other = self.orders.create_order(customer="另一客户",
                                         project_no="SO-350-004")
        j1 = self.orders.add_item(other["id"], "阀门", 5, unit_price=2000)
        # 同一份合同先挂另一张订单的一条物资
        self.svc.set_contract_items(int(self.c["id"]), [
            {"order_item_id": int(self.i1["id"]), "qty": 2,
             "unit_price": 7000},
            {"order_item_id": int(j1["id"]), "qty": 5,
             "unit_price": 1200}], "测试")
        d = ContractItemsDialog(w, self.svc, int(self.c["id"]))
        self.assertEqual(d.table.rowCount(), 2)
        with mock.patch("v2.ui.contract_items_dialog.theme.notify"):
            d._do_save()
        rows = self.svc.contract_items(int(self.c["id"]))
        self.assertEqual(len(rows), 2, "切订单保存不得抹掉其他订单的分配")
        self.assertEqual({int(r["order_item_id"]) for r in rows},
                         {int(self.i1["id"]), int(j1["id"])})
        d.close()
        w.close()

    def test_21_contract_dialog_buttons_and_action_groups(self):
        from v2.ui.contract_dialog import PurchaseContractDialog
        from v2.ui.main_window_v2 import MainWindowV2
        from v2.ui.panels.common import ACTION_GROUPS, PRIMARY_ACTIONS
        w = MainWindowV2(self.db)
        d = PurchaseContractDialog(w, self.svc, order_id=int(self.order["id"]))
        self.assertTrue(any(b.text() == "物资分配"
                            for b in d.findChildren(QPushButton)))
        d.close()
        names = [a[1] for _g, _k, acts in ACTION_GROUPS for a in acts]
        self.assertIn("_profit_analysis", names)
        self.assertIn("_profit_analysis", [x[0] for x in
                                           PRIMARY_ACTIONS["orders"]])
        self.assertTrue(hasattr(w, "_profit_analysis"))
        w.close()

    def test_22_architecture_guard(self):
        self.assertLess(len(Path("v2/ui/main_window_v2.py")
                            .read_text(encoding="utf-8").splitlines()), 900)
        self.assertLess(len(Path("v2/ui/contract_dialog.py")
                            .read_text(encoding="utf-8").splitlines()), 900)


if __name__ == "__main__":
    unittest.main()

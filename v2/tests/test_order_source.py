#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.9 订单来源（销售渠道）回归

口径（2026-11-07 张哥给定）：
  · 预设 招投标 / 北电电力 / 厦门龙净 / 京东平台 四个常用值；
  · 控件可编辑 —— 新渠道直接手输，落库为原文，数据库不设 CHECK 约束；
  · 内部订单只能由中标项目生成，来源缺省回填「招投标」；
  · 历史外部订单留空 = 未标注（不猜）。

    python -m pytest v2/tests/test_order_source.py -q
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import order_fields as ofdefs                     # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database         # noqa: E402
from v2.core.migrations import v5_order_source_cleanup         # noqa: E402
from v2.domains import (BidService, MaintenanceService,        # noqa: E402
                        OrderService)

try:
    from PySide6.QtWidgets import QApplication, QTableWidgetItem
    PYSIDE = True
except ImportError:                                            # pragma: no cover
    PYSIDE = False


class SourceFieldSpecTest(unittest.TestCase):
    """常量与模板口径：四值固定、顺序稳定、不进 17 列模板"""

    def test_01_presets(self):
        self.assertEqual(ofdefs.ORDER_SOURCES,
                         ("招投标", "北电电力", "厦门龙净", "京东平台"))

    def test_02_not_in_17_column_template(self):
        self.assertNotIn("订单来源", ofdefs.headers())
        self.assertEqual(len(ofdefs.headers()), 17)
        self.assertIn("order_source", ofdefs.EXTRA_KEYS)

    def test_03_header_alias(self):
        mapping, ignored = ofdefs.resolve_headers(["订单来源", "渠道", "物资名称"])
        self.assertEqual(mapping.get("order_source"), "订单来源")
        self.assertNotIn("渠道", ignored)


class SourceServiceTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.orders = OrderService(self.db)
        self.maint = MaintenanceService(self.db)

    def test_01_create_order_preset(self):
        o = self.orders.create_order(customer="国能沈西", order_source="北电电力")
        self.assertEqual(self.db.get_by_id("orders", o["id"])["order_source"], "北电电力")
        self.assertEqual(self.orders.summary(o["id"])["order_source"], "北电电力")

    def test_02_create_order_free_text(self):
        o = self.orders.create_order(customer="某电厂", order_source="中电投商城")
        self.assertEqual(self.db.get_by_id("orders", o["id"])["order_source"], "中电投商城")

    def test_03_blank_becomes_null(self):
        o = self.orders.create_order(customer="某电厂", order_source="   ")
        self.assertIsNone(self.db.get_by_id("orders", o["id"])["order_source"])

    def test_04_create_from_spec_passthrough(self):
        o = self.orders.create_from_spec({
            "customer": "厦门龙净环保", "order_source": "厦门龙净",
            "status": "草稿",
            "items": [{"item_name": "耐磨弯头", "qty_ordered": 2, "unit_price": 100}],
        })
        self.assertEqual(o["order_source"], "厦门龙净")

    def test_05_internal_defaults_to_bid_source(self):
        bids = BidService(self.db)
        bp = bids.create_project("中标项目", customer="电厂")
        bids.add_item(bp["id"], "耐磨弯头", qty=5, purchase_price=100, quote_ratio=0.9)
        bids.mark_won(bp["id"])
        o = self.orders.create_internal_from_bid(bp["id"])
        self.assertEqual(self.db.get_by_id("orders", o["id"])["order_source"], "招投标")

    def test_06_maintenance_can_edit_source(self):
        o = self.orders.create_order(customer="某电厂", order_source="招投标")
        self.maint.edit("order", o["id"], {"order_source": "京东平台"})
        self.assertEqual(self.db.get_by_id("orders", o["id"])["order_source"], "京东平台")
        # 清空 = 未标注
        self.maint.edit("order", o["id"], {"order_source": ""})
        self.assertEqual(self.db.get_by_id("orders", o["id"])["order_source"], "")

    def test_07_import_reads_source_column(self):
        rows = [{
            "订单项目编号": "CG-1", "需方单位": "某电厂", "订单类型": "外部订单",
            "订单状态": "草稿", "物资名称": "耐磨弯头", "单位": "件",
            "数量": "3", "单价": "50", "订单来源": "京东平台",
        }]
        report = ofdefs.parse_rows(rows)
        self.assertEqual(report["group_count"], 1)
        groups = report["groups"]
        self.assertEqual(groups[0]["head"]["order_source"], "京东平台")
        res = self.orders.import_orders(groups, operator="测试")
        self.assertEqual(res["orders"][0]["order_source"], "京东平台")


class SourceMigrationTest(unittest.TestCase):
    """v5 迁移：空白归一 + 内部订单回填「招投标」，且可重复执行"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="src_v5_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "t.db"))
        self.addCleanup(self.db.close)
        self.orders = OrderService(self.db)

    def _downgrade(self):
        self.db.execute("UPDATE schema_meta SET version = 4")

    def test_01_blank_cleared_and_idempotent(self):
        o = self.orders.create_order(customer="某电厂", order_source="招投标")
        self.db.execute("UPDATE orders SET order_source = '   ' WHERE id = ?", (o["id"],))
        self._downgrade()
        self.db.init_schema()
        self.assertIsNone(self.db.get_by_id("orders", o["id"])["order_source"])
        self.assertEqual(self.db.scalar(
            "SELECT version FROM schema_meta ORDER BY rowid DESC LIMIT 1"),
            CURRENT_SCHEMA_VERSION)
        # 再跑一次：结果不变
        v5_order_source_cleanup(self.db.conn, Path(self.tmp.name))
        self.assertIsNone(self.db.get_by_id("orders", o["id"])["order_source"])

    def test_02_internal_backfilled(self):
        bids = BidService(self.db)
        bp = bids.create_project("中标项目", customer="电厂")
        bids.add_item(bp["id"], "耐磨弯头", qty=5, purchase_price=100, quote_ratio=0.9)
        bids.mark_won(bp["id"])
        o = self.orders.create_internal_from_bid(bp["id"])
        self.db.execute("UPDATE orders SET order_source = NULL WHERE id = ?", (o["id"],))
        self._downgrade()
        self.db.init_schema()
        self.assertEqual(self.db.get_by_id("orders", o["id"])["order_source"], "招投标")


@unittest.skipUnless(PYSIDE, "未安装 PySide6")
class SourceDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)

    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        from v2.ui import theme
        self._theme = theme
        self._orig = theme.notify
        theme.notify = lambda *a, **k: None
        self.addCleanup(lambda: setattr(theme, "notify", self._orig))

    def _fill_one_item(self, dlg, qty="2"):
        dlg.table.setItem(0, 1, QTableWidgetItem("耐磨弯头"))
        dlg.table.setItem(0, 5, QTableWidgetItem(qty))

    def test_01_new_order_combo_has_presets(self):
        from v2.ui import dialogs
        dlg = dialogs.NewOrderDialog(None, self.db)
        items = [dlg.combo_source.itemData(i) for i in range(dlg.combo_source.count())]
        self.assertEqual(items, ["", *ofdefs.ORDER_SOURCES])
        self.assertTrue(dlg.combo_source.isEditable(), "必须能手工输入新渠道")

    def test_02_new_order_preset_spec(self):
        from v2.ui import dialogs
        dlg = dialogs.NewOrderDialog(None, self.db)
        dlg.combo_source.setCurrentIndex(
            dlg.combo_source.findData("厦门龙净"))
        self._fill_one_item(dlg)
        spec = dlg.result_spec()
        self.assertEqual(spec["order_source"], "厦门龙净")

    def test_03_new_order_manual_input(self):
        from v2.ui import dialogs
        dlg = dialogs.NewOrderDialog(None, self.db)
        dlg.combo_source.setEditText("中电投商城")
        self._fill_one_item(dlg)
        self.assertEqual(dlg.result_spec()["order_source"], "中电投商城")
        # 空占位不能被当成来源存库
        dlg.combo_source.setCurrentIndex(0)
        self.assertEqual(dlg.result_spec()["order_source"], "")

    def test_04_add_order_batch_source(self):
        """添加订单：本批默认来源只补空缺行，文件自带来源优先"""
        from v2.ui import dialogs
        dlg = dialogs.AddOrderDialog(None, self.db)
        dlg.rows = [
            {"订单项目编号": "A-1", "需方单位": "甲厂", "物资名称": "耐磨弯头",
             "单位": "件", "数量": "1", "单价": "10", "订单来源": ""},
            {"订单项目编号": "A-2", "需方单位": "乙厂", "物资名称": "补偿器",
             "单位": "件", "数量": "2", "单价": "20", "订单来源": "京东平台"},
        ]
        dlg.combo_source.setEditText("北电电力")
        report = ofdefs.parse_rows(dlg._rows_for_parse())
        got = {g["head"]["project_no"]: g["head"]["order_source"]
               for g in report["groups"]}
        self.assertEqual(got, {"A-1": "北电电力", "A-2": "京东平台"})


if __name__ == "__main__":
    unittest.main()

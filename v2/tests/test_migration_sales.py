#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实销售订单迁移回归（P0-1）

用构造的小样本验证解析/映射/入库/幂等/对账口径；
真实 306 单迁移由 `py -m v2.migration.migrate_sales_orders --commit` 执行，
报告落在 docs/迁移报告_销售订单.md。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import openpyxl

from v2.core.db import Database
from v2.migration.migrate_sales_orders import (
    build_groups, db_snapshot, existing_project_nos, import_groups, read_sheet,
    split_product_name,
)

HEADER = ["业务员", "下单日期", "订单编号", "需方单位", "客户简称", "订单状态",
          "产品代码", "产品名称", "含税单价", "数量", "单位", "价税合计",
          "交货日期", "ERP订单编号", "排产日期", "订单执行状态",
          "备注（订单延期）", "实际发货日期", "采购合同编号", "建议交货日期",
          "计划模式", "计划跟踪号"]


def _row(no, name, code, price, qty, amount, status="已验收"):
    return ["刘明生", "2024-07-01 00:00:00", no, "国能九江发电有限公司", "国能九江",
            status, code, name, price, qty, "件", amount,
            "2024-08-20 00:00:00", "4507723757", "20240703", "7-20已发货",
            "", "", "20240708HKNY-B-362", "2024-08-20 00:00:00", "MTS计划模式", ""]


class MigrationTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v26_mig_"))
        self.xlsx = self.tmp / "销售订单汇总.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "销售订单"
        ws.append(HEADER)
        # 3 单：SO-A 两行、SO-B 一行、SO-C 三行（含发货中状态）
        ws.append(_row("SO-A", "密封圆顶阀\\FT3.1.1.2\\气力输灰系统\\FT", "6001", 100, 2, 200))
        ws.append(_row("SO-A", "阀板组件\\FT3.2.1.1-2\\气力输灰系统", "6002", 50, 4, 200))
        ws.append(_row("SO-B", "角座阀\\HKT2.1.7.3\\气力输送机\\国产", "6003", 30, 10, 300,
                       status="发货中"))
        ws.append(_row("SO-C", "密封组件\\HKT9.1.4.4-1\\气力输送机", "6004", 20, 1, 20,
                       status="待发货"))
        ws.append(_row("SO-C", "密封组件\\HKT9.1.4.1-1\\气力输送机", "6005", 10, 2, 20,
                       status="待发货"))
        ws.append(_row("SO-C", "耐磨弯头\\DN250\\输灰管道", "6006", 500, 1, 500,
                       status="待发货"))
        wb.save(str(self.xlsx))
        self.db = Database(str(self.tmp / "erp.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_parse_and_group(self):
        rows, _ = read_sheet(self.xlsx)
        self.assertEqual(len(rows), 6)
        groups, stats = build_groups(rows)
        self.assertEqual(stats["orders"], 3)
        self.assertEqual(stats["items"], 6)
        self.assertEqual(stats["amount"], 1240.0)
        self.assertEqual(stats["qty"], 20.0)
        # 状态按「单」计数（不是按行）
        self.assertEqual(stats["status"], {"已验收": 1, "发货中": 1, "待发货": 1})

    def test_02_product_name_split(self):
        self.assertEqual(split_product_name("密封圆顶阀\\FT3.1.1.2\\气力输灰系统\\FT"),
                         ("密封圆顶阀", "FT3.1.1.2"))
        self.assertEqual(split_product_name(""), ("", ""))
        self.assertEqual(split_product_name("单一名称"), ("单一名称", ""))

    def test_03_import_and_reconcile(self):
        rows, _ = read_sheet(self.xlsx)
        groups, _ = build_groups(rows)
        result = import_groups(self.db, groups)
        self.assertEqual(len(result["created"]), 3)
        self.assertEqual(result["skipped"], 0)

        snap = db_snapshot(self.db)
        self.assertEqual(snap["orders"], 3)
        self.assertEqual(snap["items"], 6)
        self.assertEqual(snap["qty"], 20.0)
        self.assertEqual(snap["amount"], 1240.0)
        self.assertEqual(snap["bad_qty"], 0)
        self.assertEqual(snap["bad_amt"], 0)
        self.assertTrue(snap["integrity_ok"])

        a = self.db.query_one("SELECT * FROM orders WHERE project_no='SO-A'")
        self.assertEqual(a["customer"], "国能九江发电有限公司")
        self.assertEqual(a["status"], "已验收")
        self.assertEqual(a["item_qty_total"], 2)          # Q12：行数，不是 Σ数量 6
        self.assertEqual(a["amount_total"], 400.0)
        self.assertEqual(a["erp_order_no"], "4507723757")
        self.assertEqual(a["required_date"], "2024-08-20")
        item = self.db.query_one(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY line_no", (a["id"],))
        self.assertEqual(item["item_name"], "密封圆顶阀")
        self.assertEqual(item["spec"], "FT3.1.1.2")
        self.assertEqual(item["param_desc"], "")          # Q13 未拍板：不回填
        self.assertTrue(item["remark"].startswith("密封圆顶阀\\"))

    def test_04_idempotent_rerun(self):
        rows, _ = read_sheet(self.xlsx)
        groups, _ = build_groups(rows)
        import_groups(self.db, groups)
        again = import_groups(self.db, groups)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["skipped"], 3)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 3)

    def test_05_existing_project_no_detected(self):
        rows, _ = read_sheet(self.xlsx)
        groups, _ = build_groups(rows)
        import_groups(self.db, groups)
        self.assertEqual(existing_project_nos(self.db), {"SO-A", "SO-B", "SO-C"})


if __name__ == "__main__":
    unittest.main()

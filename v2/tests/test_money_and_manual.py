#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1-5 手工合计保护 + P1-6 金额精度（2026-11-07）

    python -m pytest v2/tests/test_money_and_manual.py -q

验收口径：
  · 导入时显式声明的「价税合计 / 物资数量」不再被增删明细冲掉（amount_manual=1）
  · 未声明的字段仍随明细自动重算（amount_manual=0）
  · 「恢复自动汇总」清锁并按明细重算，幂等
  · 金额一律 Decimal 两位：多行 0.1 类小数累加无二进制尾数
  · 迁移 v3 把「表头金额 ≠ Σ明细」的历史单标为手工值，防止升级后被改写
"""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import money                                      # noqa: E402
from v2.core.db import Database                                 # noqa: E402
from v2.domains import BidService, OrderService                 # noqa: E402


def _spec(**kw):
    spec = {
        "project_no": "T-001", "customer": "测试客户", "order_type": "外部订单",
        "status": "已确认", "required_date": "2026-12-31",
        "items": [
            {"item_name": "耐磨弯头", "spec": "DN200", "unit": "件",
             "qty_ordered": 3, "unit_price": 100.10},
            {"item_name": "陶瓷三通", "spec": "DN150", "unit": "件",
             "qty_ordered": 2, "unit_price": 250.05},
        ],
    }
    spec.update(kw)
    return spec


class MoneyTest(unittest.TestCase):
    def test_q2_half_up(self):
        self.assertEqual(money.q2("1.005"), Decimal("1.01"))
        self.assertEqual(money.q2(2.675), Decimal("2.68"))
        self.assertEqual(money.q2("-0.004"), Decimal("0.00"))
        self.assertEqual(money.q2(""), Decimal("0.00"))

    def test_sum2_has_no_binary_tail(self):
        vals = [Decimal("0.10")] * 10
        self.assertEqual(str(money.sum2(vals)), "1.00")
        # 直接 float 相加会得到 0.9999999999999999
        self.assertEqual(float(money.sum2(vals)), 1.0)

    def test_mul_quantized(self):
        self.assertEqual(money.mul(3, "333.335"), Decimal("1000.01"))
        self.assertEqual(money.to_db("1.005"), 1.01)
        self.assertEqual(money.to_db_price("12.34567"), 12.3457)

    def test_eq_tolerance(self):
        self.assertTrue(money.eq(100.0, 100.004))
        self.assertFalse(money.eq(100.0, 100.01))


class ManualTotalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1money_")
        self.db = Database(str(Path(self.tmp) / "erp.db"))
        self.svc = OrderService(self.db)

    def tearDown(self):
        self.db.close()

    def test_01_declared_amount_survives_item_add(self):
        """声明 1000（明细只有 800.40）→ 加一行后仍是 1000，锁为 1"""
        o = self.svc.create_from_spec(_spec(amount_total=1000, item_qty_total=8))
        self.assertAlmostEqual(o["amount_total"], 1000.0, places=2)
        self.assertEqual(o["amount_manual"], 1)
        self.assertEqual(o["item_qty_manual"], 1)

        self.svc.add_item(o["id"], "法兰", 1, unit_price=99.99)
        o2 = self.db.get_by_id("orders", o["id"])
        self.assertAlmostEqual(o2["amount_total"], 1000.0, places=2)
        self.assertEqual(o2["item_qty_total"], 8, "声明的物资项数不应被行数覆盖")

    def test_02_auto_amount_follows_items(self):
        o = self.svc.create_from_spec(_spec())          # 未声明 → 自动
        self.assertEqual(o["amount_manual"], 0)
        self.assertAlmostEqual(o["amount_total"], 800.40, places=2)
        self.assertEqual(o["item_qty_total"], 2)

        self.svc.add_item(o["id"], "法兰", 1, unit_price=99.99)
        o2 = self.db.get_by_id("orders", o["id"])
        self.assertAlmostEqual(o2["amount_total"], 900.39, places=2)
        self.assertEqual(o2["item_qty_total"], 3)

    def test_03_item_edit_respects_lock(self):
        """改明细单价：自动汇总的订单头跟着变，手工锁定的不变"""
        from v2.domains import MaintenanceService
        maint = MaintenanceService(self.db)
        auto = self.svc.create_from_spec(_spec(project_no="T-AUTO"))
        manual = self.svc.create_from_spec(_spec(project_no="T-MAN", amount_total=5000))
        item = self.svc.list_items(auto["id"])[0]
        maint.edit("order_item", item["id"], {"unit_price": 200.0}, operator="test")
        self.assertAlmostEqual(self.db.get_by_id("orders", auto["id"])["amount_total"],
                               200.0 * 3 + 250.05 * 2, places=2)

        item2 = self.svc.list_items(manual["id"])[0]
        maint.edit("order_item", item2["id"], {"unit_price": 200.0}, operator="test")
        self.assertAlmostEqual(self.db.get_by_id("orders", manual["id"])["amount_total"],
                               5000.0, places=2)

    def test_04_reset_totals_unlocks(self):
        o = self.svc.create_from_spec(_spec(amount_total=9999, item_qty_total=99))
        back = self.svc.reset_totals(o["id"])
        self.assertEqual(back["amount_manual"], 0)
        self.assertEqual(back["item_qty_manual"], 0)
        self.assertAlmostEqual(back["amount_total"], 800.40, places=2)
        self.assertEqual(back["item_qty_total"], 2)
        # 幂等
        again = self.svc.reset_totals(o["id"])
        self.assertAlmostEqual(again["amount_total"], 800.40, places=2)
        # 解锁后加明细会正常联动
        self.svc.add_item(o["id"], "垫片", 1, unit_price=1.10)
        self.assertAlmostEqual(
            self.db.get_by_id("orders", o["id"])["amount_total"], 801.50, places=2)

    def test_05_money_accumulation_exact(self):
        """10 行数量 1 × 单价 0.1 → 合计恰好 1.00，无 0.9999999 尾数"""
        spec = _spec(project_no="T-DEC", items=[
            {"item_name": f"件{i}", "qty_ordered": 1, "unit_price": 0.1}
            for i in range(10)])
        o = self.svc.create_from_spec(spec)
        self.assertEqual(repr(o["amount_total"]), "1.0")
        s = self.svc.summary(o["id"])
        self.assertEqual(repr(s["amount"]), "1.0")


class MigrationV3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1mig3_")
        self.path = Path(self.tmp) / "erp.db"

    def tearDown(self):
        for p in Path(self.tmp).rglob("*"):
            if p.is_file():
                p.unlink()
        for p in sorted(Path(self.tmp).rglob("*"), reverse=True):
            if p.is_dir():
                p.rmdir()

    def test_header_mismatch_marked_manual(self):
        db = Database(str(self.path))
        svc = OrderService(db)
        o = svc.create_from_spec(_spec())              # 自动 800.40
        db.execute("DELETE FROM schema_meta")
        db.execute("INSERT INTO schema_meta(version, applied_at, note) VALUES (1, ?, 'legacy')",
                   ("2026-11-01T00:00:00",))
        # 模拟历史单据：表头手工填 12345.67，与明细不符；金额带二进制尾数
        db.execute("UPDATE orders SET amount_total=?, item_qty_total=?, "
                   "amount_manual=0, item_qty_manual=0 WHERE id=?",
                   (12345.67, 7, o["id"]))
        db.close()

        db2 = Database(str(self.path))                  # 触发 v1 → 最新版
        from v2.core.db import CURRENT_SCHEMA_VERSION
        self.assertEqual(db2.schema_version(), CURRENT_SCHEMA_VERSION)
        row = db2.get_by_id("orders", o["id"])
        self.assertEqual(row["amount_manual"], 1, "表头金额≠Σ明细 → 应标记为手工值")
        self.assertEqual(row["item_qty_manual"], 1, "物资数量≠行数 → 应标记为手工值")
        self.assertAlmostEqual(row["amount_total"], 12345.67, places=2)
        # 迁移后明细金额量化到分
        for i in db2.query("SELECT amount FROM order_items"):
            self.assertEqual(round(i["amount"], 2), i["amount"])
        db2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.2 端到端测试（标准库 unittest，零第三方依赖）

    python -m v2.tests.test_framework_v2

重点验证 2026-11-05 两轮口径：
  ① 询配码与询报价零关联 / 询配码数量是虚值 / 订单数量是实值且为排产唯一来源
  ② 中标项目经【一张内部订单】产生排产数量 / 一个询配码项目可产生【多张订单】
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.core.db import Database                                       # noqa: E402
from v2.core import models                                             # noqa: E402
from v2.core.errors import (DuplicateError, OverAllocationError,        # noqa: E402
                            StateError, ValidationError)
from v2.domains import (BidService, PartCodeService, OrderService,      # noqa: E402
                        ProductionService, TraceService)
from v2.main_v2 import run_smoke                                        # noqa: E402
from v2.migration.migrate_v15 import migrate                            # noqa: E402


class FrameworkV22Test(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.bids = BidService(self.db)
        self.codes = PartCodeService(self.db)
        self.orders = OrderService(self.db)
        self.prod = ProductionService(self.db)
        self.trace = TraceService(self.db)

    def tearDown(self):
        self.db.close()

    def build_won_bid(self, qty1: float = 12, qty2: float = 8):
        """中标项目（两条明细，数量是实值来源）"""
        p = self.bids.create_project("中标项目", customer="甲方")
        i1 = self.bids.add_item(p["id"], "耐磨弯头", spec="DN250", qty=qty1,
                                purchase_price=1800.0, quote_ratio=0.85)
        i2 = self.bids.add_item(p["id"], "波纹补偿器", spec="DN300", qty=qty2,
                                purchase_price=4200.0, quote_ratio=0.80)
        self.bids.mark_quoted(p["id"])
        self.bids.mark_won(p["id"], True)
        return p, i1, i2

    def build_code_chain(self):
        """标准外部链路：独立询配码项目(虚值) → 订单(实值) → 已确认"""
        cp = self.codes.create_project("询配码项目", customer="甲方")
        ci1 = self.codes.add_item(cp["id"], "耐磨弯头", spec="DN250", qty_reference=100)
        ci2 = self.codes.add_item(cp["id"], "波纹补偿器", spec="DN300", qty_reference=200)
        self.codes.submit(cp["id"])
        order = self.orders.create_from_code_project(cp["id"], items=[
            {"source_code_item_id": ci1["id"], "qty_ordered": 30},
            {"source_code_item_id": ci2["id"], "qty_ordered": 45},
            {"item_name": "新增物资", "qty_ordered": 10},
        ], customer="甲方")
        self.orders.confirm(order["id"])
        return {"cp": cp, "ci1": ci1, "ci2": ci2,
                "order": order, "oi": self.orders.list_items(order["id"])}

    # ── 结构与域隔离 ────────────────────────────────────────

    def test_01_schema_tables_and_v22_columns(self):
        tables = {r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"bid_projects", "bid_items", "code_projects", "code_items",
                    "orders", "order_items", "production_projects",
                    "production_tasks", "production_links", "doc_sequence", "audit_log"}
        self.assertTrue(expected.issubset(tables), f"缺表：{expected - tables}")
        self.assertEqual(self.db.scalar("PRAGMA foreign_keys"), 1)
        for table, cols in {
            "orders": {"order_type", "source_bid_project_id", "batch_seq"},
            "order_items": {"source_bid_item_id"},
            "production_links": {"order_type_snapshot", "source_type_snapshot",
                                 "bid_no_snapshot", "bid_line_snapshot"},
        }.items():
            have = {r["name"] for r in self.db.query(f"PRAGMA table_info({table})")}
            self.assertTrue(cols.issubset(have), f"{table} 缺 v2.2 列：{cols - have}")
        idx = {r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("uq_internal_order_per_bid", idx, "缺「一中标项目一张内部订单」唯一索引")

    def test_02_code_domain_is_independent(self):
        """询配码域不得有任何指向询报价域的外键或列"""
        cp_cols = {r["name"] for r in self.db.query("PRAGMA table_info(code_projects)")}
        ci_cols = {r["name"] for r in self.db.query("PRAGMA table_info(code_items)")}
        self.assertFalse(any("bid" in c or "quote" in c for c in cp_cols | ci_cols),
                         "询配码表混入了询报价来源字段")
        fks = self.db.query("PRAGMA foreign_key_list(code_projects)")
        self.assertFalse(any(r["table"].startswith(("bid", "quote")) for r in fks))
        bp_cols = {r["name"] for r in self.db.query("PRAGMA table_info(bid_projects)")}
        bi_cols = {r["name"] for r in self.db.query("PRAGMA table_info(bid_items)")}
        self.assertFalse(any("code" in c for c in bp_cols | bi_cols))
        self.assertFalse(hasattr(self.bids, "convert_to_code_order"))

    def test_03_production_links_anchor_is_order_item(self):
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(production_links)")}
        self.assertIn("order_item_id", cols)
        self.assertNotIn("code_item_id", cols, "排产关联不得直接挂询配码明细")
        self.assertNotIn("bid_item_id", cols, "排产关联不得直接挂中标明细（须经内部订单）")

    # ── 域 1：招投标 ────────────────────────────────────────

    def test_04_bid_quote_and_won(self):
        p = self.bids.create_project("报价项目")
        self.assertTrue(p["doc_no"].startswith("ZB-"))
        self.bids.add_item(p["id"], "弯头", qty=12, purchase_price=1800.0, quote_ratio=0.85)
        self.assertAlmostEqual(self.bids.list_items(p["id"])[0]["quote_price"],
                               1800.0 / 0.85, places=2)
        self.bids.mark_quoted(p["id"])
        self.bids.mark_won(p["id"], True)
        self.assertEqual(self.db.get_by_id("bid_projects", p["id"])["status"],
                         models.BidStatus.WON)
        self.assertEqual(len(self.bids.won_projects()), 1)
        with self.assertRaises(StateError):
            self.bids.set_status(p["id"], models.BidStatus.LOST)

    # ── 域 2：询配码虚值 ────────────────────────────────────

    def test_05_virtual_qty_is_not_a_constraint(self):
        cp = self.codes.create_project("虚值项目")
        ci = self.codes.add_item(cp["id"], "弯头", qty_reference=0)
        self.codes.submit(cp["id"])
        with self.db.tx():
            self.db.update("code_items", {"qty_reference": 99999}, "id = ?", (ci["id"],))
        self.assertEqual(self.codes.statistics(cp["id"])["qty_reference_total_virtual"], 99999)
        self.assertEqual(self.codes.statistics(cp["id"])["qty_ordered_total_real"], 0)
        draft = self.codes.create_project("草稿项目")
        with self.assertRaises(StateError):
            self.orders.create_order(source_code_project_id=draft["id"])

    # ── 域 3：订单实值 ──────────────────────────────────────

    def test_06_order_qty_must_be_explicit_real_value(self):
        cp = self.codes.create_project("下单项目")
        ci = self.codes.add_item(cp["id"], "弯头", qty_reference=100)
        self.codes.submit(cp["id"])
        with self.assertRaises(ValidationError):
            self.orders.create_from_code_project(cp["id"], items=[
                {"source_code_item_id": ci["id"]}  # 缺 qty_ordered，禁止自动取虚值
            ])
        o = self.orders.create_from_code_project(cp["id"], items=[
            {"source_code_item_id": ci["id"], "qty_ordered": 30},
            {"item_name": "新增物资", "qty_ordered": 10},
        ])
        self.assertEqual(o["order_type"], models.OrderType.EXTERNAL)
        self.assertEqual(o["batch_seq"], 1)
        items = self.orders.list_items(o["id"])
        self.assertEqual(items[0]["qty_ordered"], 30.0)
        self.assertNotEqual(items[0]["qty_ordered"], ci["qty_reference"])
        self.assertIsNone(items[1]["source_code_item_id"], "订单允许新增物资")
        self.assertEqual(self.orders.summary(o["id"])["qty_ordered_real"], 40.0)
        self.assertEqual(self.codes.statistics(cp["id"])["qty_ordered_total_real"], 30.0)
        self.assertEqual(self.db.get_by_id("code_items", ci["id"])["status"],
                         models.CodeItemStatus.ORDERED)

    # ── 域 4：排产与打通 ────────────────────────────────────

    def test_07_only_won_bid_can_link_production(self):
        won, _, _ = self.build_won_bid(5, 1)
        lost = self.bids.create_project("未中标项目")
        self.bids.add_item(lost["id"], "法兰", qty=1)
        self.bids.mark_quoted(lost["id"])
        self.bids.set_status(lost["id"], models.BidStatus.LOST)
        with self.assertRaises(StateError):
            self.prod.create_project("排产X", source_bid_project_id=lost["id"])
        p = self.prod.create_project("排产A", source_bid_project_id=won["id"])
        self.assertEqual(p["source_bid_project_id"], won["id"])
        with self.assertRaises(StateError):
            self.prod.link_bid_project(p["id"], lost["id"])

    def test_08_quantity_guard_uses_order_real_qty(self):
        c = self.build_code_chain()
        pa = self.prod.create_project("第一批")
        pb = self.prod.create_project("第二批")
        self.prod.link_order_item(pa["id"], c["oi"][0]["id"])          # 30 全排
        self.prod.link_order_item(pa["id"], c["oi"][1]["id"], 20)
        self.prod.link_order_item(pb["id"], c["oi"][1]["id"], 25)
        self.assertEqual(self.orders.allocated_qty(c["oi"][1]["id"]), 45.0)
        self.assertEqual(self.orders.available_qty(c["oi"][1]["id"]), 0.0)

        # ★ 把询配码虚值改成极小值，排产守恒完全不受影响
        with self.db.tx():
            self.db.update("code_items", {"qty_reference": 0.001}, "id = ?", (c["ci2"]["id"],))
        self.assertEqual(self.orders.available_qty(c["oi"][1]["id"]), 0.0)
        pc = self.prod.create_project("超排校验")
        with self.assertRaises(OverAllocationError):
            self.prod.link_order_item(pc["id"], c["oi"][1]["id"], 1)
        self.prod.link_order_item(pc["id"], c["oi"][2]["id"], 1)
        with self.assertRaises(DuplicateError):
            self.prod.link_order_item(pc["id"], c["oi"][2]["id"], 1)
        self.assertEqual(self.db.get_by_id("production_projects", pa["id"])["status"],
                         models.ProjectStatus.SCHEDULED)
        self.assertEqual(self.db.get_by_id("orders", c["order"]["id"])["status"],
                         models.OrderStatus.SCHEDULED)

    def test_09_trace_chain_and_virtual_vs_real(self):
        won, _, _ = self.build_won_bid(5, 1)
        c = self.build_code_chain()
        p = self.prod.create_project("排产A", source_bid_project_id=won["id"])
        self.prod.link_order_item(p["id"], c["oi"][0]["id"])
        self.prod.link_order_item(p["id"], c["oi"][1]["id"], 20)
        rows = self.trace.from_production(p["id"])
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual(r["order_no"], c["order"]["doc_no"])
        self.assertEqual(r["order_type"], models.OrderType.EXTERNAL)
        self.assertEqual(r["qty_ordered"], 30.0)          # 实值
        self.assertEqual(r["allocated_qty"], 30.0)
        self.assertEqual(r["code_no"], c["cp"]["doc_no"])
        self.assertEqual(r["item_code"], c["ci1"]["item_code"])
        self.assertEqual(r["qty_reference"], 100.0)       # 虚值，仅对照
        self.assertNotEqual(r["qty_ordered"], r["qty_reference"])
        self.assertEqual(r["bid_project_no"], won["doc_no"])
        self.assertEqual(r["bid_status"], models.BidStatus.WON)
        self.assertIn("询配码", r["source_origin"])

        back = self.trace.from_code_item(c["ci1"]["id"])
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["qty_ordered"], 30.0)
        self.assertEqual(back[0]["qty_scheduled"], 30.0)
        self.assertEqual(back[0]["qty_reference_virtual"], 100.0)
        self.assertEqual(len(self.trace.from_bid_project(won["id"])), 1)
        oi_rows = self.trace.from_order_item(c["oi"][1]["id"])
        self.assertEqual(oi_rows[0]["qty_ordered_real"], 45.0)
        full = self.trace.full_chain(p["id"])
        self.assertEqual(full["link_count"], 2)
        self.assertEqual(full["total_allocated_real"], 50.0)
        self.assertEqual(len(full["orders"]), 1)
        self.assertEqual(len(full["code_projects"]), 1)

    def test_10_snapshots_and_unlink(self):
        c = self.build_code_chain()
        p = self.prod.create_project("排产A")
        link = self.prod.link_order_item(p["id"], c["oi"][0]["id"], 10)
        old_spec = link["spec_snapshot"]
        old_virtual = link["qty_reference_snapshot"]
        self.assertEqual(link["source_type_snapshot"], "询配码项目")
        with self.db.tx():
            self.db.update("order_items", {"spec": "上游改名"}, "id = ?", (c["oi"][0]["id"],))
            self.db.update("code_items", {"qty_reference": 999}, "id = ?", (c["ci1"]["id"],))
        after = self.db.get_by_id("production_links", link["id"])
        self.assertEqual(after["spec_snapshot"], old_spec)
        self.assertEqual(after["qty_reference_snapshot"], old_virtual)
        self.assertEqual(self.orders.available_qty(c["oi"][0]["id"]), 20.0)
        self.prod.update_allocation(link["id"], 30.0)
        self.assertEqual(self.orders.available_qty(c["oi"][0]["id"]), 0.0)
        self.prod.unlink(link["id"])
        self.assertEqual(self.orders.available_qty(c["oi"][0]["id"]), 30.0)
        self.assertGreater(self.db.scalar("SELECT COUNT(*) FROM audit_log"), 0)

    # ── ★ v2.2：中标项目 → 内部订单 → 排产数量 ─────────────

    def test_13_internal_order_from_won_bid(self):
        won, bi1, bi2 = self.build_won_bid(12, 8)
        io = self.orders.create_internal_from_bid(won["id"], owner="张哥")
        self.assertEqual(io["order_type"], models.OrderType.INTERNAL)
        self.assertTrue(io["doc_no"].startswith("ND-"))
        self.assertEqual(io["status"], models.OrderStatus.CONFIRMED,
                         "内部订单自动确认，无需用户确认即可排产")
        self.assertEqual(io["qty_ordered_real"], 20.0)
        items = self.orders.list_items(io["id"])
        self.assertEqual([i["qty_ordered"] for i in items], [12.0, 8.0],
                         "内部订单实值必须等于中标明细数量")
        self.assertEqual([i["source_bid_item_id"] for i in items], [bi1["id"], bi2["id"]])
        self.assertAlmostEqual(items[0]["unit_price"], 1800.0 / 0.85, places=2)
        self.assertIsNone(items[0]["source_code_item_id"], "内部订单不得挂询配码项")
        # 一个中标项目仅一张内部订单
        with self.assertRaises(DuplicateError):
            self.orders.create_internal_from_bid(won["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM code_projects"), 0,
                         "内部订单不得生成任何询配码项目")
        # 未中标项目不能生成
        lost = self.bids.create_project("未中标项目")
        self.bids.add_item(lost["id"], "法兰", qty=3)
        self.bids.mark_quoted(lost["id"])
        self.bids.set_status(lost["id"], models.BidStatus.LOST)
        with self.assertRaises(StateError):
            self.orders.create_internal_from_bid(lost["id"])
        # 订单类型与来源互斥
        with self.assertRaises(ValidationError):
            self.orders.create_order(order_type=models.OrderType.INTERNAL)
        with self.assertRaises(ValidationError):
            self.orders.create_order(source_bid_project_id=won["id"],
                                     order_type=models.OrderType.EXTERNAL)

    def test_14_internal_order_drives_production_qty(self):
        won, bi1, bi2 = self.build_won_bid(12, 8)
        result = self.prod.create_project_from_won_bid(
            won["id"], workshop="管件车间", owner="张哥", operator="张哥")
        self.assertTrue(result["created_order"], "一键排产应自动生成内部订单")
        self.assertEqual(result["total_allocated_real"], 20.0)
        self.assertEqual(len(result["links"]), 2)
        self.assertEqual(result["order"]["order_type"], models.OrderType.INTERNAL)
        links = self.prod.list_links(result["project"]["id"])
        self.assertEqual([l["allocated_qty"] for l in links], [12.0, 8.0])
        self.assertTrue(all(l["order_type_snapshot"] == models.OrderType.INTERNAL
                            for l in links))
        self.assertTrue(all(l["source_type_snapshot"] == "中标项目" for l in links))
        self.assertEqual(links[0]["bid_line_snapshot"], f"{won['doc_no']}-01")
        # 内部订单的可排量已被用完，超排必须被拦
        with self.assertRaises(OverAllocationError):
            pc = self.prod.create_project("再排一次")
            self.prod.link_order_item(pc["id"], links[0]["order_item_id"], 1)
        # 溯源到中标项目的哪一项
        rows = self.trace.from_production(result["project"]["id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source_bid_project_no"], won["doc_no"])
        self.assertEqual(rows[0]["bid_line_no"], 1)
        self.assertIn("中标项目", rows[0]["source_origin"])
        self.assertEqual(rows[0]["qty_reference"], None, "内部订单链路不应带询配码虚值")
        view = self.trace.bid_internal_order(won["id"])
        self.assertEqual(len(view), 2)
        self.assertEqual(sum(r["qty_scheduled"] for r in view), 20.0)
        self.assertEqual(len(self.trace.from_bid_project(won["id"])), 1)
        # 幂等：再点一次不会重复生成内部订单，只新建一个排产项目（无剩余可排量）
        again = self.prod.create_project_from_won_bid(won["id"], owner="张哥")
        self.assertFalse(again["created_order"])
        self.assertEqual(again["links"], [])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM orders WHERE source_bid_project_id = ?", (won["id"],)), 1)

    # ── ★ v2.2：一码多单 ────────────────────────────────────

    def test_15_one_code_project_multiple_orders(self):
        cp = self.codes.create_project("分批下单项目", customer="华东电建")
        ci1 = self.codes.add_item(cp["id"], "耐磨弯头", qty_reference=100)
        ci2 = self.codes.add_item(cp["id"], "U形波纹管", qty_reference=200)
        self.codes.submit(cp["id"])
        placed = []
        for qty in (30, 45, 20):
            o = self.orders.create_from_code_project(cp["id"], items=[
                {"source_code_item_id": ci1["id"], "qty_ordered": qty},
            ], customer="华东电建")
            self.orders.confirm(o["id"])
            placed.append(o)
        self.assertEqual([o["batch_seq"] for o in placed], [1, 2, 3])
        self.assertEqual(len({o["doc_no"] for o in placed}), 3)
        # 同一询配码项被重复下单，虚值不做任何扣减/约束
        self.assertEqual(self.db.get_by_id("code_items", ci1["id"])["qty_reference"], 100)
        stat = self.codes.statistics(cp["id"])
        self.assertEqual(stat["order_count"], 3)
        self.assertEqual(stat["qty_reference_total_virtual"], 300.0)
        self.assertEqual(stat["qty_ordered_total_real"], 95.0)
        self.assertEqual([b["qty_ordered_real"] for b in stat["batches"]],
                         [30.0, 45.0, 20.0])
        self.assertEqual(len(self.codes.order_batches(cp["id"])), 3)
        self.assertEqual(len(self.trace.from_code_project(cp["id"])), 3)
        self.assertEqual(len(self.trace.from_code_item(ci1["id"])), 3)
        # 状态为「已下单」后仍可继续分批下单
        self.assertEqual(self.db.get_by_id("code_projects", cp["id"])["status"],
                         models.CodeProjectStatus.PARTIAL_ORDERED)  # ci2 未下单
        o4 = self.orders.create_from_code_project(cp["id"], items=[
            {"source_code_item_id": ci2["id"], "qty_ordered": 999},  # 远大于虚值 200
        ])
        self.assertEqual(o4["batch_seq"], 4)
        self.assertEqual(self.codes.statistics(cp["id"])["qty_ordered_total_real"], 1094.0)
        self.assertEqual(self.db.get_by_id("code_projects", cp["id"])["status"],
                         models.CodeProjectStatus.ORDERED)
        # 三张订单各自独立守恒：把其中一单排满不影响其他单
        p = self.prod.create_project("分批排产")
        self.prod.link_order_item(p["id"], self.orders.list_items(placed[0]["id"])[0]["id"])
        self.assertEqual(self.orders.available_qty(
            self.orders.list_items(placed[1]["id"])[0]["id"]), 45.0)

    # ── 老库升级：增量列补齐 ────────────────────────────────

    def test_16_legacy_db_auto_upgrade_columns(self):
        tmp = tempfile.mkdtemp(prefix="v22_upgrade_")
        try:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            # 模拟 v2.1 老库：三张表都缺 v2.2 新列，且 orders 里有一条历史订单
            conn.executescript("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_no TEXT NOT NULL UNIQUE,
                    source_code_project_id INTEGER, customer TEXT, order_date TEXT,
                    required_date TEXT, status TEXT NOT NULL DEFAULT '草稿',
                    owner TEXT, remark TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                    line_no INTEGER NOT NULL, item_name TEXT NOT NULL, spec TEXT,
                    material TEXT, unit TEXT, qty_ordered REAL NOT NULL, unit_price REAL,
                    source_code_item_id INTEGER, remark TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE production_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
                    order_item_id INTEGER NOT NULL, allocated_qty REAL NOT NULL,
                    order_no_snapshot TEXT NOT NULL, order_line_snapshot TEXT NOT NULL,
                    item_name_snapshot TEXT, spec_snapshot TEXT, code_no_snapshot TEXT,
                    item_code_snapshot TEXT, code_item_name_snapshot TEXT,
                    qty_reference_snapshot REAL, created_by TEXT, created_at TEXT NOT NULL);
                INSERT INTO orders (doc_no, customer, status, created_at, updated_at)
                VALUES ('DD-202601-0001', '老客户', '已确认', '2026-01-01', '2026-01-01');
            """)
            conn.commit()
            conn.close()

            db = Database(path)   # 应自动补列并成功建立依赖新列的索引
            cols = {r["name"] for r in db.query("PRAGMA table_info(orders)")}
            self.assertTrue({"order_type", "source_bid_project_id", "batch_seq"}.issubset(cols))
            old = db.get_by_id("orders", 1)
            self.assertEqual(old["customer"], "老客户")
            self.assertEqual(old["order_type"], models.OrderType.EXTERNAL)
            self.assertEqual(old["batch_seq"], 1)
            # 补列后新业务链路仍可在老库上跑
            bids, orders = BidService(db), OrderService(db)
            bp = bids.create_project("老库中标项目")
            bids.add_item(bp["id"], "弯头", qty=6)
            bids.mark_quoted(bp["id"]); bids.mark_won(bp["id"], True)
            io = orders.create_internal_from_bid(bp["id"])
            self.assertEqual(io["order_type"], models.OrderType.INTERNAL)
            db.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # ── P1：Excel/CSV 通道、一致性检查、演示数据 ────────────

    def test_17_excel_io_templates_and_integrity(self):
        import shutil
        from v2.core import excelio, integrity

        tmp = tempfile.mkdtemp(prefix="v22_excel_")
        try:
            # ① 多工作表导出 + 读回（openpyxl 不可用时自动降级为多 CSV）
            path = os.path.join(tmp, "全量.xlsx")
            sheets = {
                "订单": (["订单号", "订单类型", "实值合计"],
                         [("DD-202611-0001", "外部订单", 30.0),
                          ("ND-202611-0001", "内部订单", 20.0)]),
                "明细": (["行", "物资", "数量"], [(1, "耐磨弯头", 30.0)]),
            }
            written = excelio.export_workbook(path, sheets)
            self.assertTrue(os.path.exists(written.split("; ")[0]))
            if excelio.HAS_OPENPYXL:
                rows = excelio.import_rows(path, 0)
                self.assertEqual(rows[0]["订单类型"], "外部订单")
                self.assertEqual(len(rows), 2)
                self.assertEqual(len(excelio.import_rows(path, 1)), 1)

            # ② CSV 模板 → 导入映射 → 真正写进服务层
            csv_path = os.path.join(tmp, "code_tpl.csv")
            excelio.export_template(csv_path, "code_item")
            mapped = excelio.map_template_rows("code_item", excelio.import_rows(csv_path))
            self.assertEqual(len(mapped), 1)
            self.assertEqual(mapped[0]["item_name"], "耐磨弯头")
            self.assertEqual(mapped[0]["qty_reference"], 100.0)
            cp = self.codes.create_project("Excel导入测试")
            for m in mapped:
                self.codes.add_item(cp["id"], **m)
            self.assertEqual(len(self.codes.list_items(cp["id"])), 1)
            self.assertEqual(self.codes.statistics(cp["id"])["qty_reference_total_virtual"],
                             100.0, "模板里的虚值只写入 qty_reference")

            # 界面填的提交日期应真正落库（表单字段不得被忽略）
            cp2 = self.codes.create_project("提交日期测试")
            self.codes.submit(cp2["id"], file_path="demo.xlsx", submit_date="2026-12-01")
            self.assertEqual(self.db.get_by_id("code_projects", cp2["id"])["submit_date"],
                             "2026-12-01")

            bid_csv = os.path.join(tmp, "bid_tpl.csv")
            excelio.export_template(bid_csv, "bid_item")
            bid_mapped = excelio.map_template_rows("bid_item", excelio.import_rows(bid_csv))
            bp = self.bids.create_project("Excel导入测试")
            for m in bid_mapped:
                self.bids.add_item(bp["id"], **m)
            self.assertEqual(self.bids.summary(bp["id"])["count"], 1)
            self.assertEqual(self.bids.list_items(bp["id"])[0]["qty"], 12.0)

            # ③ 一致性检查：正常链路通过；人为超排后能被抓出
            c = self.build_code_chain()
            p = self.prod.create_project("守恒检查")
            self.prod.link_order_item(p["id"], c["oi"][0]["id"])
            self.assertTrue(integrity.check(self.db)["ok"])
            with self.db.tx():
                self.db.execute(
                    "INSERT INTO production_links (project_id, order_item_id, allocated_qty, "
                    "order_no_snapshot, order_line_snapshot, created_at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (p["id"], c["oi"][2]["id"], 999.0, "FAKE", "FAKE-01"))
            report = integrity.check(self.db)
            self.assertFalse(report["ok"])
            self.assertEqual(len(report["over_alloc"]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_18_demo_data_covers_both_chains(self):
        from v2 import demo_data
        from v2.core import integrity

        msg = demo_data.seed(self.db)
        self.assertIn("内部订单", msg)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM bid_projects"), 2)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM code_projects"), 1)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders WHERE order_type='内部订单'"), 1)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders WHERE order_type='外部订单'"), 2)
        self.assertGreater(self.db.scalar("SELECT COUNT(*) FROM production_links"), 0)
        self.assertTrue(integrity.check(self.db)["ok"])
        # 内部订单实值全部转为排产数量
        io = self.db.query_one("SELECT * FROM orders WHERE order_type='内部订单'")
        self.assertEqual(self.db.scalar(
            "SELECT COALESCE(SUM(qty_ordered),0) FROM order_items WHERE order_id = ?",
            (io["id"],)), 20.0)
        self.assertEqual(self.db.scalar(
            "SELECT COALESCE(SUM(pl.allocated_qty),0) FROM production_links pl "
            "JOIN order_items oi ON oi.id = pl.order_item_id WHERE oi.order_id = ?",
            (io["id"],)), 20.0)

    # ── 集成演示与迁移 ──────────────────────────────────────

    def test_11_smoke_pipeline(self):
        r = run_smoke(verbose=False)
        self.assertEqual(r["internal_order_qty"], 20.0)
        self.assertEqual(r["internal_links"], 2)
        self.assertEqual(r["internal_allocated"], 20.0)
        self.assertTrue(r["internal_created_order"])
        self.assertEqual(r["external_order_count"], 3)
        self.assertEqual(r["batch_seqs"], [1, 2, 3])
        self.assertEqual(r["real_total"], 135.0)
        self.assertEqual(r["virtual_total"], 350.0)
        self.assertEqual(r["code_items"], 3)
        self.assertEqual(r["links_a"], 2)
        self.assertEqual(r["links_b"], 1)
        self.assertEqual(r["allocated_oi2"], 45.0)
        self.assertEqual(r["available_oi2"], 0.0)
        self.assertEqual(r["trace_a_rows"], 2)
        self.assertEqual(r["trace_b_rows"], 2)
        self.assertEqual(r["back_links"], 2)
        self.assertEqual(r["batch_rows"], 6)
        self.assertEqual(r["internal_order_rows"], 2)
        self.assertTrue(r["virtual_ne_real"])
        self.assertTrue(r["over_alloc_blocked"])
        self.assertTrue(r["lost_bid_blocked"])
        self.assertTrue(r["duplicate_internal_blocked"])
        self.assertTrue(r["lost_internal_blocked"])
        self.assertTrue(r["snapshot_ok"])
        self.assertTrue(r["virtual_snapshot_ok"])
        self.assertEqual(r["order_status"], models.OrderStatus.SCHEDULED)
        self.assertEqual(r["internal_order_type"], models.OrderType.INTERNAL)

    def test_12_migration_to_bid_domain(self):
        tmp = tempfile.mkdtemp(prefix="v22_mig_")
        try:
            old = os.path.join(tmp, "products.db")
            new = os.path.join(tmp, "erp.db")
            conn = sqlite3.connect(old)
            conn.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, spec TEXT,
                    category TEXT, quote_ratio REAL, inquiry_qty INTEGER,
                    purchase_price REAL, inquiry_unit TEXT, description TEXT,
                    project TEXT, is_won TEXT, quoter TEXT, image_path TEXT,
                    drawing_path TEXT, created_at TEXT, updated_at TEXT)
            """)
            conn.executemany("""
                INSERT INTO products (name, spec, quote_ratio, inquiry_qty,
                    purchase_price, inquiry_unit, project, is_won, quoter, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                ("耐磨弯头", "DN250", 0.85, 12, 1800.0, "件", "沈西改造", "是", "张哥", "2026-07-14T10:00:00"),
                ("法兰", "DN100", 0.90, 5, 320.0, "片", "沈西改造", "是", "张哥", "2026-07-14T10:01:00"),
                ("三通", "DN50", 0.80, 3, 200.0, "件", "台山项目", "", "张哥", "2026-07-15T09:00:00"),
            ])
            conn.commit()
            conn.close()

            dry = migrate(old, new, dry_run=True)
            self.assertEqual(dry["legacy_rows"], 3)
            self.assertEqual(dry["legacy_groups"], 2)
            self.assertFalse(os.path.exists(new))
            report = migrate(old, new, dry_run=False)
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["created_projects"], 2)
            self.assertTrue(report["reconciliation"]["row_count_match"])
            self.assertTrue(report["reconciliation"]["purchase_match"])
            self.assertTrue(report["reconciliation"]["quote_match"])
            db2 = Database(new)
            rows = db2.query("SELECT legacy_product_id FROM bid_items ORDER BY id")
            self.assertEqual([r["legacy_product_id"] for r in rows], [1, 2, 3])
            self.assertEqual(db2.scalar("SELECT COUNT(*) FROM code_projects"), 0,
                             "迁移不得生成询配码项目")
            self.assertEqual(db2.scalar("SELECT COUNT(*) FROM orders"), 0,
                             "迁移不得生成订单（含内部订单）")
            db2.close()
            conn = sqlite3.connect(old)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 3)
            conn.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

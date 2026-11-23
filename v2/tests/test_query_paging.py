#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1-10 查询下推 / 分页 + P1-11 单实例检测

    python -m pytest v2/tests/test_query_paging.py -q

验收口径：
  · filters → SQL WHERE/HAVING 翻译正确，值全部参数绑定，LIKE 特殊符已转义
  · 结果集与「内存过滤」完全一致（同条件同命中），只是过滤位置从 Python 搬到 SQLite
  · 分页只影响展示行数，不改变命中总数；末页边界不越界
  · 同一数据库的第二个实例拿到 False（具名互斥体/锁文件）
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import query as qb                                  # noqa: E402
from v2.core.db import Database                                  # noqa: E402
from v2.core import single_instance                              # noqa: E402
from v2.domains import OrderService                              # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

try:
    from PySide6.QtWidgets import QApplication
    PYSIDE = True
except ImportError:                                              # pragma: no cover
    PYSIDE = False


class WhereBuilderTest(unittest.TestCase):
    def test_empty_filters(self):
        sql, params = qb.build_where({}, ["o.doc_no"], status_column="o.status")
        self.assertEqual(sql, "")
        self.assertEqual(params, [])

    def test_keyword_multi_column(self):
        sql, params = qb.build_where({"keyword": "DD-2026"},
                                     ["o.doc_no", "o.project_no"])
        self.assertIn("o.doc_no", sql)
        self.assertIn("OR", sql)
        self.assertEqual(params, ["%DD-2026%", "%DD-2026%"])

    def test_like_escaping(self):
        sql, params = qb.build_where({"keyword": "100%_A"}, ["o.customer"])
        self.assertEqual(params, ["%100\\%\\_A%"])
        self.assertIn("ESCAPE", sql)

    def test_status_customer_date_type(self):
        sql, params = qb.build_where(
            {"status": "已确认", "customer": "沈西", "date_from": "2026-01-01",
             "date_to": "2026-12-31", "order_type": "外部订单"},
            ["o.doc_no"], status_column="o.status", customer_column="o.customer",
            date_column="o.order_date", type_column="o.order_type")
        self.assertEqual(params, ["已确认", "%沈西%", "2026-01-01", "2026-12-31",
                                  "外部订单"])
        self.assertEqual(sql.count("?"), 5)

    def test_won_only_and_capacity(self):
        sql, _ = qb.build_where({"won_only": True}, ["bp.doc_no"],
                                status_column="bp.status")
        self.assertIn("bp.status = '中标'", sql)
        self.assertIn("qty_real - qty_scheduled", qb.having_capacity())


class PushdownEquivalenceTest(unittest.TestCase):
    """同一批数据：SQL 过滤结果 == 原先内存过滤结果"""

    def setUp(self):
        self.db = Database(":memory:")
        svc = OrderService(self.db)
        for i, (cust, status) in enumerate([
                ("国能沈西", "已确认"), ("国能沈西", "已验收"), ("华能大连", "已确认"),
                ("大唐托克托", "待发货"), ("国能沈西", "发货中")]):
            svc.create_from_spec({
                "project_no": f"P-{i:03d}", "customer": cust, "status": status,
                "order_type": "外部订单", "order_date": f"2026-0{(i % 9) + 1}-05",
                "items": [{"item_name": "耐磨弯头", "qty_ordered": 1, "unit_price": 10}]})

    def tearDown(self):
        self.db.close()

    def _sql_rows(self, filters):
        where, params = qb.build_where(
            filters, ["o.doc_no", "o.project_no", "o.customer", "o.erp_order_no"],
            status_column="o.status", customer_column="o.customer",
            date_column="o.order_date", type_column="o.order_type")
        return self.db.query("SELECT * FROM orders o" + where + " ORDER BY o.id",
                             tuple(params))

    def _py_rows(self, filters):
        rows = self.db.query("SELECT * FROM orders ORDER BY id")
        out = []
        for r in rows:
            if filters.get("status") and r["status"] != filters["status"]:
                continue
            if filters.get("customer") and \
                    filters["customer"] not in (r["customer"] or ""):
                continue
            if filters.get("keyword") and \
                    filters["keyword"] not in (r["project_no"] or ""):
                continue
            out.append(r)
        return out

    def test_status_customer_keyword_match(self):
        for filters in ({"status": "已确认"}, {"customer": "沈西"},
                        {"keyword": "P-00"}, {"status": "已确认", "customer": "沈西"}):
            self.assertEqual([r["id"] for r in self._sql_rows(filters)],
                             [r["id"] for r in self._py_rows(filters)], filters)

    def test_has_capacity_filters_out_fully_scheduled(self):
        # 给 P-000 建排产单并排满 → 可排量为 0，应被 HAVING 过滤掉
        from v2.domains import OrderScheduleService
        oid = self.db.scalar("SELECT id FROM orders WHERE project_no='P-000'")
        item = self.db.query_one("SELECT * FROM order_items WHERE order_id=?", (oid,))
        svc = OrderScheduleService(self.db)
        svc.ensure(item["id"], "test")
        base = "FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id"
        sql = ("""SELECT o.id, COALESCE(SUM(oi.qty_ordered),0) AS qty_real,
                         COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                   JOIN order_items oi2 ON oi2.id = pl.order_item_id
                                   WHERE oi2.order_id = o.id),0) AS qty_scheduled
                  """ + base + " GROUP BY o.id" + qb.having_capacity())
        ids = [r["id"] for r in self.db.query(sql)]
        self.assertNotIn(oid, ids)
        self.assertEqual(len(ids), 4)


@unittest.skipUnless(PYSIDE, "未安装 PySide6")
class PagingUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)

    def setUp(self):
        from v2.ui import theme
        self.db = Database(":memory:")
        svc = OrderService(self.db)
        for i in range(5):
            svc.create_from_spec({
                "project_no": f"PG-{i}", "customer": f"客户{i}", "status": "已确认",
                "items": [{"item_name": "弯头", "qty_ordered": 1, "unit_price": 1}]})
        self._orig_notify = theme.notify
        theme.notify = lambda *a, **k: None
        from v2.ui.main_window_v2 import MainWindowV2
        self.w = MainWindowV2(self.db)
        self.w.PAGE_SIZE = 2
        self.w.switch_view("orders")
        self.w.page_offsets = {k: 0 for k in self.w.page_offsets}
        self.w.refresh_all()

    def tearDown(self):
        from v2.ui import theme
        theme.notify = self._orig_notify
        self.w.close()
        self.db.close()

    def test_01_first_page_and_total(self):
        self.assertEqual(self.w.tbl_orders.rowCount(), 2)
        self.assertEqual(self.w.page_state["orders"], (0, 2, 5))
        self.assertEqual(self.w.count_badge.text(), "2 / 5 条")
        self.assertIn("第 1–2 条", self.w.pager_label.text())
        self.assertFalse(self.w.btn_prev_page.isEnabled())
        self.assertTrue(self.w.btn_next_page.isEnabled())

    def test_02_next_and_last_page(self):
        self.w._page(1)
        self.assertEqual(self.w.page_state["orders"][0], 2)
        self.w._page(1)                       # 第 3 页：只剩 1 行
        offset, n, total = self.w.page_state["orders"]
        self.assertEqual((offset, n, total), (4, 1, 5))
        self.assertFalse(self.w.btn_next_page.isEnabled())
        self.w._page(1)                       # 越界不动作
        self.assertEqual(self.w.page_state["orders"][0], 4)

    def test_03_filter_resets_page(self):
        self.w._page(1)
        self.w.filters = {"customer": "客户3"}
        self.w.page_offsets = {k: 0 for k in self.w.page_offsets}
        self.w.refresh_all()
        self.assertEqual(self.w.tbl_orders.rowCount(), 1)
        self.assertEqual(self.w.page_state["orders"], (0, 1, 1))
        self.assertIn("客户3", self.w.tbl_orders.item(0, 1).text())

    def test_04_capacity_filter_count_sql(self):
        """回归：has_capacity 的 HAVING 引用 select 别名，计数子查询必须一并包住"""
        self.w.filters = {"has_capacity": True}
        self.w.refresh_all()                        # 不应抛 no such column: qty_real
        offset, n, total = self.w.page_state["orders"]
        self.assertEqual(total, 5)
        self.assertEqual(n, 2)


class SingleInstanceTest(unittest.TestCase):
    def test_01_same_process_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp(prefix="p1inst_")) / "erp.db"
        self.assertTrue(single_instance.acquire(str(tmp)))
        self.assertTrue(single_instance.acquire(str(tmp)), "本进程重复调用应视为成功")
        single_instance.release()

    @unittest.skipUnless(sys.platform == "win32", "具名互斥体仅 Windows 验证")
    def test_02_second_process_is_rejected(self):
        tmp = Path(tempfile.mkdtemp(prefix="p1inst2_")) / "erp.db"
        self.assertTrue(single_instance.acquire(str(tmp)))
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, r"{ROOT}")
            from v2.core import single_instance
            ok = single_instance.acquire(r"{tmp}")
            print("ACQUIRED" if ok else "BLOCKED")
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
        self.assertIn("BLOCKED", out.stdout, out.stderr)
        single_instance.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)

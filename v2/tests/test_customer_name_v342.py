#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.2 回归（2026-11-10 · 用户第 16 轮）：客户名称统一 + 智能输入

用户口径：
  1. 「需方单位」「客户」字段统一叫「客户名称」；新增询配码项目时客户名称
     可从订单收集历史值填充列表，输入关键字即可查出相应客户名称；
  2. 询报价与招投标的新增招标项目客户输入同样处理；
  3. 订单管理的新增订单客户名称操作同样处理。

锁定四条硬口径：
  ① 数据库列名仍是 customer，schema_meta 仍 v6，零迁移零改动；
  ② 历史 Excel / 文档里的「需方单位」「客户」是别名，继续可识别；
  ③ 客户候选不另建字典表，从 orders 为主 + 项目表补充去重汇总；
  ④ 关键字是「包含匹配、大小写无关」，且新客户可以自由手输。

    python -m pytest v2/tests/test_customer_name_v342.py -q
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                      # noqa: E402
from v2.core import docread                                     # noqa: E402
from v2.core import models                                      # noqa: E402
from v2.core import order_fields as ofdefs                      # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database          # noqa: E402
from v2.domains import BidService, OrderService, PartCodeService  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QWidget, QLineEdit
    PYSIDE = True
except ImportError:                                             # pragma: no cover
    PYSIDE = False

_APP = None


def _app():
    global _APP
    if _APP is None:
        from v2.ui import theme
        _APP = QApplication.instance() or QApplication([])
        theme.apply_theme(_APP)
    return _APP


class Owner(QWidget):
    """模拟主窗口 Mixin：带 db，供 FormDialog 自动收候选"""

    def __init__(self, db):
        super().__init__()
        self.db = db


@unittest.skipUnless(PYSIDE, "PySide6 未安装")
class Test342(unittest.TestCase):

    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="v342_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.bids = BidService(self.db)
        self.codes = PartCodeService(self.db)
        self.o1 = self.orders.create_order(
            customer="国能沈西热电厂", project_no="SO-342-01",
            status=models.OrderStatus.CONFIRMED)
        self.o2 = self.orders.create_order(
            customer="国能浙江北仑第一发电有限公司", project_no="SO-342-02",
            status=models.OrderStatus.CONFIRMED)
        self.o3 = self.orders.create_order(
            customer="国能沈西热电厂", project_no="SO-342-03",
            status=models.OrderStatus.CONFIRMED)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── ① 字段统一 + 别名兼容 ──────────────────────────────

    def test_01_version(self):
        self.assertEqual(__version__, "3.5.4")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        self.assertEqual(self.db.schema_version(), 7)

    def test_02_canonical_header_is_customer_name(self):
        self.assertIn("客户名称", ofdefs.headers())
        self.assertNotIn("需方单位", ofdefs.headers())
        self.assertEqual(ofdefs.headers(), docread.CANONICAL_LABELS)
        spec = next(f for f in ofdefs.HEAD_FIELDS if f["key"] == "customer")
        self.assertEqual(spec["label"], "客户名称")
        self.assertIn("需方单位", spec["aliases"])
        self.assertIn("客户", spec["aliases"])

    def test_03_legacy_alias_still_resolves(self):
        # 历史 Excel / 文档写「需方单位」「客户」仍能识别到 customer
        row = {"订单项目编号": "SO-LEGACY", "需方单位": "甲厂",
               "物资名称": "耐磨弯头", "数量": "2", "单价": "100", "总价": "200"}
        res = ofdefs.parse_rows([row])
        self.assertEqual(res["mapping"].get("customer"), "需方单位")
        self.assertEqual(res["groups"][0]["head"]["customer"], "甲厂")
        mapping, ignored = ofdefs.resolve_headers(["客户名称"])
        self.assertEqual(mapping["customer"], "客户名称")
        self.assertEqual(ignored, [])

    def test_04_main_view_headers_unified(self):
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        for view, table in (("orders", "tbl_orders"),
                            ("bids", "tbl_bids"),
                            ("codes", "tbl_codes")):
            w.switch_view(view)
            w.refresh_all()
            heads = [getattr(w, table).horizontalHeaderItem(i).text()
                     for i in range(getattr(w, table).columnCount())]
            self.assertIn("客户名称", heads, (view, heads))
            self.assertNotIn("需方单位", heads, (view, heads))
            self.assertNotIn("客户", heads, (view, heads))
        w.close()

    # ── ② 候选收集 ────────────────────────────────────────

    def test_05_collect_from_orders_dedup_and_rank(self):
        from v2.ui.customerfield import collect_customer_names
        names = collect_customer_names(self.db)
        self.assertEqual(names[:2], ["国能沈西热电厂",
                                     "国能浙江北仑第一发电有限公司"])
        self.assertEqual(len(names), 2)                       # 重复单只算一个客户

    def test_06_collect_also_from_bid_and_code_projects(self):
        from v2.ui.customerfield import collect_customer_names
        self.bids.create_project("招标项目A", customer="华东电建")
        self.codes.create_project("询配码项目A", customer="厦门龙净")
        names = collect_customer_names(self.db)
        self.assertIn("华东电建", names)
        self.assertIn("厦门龙净", names)

    def test_07_placeholders_and_blanks_dropped(self):
        from v2.ui.customerfield import clean_names
        self.assertEqual(clean_names(["", "  ", "—", "-", "无", None]), [])
        self.assertEqual(clean_names(["甲厂", " 甲厂 ", "乙厂", "乙厂"]),
                         sorted(["甲厂", "乙厂"]))

    # ── ③ 控件：关键字检索 / 自由输入 ──────────────────────

    def test_08_keyword_contains_match(self):
        from v2.ui.customerfield import CustomerComboBox
        c = CustomerComboBox(db=self.db)
        self.assertEqual(c.count(), 2)
        self.assertEqual(c.matching("北仑"), ["国能浙江北仑第一发电有限公司"])
        self.assertEqual(len(c.matching("国能")), 2)
        self.assertEqual(c.matching("不存在"), [])
        self.assertEqual(len(c.matching("")), 2)

    def test_09_completer_prefix_synced_on_typing(self):
        from v2.ui.customerfield import CustomerComboBox
        c = CustomerComboBox(db=self.db)
        c.setText("北仑")
        _app().processEvents()
        self.assertEqual(c.completer().completionPrefix(), "北仑")
        self.assertEqual(c.completer().completionCount(), 1)

    def test_10_free_text_new_customer(self):
        from v2.ui.customerfield import CustomerComboBox
        c = CustomerComboBox(db=self.db)
        c.setText("新建客户丙厂")
        self.assertEqual(c.text(), "新建客户丙厂")
        self.assertEqual(c.count(), 2, "自由输入不应污染候选列表")
        c.clear()
        self.assertEqual(c.text(), "")
        self.assertEqual(c.count(), 2, "clear 只清输入，不清候选")

    # ── ④ 三个新建表单接入 ────────────────────────────────

    def test_11_project_form_fields_use_customer_type(self):
        from v2.ui import dialogs
        for maker in (dialogs.bid_project_fields, dialogs.code_project_fields):
            f = next(f for f in maker() if f["key"] == "customer")
            self.assertEqual(f["label"], "客户名称")
            self.assertEqual(f["type"], "customer")

    def test_12_form_dialog_auto_fills_candidates_from_owner(self):
        from v2.ui import dialogs
        from v2.ui.customerfield import CustomerComboBox
        owner = Owner(self.db)
        d = dialogs.FormDialog(owner, "测试", dialogs.code_project_fields())
        w, typ = d.widgets["customer"]
        self.assertIsInstance(w, CustomerComboBox)
        self.assertEqual(w.count(), 2)
        w.setText("北仑")
        self.assertEqual(d.value("customer"), "北仑")
        d.close()

    def test_13_new_order_dialog_customer_field(self):
        from v2.ui import dialogs
        from v2.ui.customerfield import CustomerComboBox
        d = dialogs.NewOrderDialog(None, self.db)
        self.assertIsInstance(d.edit_customer, CustomerComboBox)
        self.assertEqual(d.edit_customer.count(), 2)
        d.edit_customer.setText("国能浙江北仑第一发电有限公司")
        self.assertEqual(d.result_spec()["customer"],
                         "国能浙江北仑第一发电有限公司")
        d.close()

    def test_14_order_edit_dialog_prefills_and_reads(self):
        from v2.ui import dialogs
        from v2.ui.customerfield import CustomerComboBox
        d = dialogs.OrderEditDialog(None, self.db, self.o1["id"])
        self.assertIsInstance(d.edit_customer, CustomerComboBox)
        self.assertEqual(d.edit_customer.text(), "国能沈西热电厂")
        d.edit_customer.setText("改成新客户丁厂")
        self.assertEqual(d._diff_head().get("customer"), "改成新客户丁厂")
        d.close()

    # ── ⑤ 生成的 Word 模板标签 ───────────────────────────

    def test_15_word_templates_use_customer_name(self):
        from docx import Document
        from v2.core import worddoc
        ctx = {"schedule_no": "DDPS-TEST", "order_no": "DD-TEST",
               "project_no": "SO-342-01", "customer": "国能沈西热电厂",
               "order_type": "外部订单", "status": "已确认",
               "items": [{"line_no": 1, "item_name": "耐磨弯头", "spec": "DN250",
                          "unit": "件", "qty_ordered": 2, "unit_price": 100,
                          "amount": 200}]}
        p = self.tmp / "approval.docx"
        worddoc.build_approval_docx(str(p), ctx)
        text = "\n".join([c.text for t in Document(str(p)).tables
                          for row in t.rows for c in row.cells])
        self.assertIn("客户名称", text)
        self.assertNotIn("需方单位", text)


if __name__ == "__main__":
    unittest.main()

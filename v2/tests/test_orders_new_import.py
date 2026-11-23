#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单管理回归（v2.3 · 2026-11-06 订单头 8 字段 + 明细 9 字段）

    python -m v2.tests.test_orders_new_import                         # 纯逻辑
    D:\\Program Files\\Python313\\python.exe -m v2.tests.test_orders_new_import  # 含 Qt 交互

验收口径：
  · 下单向导的类 / 主窗口动作 / 菜单入口全部消失，订单分组仍是 6 项新动作
  · 导入模板 17 列由 core/order_fields.py 单一来源派生，CSV/XLSX 可往返
  · 「订单项目编号」相同的行合并为一张订单，留空则每行独立成单
  · 真实「销售订单汇总」工作簿可直接解析（订单编号/产品代码/含税单价/价税合计别名）
  · UTF-8 BOM / GBK、逗号/分号、表头别名、Excel datetime、千分位单价均容错
  · 数量(实值)必须 > 0；坏行逐行报告且不落库；内部/外部来源互斥
  · 总价/价税合计留空自动汇总；Q12（2026-11-07 确认）「物资数量」= 物资项目数，
    留空自动 = 明细行数（不是 Σ 明细数量）；显式声明值一律原样保留
  · 订单状态支持 草稿/已确认/待发货/发货中/已验收/已交付/关闭
  · create_from_spec 统一建单；import_orders 单事务整批成功或整体回滚
  · 虚值 qty_reference 不受影响，订单数量仍是排产唯一锚点，守恒恒通过
"""
import csv
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import excelio                                     # noqa: E402
from v2.core import order_fields as of                          # noqa: E402
from v2.core.db import Database                                 # noqa: E402
from v2.core.errors import (DuplicateError, NotFoundError,      # noqa: E402
                            ValidationError)
from v2.core.integrity import check                              # noqa: E402
from v2.domains import (BidService, OrderService,               # noqa: E402
                        PartCodeService, ProductionService)
from v2.ui.main_window_v2 import ACTION_GROUPS                    # noqa: E402

try:
    from PySide6.QtWidgets import (QApplication, QMessageBox,    # noqa: E402
                                   QDialogButtonBox)
    from PySide6.QtCore import QTimer                            # noqa: E402
    PYSIDE = True
except ImportError:
    PYSIDE = False

EXPECTED_HEADERS = [
    "订单项目编号", "客户名称", "订单类型", "订单状态", "价税合计",
    "交货日期", "物资数量", "ERP订单编号",
    "物资代码", "物资名称", "实际生产物资名称", "规格", "单位",
    "数量", "单价", "总价", "参数描述",
]

# 测试文件统一用 17 个业务列 + 9 个可选兼容列，便于覆盖来源/分组等场景
EXTRA_LABELS = [
    "订单分组", "下单日期", "负责人", "备注",
    "来源询配码项目号", "来源中标项目号",
    "来源询配码项号", "来源中标明细行号", "订单来源",
]
ALL_KEYS = of.ORDER_KEYS + list(of.EXTRA_KEYS)
HEADERS_ALL = of.headers() + EXTRA_LABELS

ROOT = Path(__file__).resolve().parents[2]
REAL_WORKBOOK = ROOT / "销售订单汇总（24年7月-26年）.xlsx"


class OrderFixture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.bids = BidService(self.db)
        self.codes = PartCodeService(self.db)
        self.orders = OrderService(self.db)
        self.prod = ProductionService(self.db)

        self.cp = self.codes.create_project("订单回归询配码", customer="用户甲")
        self.ci1 = self.codes.add_item(self.cp["id"], "耐磨弯头", qty_reference=100)
        self.ci2 = self.codes.add_item(self.cp["id"], "补偿器", qty_reference=200)
        self.codes.submit(self.cp["id"], submit_date="2026-11-06")

        self.bp = self.bids.create_project("中标回归项目", customer="电厂")
        self.bi = self.bids.add_item(self.bp["id"], "耐磨弯头", qty=12,
                                     purchase_price=1000, quote_ratio=0.85)
        self.bids.mark_won(self.bp["id"])

        self.tmp = tempfile.TemporaryDirectory(prefix="orders_v23_")
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write_rows(self, name, rows, headers=None, encoding="utf-8-sig",
                   delimiter=","):
        p = self.path(name)
        with open(p, "w", encoding=encoding, newline="") as f:
            w = csv.writer(f, delimiter=delimiter)
            w.writerow(headers or HEADERS_ALL)
            w.writerows(rows)
        return p

    @staticmethod
    def row(**kw):
        base = dict(zip(ALL_KEYS, [""] * len(ALL_KEYS)))
        base.update({
            "order_type": "外部订单", "status": "草稿", "unit": "件",
            "qty_ordered": "1", "item_name": "耐磨弯头",
        })
        base.update(kw)
        return [base[k] for k in ALL_KEYS]


class OrderFieldSpecTest(OrderFixture):

    def test_01_wizard_is_removed_and_actions_replaced(self):
        from v2.ui import dialogs
        self.assertFalse(hasattr(dialogs, "OrderWizardDialog"))
        order_group = [g for g in ACTION_GROUPS if g[1] == "orders"][0]
        # v3.2.0：动作名称简化 + 正式化（短名，完整口径移到第 4 元素 tooltip）
        self.assertEqual([a[0] for a in order_group[2]], [
            # v3.5.3：两段式建单，明细录入 / 导入与「新增订单」同级
            "新增订单", "录入订单明细", "导入订单明细",
            "编辑订单", "单据识别建单", "导入订单",
            "下载订单模板", "确认订单", "重算订单合计",
            "订单排产", "合并排产", "整单排产",
            # v3.4.0：右键菜单同源的排产文件查看 / 编辑入口
            "排产文件",
            "查看订单明细", "导出订单数据",
            # v3.3.0：交期自动提醒入口
            "临期订单", "标记已发货", "交期提醒设置",
            # v3.5.0：订单盈亏与多供应商采购归集
            "经营分析"])
        self.assertTrue(all(len(a) >= 4 and a[3] for a in order_group[2]))
        self.assertFalse(any("下单向导" in a[0]
                             for g in ACTION_GROUPS for a in g[2]))
        self.assertFalse(any(a[1] == "_order_wizard"
                             for g in ACTION_GROUPS for a in g[2]))

    def test_02_17_columns_from_single_source(self):
        self.assertEqual(of.headers(), EXPECTED_HEADERS)
        self.assertEqual(len(of.HEAD_FIELDS), 8)
        self.assertEqual(len(of.ITEM_FIELDS), 9)
        self.assertEqual(of.ORDER_KEYS, [f["key"] for f in of.ORDER_FIELDS])
        self.assertEqual(excelio.TEMPLATES["order"]["headers"], EXPECTED_HEADERS)
        self.assertEqual(len(of.sample_rows()[0]), 17)

    def test_03_template_csv_round_trip_groups_two_orders(self):
        p = self.path("模板.csv")
        excelio.export_template(p, "order")
        rep = excelio.parse_order_file(p)
        self.assertEqual(rep["raw_count"], 3)
        self.assertEqual(rep["group_count"], 2)
        self.assertEqual(rep["item_count"], 3)
        self.assertEqual([len(g["items"]) for g in rep["groups"]], [2, 1])
        self.assertEqual(rep["groups"][0]["key"], "CG20261106000001")
        self.assertEqual(len(rep["matched"]), 17)
        self.assertEqual(rep["groups"][0]["head"]["amount_total"], 155100.0)
        # 模板示例：第一张订单 2 项物资 → 物资数量 = 2（Q12 项数口径）
        self.assertEqual(rep["groups"][0]["head"]["item_qty_total"], 2.0)

    def test_04_template_xlsx_round_trip(self):
        p = self.path("模板.xlsx")
        excelio.export_template(p, "order")
        if not Path(p).is_file():
            self.skipTest("openpyxl 未安装，导出降级为 CSV")
        rep = excelio.parse_order_file(p)
        self.assertEqual((rep["group_count"], rep["item_count"]), (2, 3))
        self.assertEqual(rep["groups"][0]["head"]["status"], "已确认")

    def test_05_aliases_semicolon_and_gbk_encoding(self):
        headers = ["订单编号", "需方单位", "类型", "状态", "价税合计",
                   "建议交货日期", "物资数量", "ERP订单编号",
                   "产品代码", "产品名称", "实际生产名称", "规格型号",
                   "计量单位", "下单数量", "含税单价", "金额", "参数",
                   "业务员", "下单日期"]
        p = self.write_rows(
            "gbk.csv",
            [["CG-ALIAS-001", "用户甲", "销售订单", "已验收", "33066",
              "2026/12/20", "2", "4507723757",
              "6001238991", "耐磨弯头", "堆焊耐磨弯头", "DN250", "件",
              "30", "1,850", "55500", "R=1.5D", "张哥", "2026-11-06"]],
            headers=headers, encoding="gb18030", delimiter=";")
        rep = excelio.parse_order_file(p)
        self.assertEqual((rep["group_count"], rep["item_count"]), (1, 1))
        head, item = rep["groups"][0]["head"], rep["groups"][0]["items"][0]
        self.assertEqual(head["project_no"], "CG-ALIAS-001")
        self.assertEqual(head["required_date"], "2026-12-20")
        self.assertEqual(head["status"], "已验收")
        self.assertEqual(item["qty_ordered"], 30.0)
        self.assertEqual(item["unit_price"], 1850.0)
        self.assertEqual(item["item_name"], "耐磨弯头")
        self.assertEqual(item["production_name"], "堆焊耐磨弯头")
        self.assertEqual(item["amount"], 55500.0)

    def test_06_bad_rows_are_reported_not_silently_dropped(self):
        rows = [
            self.row(project_no="A", item_name="正常", qty_ordered="3"),
            self.row(project_no="A", item_name="", qty_ordered="2"),
            self.row(project_no="A", item_name="零量", qty_ordered="0"),
            self.row(project_no="A", item_name="脏数量", qty_ordered="abc"),
            self.row(project_no="B", order_type="内部订单", item_name="缺中标"),
            self.row(project_no="C", order_type="外部订单",
                     source_bid_no=self.bp["doc_no"], item_name="混来源"),
            self.row(project_no="D", order_type="随便填", item_name="乱类型"),
        ]
        rep = excelio.parse_order_file(self.write_rows("坏行.csv", rows))
        self.assertEqual(rep["group_count"], 1)
        self.assertEqual(rep["item_count"], 1)
        self.assertEqual(sum(1 for i in rep["issues"] if i["fatal"]), 6)
        self.assertIn("虚值", rep["summary"] + " ".join(
            i["reason"] for i in rep["issues"]))

    def test_07_blank_project_no_creates_one_order_per_row(self):
        rows = [self.row(item_name="甲"), self.row(item_name="乙")]
        rep = excelio.parse_order_file(self.write_rows("无编号.csv", rows))
        self.assertEqual(rep["group_count"], 2)
        self.assertEqual(rep["item_count"], 2)

    def test_08_missing_required_headers_are_fatal(self):
        p = self.write_rows("缺列.csv",
                            [["A", "外部订单", "耐磨弯头"]],
                            headers=["订单分组", "订单类型", "物资名称"])
        rep = excelio.parse_order_file(p)
        self.assertEqual(rep["group_count"], 0)
        self.assertTrue(any(i["row"] == 1 and i["fatal"]
                            for i in rep["issues"]))

    def test_09_conflicting_head_in_same_group_warns(self):
        rows = [self.row(project_no="A", customer="甲", item_name="弯头"),
                self.row(project_no="A", customer="乙", item_name="法兰")]
        rep = excelio.parse_order_file(self.write_rows("头冲突.csv", rows))
        self.assertEqual(rep["group_count"], 1)
        self.assertEqual(rep["groups"][0]["head"]["customer"], "甲")
        self.assertTrue(any(not i["fatal"] for i in rep["issues"]))

    def test_10_derived_amounts_preview_uses_effective_values(self):
        rows = [self.row(project_no="A", item_name="弯头", qty_ordered="3",
                         unit_price="100", amount="", amount_total="",
                         item_qty_total=""),
                self.row(project_no="A", item_name="法兰", qty_ordered="2",
                         unit_price="50", amount="", amount_total="",
                         item_qty_total="")]
        rep = excelio.parse_order_file(self.write_rows("派生.csv", rows))
        g = rep["groups"][0]
        self.assertEqual(g["head"]["amount_total"], "")
        preview = of.preview_rows([g])
        self.assertEqual(preview[0][4], 400.0)     # 价税合计 = 300 + 100
        self.assertEqual(preview[0][6], 2.0)       # 物资数量 = 明细项数 2（Q12），不是 3+2=5
        self.assertEqual(preview[0][15], 300.0)   # 行总价

    def test_11_excel_datetime_and_unknown_status_are_normalized(self):
        from datetime import datetime
        rows = [self.row(project_no="A", status="随便的状态",
                        required_date=datetime(2026, 12, 20))]
        rep = excelio.parse_order_file(self.write_rows("日期状态.csv", rows))
        head = rep["groups"][0]["head"]
        self.assertEqual(head["required_date"], "2026-12-20")
        self.assertEqual(head["status"], "草稿")
        self.assertTrue(any("订单状态" in i["reason"] for i in rep["issues"]))

    @unittest.skipUnless(REAL_WORKBOOK.is_file(), "未提供真实销售订单汇总工作簿")
    def test_12_real_sales_workbook_parses_without_fatal_rows(self):
        rep = excelio.parse_order_file(str(REAL_WORKBOOK))
        self.assertEqual(rep["group_count"], 306)   # 306 个订单编号
        self.assertEqual(rep["item_count"], 734)    # 734 行明细
        self.assertEqual(sum(1 for i in rep["issues"] if i["fatal"]), 0)
        head = rep["groups"][0]["head"]
        self.assertEqual(head["project_no"], "CG20240701000120")
        self.assertEqual(head["status"], "已验收")
        self.assertEqual(head["erp_order_no"], "4507723757")
        item = rep["groups"][0]["items"][0]
        self.assertEqual(item["item_code"], "6001238991")
        self.assertEqual(item["amount"], 33066.0)   # 行「价税合计」→ 总价


class OrderServiceTest(OrderFixture):

    def _external_spec(self, **kw):
        spec = {
            "project_no": "CG-TEST-001", "customer": "用户甲",
            "order_type": "外部订单", "status": "已确认",
            "required_date": "2026-12-20", "erp_order_no": "4507723757",
            "order_date": "2026-11-06", "owner": "张哥",
            "source_code_no": self.cp["doc_no"],
            "items": [{"item_code": "6001", "item_name": "耐磨弯头",
                       "production_name": "堆焊耐磨弯头", "spec": "DN250",
                       "unit": "件", "qty_ordered": 30, "unit_price": 1850,
                       "amount": None, "param_desc": "R=1.5D",
                       "source_code_item_no": self.ci1["item_code"]}],
        }
        spec.update(kw)
        return spec

    def test_13_new_external_order_writes_all_v23_fields(self):
        o = self.orders.create_from_spec(self._external_spec(), "张哥")
        self.assertTrue(o["doc_no"].startswith("DD-"))
        self.assertEqual(o["status"], "已确认")
        self.assertEqual(o["project_no"], "CG-TEST-001")
        self.assertEqual(o["customer"], "用户甲")
        self.assertEqual(o["erp_order_no"], "4507723757")
        self.assertEqual(o["amount_total"], 55500.0)
        self.assertEqual(o["item_qty_total"], 1.0)   # Q12：1 项物资，不是数量 30
        items = self.orders.list_items(o["id"])
        self.assertEqual(items[0]["qty_ordered"], 30.0)
        self.assertEqual(items[0]["amount"], 55500.0)
        self.assertEqual(items[0]["production_name"], "堆焊耐磨弯头")
        self.assertEqual(items[0]["source_code_item_id"], self.ci1["id"])
        self.assertEqual(self.orders.summary(o["id"])["qty_ordered_real"], 30.0)
        self.assertTrue(check(self.db)["ok"])

    def test_14_declared_totals_are_preserved(self):
        o = self.orders.create_from_spec(self._external_spec(
            amount_total=999999, item_qty_total=7), "张哥")
        self.assertEqual(o["amount_total"], 999999.0)
        self.assertEqual(o["item_qty_total"], 7.0)
        self.assertEqual(self.orders.list_items(o["id"])[0]["qty_ordered"], 30.0)
        self.assertTrue(check(self.db)["ok"])

    def test_15_manual_order_without_source_is_allowed(self):
        o = self.orders.create_from_spec({
            "order_type": "外部订单", "customer": "散户",
            "items": [{"item_name": "带颈法兰", "qty_ordered": 50}]}, "张哥")
        self.assertIsNone(o["source_code_project_id"])
        self.assertEqual(o["status"], "草稿")
        self.assertIsNone(self.orders.list_items(o["id"])[0]["source_code_item_id"])

    def test_16_one_code_project_can_produce_multiple_orders(self):
        o1 = self.orders.create_from_spec(self._external_spec(), "张哥")
        o2 = self.orders.create_from_spec(self._external_spec(
            project_no="CG-TEST-002",
            items=[{"item_name": "补偿器", "qty_ordered": 5}]), "张哥")
        self.assertEqual((o1["batch_seq"], o2["batch_seq"]), (1, 2))
        self.assertEqual(len(self.codes.order_batches(self.cp["id"])), 2)

    def test_17_internal_order_is_confirmed_and_links_bid_item(self):
        o = self.orders.create_from_spec({
            "order_type": "内部订单", "source_bid_no": self.bp["doc_no"],
            "items": [{"item_name": "耐磨弯头", "qty_ordered": 12,
                       "source_bid_line_no": "1"}]}, "张哥")
        self.assertTrue(o["doc_no"].startswith("ND-"))
        self.assertEqual(o["status"], "已确认")
        self.assertEqual(self.orders.list_items(o["id"])[0]["source_bid_item_id"],
                         self.bi["id"])
        self.assertTrue(check(self.db)["ok"])

    def test_18_one_internal_order_per_won_bid(self):
        spec = {"order_type": "内部订单", "source_bid_no": self.bp["doc_no"],
                "items": [{"item_name": "耐磨弯头", "qty_ordered": 12}]}
        self.orders.create_from_spec(spec, "张哥")
        with self.assertRaises(DuplicateError):
            self.orders.create_from_spec(spec, "张哥")

    def test_19_unknown_source_numbers_are_rejected(self):
        with self.assertRaises(NotFoundError):
            self.orders.create_from_spec(
                self._external_spec(source_code_no="XPM-NOT-EXIST"), "张哥")
        bad = dict(self._external_spec()["items"][0])
        bad["source_code_item_no"] = "XPM-FAKE-01"
        with self.assertRaises(NotFoundError):
            self.orders.create_from_spec(
                self._external_spec(items=[bad]), "张哥")

    def test_20_external_and_internal_sources_are_mutually_exclusive(self):
        with self.assertRaises(ValidationError):
            self.orders.create_from_spec(
                self._external_spec(source_bid_no=self.bp["doc_no"]), "张哥")
        with self.assertRaises(ValidationError):
            self.orders.create_from_spec({
                "order_type": "内部订单", "source_bid_no": self.bp["doc_no"],
                "source_code_no": self.cp["doc_no"],
                "items": [{"item_name": "弯头", "qty_ordered": 1}]}, "张哥")
        with self.assertRaises(ValidationError):
            self.orders.create_from_spec({
                "order_type": "内部订单",
                "items": [{"item_name": "弯头", "qty_ordered": 1}]}, "张哥")
        with self.assertRaises(ValidationError):
            self.orders.create_from_spec({"order_type": "外部订单",
                                          "items": []}, "张哥")
        with self.assertRaises(ValidationError):
            self.orders.create_from_spec(
                self._external_spec(status="不存在的状态"), "张哥")

    def test_21_import_orders_batch_writes_audit_and_keeps_integrity(self):
        rows = [
            self.row(project_no="A", source_code_no=self.cp["doc_no"],
                     source_code_item_no=self.ci1["item_code"],
                     item_name="耐磨弯头", qty_ordered="30"),
            self.row(project_no="A", source_code_no=self.cp["doc_no"],
                     item_name="补偿器", qty_ordered="20"),
            self.row(project_no="B", item_name="带颈法兰", qty_ordered="50"),
        ]
        rep = excelio.parse_order_file(self.write_rows("批量.csv", rows))
        res = self.orders.import_orders(rep["groups"], "张哥")
        self.assertEqual((res["order_count"], res["item_count"]), (2, 3))
        self.assertEqual(res["qty_ordered_real"], 100.0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 2)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'import_orders'"), 1)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'import_order'"), 2)
        self.assertTrue(check(self.db)["ok"])
        self.assertEqual(self.db.get_by_id(
            "code_items", self.ci1["id"])["qty_reference"], 100)

    def test_22_import_rolls_back_the_whole_batch(self):
        rows = [
            self.row(project_no="A", item_name="正常订单", qty_ordered="3"),
            self.row(project_no="B", source_code_no=self.cp["doc_no"],
                     source_code_item_no="XPM-FAKE-99",
                     item_name="来源项不存在", qty_ordered="2"),
        ]
        rep = excelio.parse_order_file(self.write_rows("回滚.csv", rows))
        self.assertEqual(rep["group_count"], 2)
        with self.assertRaises(NotFoundError):
            self.orders.import_orders(rep["groups"], "张哥")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM order_items"), 0)
        self.assertTrue(check(self.db)["ok"])

    def test_23_production_still_anchors_on_imported_order_qty(self):
        rows = [self.row(project_no="A", status="已确认",
                         item_name="耐磨弯头", qty_ordered="9")]
        rep = excelio.parse_order_file(self.write_rows("排产.csv", rows))
        res = self.orders.import_orders(rep["groups"], "张哥")
        iid = self.orders.list_items(res["orders"][0]["id"])[0]["id"]
        proj = self.prod.create_project("导入订单排产回归")
        self.prod.link_order_item(proj["id"], iid, allocated_qty=4)
        self.assertEqual(self.orders.available_qty(iid), 5.0)
        self.db.update("code_items", {"qty_reference": 9999}, "id = ?",
                       (self.ci1["id"],))
        self.assertEqual(self.orders.available_qty(iid), 5.0)
        self.assertTrue(check(self.db)["ok"])

    @unittest.skipUnless(REAL_WORKBOOK.is_file(), "未提供真实销售订单汇总工作簿")
    def test_24_real_sales_workbook_import_keeps_conservation(self):
        rep = excelio.parse_order_file(str(REAL_WORKBOOK))
        res = self.orders.import_orders(rep["groups"][:8], "张哥")
        self.assertEqual(res["order_count"], 8)
        self.assertGreater(res["qty_ordered_real"], 0)
        self.assertGreater(res["amount_total"], 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 8)
        # Q12：真实工作簿无「物资数量」列，导入后应等于该单的明细行数
        first = res["orders"][0]
        self.assertEqual(first["item_qty_total"], float(self.db.scalar(
            "SELECT COUNT(*) FROM order_items WHERE order_id = ?",
            (first["id"],))))
        self.assertTrue(check(self.db)["ok"])

    def test_28_q12_item_qty_total_is_item_count_not_qty_sum(self):
        """Q12（2026-11-07 张哥拍板）：物资数量 = 物资项目数（明细行数）"""
        spec = {
            "order_type": "外部订单", "customer": "用户甲", "status": "已确认",
            "items": [
                {"item_name": "耐磨弯头", "qty_ordered": 30, "unit_price": 10},
                {"item_name": "波纹补偿器", "qty_ordered": 20, "unit_price": 10},
                {"item_name": "带颈法兰", "qty_ordered": 50, "unit_price": 10},
            ],
        }
        o = self.orders.create_from_spec(spec, "张哥")
        self.assertEqual(o["item_qty_total"], 3.0)       # 3 项，不是 Σ = 100
        s = self.orders.summary(o["id"])
        self.assertEqual(s["qty_ordered_real"], 100.0)   # Σ 明细数量仍走实值口径
        self.assertEqual(s["item_qty_total"], 3.0)

        # 改明细「数量」不应改变物资数量（项数口径）
        from v2.domains.maintenance import MaintenanceService
        first = self.orders.list_items(o["id"])[0]
        MaintenanceService(self.db).edit(
            "order_item", first["id"], {"qty_ordered": 999}, "张哥")
        self.assertEqual(float(self.db.scalar(
            "SELECT item_qty_total FROM orders WHERE id = ?", (o["id"],))), 3.0)
        self.assertEqual(self.orders.summary(o["id"])["qty_ordered_real"], 1069.0)
        self.assertTrue(check(self.db)["ok"])


@unittest.skipUnless(PYSIDE, "需要 PySide6")
class OrderUiTest(OrderFixture):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        from PySide6.QtGui import QFontDatabase
        theme.apply_theme(cls.app)
        for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
            if Path(p).is_file():
                QFontDatabase.addApplicationFont(p)

    def _window(self):
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        w.show()
        self.app.processEvents()
        self.addCleanup(w.close)
        return w

    def test_25_new_order_dialog_is_themed_and_has_9_columns(self):
        from v2.ui import dialogs
        w = self._window()
        d = dialogs.NewOrderDialog(w, self.db)
        self.addCleanup(d.close)
        self.assertEqual(d.objectName(), "ThemedDialog")
        self.assertEqual(d.header.objectName(), "DialogHeader")
        self.assertEqual(d.table.objectName(), "ResultTable")
        self.assertEqual(d.table.columnCount(), 9)
        self.assertEqual(d.table.rowCount(), 3)
        self.assertEqual(len(d._collect_items()[0]), 0)   # 空白行不误判
        d.table.item(0, 1).setText("耐磨弯头")          # 物资名称
        d.table.item(0, 5).setText("7.5")                # 数量
        d.table.item(0, 6).setText("1850")               # 单价
        items, errors = d._collect_items()
        self.assertEqual((len(items), len(errors)), (1, 0))
        self.assertEqual(items[0]["qty_ordered"], 7.5)
        self.assertEqual(items[0]["unit"], "件")
        self.assertIsNone(items[0]["amount"])            # 总价留空自动算
        self.assertEqual(d.result_spec()["status"], "已确认")
        box = d.findChild(QDialogButtonBox)
        self.assertTrue(all(b.text() not in ("OK", "Cancel")
                            for b in box.buttons()))

    def test_26_real_new_order_action_writes_db(self):
        w = self._window()
        before = self.db.scalar("SELECT COUNT(*) FROM orders")

        def _fill_and_click():
            m = QApplication.activeModalWidget()
            if m is None or not hasattr(m, "table"):
                return
            m.table.item(0, 1).setText("耐磨弯头")
            m.table.item(0, 5).setText("7.5")
            box = m.findChild(QDialogButtonBox)
            for b in box.buttons():
                if b.objectName() == "Primary":
                    b.click()
                    return

        QTimer.singleShot(80, _fill_and_click)
        msg = w._new_order()
        self.assertIn("新建 DD-", msg)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), before + 1)
        self.assertTrue(check(self.db)["ok"])

    def test_27_import_orders_dialog_previews_17_columns_and_writes_db(self):
        from v2.ui import dialogs
        w = self._window()
        rows = [
            self.row(project_no="A", item_name="耐磨弯头", qty_ordered="30"),
            self.row(project_no="A", item_name="补偿器", qty_ordered="20"),
            self.row(project_no="B", item_name="空名称", qty_ordered="0"),
        ]
        p = self.write_rows("向导.csv", rows)
        d = dialogs.ImportOrdersDialog(w, self.db)
        self.addCleanup(d.close)
        self.assertEqual(d.objectName(), "ThemedDialog")
        d._load(p)
        self.assertEqual(d.table.columnCount(), 17)
        self.assertEqual(d.table.rowCount(), 2)
        self.assertEqual(d.report["group_count"], 1)
        self.assertIn("跳过 1", d.summary.text())

        def _click_notify():
            m = QApplication.activeModalWidget()
            if isinstance(m, QMessageBox):
                for b in m.buttons():
                    if b.objectName() == "Primary":
                        b.click()
                        return
                m.accept()

        QTimer.singleShot(60, _click_notify)
        d._do_import()
        self.assertEqual(d.result["order_count"], 1)
        self.assertEqual(d.result["item_count"], 2)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM orders"), 1)
        self.assertTrue(check(self.db)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

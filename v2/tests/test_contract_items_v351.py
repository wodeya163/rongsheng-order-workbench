#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.5.1 回归：合同明细表自动解析 + 物资自动分配建议。

锁定的硬口径（不许回退）：
  1. 解析走【结构化表格】，不复用 contract_amount._iter_lines（.doc 的 OLE
     扫描永远读不到表格明细）；
  2. 名称全称对不上时靠类型词 / 规格数字 / 数量打分，低分不猜；
  3. 建议【只预填、不写库】，写库必须经用户点「保存分配」；
  4. 无单价不填 0；数量守恒截断；Σ 明细与台账金额对账只告警不阻断；
  5. 一份合同对同一订单明细只保留一行，多行命中合并数量 + 加权单价。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                        # noqa: E402
from v2.core.contract_items import (                                # noqa: E402
    ContractLine, ParseResult, build_suggestions, read_contract_items,
)
from v2.core.db import Database                                    # noqa: E402
from v2.domains import OrderService                                 # noqa: E402
from v2.domains.contract import PurchaseContractService             # noqa: E402
from v2.domains.profit import OrderProfitService                    # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QPushButton
    PYSIDE = True
except ImportError:                                              # pragma: no cover
    PYSIDE = False

_APP = None

HEADER = ["序号", "商品编码", "物资名称", "规格", "材质", "单位",
          "数量", "单价", "总价（元）", "备注"]
# 与真实合同 20261107KYCN-B-027 一致的明细（7,104 + 7,380 = 14,484）
ROWS_027 = [
    ["1", "1381001564", "双金属复合弯头", "内径254mm，偏心5mm", "BTMCr15-G",
     "件", "2", "3552", "7104", ""],
    ["2", "2541001565", "双金属复合三通", "内径254mm，偏心5mm", "BTMCr15-G",
     "件", "2", "3690", "7380", ""],
]


def _app():
    global _APP
    if _APP is None:
        from v2.ui import theme
        _APP = QApplication.instance() or QApplication([])
        theme.apply_theme(_APP)
    return _APP


def _cand(iid, name, qty, spec="", unit="件", remaining=None):
    return {"id": iid, "item_name": name, "spec": spec, "unit": unit,
            "qty_ordered": qty,
            "remaining_qty": qty if remaining is None else remaining}


def _lines(rows):
    return [ContractLine(name=r[0], spec=r[1], unit="件", qty=r[2],
                          unit_price=r[3], amount=r[4], row_no=i + 2)
             for i, r in enumerate(rows)]


def _parse(rows):
    return ParseResult(lines=_lines(rows), source="测试", table_index=1)


def write_docx(path: Path, rows, with_total=True):
    from docx import Document
    d = Document()
    t = d.add_table(rows=1, cols=len(HEADER))
    for i, h in enumerate(HEADER):
        t.rows[0].cells[i].text = h
    for r in rows:
        cells = t.add_row().cells
        for i, v in enumerate(r):
            cells[i].text = str(v)
    if with_total:
        cells = t.add_row().cells
        for i, v in enumerate(["", "总计：", "人民币：壹万肆仟肆佰捌拾肆圆整",
                               "14484", "", "", "", "", "", ""]):
            cells[i].text = str(v)
    d.save(str(path))
    return path


def write_xlsx(path: Path, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(list(r))
    wb.save(str(path))
    return path


class ParseTest(unittest.TestCase):
    """结构化表格解析（纯 Python 通道，不依赖 Office）"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v351parse_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_version(self):
        self.assertEqual(__version__, "3.5.4")

    def test_02_docx_two_rows_and_total_row_skipped(self):
        p = write_docx(self.tmp / "c.docx", ROWS_027)
        r = read_contract_items(p)
        self.assertTrue(r.ok)
        self.assertEqual(r.table_index, 1)
        self.assertEqual(len(r.lines), 2, "总计行必须被跳过")
        self.assertEqual(r.lines[0].name, "双金属复合弯头")
        self.assertEqual(r.lines[0].qty, 2.0)
        self.assertEqual(r.lines[0].unit_price, 3552.0)
        self.assertEqual(r.lines[0].amount, 7104.0)
        self.assertEqual(r.lines[0].code, "1381001564")
        self.assertEqual(r.total, 14484.0)

    def test_03_xlsx_channel(self):
        p = write_xlsx(self.tmp / "c.xlsx", ROWS_027)
        r = read_contract_items(p)
        self.assertTrue(r.ok)
        self.assertEqual(r.total, 14484.0)
        self.assertIn("openpyxl", r.source)

    def test_04_csv_channel(self):
        p = self.tmp / "c.csv"
        p.write_text("物资名称,规格,单位,数量,单价,总价\n"
                     "双金属复合弯头,内径254mm,件,2,2846,5692\n",
                     encoding="utf-8")
        r = read_contract_items(p)
        self.assertTrue(r.ok)
        self.assertEqual(r.total, 5692.0)
        self.assertEqual(r.lines[0].unit_price, 2846.0)

    def test_05_unit_price_derived_from_amount(self):
        # 列数必须与 HEADER 一致：单价列给 0，只给总价，验证「单价 = 总价 / 数量」派生
        rows = [["1", "", "双金属复合弯头", "内径254mm", "BTMCr15-G",
                 "件", "2", "0", "5692", ""]]
        p = write_docx(self.tmp / "no_price.docx", rows)
        r = read_contract_items(p)
        self.assertTrue(r.ok)
        self.assertEqual(r.lines[0].unit_price, 2846.0)

    def test_06_no_table_is_diagnosed_not_crash(self):
        from docx import Document
        p = self.tmp / "plain.docx"
        Document().save(str(p))
        r = read_contract_items(p)
        self.assertFalse(r.ok)
        self.assertTrue(any("明细表" in w for w in r.warnings))

    def test_07_missing_file_and_bad_ext(self):
        a = read_contract_items(self.tmp / "nope.doc")
        self.assertFalse(a.ok)
        self.assertTrue(any("不存在" in w for w in a.warnings))
        b = self.tmp / "x.exe"
        b.write_bytes(b"x")
        c = read_contract_items(b)
        self.assertFalse(c.ok)
        self.assertTrue(any("不支持" in w for w in c.warnings))


class MatchTest(unittest.TestCase):
    """名称全称对不上时的匹配口径"""

    def test_08_real_case_027_name_mismatch_still_matches(self):
        cands = [
            _cand(735, "五段弧三通\\LKT08-KJ17.4.2\\飞灰输送系统\\LQZ", 2),
            _cand(736, "七段合金衬弯头\\LKT09-KJ12.4.2\\飞灰输送系统\\LQZ", 2),
        ]
        s = build_suggestions(_parse([
            ("双金属复合弯头", "内径254mm，偏心5mm", 2, 3552, 7104),
            ("双金属复合三通", "内径254mm，偏心5mm", 2, 3690, 7380),
        ]), cands, 14484.0, "20261107KYCN-B-027")
        self.assertTrue(s["ok"])
        self.assertEqual(len(s["suggestions"]), 2)
        self.assertEqual(s["unmatched"], [])
        self.assertEqual(s["gap"], 0.0)
        by = {x["order_item_id"]: x for x in s["suggestions"]}
        self.assertEqual(by[736]["unit_price"], 3552.0)   # 弯头 → 弯头
        self.assertEqual(by[735]["unit_price"], 3690.0)   # 三通 → 三通
        self.assertTrue(all(x["confidence"] == "高" for x in by.values()))
        self.assertEqual(len(s["specs"]), 2)

    def test_09_real_case_028_unique_candidate_high_confidence(self):
        cands = [_cand(737, "五段粉料弯头\\LKF02-KJ53.1.15\\飞灰输送系统\\LQZ",
                       2, spec="DN254")]
        s = build_suggestions(_parse([
            ("双金属复合弯头", "内径254mm，偏心5mm", 2, 2846, 5692),
        ]), cands, 5692.0, "20261110KYCN-B-028")
        self.assertEqual(len(s["suggestions"]), 1)
        self.assertGreaterEqual(s["suggestions"][0]["score"], 80)
        self.assertEqual(s["suggestions"][0]["confidence"], "高")
        self.assertEqual(s["gap"], 0.0)

    def test_10_type_conflict_not_misassigned(self):
        cands = [_cand(1, "五段弧三通\\LKT08", 2)]
        s = build_suggestions(_parse([
            ("双金属复合弯头", "内径254mm", 2, 3552, 7104),
        ]), cands, 7104.0)
        self.assertFalse(s["ok"])
        self.assertEqual(s["suggestions"], [])
        self.assertEqual(len(s["unmatched"]), 1)

    def test_11_no_unit_price_never_autochecked(self):
        cands = [_cand(1, "耐磨弯头", 2)]
        s = build_suggestions(_parse([
            ("耐磨弯头", "", 2, 0, 0),
        ]), cands, 0.0)
        self.assertFalse(s["ok"])
        self.assertEqual(len(s["unmatched"]), 1)
        self.assertIn("单价", s["unmatched"][0]["reason"])

    def test_12_qty_over_remaining_is_truncated_with_warning(self):
        cands = [_cand(1, "耐磨弯头", 3, remaining=1)]
        s = build_suggestions(_parse([
            ("耐磨弯头", "", 3, 1000, 3000),
        ]), cands, 3000.0)
        self.assertEqual(s["suggestions"][0]["qty"], 1.0)
        self.assertEqual(s["suggestions"][0]["amount"], 1000.0)
        self.assertTrue(any("超出剩余" in w for w in s["warnings"]))

    def test_13_two_lines_same_item_merged_weighted_price(self):
        cands = [_cand(1, "耐磨弯头", 4)]
        s = build_suggestions(_parse([
            ("耐磨弯头", "", 2, 1000, 2000),
            ("耐磨弯头", "", 2, 3000, 6000),
        ]), cands, 8000.0)
        self.assertEqual(len(s["specs"]), 1, "同一明细只能有一行分配")
        self.assertEqual(s["specs"][0]["qty"], 4.0)
        self.assertEqual(s["specs"][0]["unit_price"], 2000.0)  # 8000/4
        self.assertEqual(s["suggestions"][0]["merged"], 1)

    def test_14_amount_gap_only_warns(self):
        cands = [_cand(1, "耐磨弯头", 2)]
        s = build_suggestions(_parse([
            ("耐磨弯头", "", 2, 1000, 2000),
        ]), cands, 2500.0)
        self.assertEqual(s["gap"], -500.0)
        self.assertTrue(s["ok"])
        self.assertTrue(any("相差" in w for w in s["warnings"]))

    def test_15_no_candidates_reports(self):
        s = build_suggestions(_parse([
            ("耐磨弯头", "", 2, 1000, 2000),
        ]), [], 2000.0)
        self.assertFalse(s["ok"])
        self.assertTrue(any("没有可分配" in w for w in s["warnings"]))


class ServiceTest(unittest.TestCase):
    """服务层：只读建议、绝不写库"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v351svc_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.profit = OrderProfitService(self.db)
        self.order = self.orders.create_order(customer="国能浙江北仑第一发电有限公司",
                                              project_no="CG20261103000221")
        self.i1 = self.orders.add_item(
            self.order["id"], "五段弧三通\\LKT08-KJ17.4.2\\飞灰输送系统\\LQZ",
            2, unit_price=10209)
        self.i2 = self.orders.add_item(
            self.order["id"], "七段合金衬弯头\\LKT09-KJ12.4.2\\飞灰输送系统\\LQZ",
            2, unit_price=10406)
        self.c = self.svc.create({
            "contract_name": "20261107KYCN-B-027委托加工合同", "supplier": "河北久通",
            "order_id": self.order["id"], "amount": 14484,
            "status": "已签订", "tax_included": 1}, "测试")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archive(self, rows=ROWS_027):
        p = write_docx(self.tmp / "合同正文.docx", rows)
        self.svc.import_files(int(self.c["id"]), [str(p)], "测试")
        return p

    def test_16_suggest_from_archived_doc_no_db_write(self):
        self._archive()
        res = self.svc.suggest_items_from_file(int(self.c["id"]))
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["suggestions"]), 2)
        self.assertEqual(res["gap"], 0.0)
        self.assertEqual(self.svc.contract_items(int(self.c["id"])), [],
                         "自动匹配绝不能写库")
        self.assertEqual(
            self.profit.order_summary(self.order["id"])["purchase_total"], 0.0)

    def test_17_no_attachment_reports_cleanly(self):
        res = self.svc.suggest_items_from_file(int(self.c["id"]))
        self.assertFalse(res["ok"])
        self.assertEqual(res["specs"], [])
        self.assertTrue(res["warnings"])

    def test_18_specs_are_applicable_and_rollup_correct(self):
        self._archive()
        res = self.svc.suggest_items_from_file(int(self.c["id"]))
        self.svc.set_contract_items(int(self.c["id"]), res["specs"], "测试")
        s = self.profit.order_summary(self.order["id"])
        self.assertEqual(s["purchase_total"], 14484.0)
        self.assertTrue(s["complete"])
        self.assertEqual(s["supplier_count"], 1)

    def test_19_other_order_is_excluded_from_suggestions(self):
        self._archive()
        other = self.orders.create_order(customer="另一客户", project_no="SO-OTHER")
        other_item = self.orders.add_item(other["id"], "波纹补偿器", 2,
                                          unit_price=1000)
        res = self.svc.suggest_items_from_file(int(self.c["id"]),
                                               int(other["id"]))
        self.assertFalse(any(x["order_item_id"] == int(other_item["id"])
                             for x in res["suggestions"]))


@unittest.skipUnless(PYSIDE, "PySide6 不可用")
class DialogTest(unittest.TestCase):
    """分配页：按钮预填 → 用户确认 → 才写库"""

    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="v351ui_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.profit = OrderProfitService(self.db)
        self.order = self.orders.create_order(customer="国能浙江北仑第一发电有限公司",
                                              project_no="CG20261103000221")
        self.i1 = self.orders.add_item(
            self.order["id"], "五段弧三通\\LKT08-KJ17.4.2\\飞灰输送系统\\LQZ",
            2, unit_price=10209)
        self.i2 = self.orders.add_item(
            self.order["id"], "七段合金衬弯头\\LKT09-KJ12.4.2\\飞灰输送系统\\LQZ",
            2, unit_price=10406)
        self.c = self.svc.create({
            "contract_name": "20261107KYCN-B-027委托加工合同", "supplier": "河北久通",
            "order_id": self.order["id"], "amount": 14484,
            "status": "已签订", "tax_included": 1}, "测试")
        p = write_docx(self.tmp / "合同正文.docx", ROWS_027)
        self.svc.import_files(int(self.c["id"]), [str(p)], "测试")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_20_button_exists(self):
        from v2.ui.contract_items_dialog import ContractItemsDialog
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        d = ContractItemsDialog(w, self.svc, int(self.c["id"]))
        self.assertTrue(any(b.text() == "从合同文档自动匹配"
                            for b in d.findChildren(QPushButton)))
        d.close()
        w.close()

    def test_21_autofill_then_save(self):
        from PySide6.QtCore import Qt
        from v2.ui.contract_items_dialog import ContractItemsDialog
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        d = ContractItemsDialog(w, self.svc, int(self.c["id"]))
        self.assertEqual(d.table.rowCount(), 2)
        with mock.patch("v2.ui.contract_items_dialog.theme.notify"):
            d._do_auto_match()
        # 预填后两行都应勾选，且数量 / 单价来自合同文档
        for row in range(2):
            self.assertEqual(d.table.item(row, 0).checkState(), Qt.Checked)
            self.assertEqual(float(d.table.item(row, 7).text()), 2.0)
        prices = sorted(float(d.table.item(r, 8).text()) for r in range(2))
        self.assertEqual(prices, [3552.0, 3690.0])
        self.assertIn("自动匹配", d.hint.text())
        self.assertEqual(self.svc.contract_items(int(self.c["id"])), [],
                         "预填阶段数据库必须零写入")
        with mock.patch("v2.ui.contract_items_dialog.theme.notify"):
            d._do_save()
        rows = self.svc.contract_items(int(self.c["id"]))
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(float(r["amount"]) for r in rows), 14484.0)
        self.assertEqual(
            self.profit.order_summary(self.order["id"])["purchase_total"],
            14484.0)
        d.close()
        w.close()


class RealDocIntegrationTest(unittest.TestCase):
    """真实 .doc 需要本机 Word/WPS；CI 里不存在文件时自动跳过。"""

    REAL = Path(r"D:\Programs\ProductSuite_v2.2\data\采购合同")

    @unittest.skipUnless(REAL.is_dir(), "无真实部署合同目录")
    def test_22_real_027_and_028_via_com(self):
        a = self.REAL / "20261107KYCN-B-027" / \
            "20261107KYCN-B-027委托加工合同-浙江-河北久通.doc"
        b = self.REAL / "20261110KYCN-B-028" / \
            "20261110KYCN-B-028委托加工合同-浙江-河北久通.doc"
        if not (a.is_file() and b.is_file()):
            self.skipTest("真实合同文件不存在")
        ra, rb = read_contract_items(a), read_contract_items(b)
        self.assertTrue(ra.ok and rb.ok)
        self.assertEqual(ra.total, 14484.0)
        self.assertEqual(rb.total, 5692.0)
        self.assertEqual(len(ra.lines), 2)
        self.assertEqual(len(rb.lines), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

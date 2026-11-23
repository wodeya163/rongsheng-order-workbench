#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.5.2 回归：导入采购合同后「自动应用物资分配」。

锁定的硬口径（不许回退）：
  1. 开关落 data/contract_prefs.json，默认开启，升级不重置，两个导入入口共用；
  2. 自动应用只在【全部行高置信 + 零未匹配 + 明细合计 = 台账金额 = 实际写入金额
     + 数量守恒 + 已签订 + 关联订单 + 无手填单价 / 无跨订单分配】时写库；
  3. 任一条件不满足只返回诊断、零写库，用户仍可在「物资分配」页手工处理；
  4. 已有非零单价的分配行一律不覆盖；0 单价占位行允许替换并记 replaced；
  5. 自动应用绝不改合同台账金额（金额仍只认合同文档，v3.4.6 口径）；
  6. schema 仍 v7，零 DDL 改动，data/ 原位保留。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                        # noqa: E402
from v2.core import contract_prefs                                 # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database            # noqa: E402
from v2.domains import OrderService                                # noqa: E402
from v2.domains.contract import (PurchaseContractService,          # noqa: E402
                                 auto_apply_summary)
from v2.domains.profit import OrderProfitService                   # noqa: E402

HEADER = ["序号", "商品编码", "物资名称", "规格", "材质", "单位",
          "数量", "单价", "总价（元）", "备注"]
DOC_NO = "20261107KYCN-B-027"
# 与真实合同一致：7,104 + 7,380 = 14,484
ROWS = [
    ["1", "1381001564", "双金属复合弯头", "内径254mm，偏心5mm", "BTMCr15-G",
     "件", "2", "3552", "7104", ""],
    ["2", "2541001565", "双金属复合三通", "内径254mm，偏心5mm", "BTMCr15-G",
     "件", "2", "3690", "7380", ""],
]


def write_docx(path: Path, rows=ROWS, amount_text="14484", with_table=True):
    from docx import Document
    d = Document()
    d.add_paragraph(
        f"合同总金额：{amount_text} 元"
        "（人民币壹万肆仟肆佰捌拾肆圆整）" if amount_text == "14484"
        else f"合同总金额：{amount_text} 元")
    if with_table:
        t = d.add_table(rows=1, cols=len(HEADER))
        for i, h in enumerate(HEADER):
            t.rows[0].cells[i].text = h
        for r in rows:
            cells = t.add_row().cells
            for i, v in enumerate(r):
                cells[i].text = str(v)
        cells = t.add_row().cells
        for i, v in enumerate(["", "总计：", "人民币：壹万肆仟肆佰捌拾肆圆整",
                               "14484", "", "", "", "", "", ""]):
            cells[i].text = str(v)
    d.save(str(path))
    return path


class PrefsTest(unittest.TestCase):
    """开关持久化：默认开启、升级不重置、损坏文件回退、内存库不落盘"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v352prefs_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_version_and_schema_unchanged(self):
        self.assertEqual(__version__, "3.5.4")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7,
                         "自动应用不得引入 DDL 变更")

    def test_02_defaults(self):
        self.assertTrue(contract_prefs.load(None)["auto_apply_items"])
        self.assertTrue(contract_prefs.load(self.tmp)["auto_apply_items"])
        self.assertFalse((self.tmp / contract_prefs.FILENAME).exists(),
                         "只读 load 不该落盘")

    def test_03_save_load_roundtrip(self):
        self.assertTrue(contract_prefs.save(self.tmp, {"auto_apply_items": False}))
        self.assertFalse(contract_prefs.load(self.tmp)["auto_apply_items"])
        self.assertFalse(contract_prefs.is_enabled(self.tmp))
        contract_prefs.save(self.tmp, {"auto_apply_items": True})
        self.assertTrue(contract_prefs.is_enabled(self.tmp))

    def test_04_broken_file_falls_back(self):
        (self.tmp / contract_prefs.FILENAME).write_text("{not json",
                                                        encoding="utf-8")
        self.assertTrue(contract_prefs.load(self.tmp)["auto_apply_items"])

    def test_05_only_bool_accepted(self):
        (self.tmp / contract_prefs.FILENAME).write_text(
            '{"auto_apply_items": "false", "unknown": 1}', encoding="utf-8")
        # 字符串 "false" 按 bool() 为 True；load 只认这个键，不做隐式宽松解析
        self.assertTrue(contract_prefs.load(self.tmp)["auto_apply_items"])


class AutoApplyTest(unittest.TestCase):
    """导入链路的严格条件与写库边界"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v352svc_"))
        self.data = self.tmp / "data"
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.profit = OrderProfitService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.data))
        self.order = self.orders.create_order(
            customer="国能浙江北仑第一发电有限公司", project_no="CG352-001")
        # 名称与合同全称不一致，逼匹配走「类型词 + 规格数字 + 数量」打分
        self.i1 = self.orders.add_item(
            self.order["id"], "五段弧三通\\LKT08-KJ17.4.2\\飞灰输送系统\\LQZ",
            2, unit_price=10209)
        self.i2 = self.orders.add_item(
            self.order["id"], "七段合金衬弯头\\LKT09-KJ12.4.2\\飞灰输送系统\\LQZ",
            2, unit_price=10406)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _import(self, auto_apply=None, rows=ROWS, with_table=True,
                amount_text="14484", name=None):
        fname = name or f"{DOC_NO}委托加工合同.docx"
        p = write_docx(self.tmp / fname, rows=rows, with_table=with_table,
                       amount_text=amount_text)
        return self.svc.import_contract_files(
            self.order["id"], [str(p)], "测试", auto_apply=auto_apply)

    def _fake_suggest(self, **over):
        base = {
            "ok": True, "source": "mock", "table_index": 1,
            "file_name": "mock.docx", "order_id": self.order["id"],
            "lines": [{"name": "模拟明细"}],
            "suggestions": [{
                "order_item_id": self.i1["id"], "qty": 2.0,
                "unit_price": 3690.0, "amount": 7380.0,
                "score": 80, "confidence": "高", "reason": "mock",
                "source_row": 2, "merged": 0, "item_name": "模拟物资"}],
            "unmatched": [], "warnings": [],
            "specs": [{"order_item_id": self.i1["id"], "qty": 2.0,
                       "unit_price": 3690.0, "tax_rate": 0, "remark": ""}],
            "total": 7380.0, "contract_amount": 7380.0, "gap": 0.0,
            "doc_no": DOC_NO,
        }
        base.update(over)
        return base

    def test_06_empty_import_keeps_result_shape(self):
        res = self.svc.import_contract_files(self.order["id"], [], "测试")
        self.assertEqual(res["files"], 0)
        self.assertEqual(res["auto_apply"], {})
        self.assertEqual(res["auto_applied"], 0)

    def test_07_default_enabled_applies_two_items(self):
        res = self._import()
        self.assertEqual(res["auto_applied"], 1)
        diag = res["auto_apply"][DOC_NO]
        self.assertTrue(diag["applied"])
        self.assertEqual(diag["count"], 2)
        self.assertEqual(diag["amount"], 14484.0)
        self.assertEqual(diag["lines"], 2)
        self.assertEqual(diag["unmatched"], 0)
        self.assertEqual(diag["gap"], 0.0)
        self.assertEqual(diag["confidence"], "高")
        cid = int(res["contracts"][0]["id"])
        rows = self.svc.contract_items(cid)
        self.assertEqual(len(rows), 2)
        self.assertEqual({float(x["unit_price"]) for x in rows},
                         {3552.0, 3690.0})
        self.assertTrue(all("导入自动应用" in x["remark"] for x in rows))
        self.assertEqual(
            self.profit.order_summary(self.order["id"])["purchase_total"],
            14484.0)

    def test_08_amount_source_still_doc(self):
        res = self._import()
        c = res["contracts"][0]
        self.assertEqual(c["amount"], 14484.0)
        self.assertEqual(c["amount_source"], "doc")
        self.assertEqual(c["status"], "已签订")
        self.assertEqual(c["tax_included"], 1)

    def test_09_audit_log_written(self):
        res = self._import()
        cid = int(res["contracts"][0]["id"])
        acts = [dict(r) for r in self.db.query(
            "SELECT * FROM audit_log WHERE entity='purchase_contract' "
            "AND entity_id=? AND action='auto_apply_items'", (cid,))]
        self.assertEqual(len(acts), 1)
        self.assertIn("14484", str(acts[0].get("after_json") or ""))

    def test_10_switch_off_never_writes(self):
        self.svc.set_auto_apply(False)
        res = self._import()
        self.assertEqual(res["auto_applied"], 0)
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("未开启", diag["reason"])
        cid = int(res["contracts"][0]["id"])
        self.assertEqual(self.svc.contract_items(cid), [])
        self.assertEqual(
            self.profit.order_summary(self.order["id"])["purchase_total"], 0.0)

    def test_11_explicit_param_overrides_prefs(self):
        self.svc.set_prefs({"auto_apply_items": True})
        off = self._import(auto_apply=False)
        self.assertEqual(off["auto_applied"], 0)
        self.svc.set_prefs({"auto_apply_items": False})
        on = self._import(auto_apply=True, name=f"{DOC_NO}委托加工合同B.docx")
        self.assertEqual(on["auto_applied"], 1)

    def test_12_low_confidence_skipped(self):
        sug = self._fake_suggest(suggestions=[{
            "order_item_id": self.i1["id"], "qty": 2.0,
            "unit_price": 3690.0, "amount": 7380.0, "score": 45,
            "confidence": "需复核", "reason": "mock", "source_row": 2}])
        with mock.patch.object(self.svc, "suggest_items_from_file",
                               return_value=sug):
            res = self._import()
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("需复核", diag["reason"])
        self.assertEqual(self.svc.contract_items(
            int(res["contracts"][0]["id"])), [])

    def test_13_unmatched_lines_skipped(self):
        sug = self._fake_suggest(unmatched=[{"name": "未知名物资"}])
        with mock.patch.object(self.svc, "suggest_items_from_file",
                               return_value=sug):
            res = self._import()
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("未匹配", diag["reason"])
        self.assertEqual(diag["unmatched"], 1)

    def test_14_amount_gap_skipped(self):
        sug = self._fake_suggest(gap=-500.0)
        with mock.patch.object(self.svc, "suggest_items_from_file",
                               return_value=sug):
            res = self._import()
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("相差", diag["reason"])

    def test_15_no_table_skipped(self):
        res = self._import(with_table=False)
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertTrue(diag["reason"])
        self.assertEqual(self.svc.contract_items(
            int(res["contracts"][0]["id"])), [])

    def test_16_apply_total_mismatch_skipped(self):
        # 数量被截断 / 合并异常时，实际写入金额会小于合同明细合计
        sug = self._fake_suggest(total=9000.0)
        with mock.patch.object(self.svc, "suggest_items_from_file",
                               return_value=sug):
            res = self._import()
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("不一致", diag["reason"])

    def test_17_existing_priced_rows_never_overwritten(self):
        first = self._import()
        cid = int(first["contracts"][0]["id"])
        before = [(x["order_item_id"], x["unit_price"])
                  for x in self.svc.contract_items(cid)]
        second = self._import(name=f"{DOC_NO}委托加工合同B.docx")
        diag = second["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("手填单价", diag["reason"])
        after = [(x["order_item_id"], x["unit_price"])
                 for x in self.svc.contract_items(cid)]
        self.assertEqual(before, after, "第二次导入绝不能覆盖已有单价")

    def test_18_zero_price_placeholders_replaced(self):
        first = self._import()
        cid = int(first["contracts"][0]["id"])
        self.svc.set_contract_items(cid, [
            {"order_item_id": x["order_item_id"], "qty": x["qty"],
             "unit_price": 0, "tax_rate": 0, "remark": "手工占位"}
            for x in self.svc.contract_items(cid)], "测试")
        self.assertEqual(
            self.profit.order_summary(self.order["id"])["purchase_total"], 0.0)
        second = self._import(name=f"{DOC_NO}委托加工合同B.docx")
        diag = second["auto_apply"][DOC_NO]
        self.assertTrue(diag["applied"], diag["reason"])
        self.assertEqual(diag["replaced"], 2)
        self.assertEqual(
            self.profit.order_summary(self.order["id"])["purchase_total"],
            14484.0)

    def test_19_cross_order_rows_skipped(self):
        first = self._import()
        cid = int(first["contracts"][0]["id"])
        other = self.orders.create_order(customer="另一张订单",
                                         project_no="CG352-002")
        oi = self.orders.add_item(other["id"], "耐磨弯头", 2, unit_price=500)
        self.svc.set_contract_items(cid, [
            {"order_item_id": oi["id"], "qty": 1, "unit_price": 0,
             "tax_rate": 0, "remark": "跨订单占位"}], "测试")
        second = self._import(name=f"{DOC_NO}委托加工合同B.docx")
        diag = second["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("另外", diag["reason"])
        self.assertEqual(
            [int(x["order_item_id"]) for x in self.svc.contract_items(cid)],
            [oi["id"]], "跨订单分配行必须原样保留")

    def test_20_quantity_conservation_rejection_caught(self):
        with mock.patch.object(
                self.svc, "set_contract_items",
                side_effect=Exception("采购数量超订单实值")):
            res = self._import()
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("写入被拦截", diag["reason"])

    def test_21_no_linked_order_skipped(self):
        p = write_docx(self.tmp / f"{DOC_NO}委托加工合同.docx")
        res = self.svc.import_contract_files(None, [str(p)], "测试")
        diag = res["auto_apply"][DOC_NO]
        self.assertFalse(diag["applied"])
        self.assertIn("未关联订单", diag["reason"])

    def test_22_summary_text_and_ui_wiring(self):
        res = self._import()
        text = auto_apply_summary(res)
        self.assertIn(DOC_NO, text)
        self.assertIn("已自动分配 2 项", text)
        self.assertIn("¥14,484.00", text)
        off = self.svc.set_auto_apply(False)
        self.assertFalse(off["auto_apply_items"])
        root = Path(__file__).resolve().parents[1]
        src = (root / "ui/panels/contract_actions.py").read_text(
            encoding="utf-8")
        self.assertIn("auto_apply_summary", src,
                      "生产排产快捷导入也必须展示自动应用诊断")
        dlg = (root / "ui/contract_dialog.py").read_text(encoding="utf-8")
        self.assertIn("auto_apply_box", dlg)
        self.assertIn("导入即自动分配", dlg)


class UITest(unittest.TestCase):
    """离屏验证开关可见、可切换、跨服务实例保持"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v352ui_"))
        try:
            from PySide6.QtWidgets import QApplication
            from v2.ui import theme
            self.app = QApplication.instance() or QApplication([])
            theme.apply_theme(self.app)
            self.PYSIDE = True
        except ImportError:                                  # pragma: no cover
            self.PYSIDE = False
            return
        self.db = Database(str(self.tmp / "erp.db"))
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))

    def tearDown(self):
        if getattr(self, "PYSIDE", False):
            self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_23_checkbox_persists_across_instances(self):
        if not self.PYSIDE:
            self.skipTest("PySide6 不可用")
        from PySide6.QtWidgets import QCheckBox
        from v2.ui.contract_dialog import PurchaseContractDialog
        w = PurchaseContractDialog(None, self.svc)
        boxes = w.findChildren(QCheckBox)
        self.assertEqual(len(boxes), 1)
        box = boxes[0]
        self.assertEqual(box.text(), "导入即自动分配")
        self.assertTrue(box.isChecked())
        box.setChecked(False)
        self.assertFalse(self.svc.prefs()["auto_apply_items"])
        self.assertTrue(
            (self.tmp / "data" / contract_prefs.FILENAME).is_file())
        fresh = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.assertFalse(fresh.prefs()["auto_apply_items"],
                         "新实例必须读到关掉的开关")
        w.close()
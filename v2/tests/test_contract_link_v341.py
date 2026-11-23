#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.1 回归（2026-11-09 · 用户第 15 轮）：合同-订单联动

需求 1：采购合同管理的订单下拉显示「订单项目编号（客户名称）」，不再显示系统单号
需求 2：签订合同后，订单主表在「汇总口径」前显示「采购合同编号」列，
        单击该列直接进入该订单的采购合同（管理窗口并定位）
需求 3：订单管理页面移除「系统单号」列

锁定口径：
  ① 「签订后」= 已签订 / 执行中 / 已完成（SIGNED_STATUSES）；草稿、作废不显示；
  ② 多份已签订合同用「、」连接展示在一格；
  ③ 列布局：删系统单号后 16 列，采购合同编号在汇总口径前（索引 14），
    客户名称仍是拉伸宽列（stretch_col=1）。

    python -m pytest v2/tests/test_contract_link_v341.py -q
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                      # noqa: E402
from v2.core import models                                      # noqa: E402
from v2.core.db import Database                                 # noqa: E402
from v2.domains import OrderService                             # noqa: E402
from v2.domains.contract import SIGNED_STATUSES, PurchaseContractService  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
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


@unittest.skipUnless(PYSIDE, "PySide6 未安装")
class Test341(unittest.TestCase):

    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="v341_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.contracts = PurchaseContractService(self.db, str(self.tmp))
        self.order = self.orders.create_order(
            customer="国能浙江北仑第一发电", project_no="SO-2026-341",
            status=models.OrderStatus.CONFIRMED)

    def tearDown(self):
        self.db.close()

    def _window(self):
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        w.switch_view("orders")
        w.refresh_all()
        return w

    def _headers(self, w):
        return [w.tbl_orders.horizontalHeaderItem(i).text()
                for i in range(w.tbl_orders.columnCount())]

    # ── 需求 1：订单下拉显示项目编号 ─────────────────────

    def test_01_version_bumped(self):
        self.assertEqual(__version__, "3.5.4")

    def test_02_filter_dropdown_shows_project_no(self):
        from v2.ui.contract_dialog import PurchaseContractDialog
        d = PurchaseContractDialog(None, self.contracts)
        captions = [d.order_box.itemText(i)
                    for i in range(d.order_box.count())]
        self.assertTrue(any("SO-2026-341（国能浙江北仑第一发电）" == c
                            for c in captions), captions)
        self.assertFalse(any(c.startswith("DD-") and "（" in c
                             for c in captions if "SO-" not in c),
                          "不应再以系统单号开头展示")
        d.close()

    def test_03_form_dropdown_fallback_to_doc_no(self):
        from v2.ui.contract_dialog import PurchaseContractDialog
        # 项目编号为空的老订单 → 回退显示系统单号
        legacy = self.orders.create_order(
            customer="无项目编号客户", project_no="", status=models.OrderStatus.CONFIRMED)
        d2 = PurchaseContractDialog(None, self.contracts)
        found = {}
        for i in range(d2.order_box.count()):
            oid = d2.order_box.itemData(i)
            if oid:
                found[oid] = d2.order_box.itemText(i)
        self.assertEqual(found[legacy["id"]], f"{legacy['doc_no']}（无项目编号客户）")
        d2.close()

    # ── 需求 2：订单表采购合同编号列 ─────────────────────

    def test_04_headers_layout(self):
        w = self._window()
        headers = self._headers(w)
        self.assertNotIn("系统单号", headers)               # 需求 3
        self.assertIn("采购合同编号", headers)
        self.assertEqual(headers.index("采购合同编号"),
                         headers.index("汇总口径") - 1)     # 汇总口径前
        self.assertEqual(headers[0], "订单项目编号")
        self.assertEqual(w.tbl_orders.columnCount(), 20)
        w.close()

    def test_05_draft_and_void_not_shown_signed_shown(self):
        c1 = self.contracts.create({
            "contract_name": "草稿合同", "order_id": self.order["id"],
            "amount": 1.0, "status": "草稿"}, "测试")
        c2 = self.contracts.create({
            "contract_name": "作废合同", "order_id": self.order["id"],
            "amount": 2.0, "status": "作废"}, "测试")
        w = self._window()
        col = self._headers(w).index("采购合同编号")
        self.assertEqual(w.tbl_orders.item(0, col).text(), "—")
        self.contracts.set_status(c1["id"], "已签订", "测试")
        w.refresh_all()
        self.assertEqual(w.tbl_orders.item(0, col).text(), c1["doc_no"])
        # 执行中 / 已完成同样算签订后
        self.contracts.set_status(c1["id"], "执行中", "测试")
        w.refresh_all()
        self.assertEqual(w.tbl_orders.item(0, col).text(), c1["doc_no"])
        self.assertEqual(list(SIGNED_STATUSES), ["已签订", "执行中", "已完成"])
        w.close()

    def test_06_multiple_signed_contracts_joined(self):
        a = self.contracts.create({
            "contract_name": "合同A", "order_id": self.order["id"],
            "amount": 3.0, "status": "已签订"}, "测试")
        b = self.contracts.create({
            "contract_name": "合同B", "order_id": self.order["id"],
            "amount": 4.0, "status": "已完成"}, "测试")
        w = self._window()
        col = self._headers(w).index("采购合同编号")
        text = w.tbl_orders.item(0, col).text()
        self.assertIn(a["doc_no"], text)
        self.assertIn(b["doc_no"], text)
        self.assertIn("、", text)
        w.close()

    def test_07_click_contract_cell_opens_dialog(self):
        c = self.contracts.create({
            "contract_name": "点击进入合同", "order_id": self.order["id"],
            "amount": 5.0, "status": "已签订"}, "测试")
        w = self._window()
        w.tbl_orders.selectRow(0)
        col = self._headers(w).index("采购合同编号")
        captured = {}

        class FakeDialog:
            def __init__(self, parent, svc, order_id=None, focus_contract_id=None):
                captured["order_id"] = order_id
                captured["focus"] = focus_contract_id

            def exec(self):
                return 0

        with mock.patch("v2.ui.contract_dialog.PurchaseContractDialog",
                        FakeDialog):
            w._on_order_contract_click(0, col)
        self.assertEqual(captured["order_id"], self.order["id"])
        self.assertEqual(captured["focus"], int(c["id"]))

    def test_08_click_other_columns_noop(self):
        self.contracts.create({
            "contract_name": "不该弹出", "order_id": self.order["id"],
            "amount": 6.0, "status": "已签订"}, "测试")
        w = self._window()
        w.tbl_orders.selectRow(0)
        with mock.patch("v2.ui.contract_dialog.PurchaseContractDialog") as m:
            w._on_order_contract_click(0, 0)               # 项目编号列：无动作
            m.assert_not_called()
        w.close()

    def test_09_click_without_signed_contract_notifies_only(self):
        self.contracts.create({
            "contract_name": "草稿不弹窗", "order_id": self.order["id"],
            "amount": 7.0, "status": "草稿"}, "测试")
        w = self._window()
        w.tbl_orders.selectRow(0)
        col = self._headers(w).index("采购合同编号")
        from v2.ui import theme
        notes = []
        with mock.patch.object(theme, "notify", lambda msg: notes.append(msg)):
            w._on_order_contract_click(0, col)
        self.assertTrue(any("暂无已签订" in n for n in notes), notes)
        w.close()


if __name__ == "__main__":
    unittest.main()

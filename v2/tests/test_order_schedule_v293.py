#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单排产 v2.9.3 回归（2026-11-07）

覆盖：
  ★ 整单排产：全部物资各建一张排产单并排满，共用一张整单审批文档（幂等）
  · 审批复用：已生成过的审批文档直接打开，不重复生成
  · 附件目录按订单归档：data/订单排产附件/<订单号>/<排产单号>/
  · 【v3.1.2 口径】存在 Word/Excel 审批文件 → 保存即默认该物资已排产
    （v2.9.3 的「≥2 类附件」口径已废止，本文件同步改断言）
  · 「导入生产排产审批」单一入口（图片/图纸按钮已删）/ 动作清单「整单排产」入口
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import models, worddoc                                # noqa: E402
from v2.core.db import Database                                    # noqa: E402
from v2.domains import OrderService, OrderScheduleService           # noqa: E402
from v2.domains.schedule import (KIND_IMAGE, KIND_DRAWING, KIND_APPROVAL,
                                 APPROVAL_FILTER, AUTO_FILL_KINDS)  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    PYSIDE = True
except ImportError:
    PYSIDE = False

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _png(tmp: Path, name: str) -> str:
    p = Path(tmp) / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)   # 最小 PNG 头即可，服务层只复制不解析
    return str(p)


class WholeScheduleFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v293_sched_")
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = OrderScheduleService(self.db, self.tmp)
        self.order = self.orders.create_order(
            customer="华东电建", project_no="SO-2026-093",
            status=models.OrderStatus.CONFIRMED)
        self.i1 = self.orders.add_item(
            self.order["id"], item_name="耐磨弯头", item_code="SP-250",
            spec="DN250 S=8", unit="件", qty_ordered=10, unit_price=2100.0)
        self.i2 = self.orders.add_item(
            self.order["id"], item_name="U形波纹补偿器", item_code="SP-300-U",
            spec="DN300 70B125", unit="台", qty_ordered=5, unit_price=5200.0)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        return self.db.get_by_id("orders", self.order["id"])["status"]

    def approval_rows(self):
        return self.db.query(
            "SELECT f.* FROM order_schedule_files f "
            "JOIN order_schedules s ON s.id = f.schedule_id "
            "WHERE s.order_id = ? AND f.kind = ?",
            (self.order["id"], KIND_APPROVAL))


class WholeOrderTest(WholeScheduleFixture):

    def test_01_whole_order_fills_all_and_schedules(self):
        res = self.svc.ensure_order(self.order["id"], "测试")
        scheds = self.svc.list(self.order["id"])
        self.assertEqual(len(scheds), 2)
        self.assertEqual(res["created"], [s["doc_no"] for s in scheds])
        for s in scheds:
            item = self.db.get_by_id("order_items", s["order_item_id"])
            self.assertAlmostEqual(float(s["allocated_qty"]),
                                   float(item["qty_ordered"]), places=6)
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        # 整单审批文档生成在订单目录下，且登记到每张排产单
        rows = self.approval_rows()
        self.assertEqual(len(rows), 2)
        paths = {r["file_path"] for r in rows}
        self.assertEqual(len(paths), 1)                     # 一个物理文件
        self.assertIn(f"/{self.order['doc_no']}/", rows[0]["file_path"])
        self.assertTrue(self.svc.to_abs(rows[0]["file_path"]).is_file())

    def test_02_whole_order_is_idempotent(self):
        first = self.svc.ensure_order(self.order["id"], "测试")
        n_files = len(self.approval_rows())
        n_docx = len(list((Path(self.tmp) / self.order["doc_no"]).glob("*.docx")))
        second = self.svc.ensure_order(self.order["id"], "测试")
        self.assertEqual(second["created"], [])             # 不重复建单
        self.assertEqual(len(self.approval_rows()), n_files)  # 不重复登记
        self.assertEqual(
            len(list((Path(self.tmp) / self.order["doc_no"]).glob("*.docx"))),
            n_docx)                                         # 不重复生成文档
        self.assertEqual(Path(first["approval_path"]).name,
                         Path(second["approval_path"]).name)

    def test_03_whole_order_needs_items(self):
        self.orders.create_order(customer="空单", status=models.OrderStatus.CONFIRMED)
        empty = self.db.query_one(
            "SELECT id FROM orders WHERE customer = '空单'")
        with self.assertRaises(Exception):
            self.svc.ensure_order(empty["id"], "测试")


class ApprovalReuseTest(WholeScheduleFixture):

    def test_04_ensure_approval_reuses_existing(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        p1, created1 = self.svc.ensure_approval(s["id"], "测试")
        self.assertTrue(created1)
        self.assertTrue(Path(p1).is_file())
        n = len(self.svc.files(s["id"]))
        p2, created2 = self.svc.ensure_approval(s["id"], "测试")
        self.assertFalse(created2)
        self.assertEqual(Path(p1).resolve(), Path(p2).resolve())
        self.assertEqual(len(self.svc.files(s["id"])), n)   # 不重复登记

    def test_05_order_folder_layout(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        self.svc.add_files(s["id"], KIND_IMAGE,
                           [_png(self.tmp, "a.png")], "测试")
        rows = self.svc.files(s["id"])
        self.assertTrue(all(r["file_path"].startswith(
            f"订单排产附件/{self.order['doc_no']}/{s['doc_no']}") for r in rows))

    def test_06_import_approval_kind(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        src = Path(self.tmp) / "外部审批.docx"
        src.write_bytes(b"PK\x03\x04fake-docx")
        added = self.svc.add_files(s["id"], KIND_APPROVAL, [str(src)], "测试")
        self.assertEqual(added[0]["kind"], KIND_APPROVAL)
        self.assertIn("审批", self.svc.file_kinds(s["id"]))
        self.assertIn("*.docx", APPROVAL_FILTER)


class AutoFillTest(WholeScheduleFixture):

    def _kinds(self, sid):
        return self.svc.file_kinds(sid)

    def test_07_approval_doc_auto_fill_on_save(self):
        """v3.1.2：只有图片不触发；导入 Word 审批文件才自动补满"""
        s = self.svc.ensure(self.i1["id"], "测试")
        self.svc.set_allocated_qty(s["id"], 4.0, "测试")   # 先人为改小，模拟未排满
        self.svc.add_files(s["id"], KIND_IMAGE, [_png(self.tmp, "b.png")], "测试")
        filled, kinds = self.svc.auto_fill_if_ready(s["id"], "测试")
        self.assertEqual((filled, kinds), (False, 1))       # 1 类图片不触发
        self.svc.add_files(s["id"], KIND_APPROVAL,
                           [_png(self.tmp, "c.docx")], "测试")
        filled, kinds = self.svc.auto_fill_if_ready(s["id"], "测试")
        self.assertEqual((filled, kinds), (True, 2))        # Word 审批 → 自动补满
        s2 = self.svc.get(s["id"])
        self.assertAlmostEqual(float(s2["allocated_qty"]), 10.0, places=6)
        # 本单还有另一项物资未排 → 整单状态不流转；但本物资可排量已为 0
        self.assertAlmostEqual(self.orders.available_qty(self.i1["id"]), 0.0,
                               places=6)

    def test_08_partial_qty_respected_without_approval_doc(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        filled, kinds = self.svc.auto_fill_if_ready(s["id"], "测试")
        self.assertEqual((filled, kinds), (False, 0))
        self.assertAlmostEqual(float(self.svc.get(s["id"])["allocated_qty"]),
                               10.0, places=6)  # 建单本身已排满（ensure 语义），不受影响


@unittest.skipUnless(PYSIDE, "PySide6 不可用")
class DialogAndUITest(WholeScheduleFixture):

    def test_09_dialog_has_import_approval_button_and_kind_hint(self):
        """v3.1.2：只留两个动作按钮，导入审批一个入口包办任意类型文件"""
        _app()
        from v2.ui import dialogs
        from PySide6.QtWidgets import QPushButton
        s = self.svc.ensure(self.i1["id"], "测试")
        dlg = dialogs.OrderScheduleDialog(None, self.svc, s["id"])
        texts = [b.text() for b in dlg.findChildren(QPushButton)]
        self.assertIn("新建生产排产审批", texts)
        self.assertIn("导入生产排产审批", texts)
        self.assertNotIn("导入图片", texts)
        self.assertNotIn("导入图纸", texts)
        self.assertFalse(any("启动本机 Word" in t for t in texts))
        self.assertIn("0 类", dlg.kind_hint.text())
        self.assertIn("Word/Excel", dlg.kind_hint.text())
        dlg.close()

    def test_10_kind_hint_updates_after_import(self):
        _app()
        from v2.ui import dialogs
        s = self.svc.ensure(self.i1["id"], "测试")
        self.svc.add_files(s["id"], KIND_DRAWING, [_png(self.tmp, "d.dwg")], "测试")
        self.svc.add_files(s["id"], KIND_APPROVAL,
                           [_png(self.tmp, "e.docx")], "测试")
        dlg = dialogs.OrderScheduleDialog(None, self.svc, s["id"])
        self.assertIn("已含 2 类", dlg.kind_hint.text())
        self.assertIn("已排产", dlg.kind_hint.text())
        dlg.close()

    def test_11_action_group_contains_whole_schedule(self):
        from v2.ui.panels.common import ACTION_GROUPS
        orders = [g for g in ACTION_GROUPS if g[1] == "orders"][0]
        names = [a[0] for a in orders[2]]
        methods = [a[1] for a in orders[2]]
        self.assertIn("整单排产", names)
        self.assertIn("_whole_order_schedule", methods)


class WordDocTest(WholeScheduleFixture):

    def test_12_order_approval_docx_contains_all_items(self):
        from docx import Document
        res = self.svc.ensure_order(self.order["id"], "测试")
        doc = Document(res["approval_path"])
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("整单", text)
        table_text = "\n".join(
            c.text for t in doc.tables for row in t.rows for c in row.cells)
        self.assertIn("耐磨弯头", table_text)
        self.assertIn("U形波纹补偿器", table_text)
        self.assertIn(self.order["doc_no"], text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

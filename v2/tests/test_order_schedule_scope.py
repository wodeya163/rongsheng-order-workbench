#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单排产 v3.0 回归（2026-11-07）

口径（用户第 6 轮给定）：
  · 整单排产也要先启动【订单排产】对话框，不再直接弹生产排产审批文件编辑窗口；
  · 对话框里对排产单做「分项 / 合并 / 整单」标记；
  · 订单明细可多选多种物料做合并排产，合并同在订单排产窗口里操作。

覆盖：
  ① 范围判定（scope_label）与三档标记落库；
  ② 合并/整单共用一张审批文档（物理单份 + 登记到多张排产单）；
  ③ 批次号复用 / 换子集新建批次；
  ④ 共用附件的移除与单张撤销不误删兄弟文件；
  ⑤ v6 迁移回填（历史共享审批文档 → 整单）；
  ⑥ 对话框：勾选改范围、按钮文案、拒绝不建单、保存落库；
  ⑦ 入口：整单/合并都走对话框，不再直接调 ensure_order。

    python -m pytest v2/tests/test_order_schedule_scope.py -q
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import models                                     # noqa: E402
from v2.core.db import Database                                # noqa: E402
from v2.domains import OrderService, OrderScheduleService       # noqa: E402
from v2.domains.schedule import (AUTO_FILL_KINDS, KIND_APPROVAL,  # noqa: E402
                                 KIND_IMAGE, SCOPE_ITEM, SCOPE_MERGE,
                                 SCOPE_WHOLE, scope_label)

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QPushButton
    PYSIDE = True
except ImportError:                                            # pragma: no cover
    PYSIDE = False

_APP = None


def _app():
    global _APP
    if _APP is None:
        from v2.ui import theme
        _APP = QApplication.instance() or QApplication([])
        theme.apply_theme(_APP)
    return _APP


def _png(tmp: Path, name: str) -> str:
    p = Path(tmp) / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 48)
    return str(p)


class ScopeFixture(unittest.TestCase):
    """三物资订单：整单=3 项，合并=2 项，分项=1 项"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v30_scope_")
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = OrderScheduleService(self.db, self.tmp)
        self.order = self.orders.create_order(
            customer="华东电建", project_no="SO-2026-110",
            status=models.OrderStatus.CONFIRMED)
        self.i1 = self.orders.add_item(
            self.order["id"], item_name="耐磨弯头", item_code="SP-250",
            spec="DN250 S=8", unit="件", qty_ordered=10, unit_price=2100.0)
        self.i2 = self.orders.add_item(
            self.order["id"], item_name="U形波纹补偿器", item_code="SP-300-U",
            spec="DN300 70B125", unit="台", qty_ordered=5, unit_price=5200.0)
        self.i3 = self.orders.add_item(
            self.order["id"], item_name="落煤管", item_code="SP-400",
            spec="DN400", unit="根", qty_ordered=3, unit_price=800.0)
        self.ids = [self.i1["id"], self.i2["id"], self.i3["id"]]

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sched(self, item_id):
        return self.svc.get_for_item(item_id)

    def status(self):
        return self.db.get_by_id("orders", self.order["id"])["status"]

    def approval_rows(self):
        return self.db.query(
            "SELECT f.* FROM order_schedule_files f "
            "JOIN order_schedules s ON s.id = f.schedule_id "
            "WHERE s.order_id = ? AND f.kind = ? ORDER BY f.id",
            (self.order["id"], KIND_APPROVAL))


class ScopeLabelTest(unittest.TestCase):

    def test_01_three_levels(self):
        self.assertEqual(scope_label(1, 3), SCOPE_ITEM)
        self.assertEqual(scope_label(2, 3), SCOPE_MERGE)
        self.assertEqual(scope_label(3, 3), SCOPE_WHOLE)
        # 单项订单：1/1 就是这张单的全部，按分项走（不存在「合并」语义）
        self.assertEqual(scope_label(1, 1), SCOPE_ITEM)
        self.assertEqual(scope_label(0, 3), SCOPE_ITEM)


class EnsureScopeTest(ScopeFixture):

    def test_02_single_item_is_item_scope(self):
        res = self.svc.ensure_scope(self.order["id"], [self.i1["id"]], "测试")
        self.assertEqual(res["scope"], SCOPE_ITEM)
        self.assertEqual(res["batch_no"], "")
        self.assertEqual(len(res["schedules"]), 1)
        s = self.sched(self.i1["id"])
        self.assertEqual(s["scope"], SCOPE_ITEM)
        self.assertIsNone(s["batch_no"])
        # 幂等：再调一次不新建单
        res2 = self.svc.ensure_scope(self.order["id"], [self.i1["id"]], "测试")
        self.assertEqual(res2["created"], [])
        self.assertEqual(len(res2["schedules"]), 1)

    def test_03_two_items_is_merge_scope_with_batch(self):
        res = self.svc.ensure_scope(
            self.order["id"], [self.i1["id"], self.i2["id"]], "测试")
        self.assertEqual(res["scope"], SCOPE_MERGE)
        self.assertTrue(res["batch_no"].endswith("-B01"))
        for iid in (self.i1["id"], self.i2["id"]):
            s = self.sched(iid)
            self.assertEqual(s["scope"], SCOPE_MERGE)
            self.assertEqual(s["batch_no"], res["batch_no"])
        self.assertEqual(len(self.svc.batch_schedules(
            self.order["id"], res["batch_no"])), 2)

    def test_04_all_items_is_whole_scope_and_batch_reused(self):
        first = self.svc.ensure_scope(self.order["id"], self.ids, "测试")
        self.assertEqual(first["scope"], SCOPE_WHOLE)
        second = self.svc.ensure_scope(self.order["id"], self.ids, "测试")
        self.assertEqual(second["created"], [])
        self.assertEqual(second["batch_no"], first["batch_no"])   # 同集合复用批次
        self.assertEqual(len(self.svc.list(self.order["id"])), 3)

    def test_05_other_subset_gets_new_batch(self):
        first = self.svc.ensure_scope(
            self.order["id"], [self.i1["id"], self.i2["id"]], "测试")
        second = self.svc.ensure_scope(
            self.order["id"], [self.i2["id"], self.i3["id"]], "测试")
        self.assertEqual(second["scope"], SCOPE_MERGE)
        self.assertNotEqual(first["batch_no"], second["batch_no"])
        self.assertEqual(self.sched(self.i3["id"])["batch_no"], second["batch_no"])

    def test_06_back_to_single_clears_batch(self):
        res = self.svc.ensure_scope(self.order["id"], self.ids, "测试")
        self.assertTrue(res["batch_no"])
        back = self.svc.ensure_scope(self.order["id"], [self.i3["id"]], "测试")
        self.assertEqual(back["scope"], SCOPE_ITEM)
        s = self.sched(self.i3["id"])
        self.assertEqual(s["scope"], SCOPE_ITEM)
        self.assertIsNone(s["batch_no"])

    def test_07_rejects_empty_selection(self):
        with self.assertRaises(Exception):
            self.svc.ensure_scope(self.order["id"], [], "测试")
        with self.assertRaises(Exception):
            self.svc.ensure_scope(self.order["id"], [999999], "测试")


class ScopeApprovalTest(ScopeFixture):

    def test_08_merge_builds_one_shared_doc(self):
        from docx import Document
        res = self.svc.ensure_scope(
            self.order["id"], [self.i1["id"], self.i2["id"]], "测试")
        sids = [s["id"] for s in res["schedules"]]
        path, created = self.svc.build_scope_approval(
            self.order["id"], sids, SCOPE_MERGE, "测试")
        self.assertTrue(created)
        self.assertIn("合并", Path(path).name)
        self.assertTrue(Path(path).is_file())
        rows = self.approval_rows()
        self.assertEqual(len(rows), 2)                 # 两张排产单各登记一条
        self.assertEqual(len({r["file_path"] for r in rows}), 1)   # 物理只一份
        text = "\n".join(p.text for p in Document(path).paragraphs)
        table = "\n".join(c.text for t in Document(path).tables
                          for row in t.rows for c in row.cells)
        self.assertIn("合并", text)
        self.assertIn("耐磨弯头", table)
        self.assertIn("U形波纹补偿器", table)
        self.assertNotIn("落煤管", table)              # 未勾选的不进合并审批单

    def test_09_merge_approval_is_reused(self):
        res = self.svc.ensure_scope(
            self.order["id"], [self.i1["id"], self.i2["id"]], "测试")
        sids = [s["id"] for s in res["schedules"]]
        p1, c1 = self.svc.build_scope_approval(self.order["id"], sids, SCOPE_MERGE, "测试")
        n_docx = len(list((Path(self.tmp) / self.order["doc_no"]).glob("*.docx")))
        p2, c2 = self.svc.build_scope_approval(self.order["id"], sids, SCOPE_MERGE, "测试")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(Path(p1).resolve(), Path(p2).resolve())
        self.assertEqual(len(self.approval_rows()), 2)             # 不重复登记
        self.assertEqual(
            len(list((Path(self.tmp) / self.order["doc_no"]).glob("*.docx"))), n_docx)

    def test_10_whole_doc_covers_all_items(self):
        from docx import Document
        res = self.svc.ensure_scope(self.order["id"], self.ids, "测试")
        sids = [s["id"] for s in res["schedules"]]
        path, _ = self.svc.build_scope_approval(
            self.order["id"], sids, SCOPE_WHOLE, "测试")
        self.assertIn("整单", Path(path).name)
        table = "\n".join(c.text for t in Document(path).tables
                          for row in t.rows for c in row.cells)
        for name in ("耐磨弯头", "U形波纹补偿器", "落煤管"):
            self.assertIn(name, table)
        self.assertEqual(len(self.approval_rows()), 3)

    def test_11_item_scope_reuses_single_approval(self):
        res = self.svc.ensure_scope(self.order["id"], [self.i1["id"]], "测试")
        sid = res["schedules"][0]["id"]
        p1, c1 = self.svc.build_scope_approval(
            self.order["id"], [sid], SCOPE_ITEM, "测试")
        p2, c2 = self.svc.build_scope_approval(
            self.order["id"], [sid], SCOPE_ITEM, "测试")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(Path(p1).resolve(), Path(p2).resolve())
        self.assertNotIn("合并", Path(p1).name)


class SharedFilesTest(ScopeFixture):

    def _merge_with_files(self):
        res = self.svc.ensure_scope(
            self.order["id"], [self.i1["id"], self.i2["id"]], "测试")
        sids = [s["id"] for s in res["schedules"]]
        self.svc.add_files_to(sids, KIND_IMAGE, [_png(Path(self.tmp), "a.png")], "测试")
        return sids

    def test_12_files_registered_to_each_schedule(self):
        sids = self._merge_with_files()
        for sid in sids:
            self.assertEqual(len(self.svc.files(sid)), 1)
        merged = self.svc.scope_files(sids)
        self.assertEqual(len(merged), 1)                  # 去重后一条
        self.assertEqual(merged[0]["shared"], 2)
        self.assertEqual(self.svc.file_kinds(sids[0]), [KIND_IMAGE])

    def test_13_remove_shared_file_cleans_all_rows(self):
        sids = self._merge_with_files()
        fid = self.svc.files(sids[0])[0]["id"]
        n = self.svc.remove_file(fid, "测试")
        self.assertEqual(n, 2)
        for sid in sids:
            self.assertEqual(self.svc.files(sid), [])

    def test_14_delete_one_schedule_keeps_shared_file(self):
        sids = self._merge_with_files()
        path = self.svc.to_abs(self.svc.files(sids[0])[0]["file_path"])
        self.assertTrue(path.is_file())
        self.svc.delete(sids[0], "测试")
        self.assertTrue(path.is_file())                   # 兄弟单还在用，不能删
        self.assertEqual(len(self.svc.files(sids[1])), 1)
        self.svc.delete(sids[1], "测试")
        self.assertFalse(path.is_file())                  # 最后一个走了才清物理文件

    def test_15_approval_doc_triggers_autofill_per_schedule(self):
        sids = self._merge_with_files()
        self.svc.add_files_to(sids, KIND_APPROVAL,
                              [_png(Path(self.tmp), "b.docx")], "测试")
        for sid in sids:
            filled, kinds = self.svc.auto_fill_if_ready(sid, "测试")
            self.assertTrue(filled)
            self.assertEqual(kinds, AUTO_FILL_KINDS)


class MigrationTest(ScopeFixture):

    def test_16_shared_approval_backfilled_as_whole(self):
        from v2.core.migrations import v6_schedule_scope_backfill
        # 造一条「历史整单」痕迹：两张排产单共用一个审批文件、scope 还是默认分项
        res = self.svc.ensure_scope(self.order["id"], [self.i1["id"], self.i2["id"]], "测试")
        sids = [s["id"] for s in res["schedules"]]
        path, _ = self.svc.build_scope_approval(
            self.order["id"], sids, SCOPE_MERGE, "测试")
        for sid in sids:
            self.db.execute("UPDATE order_schedules SET scope = '分项', "
                            "batch_no = NULL WHERE id = ?", (sid,))
        v6_schedule_scope_backfill(self.db.conn, Path(self.tmp))
        rows = [self.svc.get(s) for s in sids]
        self.assertEqual({r["scope"] for r in rows}, {SCOPE_WHOLE})
        self.assertEqual({r["batch_no"] for r in rows}, {rows[0]["batch_no"]})
        self.assertTrue(rows[0]["batch_no"].endswith("-B01"))
        # 幂等：再跑一次批次号不变、不新增批次
        v6_schedule_scope_backfill(self.db.conn, Path(self.tmp))
        again = [self.svc.get(s) for s in sids]
        self.assertEqual({r["batch_no"] for r in again}, {rows[0]["batch_no"]})
        self.assertTrue(Path(path).is_file())


@unittest.skipUnless(PYSIDE, "PySide6 不可用")
class DialogScopeTest(ScopeFixture):

    def _dialog(self, **kw):
        from v2.ui import dialogs
        _app()
        return dialogs.OrderScheduleDialog(None, self.svc, **kw)

    def test_17_dialog_shows_scope_and_switch_button_text(self):
        d = self._dialog(order_id=self.order["id"], item_ids=[self.i1["id"]])
        self.assertIn("分项", d.scope_value.text())
        texts = [b.text() for b in d.findChildren(QPushButton)]
        self.assertIn("新建生产排产审批", texts)
        self.assertIn("导入生产排产审批", texts)
        self.assertFalse(any("启动本机 Word" in t for t in texts))
        self.assertEqual(d.item_table.rowCount(), 3)
        self.assertEqual(d.item_table.columnCount(), 10)
        self.assertEqual(d.table.columnCount(), 4)          # 附件表仍是 4 列
        d.item_table.item(1, 0).setCheckState(Qt.Checked)
        self.assertIn("合并", d.scope_value.text())
        self.assertIn("合并 2 项", d.btn_approval.text())
        d._set_checked(all_on=True)
        self.assertIn("整单", d.scope_value.text())
        self.assertIn("整单", d.btn_approval.text())
        d.close()

    def test_18_whole_entry_preselects_all_items(self):
        d = self._dialog(order_id=self.order["id"], scope=SCOPE_WHOLE)
        self.assertEqual(len(d._checked_item_ids()), 3)
        self.assertIn("整单", d.scope_value.text())
        d.close()

    def test_19_reject_without_touch_creates_nothing(self):
        d = self._dialog(order_id=self.order["id"], scope=SCOPE_WHOLE)
        d.reject()
        self.assertEqual(self.svc.list(self.order["id"]), [])
        d.close()

    def test_20_save_writes_scope_and_fills(self):
        d = self._dialog(order_id=self.order["id"], item_ids=self.ids[:2])
        self.assertTrue(d._save())
        s1, s2 = self.sched(self.i1["id"]), self.sched(self.i2["id"])
        self.assertEqual(s1["scope"], SCOPE_MERGE)
        self.assertEqual(s1["batch_no"], s2["batch_no"])
        self.assertAlmostEqual(float(s1["allocated_qty"]), 10.0, places=6)
        self.assertAlmostEqual(float(s2["allocated_qty"]), 5.0, places=6)
        # 第三项未排 → 订单不流转
        self.assertNotEqual(self.status(), models.OrderStatus.SCHEDULED)
        d.close()

    def test_21_whole_save_schedules_order(self):
        d = self._dialog(order_id=self.order["id"], scope=SCOPE_WHOLE)
        self.assertTrue(d._save())
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        self.assertEqual({s["scope"] for s in self.svc.list(self.order["id"])},
                         {SCOPE_WHOLE})
        d.close()

    def test_22_dialog_opens_from_existing_schedule(self):
        res = self.svc.ensure_scope(self.order["id"], [self.i1["id"]], "测试")
        d = self._dialog(schedule_id=res["schedules"][0]["id"])
        self.assertIn("分项", d.scope_value.text())
        self.assertIn("全部物资", d.hint.text())
        d.close()


class EntryPointTest(ScopeFixture):
    """入口必须先进对话框，而不是直接生成审批文档并弹 Word"""

    def _stub(self):
        from v2.ui.panels.order_actions import OrderActionsMixin

        class _Stub(OrderActionsMixin):
            current_view = "orders"

        stub = _Stub()
        stub.db = self.db
        stub.schedules = self.svc
        return stub

    def test_23_whole_order_opens_dialog_not_word(self):
        stub = self._stub()
        fake = mock.MagicMock()
        fake.exec.return_value = 0
        fake.summary_text.return_value = "整单排产"
        with mock.patch("v2.ui.panels.order_actions.dialogs.OrderScheduleDialog",
                        return_value=fake) as ctor, \
                mock.patch.object(OrderScheduleService, "ensure_order") as ensure:
            stub._whole_order_schedule(self.order["id"])
        ctor.assert_called_once()
        kwargs = ctor.call_args.kwargs
        self.assertEqual(kwargs.get("order_id"), self.order["id"])
        self.assertEqual(kwargs.get("scope"), SCOPE_WHOLE)
        fake.exec.assert_called_once()
        ensure.assert_not_called()

    def test_24_item_entry_opens_dialog_without_precreating(self):
        stub = self._stub()
        fake = mock.MagicMock()
        fake.exec.return_value = 1
        fake.summary_text.return_value = "分项排产"
        with mock.patch("v2.ui.panels.order_actions.dialogs.OrderScheduleDialog",
                        return_value=fake) as ctor:
            msg = stub._open_order_schedule(self.i2["id"])
        self.assertEqual(msg, "分项排产")
        self.assertEqual(ctor.call_args.kwargs.get("item_ids"), [self.i2["id"]])
        self.assertEqual(self.svc.list(self.order["id"]), [])   # 未确认前不落库

    def test_25_merge_needs_two_items(self):
        stub = self._stub()
        with mock.patch("v2.ui.panels.order_actions.dialogs.OrderScheduleDialog") as ctor, \
                mock.patch("v2.ui.panels.order_actions.theme.notify") as notify:
            self.assertIsNone(stub._merge_order_schedule(
                order_id=self.order["id"], item_ids=[self.i1["id"]]))
        ctor.assert_not_called()
        notify.assert_called_once()

    def test_26_action_groups_have_merge_entry(self):
        from v2.ui.panels.common import ACTION_GROUPS
        orders = [g for g in ACTION_GROUPS if g[1] == "orders"][0]
        names = [a[0] for a in orders[2]]
        methods = [a[1] for a in orders[2]]
        self.assertIn("合并排产", names)
        self.assertIn("_merge_order_schedule", methods)
        self.assertIn("_whole_order_schedule", methods)
        self.assertTrue(hasattr(OrderService, "apply_edits"))


class BackCompatTest(ScopeFixture):

    def test_27_ensure_order_still_one_whole_doc(self):
        res = self.svc.ensure_order(self.order["id"], "测试")
        self.assertEqual(res["schedule_count"], 3)
        self.assertTrue(res["batch_no"])
        self.assertTrue(Path(res["approval_path"]).is_file())
        self.assertEqual({s["scope"] for s in self.svc.list(self.order["id"])},
                         {SCOPE_WHOLE})
        self.assertEqual(len({r["file_path"] for r in self.approval_rows()}), 1)
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)

    def test_28_item_column_marks_scope(self):
        self.svc.ensure_scope(self.order["id"], self.ids, "测试")
        from v2.domains.schedule import scope_label as _sl   # noqa: F401
        for iid in self.ids:
            self.assertEqual(self.sched(iid)["scope"], SCOPE_WHOLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)

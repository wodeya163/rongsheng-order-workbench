#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单据维护回归（v2.2 · P0）：编辑 / 作废 / 删除 + 引用拦截 + 审计留痕

    python -m v2.tests.test_maintenance                       # 纯逻辑（零依赖可跑）
    D:\\Program Files\\Python313\\python.exe -m v2.tests.test_maintenance   # 含 Qt 交互

验收口径（改动必须同时满足）：
  · 链路锚点（单号/行号/来源/订单类型/状态）不可编辑
  · 跨实体引用存在时，作废与删除一律硬拦截并列出引用方
  · 订单实值 qty_ordered 不得改到小于已排量，数量守恒检查始终通过
  · 每次写操作都在 audit_log 留下 before/after 与操作人
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import models                                    # noqa: E402
from v2.core.db import Database                               # noqa: E402
from v2.core.errors import (NotFoundError, ReferencedError,   # noqa: E402
                            StateError, ValidationError)
from v2.core.integrity import check                           # noqa: E402
from v2.domains import (BidService, MaintenanceService,       # noqa: E402
                        OrderService, PartCodeService,
                        ProductionService)

try:
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox
    PYSIDE = True
except ImportError:
    PYSIDE = False


class MaintenanceFixture(unittest.TestCase):
    """建一套完整双链路数据，供各用例复用"""

    def setUp(self):
        self.db = Database(":memory:")
        self.m = MaintenanceService(self.db)
        self.b = BidService(self.db)
        self.c = PartCodeService(self.db)
        self.o = OrderService(self.db)
        self.p = ProductionService(self.db)

        self.bp = self.b.create_project("沈西#1炉干渣机改造", customer="国能沈西")
        self.bi = self.b.add_item(self.bp["id"], "耐磨弯头", qty=12,
                                  purchase_price=1000, quote_ratio=0.85)
        self.cp = self.c.create_project("询配码文件A")
        self.ci = self.c.add_item(self.cp["id"], "弯头", qty_reference=100)
        self.c.submit(self.cp["id"], submit_date="2026-11-06")
        self.order = self.o.create_from_code_project(
            self.cp["id"],
            [{"source_code_item_id": self.ci["id"], "qty_ordered": 30}],
            customer="用户甲")
        self.oi = self.o.list_items(self.order["id"])[0]
        self.o.confirm(self.order["id"])
        self.proj = self.p.create_project("排产A")
        self.link = self.p.link_order_item(self.proj["id"], self.oi["id"], 20)

    def tearDown(self):
        self.db.close()


class CoreMaintenanceTest(MaintenanceFixture):

    def test_01_edit_and_audit_trail(self):
        row = self.m.edit("bid_project", self.bp["id"],
                          {"project_name": "改名后项目", "owner": "张哥"}, "张哥")
        self.assertEqual(row["project_name"], "改名后项目")
        self.assertEqual(row["owner"], "张哥")
        trail = self.m.audit_trail("bid_project", self.bp["id"])
        self.assertEqual(trail[0]["action"], "edit")
        self.assertEqual(trail[0]["action_label"], "编辑")
        self.assertEqual(trail[0]["operator"], "张哥")
        self.assertIn("沈西#1炉干渣机改造", trail[0]["before_json"])
        self.assertIn("改名后项目", trail[0]["after_json"])
        # 无实质变更不制造审计噪音
        n = len(trail)
        self.m.edit("bid_project", self.bp["id"], {"owner": "张哥"})
        self.assertEqual(len(self.m.audit_trail("bid_project", self.bp["id"])), n)

    def test_02_anchor_fields_are_protected(self):
        cases = [
            ("bid_project", self.bp["id"], {"doc_no": "ZB-FAKE"}),
            ("bid_project", self.bp["id"], {"status": "中标"}),
            ("code_item", self.ci["id"], {"item_code": "XPM-FAKE"}),
            ("code_item", self.ci["id"], {"code_project_id": 999}),
            ("order", self.order["id"], {"order_type": "内部订单"}),
            ("order", self.order["id"], {"source_code_project_id": 999}),
            ("order_item", self.oi["id"], {"line_no": 9}),
            ("order_item", self.oi["id"], {"source_code_item_id": 999}),
        ]
        for entity, eid, bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.m.edit(entity, eid, bad)

    def test_03_void_is_terminal_and_audited(self):
        row = self.m.void("bid_project", self.bp["id"], reason="录错重开", operator="张哥")
        self.assertEqual(row["status"], models.VOID_STATUS)
        self.assertEqual(models.BidStatus.VOID, models.VOID_STATUS)
        with self.assertRaises(StateError):
            self.m.void("bid_project", self.bp["id"])
        with self.assertRaises(StateError):
            self.m.edit("bid_project", self.bp["id"], {"owner": "再改"})
        trail = self.m.audit_trail("bid_project", self.bp["id"])
        self.assertEqual(trail[0]["action_label"], "作废")
        self.assertIn("录错重开", trail[0]["after_json"])

    def test_04_detail_rows_cannot_be_voided(self):
        for entity, eid in (("bid_item", self.bi["id"]),
                            ("order_item", self.oi["id"]),
                            ("production_link", self.link["id"])):
            with self.subTest(entity=entity):
                with self.assertRaises(ValidationError):
                    self.m.void(entity, eid, reason="x")

    def test_05_refs_block_void_and_delete(self):
        # 询配码项目已被外部订单引用
        refs = self.m.references("code_project", self.cp["id"])
        self.assertEqual(refs[0]["label"], "外部订单")
        self.assertIn(self.order["doc_no"], refs[0]["samples"])
        for fn in (lambda: self.m.void("code_project", self.cp["id"]),
                   lambda: self.m.delete("code_project", self.cp["id"])):
            with self.assertRaises(ReferencedError) as ctx:
                fn()
            self.assertIn(self.order["doc_no"], str(ctx.exception))
        # 订单明细已被排产关联引用
        with self.assertRaises(ReferencedError):
            self.m.delete("order_item", self.oi["id"])
        # 订单自身经明细被排产引用（自定义 where 引用规则）
        with self.assertRaises(ReferencedError):
            self.m.delete("order", self.order["id"])
        self.assertTrue(check(self.db)["ok"])

    def test_06_order_real_qty_cannot_break_conservation(self):
        with self.assertRaises(ValidationError) as ctx:
            self.m.edit("order_item", self.oi["id"], {"qty_ordered": 5})
        self.assertIn("守恒", str(ctx.exception))
        row = self.m.edit("order_item", self.oi["id"],
                          {"qty_ordered": 25, "remark": "合法改量"})
        self.assertEqual(row["qty_ordered"], 25)
        self.assertEqual(self.o.available_qty(self.oi["id"]), 5)
        self.assertTrue(check(self.db)["ok"])

    def test_07_link_allocation_cannot_exceed_real_qty(self):
        with self.assertRaises(ValidationError):
            self.m.edit("production_link", self.link["id"], {"allocated_qty": 99})
        row = self.m.edit("production_link", self.link["id"], {"allocated_qty": 10})
        self.assertEqual(row["allocated_qty"], 10)
        self.assertTrue(check(self.db)["ok"])

    def test_08_virtual_qty_is_free_to_edit(self):
        # 虚值随意改，不参与守恒，不触发任何拦截
        row = self.m.edit("code_item", self.ci["id"],
                          {"qty_reference": 9999, "item_name": "弯头（改规格描述）"})
        self.assertEqual(row["qty_reference"], 9999)
        self.assertEqual(row["item_name"], "弯头（改规格描述）")
        self.assertTrue(check(self.db)["ok"])
        # 快照免疫：上游改名不影响已冻结的排产关联
        self.assertEqual(self.db.get_by_id(
            "production_links", self.link["id"])["item_name_snapshot"], "弯头")

    def test_09_bid_item_locked_after_internal_order(self):
        won = self.b.create_project("中标项目B")
        bi = self.b.add_item(won["id"], "斗式提升机", qty=8,
                             purchase_price=500, quote_ratio=0.8)
        self.b.mark_won(won["id"])
        self.o.create_internal_from_bid(won["id"])
        with self.assertRaises(ValidationError) as ctx:
            self.m.edit("bid_item", bi["id"], {"qty": 99})
        self.assertIn("内部订单", str(ctx.exception))
        # 描述类字段仍可改（快照已冻结，不影响排产）
        row = self.m.edit("bid_item", bi["id"], {"remark": "补充说明"})
        self.assertEqual(row["remark"], "补充说明")
        self.assertTrue(check(self.db)["ok"])

    def test_10_delete_cascades_children_with_audit(self):
        draft = self.b.create_project("录错的草稿")
        self.b.add_item(draft["id"], "废行1")
        self.b.add_item(draft["id"], "废行2")
        res = self.m.delete("bid_project", draft["id"], "张哥")
        self.assertEqual(res["deleted_children"], {"bid_items": 2})
        self.assertIsNone(self.db.get_by_id("bid_projects", draft["id"]))
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM bid_items WHERE bid_project_id = ?",
            (draft["id"],)), 0)
        trail = self.m.audit_trail("bid_project", draft["id"])
        kinds = [r["action"] for r in trail]
        self.assertEqual(kinds.count("delete_child"), 2)
        self.assertIn("delete", kinds)
        # 只校验本次删除动作产生的留痕；create 是既有行为（operator 取 owner，默认空）
        self.assertTrue(all(r["operator"] == "张哥" for r in trail
                            if r["action"] in ("delete", "delete_child")))

    def test_11_delete_order_refreshes_code_project_status(self):
        # 用独立的一组数据，避免受 fixture 里已排产订单的运行态拦截
        cp2 = self.c.create_project("询配码B")
        ci2 = self.c.add_item(cp2["id"], "法兰")
        self.c.submit(cp2["id"], submit_date="2026-11-06")
        o2 = self.o.create_from_code_project(
            cp2["id"], [{"source_code_item_id": ci2["id"], "qty_ordered": 7}])
        self.assertEqual(self.db.get_by_id(
            "code_projects", cp2["id"])["status"], models.CodeProjectStatus.ORDERED)
        self.m.delete("order", o2["id"])
        self.assertEqual(self.db.get_by_id(
            "code_projects", cp2["id"])["status"],
            models.CodeProjectStatus.SUBMITTED)
        self.assertTrue(check(self.db)["ok"])

    def test_12_running_status_cannot_be_deleted(self):
        won = self.b.create_project("已中标项目")
        self.b.mark_won(won["id"])
        with self.assertRaises(StateError):
            self.m.delete("bid_project", won["id"])
        # 订单此刻被排产关联引用 → 引用优先拦截
        with self.assertRaises(ReferencedError):
            self.m.delete("order", self.order["id"])
        # 解除引用后，运行态（已排产）拦截生效
        self.p.unlink(self.link["id"])
        with self.assertRaises(StateError):
            self.m.delete("order", self.order["id"])
        self.assertTrue(check(self.db)["ok"])

    def test_13_metadata_and_error_contract(self):
        defs = self.m.field_defs("code_item", self.ci["id"])
        self.assertEqual(next(f for f in defs if f["key"] == "item_name")["default"],
                         "弯头")
        self.assertIn(self.ci["item_code"],
                      self.m.describe("code_item", self.ci))
        with self.assertRaises(ValidationError):
            self.m.spec("不存在")
        with self.assertRaises(NotFoundError):
            self.m.edit("bid_project", 99999, {"owner": "x"})
        with self.assertRaises(ValidationError):
            self.m.edit("order_item", self.oi["id"], {"qty_ordered": "abc"})
        with self.assertRaises(ValidationError):
            self.m.edit("order_item", self.oi["id"], {"qty_ordered": -1})

    def test_14_audit_trail_filters_and_labels(self):
        self.m.edit("bid_project", self.bp["id"], {"owner": "张哥"})
        only = self.m.audit_trail("bid_project", self.bp["id"])
        self.assertTrue(all(r["entity"] == "bid_project" for r in only))
        self.assertTrue(all(r["entity_label"] == "招投标项目" for r in only))
        allrows = self.m.audit_trail(limit=500)
        self.assertGreaterEqual(len(allrows), len(only))
        self.assertLessEqual(len(self.m.audit_trail(limit=2)), 2)


@unittest.skipUnless(PYSIDE, "PySide6 未安装，跳过 Qt 交互用例")
class MaintenanceUiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from PySide6.QtGui import QFontDatabase
        from pathlib import Path
        for p in ("C:/Windows/Fonts/msyh.ttc",):
            if Path(p).is_file():
                QFontDatabase.addApplicationFont(p)
        from v2.ui import theme
        from v2 import demo_data
        from v2.ui.main_window_v2 import MainWindowV2
        theme.apply_theme(cls.app)
        cls.db = Database(":memory:")
        demo_data.seed(cls.db)
        cls.w = MainWindowV2(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.w.close()
        cls.db.close()

    @staticmethod
    def _auto_click(text="确定", delay=60, set_text=None):
        """在 exec() 启动的事件循环里自动点击模态框按钮（可先改一个文本框）"""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QLineEdit

        def _do():
            w = QApplication.activeModalWidget()
            if w is None:
                return
            if isinstance(w, QMessageBox):
                for b in w.buttons():
                    if b.text() == text:
                        b.click()
                        return
                w.accept()
                return
            if set_text:
                edits = [e for e in w.findChildren(QLineEdit) if e.isVisible()]
                if edits:
                    edits[0].setText(set_text)
            box = w.findChild(QDialogButtonBox)
            if box is not None:
                for b in box.buttons():
                    if b.objectName() == "Primary":
                        b.click()
                        return
            w.accept()
        QTimer.singleShot(delay, _do)

    def test_15_maintenance_actions_registered(self):
        self.assertEqual(self.w.action_tree.topLevelItemCount(), 6)
        # v3.3.0：新增「提醒(&R)」菜单组（查看临期订单 / 标记已发货 / 设置）
        self.assertEqual(len(self.w.menuBar().actions()), 8)
        last = self.w.action_tree.topLevelItem(5)
        labels = [last.child(i).text(0) for i in range(last.childCount())]
        self.assertEqual(labels, ["编辑记录", "作废单据",
                                  "删除记录", "审计留痕"])
        for name in ("_edit_selected", "_void_selected", "_delete_selected",
                     "_show_audit"):
            self.assertTrue(callable(getattr(self.w, name)))

    def test_16_focus_switches_between_main_and_detail_tables(self):
        self.w.switch_view("bids")
        self.w.tbl_bids.selectRow(0)
        t = self.w._pick_target(silent=True)
        self.assertEqual(t[0], "bid_project")
        self.w.tbl_bid_items.selectRow(0)
        t2 = self.w._pick_target(silent=True)
        self.assertEqual(t2[0], "bid_item")
        self.assertIn(t2[2], self.w.target_label.text())

    def test_17_edit_dialog_is_themed_and_prefilled(self):
        from PySide6.QtWidgets import QLabel
        from v2.ui import dialogs
        self.w.switch_view("bids")
        self.w.tbl_bids.selectRow(0)
        entity, eid, desc = self.w._pick_target(silent=True)
        d = dialogs.FormDialog(self.w, "编辑 · " + desc,
                               self.w.maint.field_defs(entity, eid),
                               subtitle="锚点不可改")
        self.assertEqual(d.objectName(), "ThemedDialog")
        self.assertEqual(d.header.objectName(), "DialogHeader")
        self.assertEqual(d.body.objectName(), "DialogBody")
        self.assertEqual(d.footer.objectName(), "DialogFooter")
        self.assertIn("锚点不可改",
                      " ".join(x.text() for x in d.header.findChildren(QLabel)))
        values = d.values()
        self.assertTrue(values["project_name"])
        self.assertNotIn("doc_no", values)
        d.close()

    def test_18_real_edit_action_writes_db_and_audit(self):
        self.w.switch_view("bids")
        self.w.tbl_bids.selectRow(0)
        entity, eid, _ = self.w._pick_target(silent=True)
        self._auto_click(set_text="界面改名验证")
        self.w._edit_selected()
        row = self.w.db.get_by_id("bid_projects", eid)
        self.assertEqual(row["project_name"], "界面改名验证")
        trail = self.w.maint.audit_trail("bid_project", eid, limit=5)
        self.assertIn("edit", [r["action"] for r in trail])
        # P1-9：界面动作不再写死「界面操作」，落当前操作人（Windows 登录名或 operator.txt）
        from v2.core import current_user
        self.assertEqual(trail[0]["operator"], current_user.name(self.w.db.data_dir))
        self.assertNotEqual(trail[0]["operator"], "界面操作")
        self.assertTrue(check(self.w.db)["ok"])

    def test_19_real_delete_action_is_confirmed_and_audited(self):
        from v2.domains import BidService
        row = BidService(self.w.db).create_project("待删除的录错草稿")
        self.w.refresh_all()
        self.w.switch_view("bids")
        from v2.ui.main_window_v2 import select_id
        select_id(self.w.tbl_bids, row["id"])
        self.w._mark_focus(self.w.tbl_bids)
        self._auto_click(text="确定")
        self.w._delete_selected()
        self.assertIsNone(self.w.db.get_by_id("bid_projects", row["id"]))
        trail = self.w.maint.audit_trail("bid_project", row["id"])
        self.assertIn("delete", [r["action"] for r in trail])
        self.assertTrue(check(self.w.db)["ok"])

    def test_20_void_action_blocks_referenced_document(self):
        self.w.switch_view("codes")
        self.w.tbl_codes.selectRow(0)
        entity, eid, _ = self.w._pick_target(silent=True)
        refs = self.w.maint.references(entity, eid)
        if refs:
            # 被引用时不弹确认框，只给提示；提示框由 QTimer 自动关闭，数据必须原封不动
            status = self.w.db.get_by_id("code_projects", eid)["status"]
            self._auto_click()
            self.w._void_selected()
            self.assertEqual(self.w.db.get_by_id("code_projects", eid)["status"],
                             status)
        else:
            self.skipTest("演示数据里该询配码项目暂无订单引用")


if __name__ == "__main__":
    unittest.main(verbosity=2)

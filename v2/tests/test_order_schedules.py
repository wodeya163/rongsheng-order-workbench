#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单排产 v2.4 回归（2026-11-07）

覆盖：
  · 新 DDL / 老库升级、一条明细一张排产单的幂等性
  · 建单自动建生产项目 + production_links，数量守恒不变
  ★ 所有物资都有订单排产单且实值排满 → 订单状态自动「已排产」；撤销后回退
  · 图片 / 图纸导入、审批 .docx 生成、本机 Word 启动回落
  · 取消「新建排产项目」、右键菜单、对话框三大按钮、结果表新列
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import integrity, models, worddoc                     # noqa: E402
from v2.core.db import Database                                    # noqa: E402
from v2.domains import OrderService, OrderScheduleService           # noqa: E402
from v2.domains.schedule import (KIND_IMAGE, KIND_DRAWING, KIND_APPROVAL,
                                 IMAGE_FILTER, DRAWING_FILTER)      # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QPushButton
    PYSIDE = True
except ImportError:
    PYSIDE = False


class ScheduleFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v24_sched_")
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = OrderScheduleService(self.db, self.tmp)
        self.order = self.orders.create_order(
            customer="华东电建", project_no="SO-2026-001",
            erp_order_no="4507723903", required_date="2026-12-01",
            status=models.OrderStatus.CONFIRMED)
        self.i1 = self.orders.add_item(
            self.order["id"], item_name="耐磨弯头", item_code="SP-250",
            production_name="90度内衬耐磨弯头", spec="DN250 S=8", unit="件",
            qty_ordered=10, unit_price=2100.0, param_desc="耐磨层≥4mm")
        self.i2 = self.orders.add_item(
            self.order["id"], item_name="U形波纹补偿器", item_code="SP-300-U",
            spec="DN300 70B125", unit="台", qty_ordered=5, unit_price=5200.0)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        return self.db.get_by_id("orders", self.order["id"])["status"]


class DDLTest(ScheduleFixture):

    def test_01_new_tables_exist(self):
        tables = {r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("order_schedules", tables)
        self.assertIn("order_schedule_files", tables)
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(order_schedules)")}
        for c in ("doc_no", "order_item_id", "project_id", "link_id",
                  "allocated_qty", "status_before", "project_created"):
            self.assertIn(c, cols)

    def test_02_old_db_auto_upgrade(self):
        """模拟老库：缺两张新表 + 缺 v2.3 的 project_no 列，重开后自动补齐且旧数据不丢"""
        path = Path(self.tmp) / "legacy.db"
        old = Database(str(path))
        old.execute("INSERT INTO orders (doc_no, created_at, updated_at) "
                    "VALUES ('OLD-1', '2025-01-01', '2025-01-01')")
        old.execute("DROP TABLE order_schedule_files")
        old.execute("DROP TABLE order_schedules")
        old.execute("DROP INDEX IF EXISTS idx_o_project_no")
        old.execute("ALTER TABLE orders DROP COLUMN project_no")
        old.close()
        db = Database(str(path))
        cols = {r["name"] for r in db.query("PRAGMA table_info(orders)")}
        self.assertIn("project_no", cols)
        self.assertIn("item_qty_total", cols)
        self.assertEqual(db.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='order_schedules'"), 1)
        self.assertEqual(db.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='order_schedule_files'"), 1)
        self.assertEqual(db.scalar(
            "SELECT doc_no FROM orders WHERE doc_no='OLD-1'"), "OLD-1")
        db.close()


class EnsureTest(ScheduleFixture):

    def test_03_ensure_creates_schedule_project_link(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        self.assertTrue(s["doc_no"].startswith("DDPS-"))
        self.assertEqual(s["status_before"], models.OrderStatus.CONFIRMED)
        self.assertEqual(s["allocated_qty"], 10.0)
        self.assertEqual(s["project_created"], 1)
        link = self.db.get_by_id("production_links", s["link_id"])
        self.assertEqual(link["order_item_id"], self.i1["id"])
        self.assertEqual(link["allocated_qty"], 10.0)
        self.assertEqual(self.orders.available_qty(self.i1["id"]), 0.0)
        self.assertTrue(integrity.check(self.db)["ok"])

    def test_04_ensure_is_idempotent(self):
        a = self.svc.ensure(self.i1["id"], "测试")
        n_proj = self.db.scalar("SELECT COUNT(*) FROM production_projects")
        n_link = self.db.scalar("SELECT COUNT(*) FROM production_links")
        b = self.svc.ensure(self.i1["id"], "测试")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM production_projects"), n_proj)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM production_links"), n_link)

    def test_05_partial_schedule_keeps_status(self):
        self.svc.ensure(self.i1["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.CONFIRMED)
        s = self.svc.summary(self.order["id"])
        self.assertEqual((s["item_count"], s["scheduled_count"]), (2, 1))
        self.assertFalse(s["all_covered"])

    def test_06_all_items_scheduled_auto_status(self):
        self.svc.ensure(self.i1["id"], "测试")
        self.svc.ensure(self.i2["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        actions = [r["action"] for r in self.db.query(
            "SELECT action FROM audit_log WHERE entity='order' ORDER BY id")]
        self.assertIn("auto_scheduled", actions)
        self.assertTrue(integrity.check(self.db)["ok"])

    def test_07_delete_schedule_restores_status(self):
        a = self.svc.ensure(self.i1["id"], "测试")
        b = self.svc.ensure(self.i2["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        self.svc.delete(b["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.CONFIRMED)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM order_schedules"), 1)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM production_links"), 1)
        self.assertTrue(integrity.check(self.db)["ok"])

    def test_08_draft_order_needs_confirm(self):
        from v2.core.errors import StateError
        o = self.orders.create_order(status=models.OrderStatus.DRAFT)
        i = self.orders.add_item(o["id"], item_name="法兰", qty_ordered=2)
        with self.assertRaises(StateError):
            self.svc.ensure(i["id"], "测试")

    def test_09_closed_order_cannot_schedule(self):
        from v2.core.errors import StateError
        with self.db.tx():
            self.db.update("orders", {"status": models.OrderStatus.CLOSED},
                           "id = ?", (self.order["id"],))
        with self.assertRaises(StateError):
            self.svc.ensure(self.i1["id"], "测试")

    def test_10_real_status_wait_ship_is_covered(self):
        """真实销售订单状态「待发货」：可建单、可自动置已排产、撤销后回到待发货"""
        with self.db.tx():
            self.db.update("orders", {"status": models.OrderStatus.WAIT_SHIP},
                           "id = ?", (self.order["id"],))
        a = self.svc.ensure(self.i1["id"], "测试")
        self.assertEqual(a["status_before"], models.OrderStatus.WAIT_SHIP)
        b = self.svc.ensure(self.i2["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        self.svc.delete(b["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.WAIT_SHIP)

    def test_11_set_qty_updates_link_and_status(self):
        a = self.svc.ensure(self.i1["id"], "测试")
        b = self.svc.ensure(self.i2["id"], "测试")
        self.svc.set_allocated_qty(a["id"], 6.0, "测试")
        self.assertEqual(self.orders.available_qty(self.i1["id"]), 4.0)
        self.assertEqual(self.status(), models.OrderStatus.CONFIRMED)
        self.svc.set_allocated_qty(a["id"], 10.0, "测试")
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        self.assertTrue(integrity.check(self.db)["ok"])

    def test_12_demo_data_no_status_regression(self):
        """演示数据订单本就是「已排产」：补建排产单不得把状态倒退回已确认"""
        self.db.close()
        self.db = Database(":memory:")
        from v2 import demo_data
        demo_data.seed(self.db)
        svc = OrderScheduleService(self.db, self.tmp)
        o = self.db.query(
            "SELECT * FROM orders WHERE order_type='外部订单' ORDER BY id")[0]
        before = o["status"]
        for i in self.db.query("SELECT id FROM order_items WHERE order_id=?", (o["id"],)):
            svc.ensure(i["id"], "测试")
        self.assertEqual(self.db.get_by_id("orders", o["id"])["status"], before)
        self.assertTrue(svc.summary(o["id"])["all_covered"])
        self.assertTrue(integrity.check(self.db)["ok"])


class AttachmentTest(ScheduleFixture):

    def test_13_import_image_and_drawing(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        pic = Path(self.tmp) / "磨损照片.png"
        dwg = Path(self.tmp) / "弯头图纸.dwg"
        pic.write_bytes(b"PNGDATA")
        dwg.write_bytes(b"DWGDATA" * 3)
        rows = self.svc.add_files(s["id"], KIND_IMAGE, [str(pic)], "测试")
        rows += self.svc.add_files(s["id"], KIND_DRAWING, [str(dwg)], "测试")
        self.assertEqual([r["kind"] for r in rows], [KIND_IMAGE, KIND_DRAWING])
        # P0-3：库里只存相对 data/ 的路径，物理文件用 to_abs() 换算后可打开
        self.assertTrue(all(not Path(r["file_path"]).is_absolute() for r in rows))
        self.assertTrue(all(r["file_path"].startswith("订单排产附件/") for r in rows))
        self.assertTrue(all(self.svc.to_abs(r["file_path"]).is_file() for r in rows))
        self.assertEqual(len(self.svc.files(s["id"])), 2)
        self.assertIn("图片", IMAGE_FILTER)
        self.assertIn("dwg", DRAWING_FILTER)

    def test_13b_attachment_paths_survive_folder_move(self):
        """整体挪动数据目录后，附件仍可按相对路径打开（P0-3 核心验收）"""
        s = self.svc.ensure(self.i1["id"], "测试")
        p = Path(self.tmp) / "照片.png"
        p.write_bytes(b"PNG")
        self.svc.add_files(s["id"], KIND_IMAGE, [str(p)], "测试")
        new_root = Path(self.tmp).parent / (Path(self.tmp).name + "_moved")
        shutil.copytree(self.tmp, new_root)
        try:
            moved = OrderScheduleService(self.db, str(new_root))
            f = moved.files(s["id"])[0]
            self.assertTrue(moved.to_abs(f["file_path"]).is_file())
            self.assertEqual(moved.missing_files(), [])
            # 旧根目录已不存在同名文件时，原根仍能解析（路径未被写死）
            self.assertIn("订单排产附件", f["file_path"])
        finally:
            shutil.rmtree(new_root, ignore_errors=True)

    def test_13c_missing_files_and_repair(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        p = Path(self.tmp) / "丢失.png"
        p.write_bytes(b"X")
        self.svc.add_files(s["id"], KIND_IMAGE, [str(p)], "测试")
        f = self.svc.files(s["id"])[0]
        self.svc.to_abs(f["file_path"]).unlink()
        missing = self.svc.missing_files()
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["file_name"], "丢失.png")
        # 历史绝对路径 → 相对路径（幂等）
        abs_path = str(self.svc.to_abs(f["file_path"]))
        self.db.execute("UPDATE order_schedule_files SET file_path = ? WHERE id = ?",
                        (abs_path, f["id"]))
        self.assertGreaterEqual(self.svc.repair_paths(), 1)
        self.assertEqual(self.svc.repair_paths(), 0)

    def test_14_duplicate_name_is_not_overwritten(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        p = Path(self.tmp) / "同一名字.jpg"
        p.write_bytes(b"A")
        self.svc.add_files(s["id"], KIND_IMAGE, [str(p)], "测试")
        p.write_bytes(b"BB")
        self.svc.add_files(s["id"], KIND_IMAGE, [str(p)], "测试")
        names = [f["file_name"] for f in self.svc.files(s["id"])]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertIn("同一名字.jpg", names)

    def test_15_remove_file(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        p = Path(self.tmp) / "a.png"
        p.write_bytes(b"A")
        fid = self.svc.add_files(s["id"], KIND_IMAGE, [str(p)], "测试")[0]["id"]
        self.svc.remove_file(fid, "测试")
        self.assertEqual(self.svc.files(s["id"]), [])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM order_schedules"), 1)

    def test_16_build_approval_docx(self):
        s = self.svc.ensure(self.i1["id"], "测试")
        path = self.svc.build_approval(s["id"], "测试")
        self.assertTrue(Path(path).is_file())
        from docx import Document
        doc = Document(path)
        self.assertIn("生产排产审批单", doc.paragraphs[0].text)
        self.assertEqual(len(doc.tables), 4)
        kinds = [f["kind"] for f in self.svc.files(s["id"])]
        self.assertIn(KIND_APPROVAL, kinds)
        self.assertEqual(self.svc.get(s["id"])["status"],
                         models.ScheduleStatus.COMPILED)
        ctx = self.svc.approval_context(s["id"])
        self.assertEqual(ctx["item"]["item_name"], "耐磨弯头")
        self.assertEqual(ctx["erp_order_no"], "4507723903")

    def test_17_open_in_word_falls_back_to_startfile(self):
        """Word COM 不可用时，回落到系统文件关联且不抛异常"""
        s = self.svc.ensure(self.i1["id"], "测试")
        path = self.svc.build_approval(s["id"], "测试")
        import types
        called = []
        fake_com = mock.MagicMock()
        fake_com.DispatchEx.side_effect = RuntimeError("Word 未安装")
        pkg = types.ModuleType("win32com")
        pkg.client = fake_com
        with mock.patch.dict("sys.modules", {"win32com": pkg,
                                             "win32com.client": fake_com}), \
             mock.patch.object(worddoc.os, "startfile",
                               side_effect=lambda p: called.append(p), create=True):
            ok, msg = worddoc.open_in_word(path)
        self.assertTrue(ok)
        self.assertEqual(called, [str(Path(path).resolve())])
        self.assertIn("系统默认程序", msg)

    def test_18_missing_file_is_reported_not_raised(self):
        ok, msg = worddoc.open_in_word(str(Path(self.tmp) / "nope.docx"))
        self.assertFalse(ok)
        self.assertIn("不存在", msg)


@unittest.skipUnless(PYSIDE, "PySide6 未安装，跳过 Qt 用例")
class ScheduleUITest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)
        cls.tmp = tempfile.mkdtemp(prefix="v24_ui_")
        cls.db = Database(":memory:")
        from v2 import demo_data
        from v2.ui.main_window_v2 import MainWindowV2
        demo_data.seed(cls.db)
        cls.win = MainWindowV2(cls.db)
        cls.win.schedules = OrderScheduleService(cls.db, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_19_old_new_project_action_is_removed(self):
        from v2.ui.main_window_v2 import ACTION_GROUPS
        labels = [a[0] for _, _, acts in ACTION_GROUPS for a in acts]
        self.assertNotIn("新建排产项目", labels)
        self.assertFalse(hasattr(self.win, "_new_project"))
        self.assertTrue(hasattr(self.win, "_order_schedule"))
        self.assertTrue(sum("订单排产" in x for x in labels) >= 2)

    def test_20_context_menu_enabled_on_both_order_tables(self):
        from PySide6.QtCore import Qt
        for table in (self.win.tbl_orders, self.win.tbl_order_items):
            self.assertEqual(table.contextMenuPolicy(),
                             Qt.ContextMenuPolicy.CustomContextMenu)

    def test_21_order_items_has_schedule_column(self):
        self.win.switch_view("orders")
        self.win.refresh_all()
        headers = [self.win.tbl_order_items.horizontalHeaderItem(i).text()
                   for i in range(self.win.tbl_order_items.columnCount())]
        self.assertEqual(headers[-1], "订单排产单")

    def test_22_dialog_has_two_action_buttons_and_themed_shell(self):
        """v3.1.2：导入图片 / 导入图纸两个按钮已删除，动作收敛为两个"""
        from v2.ui import dialogs
        from v2.ui.dialogs import ThemedDialog
        item = self.db.query("SELECT id FROM order_items ORDER BY id")[0]["id"]
        s = self.win.schedules.ensure(item, "测试")
        d = dialogs.OrderScheduleDialog(self.win, self.win.schedules, s["id"])
        self.assertIsInstance(d, ThemedDialog)
        self.assertEqual(d.objectName(), "ThemedDialog")
        texts = [b.text() for b in d.findChildren(QPushButton)]
        self.assertIn("新建生产排产审批", texts)
        self.assertIn("导入生产排产审批", texts)
        self.assertNotIn("导入图片", texts)
        self.assertNotIn("导入图纸", texts)
        self.assertFalse(any("启动本机 Word" in t for t in texts))
        self.assertEqual(d.table.columnCount(), 4)
        self.assertIn("全部物资", d.hint.text())
        d.close()

    def test_23_dialog_save_persists_remark(self):
        from v2.ui import dialogs
        item = self.db.query("SELECT id FROM order_items ORDER BY id")[0]["id"]
        s = self.win.schedules.ensure(item, "测试")
        d = dialogs.OrderScheduleDialog(self.win, self.win.schedules, s["id"])
        d.remark.setPlainText("需附材质证明书")
        self.assertTrue(d._save())
        self.assertEqual(self.win.schedules.get(s["id"])["remark"], "需附材质证明书")
        d.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

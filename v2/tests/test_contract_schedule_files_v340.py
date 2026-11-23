#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.0 回归（2026-11-08 · 用户第 14 轮）

需求 1：选中订单右键「排产文件」→ 查看 / 编辑 / 更改该单全部排产文件
需求 2：生产排产操作模块新增「采购合同管理」→ 生成 / 导入 / 管理

重点锁定四条硬口径：
  ① 老库零迁移：新表靠 IF NOT EXISTS 幂等补建，schema_meta 仍 v6；
  ② 合并/整单共用文件：所有写操作同步整组兄弟登记，不留死链；
  ③ 合同物资清单不落第二份数量快照，订单与守恒链路零改动；
  ④ 导航 / 菜单 / 快捷栏 / 右键菜单四处处处可达，且沿用单一数据源。

    python -m pytest v2/tests/test_contract_schedule_files_v340.py -q
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                      # noqa: E402
from v2.core import models                                      # noqa: E402
from v2.core import tables as tbl                               # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database          # noqa: E402
from v2.domains import OrderScheduleService, OrderService        # noqa: E402
from v2.domains import contract as cmod                          # noqa: E402
from v2.domains.contract import (                                # noqa: E402
    CONTRACT_FILTER, KIND_ATTACH, KIND_MAIN, KIND_SCAN,
    PurchaseContractService, classify_contract_file,
)
from v2.domains import schedule as smod                          # noqa: E402

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QAction
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


def _file(folder: Path, name: str, payload: bytes = b"fake-content") -> str:
    p = Path(folder) / name
    p.write_bytes(payload)
    return str(p)


class Fixture(unittest.TestCase):
    """三物资订单（已确认），全部排产单按整单建立；附件落临时 data 根"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v340_"))
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.sched = OrderScheduleService(self.db, str(self.tmp))
        self.contracts = PurchaseContractService(self.db, str(self.tmp))
        self.order = self.orders.create_order(
            customer="国能沈西热电", project_no="SO-2026-340",
            status=models.OrderStatus.CONFIRMED)
        self.items = [
            self.orders.add_item(self.order["id"], item_name=name, spec=spec,
                                 unit="件", qty_ordered=qty, unit_price=price)
            for name, spec, qty, price in (
                ("耐磨弯头", "DN250", 10, 2100.0),
                ("波纹补偿器", "DN300", 5, 5200.0),
                ("落煤管", "DN400", 3, 800.0),
            )]
        self.item_ids = [i["id"] for i in self.items]
        self.sids = [s["id"] for s in
                     self.sched.ensure_scope(self.order["id"], self.item_ids,
                                             "测试", "整单")["schedules"]]

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── ① 数据层：零迁移补表 ───────────────────────────────────

class SchemaV340Test(unittest.TestCase):

    def test_01_fresh_db_has_contract_tables_and_version_still_v6(self):
        db = Database(":memory:")
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("purchase_contracts", names)
        self.assertIn("purchase_contract_files", names)
        self.assertEqual(db.schema_version(), CURRENT_SCHEMA_VERSION)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        db.close()

    def test_02_old_library_autocreate_without_data_change(self):
        """模拟 v6 老库：删掉新表 + 写一条业务数据 → 重启后自动补表且数据不变"""
        tmp = Path(tempfile.mkdtemp(prefix="v340_olddb_"))
        try:
            path = str(tmp / "erp.db")
            db = Database(path)
            db.insert("bid_projects", {
                "doc_no": "ZB-OLD-1", "project_name": "老库业务数据",
                "status": "草稿", "created_at": "2026-11-08T00:00:00",
                "updated_at": "2026-11-08T00:00:00"})
            db.execute("DROP TABLE purchase_contract_files")
            db.execute("DROP TABLE purchase_contracts")
            before = db.schema_version()
            db.close()

            db2 = Database(path)          # 再启动 = 走一遍 init_schema
            names = {r["name"] for r in db2.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("purchase_contracts", names)
            self.assertIn("purchase_contract_files", names)
            self.assertEqual(db2.schema_version(), before)   # 未写任何新版本记录
            self.assertEqual(db2.get_by_id("bid_projects", 1)["project_name"],
                             "老库业务数据")
            versions = db2.query("SELECT version FROM schema_meta")
            self.assertEqual(len(versions), 1)
            db2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_03_table_whitelist_matches_ddl(self):
        schema = Path(__file__).resolve().parents[1] / "core" / "schema.sql"
        self.assertEqual(tbl.missing_from_declaration(schema), set())
        self.assertEqual(tbl.undeclared_in_ddl(schema), set())
        self.assertIn("purchase_contracts", tbl.TABLES)
        self.assertIn("purchase_contract_files", tbl.TABLES)


# ── ② 需求 1：排产文件查看 / 编辑 / 更改 ───────────────────

class ScheduleFilesServiceTest(Fixture):

    def test_04_order_files_dedup_shared_files(self):
        paths = [_file(self.tmp, "整单审批.docx"),
                 _file(self.tmp, "现场照片.png")]
        res = self.sched.import_files(self.sids, paths, "测试")
        self.assertEqual(res["files"], 2)
        rows = self.sched.order_files(self.order["id"])
        self.assertEqual(len(rows), 2, "合并/整单共用文件只应显示一行")
        self.assertTrue(all(f["shared"] == len(self.sids) for f in rows))
        self.assertTrue(all(f["exists"] for f in rows))
        self.assertTrue(all(len(f["file_ids"]) == len(self.sids) for f in rows))
        # 物理只存一份
        physical = list((self.tmp / "订单排产附件").rglob("整单审批.docx"))
        self.assertEqual(len(physical), 1)

    def test_05_order_schedule_rows_have_items_and_counts(self):
        self.sched.import_files(self.sids, [_file(self.tmp, "审批.xlsx")], "测试")
        rows = self.sched.order_schedule_rows(self.order["id"])
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["line_no"] for r in rows], [1, 2, 3])
        self.assertTrue(all(r["file_count"] == 1 for r in rows))
        self.assertIn("耐磨弯头", [r["item_name"] for r in rows])

    def test_06_rename_syncs_all_sibling_registrations(self):
        self.sched.import_files(self.sids, [_file(self.tmp, "旧名称.docx")], "测试")
        f = self.sched.order_files(self.order["id"])[0]
        self.sched.rename_file(f["file_ids"][0], "新名称.docx", "测试")
        rows = self.sched.order_files(self.order["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_name"], "新名称.docx")
        self.assertTrue(rows[0]["exists"])
        for s in self.db.query("SELECT * FROM order_schedule_files"):
            self.assertEqual(s["file_name"], "新名称.docx")
            self.assertEqual(s["file_path"], rows[0]["file_path"])
        self.assertTrue((self.tmp / "订单排产附件" / self.order["doc_no"] /
                         self.sched.get(self.sids[0])["doc_no"] /
                         "新名称.docx").is_file())

    def test_07_rename_rejects_extension_change_and_duplicate(self):
        self.sched.import_files(
            self.sids, [_file(self.tmp, "甲.docx"), _file(self.tmp, "乙.docx")],
            "测试")
        rows = self.sched.order_files(self.order["id"])
        with self.assertRaises(Exception):
            self.sched.rename_file(rows[0]["file_ids"][0], "改成文本.txt")
        with self.assertRaises(Exception):
            self.sched.rename_file(rows[0]["file_ids"][0], "乙.docx")

    def test_08_replace_keeps_registry_and_updates_size(self):
        self.sched.import_files(self.sids, [_file(self.tmp, "审批.docx", b"old")],
                                "测试")
        f = self.sched.order_files(self.order["id"])[0]
        src = _file(self.tmp, "定稿.docx", b"new-content-much-longer")
        self.sched.replace_file(f["file_ids"][0], src, "测试")
        rows = self.sched.order_files(self.order["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_name"], "审批.docx", "文件名/登记关系不变")
        self.assertEqual(rows[0]["file_size"], len(b"new-content-much-longer"))
        self.assertTrue(all(r["file_size"] == rows[0]["file_size"]
                            for r in self.db.query(
                                "SELECT * FROM order_schedule_files")))
        self.assertEqual(Path(rows[0]["path"]).read_bytes(),
                         b"new-content-much-longer")

    def test_09_set_kind_and_remove_sync_siblings(self):
        self.sched.import_files(self.sids, [_file(self.tmp, "扫描.pdf")], "测试")
        f = self.sched.order_files(self.order["id"])[0]
        self.assertEqual(f["kind"], smod.KIND_APPROVAL)
        self.sched.set_file_kind(f["file_ids"][0], smod.KIND_OTHER, "测试")
        self.assertEqual({r["kind"] for r in
                          self.db.query("SELECT * FROM order_schedule_files")},
                         {smod.KIND_OTHER})
        f = self.sched.order_files(self.order["id"])[0]
        n = self.sched.remove_file(f["file_ids"][0], "测试")
        self.assertEqual(n, len(self.sids), "按 file_path 清理全部兄弟登记")
        self.assertEqual(self.sched.order_files(self.order["id"]), [])

    def test_10_add_files_for_order_and_guard_without_schedule(self):
        res = self.sched.add_files_for_order(
            self.order["id"], [_file(self.tmp, "补充资料.zip")], "测试")
        self.assertEqual(res["files"], 1)
        self.assertEqual(len(self.sched.order_files(self.order["id"])), 1)
        empty = self.orders.create_order(customer="无排产单", status="已确认")
        with self.assertRaises(Exception):
            self.sched.add_files_for_order(empty["id"],
                                           [_file(self.tmp, "x.png")], "测试")

    def test_11_actions_are_audited(self):
        self.sched.import_files(self.sids, [_file(self.tmp, "审批.docx")], "测试")
        f = self.sched.order_files(self.order["id"])[0]
        self.sched.rename_file(f["file_ids"][0], "更名.docx", "测试")
        actions = [r["action"] for r in self.db.query(
            "SELECT action FROM audit_log "
            "WHERE entity = 'order_schedule_file'")]
        for expected in ("import_shared", "rename"):
            self.assertIn(expected, actions)


# ── ③ 需求 2：采购合同服务层 ───────────────────────────────

class ContractServiceTest(Fixture):

    def _contract(self, **extra):
        spec = {"contract_name": "耐磨弯头采购合同", "supplier": "华北耐磨件厂",
                "buyer": self.order["customer"], "order_id": self.order["id"],
                "amount": 28000.0, "sign_date": "2026-11-08",
                "delivery_date": "2026-11-30", "status": "草稿",
                "payment_terms": "验收后 30 天电汇"}
        spec.update(extra)
        return self.contracts.create(spec, "测试")

    def test_12_create_numbers_and_audit(self):
        a = self._contract()
        b = self._contract()
        self.assertTrue(a["doc_no"].startswith("CG-"))
        self.assertNotEqual(a["doc_no"], b["doc_no"])
        self.assertEqual(a["order_id"], self.order["id"])
        self.assertEqual(
            self.contracts.list(order_id=self.order["id"])[0]["order_no"],
            self.order["doc_no"])
        self.assertTrue(self.db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE entity='purchase_contract' "
            "AND action='create'") >= 2)

    def test_13_update_status_and_clear_order_link(self):
        c = self._contract()
        self.contracts.update(c["id"], {"supplier": "新供应商", "amount": 31000.5,
                                        "buyer": "国能沈西热电厂"}, "测试")
        row = self.contracts.get(c["id"])
        self.assertEqual(row["supplier"], "新供应商")
        self.assertEqual(row["amount"], 31000.5)
        self.contracts.set_status(c["id"], "已签订", "测试")
        self.assertEqual(self.contracts.get(c["id"])["status"], "已签订")
        self.contracts.update(c["id"], {"order_id": None}, "测试")
        self.assertIsNone(self.contracts.get(c["id"])["order_id"])
        with self.assertRaises(Exception):
            self.contracts.set_status(c["id"], "未知状态")
        with self.assertRaises(Exception):
            self.contracts.update(c["id"], {"contract_name": ""})

    def test_14_generate_docx_uses_order_items(self):
        c = self._contract()
        res = self.contracts.generate(c["id"], "测试")
        self.assertTrue(res["created"])
        self.assertTrue(Path(res["path"]).is_file())
        self.assertIn(c["doc_no"], Path(res["path"]).name)
        from docx import Document
        doc = Document(res["path"])
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("采购物资清单", text)
        self.assertIn("合同金额与交付", text)
        cells = "\n".join(cell.text for t in doc.tables for row in t.rows
                          for cell in row.cells)
        self.assertIn("耐磨弯头", cells)
        self.assertIn("波纹补偿器", cells)
        self.assertIn(self.order["customer"], cells)
        # 幂等：第二次不重复造文件、不重复登记
        again = self.contracts.generate(c["id"], "测试")
        self.assertFalse(again["created"])
        self.assertEqual(len(self.contracts.files(c["id"])), 1)
        self.assertEqual(self.contracts.files(c["id"])[0]["kind"], KIND_MAIN)
        # 数量守恒链路零改动
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM production_links"), len(self.sids))

    def test_15_classify_and_import_any_format(self):
        self.assertIsInstance(CONTRACT_FILTER, str)
        self.assertIn("所有文件 (*.*)", CONTRACT_FILTER)
        self.assertEqual(classify_contract_file("合同.docx"), KIND_MAIN)
        self.assertEqual(classify_contract_file("清单.xlsx"), KIND_MAIN)
        self.assertEqual(classify_contract_file("盖章.pdf"), KIND_SCAN)
        self.assertEqual(classify_contract_file("扫描.png"), KIND_SCAN)
        self.assertEqual(classify_contract_file("附件.zip"), KIND_ATTACH)
        c = self._contract()
        paths = [_file(self.tmp, "双方合同.docx"),
                 _file(self.tmp, "盖章合同.pdf"),
                 _file(self.tmp, "技术协议.zip")]
        res = self.contracts.import_files(c["id"], paths, "测试")
        self.assertEqual(res["files"], 3)
        self.assertEqual(res["kinds"], {KIND_MAIN: 1, KIND_SCAN: 1,
                                        KIND_ATTACH: 1})
        files = self.contracts.files(c["id"])
        self.assertEqual(len(files), 3)
        self.assertTrue(all(f["exists"] for f in files))
        self.assertTrue(all(f["file_path"].startswith("采购合同/") for f in files))
        self.assertTrue(all(self.contracts.to_abs(f["file_path"]).is_file()
                            for f in files))

    def test_16_file_edit_rename_replace_kind_remove(self):
        c = self._contract()
        self.contracts.import_files(
            c["id"], [_file(self.tmp, "原合同.docx", b"old")], "测试")
        f = self.contracts.files(c["id"])[0]
        with self.assertRaises(Exception):
            self.contracts.rename_file(f["id"], "换成.pdf")
        self.contracts.rename_file(f["id"], "定稿合同.docx", "测试")
        f = self.contracts.files(c["id"])[0]
        self.assertEqual(f["file_name"], "定稿合同.docx")
        self.assertTrue(f["exists"])
        src = _file(self.tmp, "外部定稿.docx", b"brand-new-content")
        self.contracts.replace_file(f["id"], src, "测试")
        f = self.contracts.files(c["id"])[0]
        self.assertEqual(f["file_name"], "定稿合同.docx")
        self.assertEqual(Path(f["path"]).read_bytes(), b"brand-new-content")
        self.assertEqual(f["file_size"], len(b"brand-new-content"))
        self.contracts.set_file_kind(f["id"], KIND_ATTACH, "测试")
        self.assertEqual(self.contracts.files(c["id"])[0]["kind"], KIND_ATTACH)
        self.assertEqual(self.contracts.remove_file(f["id"], "测试"), 1)
        self.assertEqual(self.contracts.files(c["id"]), [])
        self.assertFalse(Path(f["path"]).exists())

    def test_17_list_filters_and_export_sheets(self):
        a = self._contract(supplier="甲厂", status="已签订")
        self._contract(contract_name="另一份合同", supplier="乙厂")
        self.assertEqual(len(self.contracts.list()), 2)
        self.assertEqual(len(self.contracts.list(keyword="甲厂")), 1)
        self.assertEqual(len(self.contracts.list(status="已签订")), 1)
        self.assertEqual(self.contracts.list()[0]["id"], a["id"] + 1)
        sheets = self.contracts.export_sheets()
        self.assertIn("采购合同台账", sheets)
        self.assertIn("合同附件", sheets)
        self.assertEqual(len(sheets["采购合同台账"][1]), 2)

    def test_18_delete_cleans_files_and_keeps_order(self):
        c = self._contract()
        self.contracts.import_files(c["id"], [_file(self.tmp, "合同.pdf")], "测试")
        path = Path(self.contracts.files(c["id"])[0]["path"])
        res = self.contracts.delete(c["id"], "测试")
        self.assertEqual(res["files"], 1)
        self.assertIsNone(self.contracts.get(c["id"]))
        self.assertEqual(self.contracts.files(c["id"]), [])
        self.assertFalse(path.exists())
        self.assertIsNotNone(self.db.get_by_id("orders", self.order["id"]))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM order_items"), 3)


# ── ④ UI：右键菜单 / 导航 / 管理窗口 ───────────────────────

@unittest.skipUnless(PYSIDE, "未安装 PySide6")
class UiV340Test(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_19_schedule_files_dialog_shows_tables(self):
        from v2.ui.schedule_files_dialog import ScheduleFilesDialog
        self.sched.import_files(
            self.sids, [_file(self.tmp, "整单审批.docx"),
                        _file(self.tmp, "图纸.dwg")], "测试")
        d = ScheduleFilesDialog(None, self.sched, self.order["id"])
        self.assertEqual(d.sched_table.rowCount(), 3)
        self.assertEqual(d.file_table.rowCount(), 2)
        self.assertIn("3 张订单排产单", d.hint.text())
        self.assertIn("仅查看", d.summary_text())
        d.close()

    def test_20_right_click_menu_has_schedule_files_action(self):
        from v2.ui.main_window_v2 import MainWindowV2
        from v2.ui.panels import order_actions
        w = MainWindowV2(self.db)
        w.switch_view("orders")
        w.refresh_all()

        class FakeMenu:
            def __init__(self):
                self.actions = []

            def addAction(self, a):
                if isinstance(a, str):
                    a = QAction(a, None)
                self.actions.append(a)
                return a

            def addSeparator(self):
                pass

            def addMenu(self, _title):
                return FakeMenu()

            def exec(self, _pos):
                return None

        fake = FakeMenu()
        idx = w.tbl_orders.model().index(0, 0)
        with mock.patch.object(order_actions, "QMenu", return_value=fake), \
             mock.patch.object(w.tbl_orders, "indexAt", return_value=idx):
            w._orders_context_menu(QPoint(3, 3))
        texts = [a.text() for a in fake.actions]
        self.assertTrue(any("排产文件" in t for t in texts), texts)
        w.close()

    def test_21_contract_dialog_rows_and_filters(self):
        from v2.ui.contract_dialog import PurchaseContractDialog
        c = self.contracts.create({
            "contract_name": "窗口测试合同", "supplier": "甲厂",
            "order_id": self.order["id"], "amount": 999.0}, "测试")
        self.contracts.generate(c["id"], "测试")
        d = PurchaseContractDialog(None, self.contracts)
        self.assertEqual(d.contract_table.rowCount(), 1)
        self.assertEqual(d.file_table.rowCount(), 1)
        d.keyword.setText("不存在的供方")
        d._reload_contracts()
        self.assertEqual(d.contract_table.rowCount(), 0)
        d._reset_filter()
        self.assertEqual(d.contract_table.rowCount(), 1)
        d.close()

    def test_22_nav_menu_primary_actions_and_version(self):
        from v2.ui.main_window_v2 import MainWindowV2
        from v2.ui.panels.common import ACTION_GROUPS, PRIMARY_ACTIONS, find_action
        self.assertEqual(__version__, "3.5.4")
        labels = [a[0] for _, _, acts in ACTION_GROUPS for a in acts]
        for name in ("排产文件", "采购合同管理", "生成采购合同", "导入采购合同"):
            self.assertIn(name, labels)
        self.assertIn("_view_schedule_files",
                      [fn for fn, _ in PRIMARY_ACTIONS["orders"]])
        self.assertIn("_purchase_contracts",
                      [fn for fn, _ in PRIMARY_ACTIONS["production"]])
        for fn in ("_purchase_contracts", "_generate_purchase_contract",
                   "_import_purchase_contract", "_view_schedule_files"):
            self.assertTrue(find_action(fn))
        w = MainWindowV2(self.db)
        for fn in ("_purchase_contracts", "_generate_purchase_contract",
                   "_import_purchase_contract", "_view_schedule_files"):
            self.assertTrue(hasattr(w, fn), fn)
        prod_menu = [m.text() for m in w.menuBar().actions()]
        self.assertEqual(len(prod_menu), 8)
        w.close()

    def test_23_main_window_stays_under_architecture_guard(self):
        path = Path(__file__).resolve().parents[1] / "ui" / "main_window_v2.py"
        self.assertLess(len(path.read_text(encoding="utf-8").splitlines()), 900)

    def test_24_quick_generate_action_end_to_end(self):
        """选中订单 → 生成采购合同：自动建草稿台账 + Word 文档 + 聚焦打开管理窗口"""
        from v2.ui.main_window_v2 import MainWindowV2
        from v2.ui.contract_dialog import PurchaseContractDialog
        w = MainWindowV2(self.db)
        w.contracts = self.contracts      # 内存库默认数据根是项目 data/，测试统一到临时目录
        w.switch_view("orders")
        w.refresh_all()
        w.tbl_orders.selectRow(0)
        with mock.patch.object(PurchaseContractDialog, "exec", return_value=0):
            text = w._generate_purchase_contract()
        rows = self.contracts.list(order_id=self.order["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["buyer"], self.order["customer"])
        fresh_order = self.db.get_by_id("orders", self.order["id"])
        # v3.4.6 / v3.5.0 口径：合同金额只认合同文档，草稿金额留 0，
        # 绝不自动带出订单价税合计（否则会污染采购成本与利润率）
        self.assertEqual(rows[0]["amount"], 0.0)
        self.assertNotEqual(fresh_order["amount_total"], rows[0]["amount"])
        self.assertEqual(rows[0]["amount_source"], "manual")
        self.assertEqual(rows[0]["status"], "草稿")
        files = self.contracts.files(rows[0]["id"])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["kind"], KIND_MAIN)
        self.assertTrue(files[0]["exists"])
        self.assertIn("新生成", text)
        w.close()

    def test_25_quick_import_action_end_to_end(self):
        """选中订单 → 导入采购合同：已有草稿台账复用，不重复建合同"""
        from v2.ui.main_window_v2 import MainWindowV2
        from v2.ui.contract_dialog import PurchaseContractDialog
        from v2.ui import theme
        w = MainWindowV2(self.db)
        w.contracts = self.contracts      # 同上：避免写到项目 data/
        w.switch_view("orders")
        w.refresh_all()
        w.tbl_orders.selectRow(0)
        paths = [_file(self.tmp, "双方已签合同.docx"),
                 _file(self.tmp, "盖章扫描.pdf")]
        with mock.patch.object(theme, "pick_paths", return_value=paths), \
             mock.patch.object(PurchaseContractDialog, "exec", return_value=0):
            text = w._import_purchase_contract()
        rows = self.contracts.list(order_id=self.order["id"])
        self.assertEqual(len(rows), 1, "没有重复建草稿合同")
        self.assertEqual(len(self.contracts.files(rows[0]["id"])), 2)
        self.assertIn("已导入 2 个文件", text)
        w.close()


if __name__ == "__main__":
    unittest.main()

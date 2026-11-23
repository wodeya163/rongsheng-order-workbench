#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.5 回归（2026-11-11 · 用户第 20 轮）：采购合同「导入即签订」+ 往来单位智能输入

用户口径：
  1. 导入采购合同时自动截取文件名「中文前面的字符」作合同编号、文件名作合同名称、
     状态自动变「已签订」；只有新建合同最初状态为「草稿」；
  2. 随合同项目增加，自动收集采购合同的需方 / 供方名称填充到新建输入框，
     输入字符即弹出相应名称。

锁定硬口径：
  ① schema_meta 仍 v6、不建往来单位字典表、零迁移零改动真实库；
  ② 编号解析 = 第一个中文字符前的 ASCII 段（尾部 -- / _ / 空白清理），
     解析不到退回系统自动号；名称 = 文件名 stem；
  ③ 导入涉及的合同一律「已签订」（执行中 / 已完成不回退），草稿台账升格时
     目录与 file_path 同步迁移，不留死链、不留第二份空台账；
  ④ 候选从 purchase_contracts.buyer / supplier 现查（需方补 orders.customer），
     关键字包含匹配、大小写无关、可自由新输。

    python -m pytest v2/tests/test_contract_import_v345.py -q
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                       # noqa: E402
from v2.core import models                                       # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database           # noqa: E402
from v2.core.errors import ValidationError                        # noqa: E402
from v2.domains import OrderService                               # noqa: E402
from v2.domains.contract import (                                 # noqa: E402
    LATER_STATUSES, SIGNED_STATUSES, STATUS_DRAFT, STATUS_SIGNED,
    PurchaseContractService, contract_name_from_filename,
    doc_no_from_filename,
)

try:
    from PySide6.QtWidgets import QApplication, QWidget
    PYSIDE = True
except ImportError:                                              # pragma: no cover
    PYSIDE = False

_APP = None
SAMPLE = "CG20261103000221--国能浙江北仑第一发电有限公司-- 销售排产单-B.xlsx"


def _app():
    global _APP
    if _APP is None:
        from v2.ui import theme
        _APP = QApplication.instance() or QApplication([])
        theme.apply_theme(_APP)
    return _APP


class Owner(QWidget):
    """模拟带 db 的表单宿主"""

    def __init__(self, db):
        super().__init__()
        self.db = db


class ContractImport345Test(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v345_"))
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.order = self.orders.create_order(
            customer="国能浙江北仑第一发电有限公司", project_no="SO-345-001",
            status=models.OrderStatus.CONFIRMED)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name: str) -> str:
        p = self.src / name
        p.write_bytes(b"contract-content" * 8)
        return str(p)

    def _rows(self):
        return self.svc.list(order_id=self.order["id"])

    # ── ① 版本 / 零迁移 ────────────────────────────────────

    def test_01_version_and_schema_unchanged(self):
        self.assertEqual(__version__, "3.5.4")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        self.assertEqual(self.db.schema_version(), 7)
        tables = [r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("purchase_contracts", tables)
        self.assertNotIn("party_dictionary", tables)   # 不建往来单位字典表

    def test_02_status_constants(self):
        self.assertEqual(STATUS_DRAFT, "草稿")
        self.assertEqual(STATUS_SIGNED, "已签订")
        self.assertEqual(tuple(SIGNED_STATUSES), ("已签订", "执行中", "已完成"))
        self.assertEqual(tuple(LATER_STATUSES), ("执行中", "已完成"))

    # ── ② 文件名解析 ──────────────────────────────────────

    def test_03_doc_no_before_first_chinese(self):
        self.assertEqual(doc_no_from_filename(SAMPLE), "CG20261103000221")
        self.assertEqual(doc_no_from_filename(
            "CG2026-001_采购合同.docx"), "CG2026-001")
        self.assertEqual(doc_no_from_filename(
            "HT-2026 -- 北电电力合同.pdf"), "HT-2026")

    def test_04_doc_no_edge_cases(self):
        self.assertEqual(doc_no_from_filename("Contract_2026.pdf"),
                         "Contract_2026", "无中文时取整段编号")
        for name in ("双方已签合同.docx", "采购合同.docx", "——合同.pdf", "123.docx"):
            if name == "123.docx":
                self.assertEqual(doc_no_from_filename(name), "123")
            else:
                self.assertEqual(doc_no_from_filename(name), "",
                                 f"纯中文/占位开头不应硬塞进编号：{name}")
        self.assertEqual(doc_no_from_filename(
            str(Path("C:/tmp") / SAMPLE)), "CG20261103000221")
        self.assertLessEqual(len(doc_no_from_filename("A" * 200 + "中文.docx")), 64)

    def test_05_contract_name_is_filename_stem(self):
        self.assertEqual(contract_name_from_filename(SAMPLE),
                         "CG20261103000221--国能浙江北仑第一发电有限公司-- 销售排产单-B")
        self.assertEqual(contract_name_from_filename("盖章扫描.PDF"), "盖章扫描")

    # ── ③ 只有新建合同才是草稿 ────────────────────────────

    def test_06_create_defaults_to_draft(self):
        c = self.svc.create({"contract_name": "手工新建"}, "测试")
        self.assertEqual(c["status"], STATUS_DRAFT)
        self.assertTrue(c["doc_no"].startswith("CG-"))
        self.assertEqual(self.svc.create({"contract_name": "第二份"}, "测试")
                         ["status"], STATUS_DRAFT)

    def test_07_create_accepts_explicit_doc_no_and_rejects_duplicate(self):
        c = self.svc.create({"contract_name": "导入型",
                             "doc_no": "CG20261103000221",
                             "status": STATUS_SIGNED}, "测试")
        self.assertEqual(c["doc_no"], "CG20261103000221")
        self.assertEqual(c["status"], STATUS_SIGNED)
        with self.assertRaises(ValidationError):
            self.svc.create({"contract_name": "重复",
                             "doc_no": "CG20261103000221"}, "测试")

    def test_08_mark_signed_state_matrix(self):
        c = self.svc.create({"contract_name": "状态矩阵"}, "测试")
        self.assertEqual(self.svc.mark_signed(c["id"], "测试")["status"],
                         STATUS_SIGNED)
        for state in LATER_STATUSES:
            self.svc.set_status(c["id"], state, "测试")
            self.assertEqual(self.svc.mark_signed(c["id"], "测试")["status"],
                             state, "签订后的阶段不得回退")
        self.svc.set_status(c["id"], "作废", "测试")
        self.assertEqual(self.svc.mark_signed(c["id"], "测试")["status"],
                         STATUS_SIGNED, "作废合同重新导入文件应恢复已签订")

    # ── ④ 草稿升格时的目录 / 附件迁移 ─────────────────────

    def test_09_rename_doc_no_moves_files_and_paths(self):
        c = self.svc.create({"contract_name": "待升格合同"}, "测试")
        self.svc.generate(c["id"], "测试")
        old_dir = self.svc.contract_dir(c["doc_no"])
        self.assertTrue(old_dir.is_dir())
        new = self.svc.rename_doc_no(c["id"], "CG20261103000777", "测试")
        self.assertEqual(new["doc_no"], "CG20261103000777")
        files = self.svc.files(c["id"])
        self.assertEqual(len(files), 1)
        self.assertTrue(all(f["exists"] for f in files), "附件不能变死链")
        self.assertIn("采购合同/CG20261103000777/", files[0]["file_path"])
        self.assertFalse(old_dir.exists(), "旧编号目录应清空")
        other = self.svc.create({"contract_name": "占位合同"}, "测试")
        with self.assertRaises(ValidationError):
            self.svc.rename_doc_no(other["id"], "CG20261103000777", "测试")
        self.assertEqual(
            self.svc.rename_doc_no(c["id"], "CG20261103000777", "测试")["doc_no"],
            "CG20261103000777", "同号重命名应幂等")

    # ── ⑤ 导入即签订（服务层）─────────────────────────────

    def test_10_single_import_sets_no_name_status(self):
        p = self._file(SAMPLE)
        res = self.svc.import_contract_files(self.order["id"], [p], "测试")
        self.assertEqual(res["files"], 1)
        self.assertEqual(res["created"], 1)
        c = res["contracts"][0]
        self.assertEqual(c["doc_no"], "CG20261103000221")
        self.assertEqual(c["contract_name"], contract_name_from_filename(SAMPLE))
        self.assertEqual(c["status"], STATUS_SIGNED)
        self.assertEqual(c["buyer"], self.order["customer"])
        self.assertEqual(c["order_id"], self.order["id"])
        files = self.svc.files(c["id"])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["kind"], "合同正文")
        self.assertTrue(files[0]["exists"])
        self.assertIn("采购合同/CG20261103000221/", files[0]["file_path"])
        logs = self.db.query(
            "SELECT action FROM audit_log WHERE entity='purchase_contract' "
            "AND action='import_signed'")
        self.assertEqual(len(logs), 1)

    def test_11_same_no_files_share_one_contract(self):
        p1 = self._file(SAMPLE)
        p2 = self._file("CG20261103000221--国能浙江北仑--盖章扫描.pdf")
        res = self.svc.import_contract_files(
            self.order["id"], [p1, p2], "测试")
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(res["files"], 2)
        self.assertEqual(len(self.svc.files(res["contracts"][0]["id"])), 2)
        self.assertEqual(res["kinds"], {"合同正文": 1, "扫描件": 1})

    def test_12_different_no_files_create_separate_contracts(self):
        p1 = self._file(SAMPLE)
        p2 = self._file("CG20261103000999--厦门龙净环保--采购合同.docx")
        res = self.svc.import_contract_files(
            self.order["id"], [p1, p2], "测试")
        self.assertEqual(len(res["contracts"]), 2)
        self.assertEqual({c["doc_no"] for c in res["contracts"]},
                         {"CG20261103000221", "CG20261103000999"})
        self.assertTrue(all(c["status"] == STATUS_SIGNED
                            for c in res["contracts"]))
        self.assertEqual(len(self._rows()), 2)

    def test_13_existing_no_is_reused(self):
        c = self.svc.create({"contract_name": "已登记",
                             "doc_no": "CG20261103000221",
                             "status": "执行中"}, "测试")
        res = self.svc.import_contract_files(
            self.order["id"], [self._file(SAMPLE)], "测试")
        self.assertEqual(len(self.svc.list()), 1, "不得重复建档")
        self.assertEqual(res["contracts"][0]["id"], c["id"])
        self.assertEqual(res["contracts"][0]["status"], "执行中",
                         "执行中不得回退为已签订")
        self.assertEqual(res["contracts"][0]["contract_name"], "已登记",
                         "已签订后的台账名称不被文件名覆盖")

    def test_14_no_parseable_no_falls_back_to_auto_number(self):
        res = self.svc.import_contract_files(
            self.order["id"], [self._file("双方已签合同.docx"),
                               self._file("盖章扫描.pdf")], "测试")
        self.assertEqual(len(self._rows()), 1)
        c = res["contracts"][0]
        self.assertTrue(c["doc_no"].startswith("CG-"))
        self.assertEqual(c["contract_name"], "双方已签合同")
        self.assertEqual(c["status"], STATUS_SIGNED)
        self.assertEqual(len(self.svc.files(c["id"])), 2)

    def test_15_draft_contract_is_promoted_with_attachments(self):
        c = self.svc.create({"contract_name": "DD 采购合同",
                             "order_id": self.order["id"]}, "测试")
        self.svc.generate(c["id"], "测试")
        res = self.svc.import_contract_files(
            self.order["id"], [self._file(SAMPLE)], "测试",
            contract_id=c["id"])
        self.assertEqual(len(self._rows()), 1, "不留下第二份空台账")
        t = res["contracts"][0]
        self.assertEqual(t["id"], c["id"])
        self.assertEqual(t["doc_no"], "CG20261103000221")
        self.assertEqual(t["contract_name"],
                         contract_name_from_filename(SAMPLE))
        self.assertEqual(t["status"], STATUS_SIGNED)
        files = self.svc.files(t["id"])
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f["exists"] for f in files))
        self.assertTrue(all("CG20261103000221" in f["file_path"]
                            for f in files))

    def test_16_void_contract_becomes_signed_on_import(self):
        c = self.svc.create({"contract_name": "作废合同",
                             "doc_no": "CG20261103000221",
                             "status": "作废"}, "测试")
        res = self.svc.import_contract_files(
            self.order["id"], [self._file(SAMPLE)], "测试",
            contract_id=c["id"])
        self.assertEqual(res["contracts"][0]["status"], STATUS_SIGNED)

    def test_17_import_missing_file_raises(self):
        with self.assertRaises(ValidationError):
            self.svc.import_contract_files(
                self.order["id"], [str(self.src / "不存在.docx")], "测试")
        self.assertEqual(len(self._rows()), 0)

    def test_18_empty_import_is_noop(self):
        res = self.svc.import_contract_files(self.order["id"], [], "测试")
        self.assertEqual(res["files"], 0)
        self.assertEqual(res["contracts"], [])
        self.assertEqual(len(self._rows()), 0)


@unittest.skipUnless(PYSIDE, "PySide6 未安装")
class ContractImport345UITest(unittest.TestCase):
    """UI 层：候选收集 / 智能输入 / 表单 / 两个导入入口"""

    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="v345ui_"))
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.order = self.orders.create_order(
            customer="国能浙江北仑第一发电有限公司", project_no="SO-345-001",
            status=models.OrderStatus.CONFIRMED)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name: str) -> str:
        p = self.src / name
        p.write_bytes(b"contract-content" * 8)
        return str(p)

    def _rows(self):
        return self.svc.list(order_id=self.order["id"])

    # ── ⑥ 需方 / 供方候选 ─────────────────────────────────

    def test_19_collect_party_names(self):
        from v2.ui.partyfield import collect_party_names
        for supplier, buyer in (
                ("江苏某某机械有限公司", "国能沈西热电厂"),
                ("江苏某某机械有限公司", "国能沈西热电厂"),
                ("厦门龙净环保股份有限公司", "—"),
                ("北京电力设备总厂", "国能浙江北仑第一发电有限公司"),
                ("", "")):
            self.svc.create({"contract_name": "候选合同",
                             "supplier": supplier, "buyer": buyer}, "测试")
        sup = collect_party_names(self.db, "supplier")
        self.assertEqual(sup[0], "江苏某某机械有限公司", "按出现频次降序")
        self.assertIn("厦门龙净环保股份有限公司", sup)
        self.assertNotIn("—", sup)
        self.assertNotIn("", sup)
        buy = collect_party_names(self.db, "buyer")
        self.assertIn("国能沈西热电厂", buy)
        self.assertIn("国能浙江北仑第一发电有限公司", buy)
        self.assertIn(self.order["customer"], buy, "需方补订单客户名称")
        with self.assertRaises(ValueError):
            collect_party_names(self.db, "evil_column")

    def test_20_party_combo_contains_match_and_free_input(self):
        from v2.ui.partyfield import PartyComboBox
        self.svc.create({"contract_name": "A",
                         "supplier": "江苏某某机械有限公司",
                         "buyer": "国能沈西热电厂"}, "测试")
        self.svc.create({"contract_name": "B",
                         "supplier": "厦门龙净环保股份有限公司"}, "测试")
        w = PartyComboBox(db=self.db, column="supplier")
        self.assertEqual(len(w.names()), 2)
        w.lineEdit().setText("龙净")
        self.assertEqual(w._completer.completionPrefix(), "龙净")
        self.assertEqual(w._completer.completionCount(), 1)
        self.assertEqual(w.matching("龙净"), ["厦门龙净环保股份有限公司"])
        w.setText("全新供应商股份有限公司")
        self.assertEqual(w.text(), "全新供应商股份有限公司", "允许自由新输")
        w.clear()
        self.assertEqual(w.text(), "")
        self.assertEqual(len(w.names()), 2, "clear 不应清掉候选库")
        b = PartyComboBox(db=self.db, column="buyer", value="国能沈西热电厂")
        self.assertEqual(b.text(), "国能沈西热电厂")
        self.assertTrue(any("国能" in n for n in b.names()))
        self.assertEqual(b.matching("北仑"),
                         ["国能浙江北仑第一发电有限公司"])

    def test_21_form_dialog_party_value(self):
        from v2.ui.dialogs import FormDialog
        self.svc.create({"contract_name": "A",
                         "supplier": "江苏某某机械有限公司"}, "测试")
        self.owner = Owner(self.db)
        d = FormDialog(self.owner, "测试", [
            {"key": "supplier", "label": "供方", "type": "party",
             "party_column": "supplier", "default": ""},
            {"key": "buyer", "label": "需方", "type": "party",
             "party_column": "buyer", "default": "国能沈西热电厂"},
        ])
        d.widgets["supplier"][0].setText("新供方")
        self.assertEqual(d.values(),
                         {"supplier": "新供方", "buyer": "国能沈西热电厂"})
        d.close()

    def test_22_contract_form_uses_party_and_draft_default(self):
        from v2.ui import dialogs
        from v2.ui.contract_dialog import PurchaseContractDialog
        d = PurchaseContractDialog(None, self.svc)
        captured = {}

        def fake_ask(parent, title, fields, **kw):
            captured["title"] = title
            captured["fields"] = fields
            return None

        with mock.patch.object(dialogs, "ask", side_effect=fake_ask):
            d._form()
        fields = {f["key"]: f for f in captured["fields"]}
        self.assertEqual(captured["title"], "新建采购合同")
        self.assertEqual(fields["supplier"]["type"], "party")
        self.assertEqual(fields["supplier"]["party_column"], "supplier")
        self.assertEqual(fields["buyer"]["type"], "party")
        self.assertEqual(fields["buyer"]["party_column"], "buyer")
        self.assertEqual(fields["status"]["default"], STATUS_DRAFT)
        d.close()

    # ── ⑦ 两个导入入口 E2E ───────────────────────────────

    def test_23_main_window_quick_import_e2e(self):
        from v2.ui import theme
        from v2.ui.contract_dialog import PurchaseContractDialog
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        w.contracts = self.svc
        w.switch_view("orders")
        w.refresh_all()
        w.tbl_orders.selectRow(0)
        with mock.patch.object(theme, "pick_paths",
                               return_value=[self._file(SAMPLE)]), \
             mock.patch.object(PurchaseContractDialog, "exec", return_value=0):
            text = w._import_purchase_contract()
        self.assertIn("已导入 1 个文件", text)
        self.assertIn("CG20261103000221", text)
        self.assertIn("已签订", text)
        c = self._rows()[0]
        self.assertEqual(c["doc_no"], "CG20261103000221")
        self.assertEqual(c["status"], STATUS_SIGNED)
        self.assertEqual(c["contract_name"],
                         contract_name_from_filename(SAMPLE))
        w.close()

    def test_24_quick_import_legacy_filenames_keeps_one_contract(self):
        """旧行为兼容：全是中文文件名时仍归到一份台账、不重复建合同"""
        from v2.ui import theme
        from v2.ui.contract_dialog import PurchaseContractDialog
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        w.contracts = self.svc
        w.switch_view("orders")
        w.refresh_all()
        w.tbl_orders.selectRow(0)
        paths = [self._file("双方已签合同.docx"), self._file("盖章扫描.pdf")]
        with mock.patch.object(theme, "pick_paths", return_value=paths), \
             mock.patch.object(PurchaseContractDialog, "exec", return_value=0):
            text = w._import_purchase_contract()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.svc.files(rows[0]["id"])), 2)
        self.assertEqual(rows[0]["status"], STATUS_SIGNED)
        self.assertIn("已导入 2 个文件", text)
        w.close()

    def test_25_contract_dialog_import_e2e(self):
        from v2.ui import theme
        from v2.ui.contract_dialog import PurchaseContractDialog
        c = self.svc.create({"contract_name": "空白草稿",
                             "order_id": self.order["id"]}, "测试")
        d = PurchaseContractDialog(None, self.svc)
        d.focus_id = c["id"]
        d._reload_contracts()
        with mock.patch.object(theme, "pick_paths",
                               return_value=[self._file(SAMPLE)]):
            d._do_import()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["doc_no"], "CG20261103000221")
        self.assertEqual(rows[0]["status"], STATUS_SIGNED)
        self.assertIn("编号取文件名中文前的字符", d.hint.text())
        d.close()

    def test_26_order_view_shows_imported_contract_no(self):
        """导入后订单主表「采购合同编号」列立即显示（SIGNED_STATUSES 口径）"""
        from v2.ui.main_window_v2 import MainWindowV2
        w = MainWindowV2(self.db)
        w.contracts = self.svc
        w.switch_view("orders")
        w.refresh_all()
        headers = [w.tbl_orders.horizontalHeaderItem(i).text()
                   for i in range(w.tbl_orders.columnCount())]
        col = headers.index("采购合同编号")
        self.assertEqual(w.tbl_orders.item(0, col).text(), "—")
        self.svc.import_contract_files(
            self.order["id"], [self._file(SAMPLE)], "测试")
        w.refresh_all()
        self.assertEqual(w.tbl_orders.item(0, col).text(),
                         "CG20261103000221")
        w.close()


if __name__ == "__main__":
    unittest.main()

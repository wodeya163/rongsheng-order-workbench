#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
询配码明细 10 字段 + CSV/Excel 导入回归（v2.2 · 2026-11-06）

    python -m v2.tests.test_code_item_fields                        # 纯逻辑（零依赖）
    D:\\Program Files\\Python313\\python.exe -m v2.tests.test_code_item_fields  # 含 Qt 交互

验收口径：
  · 表头严格 10 列且顺序 = 询价物料名称/商品编码/物料编码/物料名称/单位/
    厂家价格(元)/物料具体情况/参考数量/备注/技术要求
  · 手动录入、导入模板、导出文件、结果表列、编辑白名单全部取自 core/fields.py
  · .xlsx / .csv / .tsv 均可导入；兼容 UTF-8 BOM / GBK、逗号/分号/制表符、表头别名
  · 坏行有逐行报告，批量导入单事务全成功或全回滚，并写审计留痕
  · 参考数量仍是虚值、厂家价格只是询价信息，二者都不参与守恒与订单快照
  · 老库自动补列，历史数据可读
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import excelio, fields                              # noqa: E402
from v2.core.db import Database                                  # noqa: E402
from v2.core.errors import ValidationError                       # noqa: E402
from v2.core.integrity import check                              # noqa: E402
from v2.domains import (MaintenanceService, OrderService,        # noqa: E402
                        PartCodeService, ProductionService)

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    PYSIDE = True
except ImportError:
    PYSIDE = False

EXPECTED_HEADERS = ["询价物料名称", "商品编码", "物料编码", "物料名称", "单位",
                    "厂家价格(元)", "物料具体情况", "参考数量", "备注", "技术要求"]


class FieldSpecFixture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.codes = PartCodeService(self.db)
        self.cp = self.codes.create_project("字段回归项目", customer="华东电建")
        self.tmp = tempfile.TemporaryDirectory(prefix="code_item_fields_")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db.close)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write_text(self, name, text, encoding="utf-8-sig"):
        p = self.path(name)
        Path(p).write_bytes(text.encode(encoding))
        return p


class CodeItemFieldSpecTest(FieldSpecFixture):

    def test_01_headers_are_exactly_the_10_agreed_columns(self):
        self.assertEqual(fields.headers(), EXPECTED_HEADERS)
        self.assertEqual(len(fields.CODE_ITEM_KEYS), 10)
        self.assertEqual(len(fields.form_fields()), 10)
        self.assertEqual(len(fields.sample_row()), 10)

    def test_02_ddl_has_all_new_columns_and_indexes(self):
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(code_items)")}
        self.assertTrue(set(fields.CODE_ITEM_KEYS) <= cols)
        for c in ("inquiry_item_name", "goods_code", "material_code",
                  "factory_price", "item_detail"):
            self.assertIn(c, cols)
        idx = {r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_ci_goods", idx)
        self.assertIn("idx_ci_material", idx)

    def test_03_add_item_writes_all_10_business_fields(self):
        row = self.codes.add_item(
            self.cp["id"], "耐磨弯头",
            inquiry_item_name="90度耐磨弯头", goods_code="SP-250-90",
            material_code="WL-003215", unit="件", factory_price=1850.5,
            item_detail="DN250 S=8", qty_reference=100,
            remark="虚值仅统计", tech_requirement="堆焊 ≥4mm",
            spec="90° DN250", material="20#")
        self.assertEqual(row["inquiry_item_name"], "90度耐磨弯头")
        self.assertEqual(row["goods_code"], "SP-250-90")
        self.assertEqual(row["material_code"], "WL-003215")
        self.assertEqual(row["item_name"], "耐磨弯头")
        self.assertEqual(row["factory_price"], 1850.5)
        self.assertEqual(row["qty_reference"], 100.0)
        self.assertEqual(row["tech_requirement"], "堆焊 ≥4mm")
        self.assertEqual(row["spec"], "90° DN250")     # 历史字段仍可写
        self.assertTrue(row["item_code"].startswith("XPM-"))

    def test_04_system_anchor_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.codes.add_item(self.cp["id"], "弯头", item_code="XPM-FAKE")
        with self.assertRaises(ValidationError):
            self.codes.add_item(self.cp["id"], "弯头", unknown_column=1)
        self.assertEqual(len(self.codes.list_items(self.cp["id"])), 0)

    def test_05_inquiry_name_falls_back_to_item_name(self):
        row = self.codes.add_item(self.cp["id"], "",
                                  inquiry_item_name="用户询价名")
        self.assertEqual(row["item_name"], "用户询价名")
        with self.assertRaises(ValidationError):
            self.codes.add_item(self.cp["id"], "")

    def test_06_numeric_fields_are_normalized(self):
        row = self.codes.add_item(self.cp["id"], "弯头",
                                  qty_reference="12.5", factory_price=1850)
        self.assertEqual(row["qty_reference"], 12.5)
        self.assertEqual(row["factory_price"], 1850.0)
        # 服务层只做 float() 容错，逗号千分位不是数字 → 必须报错，不允许脏数据落库
        with self.assertRaises(ValidationError):
            self.codes.add_item(self.cp["id"], "法兰", factory_price="1,850")
        self.assertEqual(len(self.codes.list_items(self.cp["id"])), 1)

    def test_06b_numeric_fields_normalization_via_import_parser(self):
        p = self.write_text(
            "numbers.csv",
            "物料名称,厂家价格(元),参考数量\n弯头,\"1,850.50\",100\n法兰,面议,5\n")
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(rep["items"][0]["factory_price"], 1850.5)
        self.assertEqual(rep["items"][1]["factory_price"], 0.0)
        self.assertTrue(any(i["level"] == "warn" for i in rep["issues"]))

    def test_07_template_roundtrip_csv(self):
        p = self.path("模板.csv")
        excelio.export_template(p, "code_item")
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(len(rep["items"]), 1)
        self.assertEqual(rep["matched"], EXPECTED_HEADERS)
        self.assertEqual(rep["items"][0]["item_name"], "耐磨弯头")
        self.assertEqual(rep["items"][0]["qty_reference"], 100.0)
        # 旧接口 map_template_rows 仍可用
        mapped = excelio.map_template_rows("code_item", excelio.import_rows(p))
        self.assertEqual(mapped[0]["qty_reference"], 100.0)
        result = self.codes.add_items_bulk(self.cp["id"], rep["items"], "张哥")
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.codes.statistics(self.cp["id"])
                         ["qty_reference_total_virtual"], 100.0)

    @unittest.skipUnless(excelio.HAS_OPENPYXL, "openpyxl 未安装")
    def test_08_template_roundtrip_xlsx(self):
        p = self.path("模板.xlsx")
        excelio.export_template(p, "code_item")
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(len(rep["items"]), 1)
        self.assertEqual(rep["items"][0]["goods_code"], "SP-250-90")
        self.assertEqual(rep["items"][0]["tech_requirement"][:2], "内壁")
        self.codes.add_items_bulk(self.cp["id"], rep["items"])
        self.assertEqual(len(self.codes.list_items(self.cp["id"])), 1)

    def test_09_gbk_csv_with_semicolon_and_aliases(self):
        p = self.write_text(
            "gbk.csv",
            ";".join(["询价名称", "商品编号", "物料号", "物资名称", "计量单位",
                      "厂家价格（元）", "物料情况", "数量(虚值)", "备注说明",
                      "技术条件"]) + "\n" +
            "弯头A;G1;M1;耐磨弯头;件;1,850.50;DN250;100;备;堆焊\n",
            encoding="gb18030")
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(len(rep["items"]), 1)
        self.assertEqual(rep["ignored"], [])
        row = rep["items"][0]
        self.assertEqual(row["inquiry_item_name"], "弯头A")
        self.assertEqual(row["goods_code"], "G1")
        self.assertEqual(row["material_code"], "M1")
        self.assertEqual(row["item_name"], "耐磨弯头")
        self.assertEqual(row["factory_price"], 1850.5)
        self.assertEqual(row["qty_reference"], 100.0)
        self.assertEqual(row["tech_requirement"], "堆焊")

    def test_10_tsv_is_supported(self):
        p = self.write_text("明细.tsv", "\t".join(EXPECTED_HEADERS) + "\n"
                            "询价A\tG\tM\t弯头\t件\t100\tDN200\t3\t无\t无\n")
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(len(rep["items"]), 1)
        self.assertEqual(rep["items"][0]["item_name"], "弯头")

    def test_11_bad_rows_are_reported_not_swallowed(self):
        p = self.write_text(
            "bad.csv",
            "询价物料名称,物料名称,参考数量,未知列\n"
            "A,弯头A,10,X\n"
            ", ,20,Y\n"                     # 两个名称都空 → 跳过
            "B,,abc,Z\n")                   # 名称兜底 + 数值警告
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(len(rep["items"]), 2)
        fatal = [i for i in rep["issues"] if i["fatal"]]
        warns = [i for i in rep["issues"] if not i["fatal"]]
        self.assertEqual(len(fatal), 1)
        self.assertEqual(fatal[0]["row"], 3)
        self.assertEqual(rep["items"][1]["item_name"], "B")
        self.assertEqual(rep["items"][1]["qty_reference"], 0.0)
        self.assertTrue(warns)
        self.assertEqual(rep["ignored"], ["未知列"])
        self.assertIn("第 3 行", fields.issue_summary(rep["issues"]))

    def test_12_unrecognized_headers_are_rejected_clearly(self):
        p = self.write_text("none.csv", "列A,列B\n1,2\n")
        rep = excelio.parse_code_item_file(p)
        self.assertEqual(rep["items"], [])
        self.assertTrue(rep["issues"][0]["fatal"])
        self.assertIn("物料名称", rep["issues"][0]["reason"])

    def test_13_bulk_import_is_atomic(self):
        good = {"item_name": "A", "inquiry_item_name": "a", "goods_code": "G",
                "material_code": "M", "unit": "件", "factory_price": 1.0,
                "item_detail": "", "qty_reference": 1.0, "remark": "",
                "tech_requirement": "", "spec": "", "material": "",
                "std_code": "", "drawing_no": ""}
        bad = dict(good, item_name="B", qty_reference="不是数字")
        with self.assertRaises(ValidationError):
            self.codes.add_items_bulk(self.cp["id"], [good, bad])
        self.assertEqual(len(self.codes.list_items(self.cp["id"])), 0)
        with self.assertRaises(ValidationError):
            self.codes.add_items_bulk(self.cp["id"], [good, dict(bad, item_code="FAKE")])
        self.assertEqual(len(self.codes.list_items(self.cp["id"])), 0)

    def test_14_bulk_import_writes_audit_trail(self):
        p = self.path("audit.csv")
        excelio.export_template(p, "code_item")
        rep = excelio.parse_code_item_file(p)
        self.codes.add_items_bulk(self.cp["id"], rep["items"], operator="张哥")
        trail = self.db.query(
            "SELECT * FROM audit_log WHERE action = 'import_code_items'")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["operator"], "张哥")
        self.assertIn(self.cp["doc_no"], trail[0]["after_json"])

    def test_15_virtual_qty_and_factory_price_never_affect_conservation(self):
        row = self.codes.add_item(
            self.cp["id"], "弯头", qty_reference=100, factory_price=9999.0,
            goods_code="G", material_code="M")
        self.codes.submit(self.cp["id"], submit_date="2026-11-06")
        orders = OrderService(self.db)
        order = orders.create_from_code_project(
            self.cp["id"],
            [{"source_code_item_id": row["id"], "qty_ordered": 30}],
            customer="用户甲")
        oi = orders.list_items(order["id"])[0]
        orders.confirm(order["id"])
        prod = ProductionService(self.db)
        proj = prod.create_project("排产A")
        link = prod.link_order_item(proj["id"], oi["id"], 20)
        self.assertEqual(link["qty_reference_snapshot"], 100.0)

        self.db.update("code_items",
                       {"qty_reference": 999, "factory_price": 1.0,
                        "goods_code": "G-NEW", "material_code": "M-NEW"},
                       "id = ?", (row["id"],))
        after = self.db.get_by_id("production_links", link["id"])
        self.assertEqual(after["qty_reference_snapshot"], 100.0,
                         "虚值快照必须免疫于源明细修改")
        self.assertEqual(self.codes.statistics(self.cp["id"])
                         ["qty_ordered_total_real"], 30.0)
        self.assertTrue(check(self.db)["ok"])

    def test_16_legacy_database_is_auto_upgraded(self):
        legacy = self.path("legacy.db")
        con = sqlite3.connect(legacy)
        con.executescript("""
            CREATE TABLE doc_sequence (prefix TEXT NOT NULL, period TEXT NOT NULL,
                next_val INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(prefix,period));
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT,
                entity_id INTEGER, action TEXT, before_json TEXT, after_json TEXT,
                operator TEXT, at TEXT);
            CREATE TABLE code_projects (id INTEGER PRIMARY KEY AUTOINCREMENT, doc_no TEXT,
                project_name TEXT, customer TEXT, status TEXT, submit_date TEXT,
                file_path TEXT, version TEXT, owner TEXT, remark TEXT,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE code_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_project_id INTEGER, line_no INTEGER, item_code TEXT, item_name TEXT,
                spec TEXT, material TEXT, std_code TEXT, drawing_no TEXT,
                tech_requirement TEXT, unit TEXT, qty_reference REAL, status TEXT,
                remark TEXT, created_at TEXT, updated_at TEXT);
            INSERT INTO code_projects (doc_no,project_name,status,created_at,updated_at)
                VALUES ('XPM-OLD-0001','历史项目','草稿','2026-01-01','2026-01-01');
            INSERT INTO code_items (code_project_id,line_no,item_code,item_name,
                qty_reference,status,created_at,updated_at)
                VALUES (1,1,'XPM-OLD-0001-01','老物资',10,'待下单','2026-01-01','2026-01-01');
        """)
        con.commit()
        con.close()
        db = Database(legacy)
        try:
            cols = {r["name"] for r in db.query("PRAGMA table_info(code_items)")}
            for c in ("inquiry_item_name", "goods_code", "material_code",
                      "factory_price", "item_detail"):
                self.assertIn(c, cols)
            old = db.get_by_id("code_items", 1)
            self.assertEqual(old["item_name"], "老物资")
            self.assertEqual(old["factory_price"], 0.0)
            svc = PartCodeService(db)
            new = svc.add_item(1, "新物资", goods_code="G9", factory_price=99)
            self.assertEqual(new["goods_code"], "G9")
            self.assertEqual(len(svc.list_items(1)), 2)
            self.assertTrue(check(db)["ok"])
        finally:
            db.close()

    def test_17_export_rows_follow_header_order_and_null_safe(self):
        rows = fields.export_rows([{k: None for k in fields.CODE_ITEM_KEYS}])
        self.assertEqual(len(rows[0]), 10)
        self.assertTrue(all(v == "" for v in rows[0]))
        row = self.codes.add_item(self.cp["id"], "弯头", goods_code="G",
                                  qty_reference=7)
        exported = fields.export_rows([row])[0]
        self.assertEqual(exported[1], "G")
        self.assertEqual(exported[3], "弯头")
        self.assertEqual(exported[7], 7.0)

    def test_18_maintenance_edit_whitelist_covers_new_fields_only(self):
        row = self.codes.add_item(self.cp["id"], "弯头", goods_code="G0",
                                  factory_price=10.0)
        m = MaintenanceService(self.db)
        defs = m.field_defs("code_item", row["id"])
        keys = {f["key"] for f in defs}
        self.assertTrue(set(fields.CODE_ITEM_KEYS) <= keys)
        self.assertNotIn("item_code", keys)
        self.assertNotIn("code_project_id", keys)
        edited = m.edit("code_item", row["id"], {
            "goods_code": "G1", "material_code": "M1",
            "factory_price": 88.5, "item_detail": "DN250",
            "inquiry_item_name": "询价名"}, "张哥")
        self.assertEqual(edited["goods_code"], "G1")
        self.assertEqual(edited["factory_price"], 88.5)
        with self.assertRaises(ValidationError):
            m.edit("code_item", row["id"], {"item_code": "XPM-FAKE"})
        trail = m.audit_trail("code_item", row["id"], limit=5)
        self.assertTrue(any(r["action"] == "edit" for r in trail))
        self.assertTrue(check(self.db)["ok"])


@unittest.skipUnless(PYSIDE, "PySide6 未安装，跳过 Qt 交互用例")
class CodeItemUiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from PySide6.QtGui import QFontDatabase
        for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
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

    def test_19_result_table_has_13_columns_in_agreed_order(self):
        self.w.switch_view("codes")
        self.w.tbl_codes.selectRow(0)
        self.app.processEvents()
        headers = [self.w.tbl_code_items.horizontalHeaderItem(i).text()
                   for i in range(self.w.tbl_code_items.columnCount())]
        self.assertEqual(headers, ["系统项号", *EXPECTED_HEADERS,
                                   "状态", "被下单次数"])
        self.assertEqual(self.w.tbl_code_items.item(0, 2).text(), "SP-250-90")

    def test_20_manual_entry_dialog_has_10_themed_fields(self):
        from v2.ui import dialogs
        fdefs = dialogs.code_item_fields()
        self.assertEqual([f["key"] for f in fdefs], fields.CODE_ITEM_KEYS)
        d = dialogs.FormDialog(self.w, "添加询配码明细", fdefs,
                               subtitle="10 个业务字段", columns=2)
        self.assertEqual(d.objectName(), "ThemedDialog")
        self.assertEqual(d.header.objectName(), "DialogHeader")
        self.assertEqual(len(d.widgets), 10)
        self.assertIn("factory_price", d.values())
        self.assertGreaterEqual(d.width(), 900)
        d.close()

    def test_21_import_actions_are_registered(self):
        group = self.w.action_tree.topLevelItem(1)
        labels = [group.child(i).text(0) for i in range(group.childCount())]
        self.assertIn("导入配码明细", labels)
        self.assertIn("下载导入模板", labels)
        self.assertTrue(callable(self.w._download_code_template))
        self.assertTrue(hasattr(__import__(
            "v2.ui.dialogs", fromlist=["ImportCodeItemsDialog"]),
            "ImportCodeItemsDialog"))

    def test_22_import_wizard_previews_and_writes_db(self):
        from PySide6.QtCore import QTimer
        from v2.ui import dialogs
        from v2.core import excelio as xio
        p = os.path.join(tempfile.gettempdir(), "wb_code_item_ui_test.csv")
        xio.export_template(p, "code_item")
        self.w.switch_view("codes")
        self.w.tbl_codes.selectRow(0)
        from v2.ui.main_window_v2 import selected_id
        dlg = dialogs.ImportCodeItemsDialog(
            self.w, self.db, selected_id(self.w.tbl_codes))
        self.assertEqual(dlg.objectName(), "ThemedDialog")
        dlg._load(p)
        self.assertEqual(dlg.table.columnCount(), 10)
        self.assertEqual(dlg.table.rowCount(), 1)
        self.assertIn("有效行 1", dlg.summary.text())
        before = self.db.scalar("SELECT COUNT(*) FROM code_items")

        def click_ok():
            m = QApplication.activeModalWidget()
            if isinstance(m, QMessageBox):
                for b in m.buttons():
                    if b.objectName() == "Primary":
                        b.click()
                        return
        QTimer.singleShot(60, click_ok)
        dlg._do_import()
        self.assertEqual(dlg.result["count"], 1)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM code_items"),
                         before + 1)
        new = self.db.query("SELECT * FROM code_items ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(new["goods_code"], "SP-250-90")
        self.assertEqual(new["qty_reference"], 100.0)
        dlg.close()
        os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)

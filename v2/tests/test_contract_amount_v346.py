#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.4.6 回归（2026-11-11 · 用户第 21 轮）：采购合同金额取自合同文档

用户口径：生产排产管理界面导入采购合同时，合同金额应读取合同文档中的总金额，
不再读取订单的总金额。

锁定硬口径：
  ① `import_contract_files()` 建档不再写 orders.amount_total，金额只认文档；
  ② 支持明确总金额标签 / 中文大写金额 / 合计行，旧版 .doc 不依赖 Office；
  ③ 不含税金额、增值税金额、单价、合同额 20% 等干扰项必须排除；
  ④ 识别不到总金额留空 0 并提示补录；用户手工金额不覆盖；
  ⑤ 历史被订单总额顶替的台账可经再次导入或「重读金额」纠正；
  ⑥ schema_meta 仍 v6、18 表、零迁移、data/ 原位保留。

    python -m pytest v2/tests/test_contract_amount_v346.py -q
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2 import __version__                                       # noqa: E402
from v2.core import contract_amount, models                      # noqa: E402
from v2.core.db import CURRENT_SCHEMA_VERSION, Database           # noqa: E402
from v2.domains import OrderService                               # noqa: E402
from v2.domains.contract import (                                 # noqa: E402
    STATUS_DRAFT, STATUS_SIGNED, PurchaseContractService, amount_summary,
)

try:
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
    PYSIDE = True
except ImportError:                                              # pragma: no cover
    PYSIDE = False

_APP = None
ROOT = Path(__file__).resolve().parents[2]
REAL_DOC = Path(
    "D:/Programs/ProductSuite_v2.2/data/采购合同/20261107KYCN-B-027/"
    "20261107KYCN-B-027委托加工合同-浙江-河北久通.doc")


def _app():
    global _APP
    if _APP is None:
        from v2.ui import theme
        _APP = QApplication.instance() or QApplication([])
        theme.apply_theme(_APP)
    return _APP


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class Owner(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db


# ── 解析层 ─────────────────────────────────────────────────

class ContractAmountParserTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v346_parse_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, data: bytes) -> str:
        p = self.tmp / name
        p.write_bytes(data)
        return str(p)

    def _text(self, name: str, text: str, encoding: str = "utf-8") -> str:
        return self._write(name, text.encode(encoding))

    def _xlsx(self, name: str, rows) -> str:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rows:
            ws.append(list(row))
        p = self.tmp / name
        wb.save(str(p))
        wb.close()
        return str(p)

    def _docx(self, name: str, paragraphs, table_rows=None) -> str:
        from docx import Document
        doc = Document()
        for line in paragraphs:
            doc.add_paragraph(line)
        if table_rows:
            t = doc.add_table(rows=0, cols=2)
            for k, v in table_rows:
                cells = t.add_row().cells
                cells[0].text, cells[1].text = str(k), str(v)
        p = self.tmp / name
        doc.save(str(p))
        return str(p)

    def _ole_doc(self, name: str, text: str) -> str:
        """自包含地构造一份「OLE 头 + UTF-16LE 正文」的旧版 .doc 二进制"""
        ole = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 64
        return self._write(name, ole + b"\x00\x00"
                           + text.encode("utf-16-le") + b"\x00" * 64)

    # ① 版本 / 零迁移 ------------------------------------------------

    def test_01_version_and_schema(self):
        self.assertEqual(__version__, "3.5.4")
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        db = Database(":memory:")
        try:
            self.assertEqual(db.schema_version(), 7)
            tables = [r["name"] for r in db.query(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertGreaterEqual(len([t for t in tables
                                         if not t.startswith("sqlite_")]), 17)
            self.assertIn("purchase_contracts", tables)
            self.assertIn("purchase_contract_files", tables)
            self.assertNotIn("contract_amount_cache", tables)
        finally:
            db.close()
        self.assertIn("--hidden-import\", \"v2.core.contract_amount\"",
                      _src("build_v2.py"))

    # ② 中文大写金额 ------------------------------------------------

    def test_02_chinese_upper_amounts(self):
        cases = {
            "壹万肆仟肆佰捌拾肆圆整": 14484.0,
            "人民币：壹万肆仟肆佰捌拾肆圆整": 14484.0,
            "贰万叁仟伍佰圆陆角伍分": 23500.65,
            "壹佰贰拾叁万肆仟伍佰陆拾柒元捌角": 1234567.8,
            "壹拾万元整": 100000.0,
            "伍仟陆佰玖拾贰圆整": 5692.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    contract_amount.chinese_upper_to_number(text), expected)
        for text in ("质保壹年", "本合同一式贰份", "签订日期2026-11-10", ""):
            with self.subTest(text=text):
                self.assertIsNone(
                    contract_amount.chinese_upper_to_number(text))

    # ③ 行级标签 / 干扰项 ------------------------------------------

    def test_03_strong_label_and_position(self):
        hits = contract_amount.extract_from_lines(
            ["合同编号：CG20261103000221 | 合同总金额 | ¥ 1,234,567.89"])
        self.assertEqual(hits[0]["amount"], 1234567.89)
        self.assertEqual(hits[0]["label"], "合同总金额")
        hits = contract_amount.extract_from_lines(
            ["签订日期：2026-11-10 | 合同金额：14484"])
        self.assertEqual([h["amount"] for h in hits], [14484.0])

    def test_04_negative_phrases_are_masked(self):
        lines = [
            "（含税不含运，其中不含税金额12817.7元，增值税金额1666.3元）",
            "违约方承担合同额20%的违约金",
            "单价 | 3552 | 7104",
            "质保壹年，验收精度符合要求",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(
                    contract_amount.extract_from_lines([line]), [])

    def test_05_upper_amount_beats_distractors(self):
        lines = [
            "委托加工采购合同｜编号：20261107KYCN-B-027",
            "总计：",
            "人民币：壹万肆仟肆佰捌拾肆圆整",
            "（含税不含运，其中不含税金额12817.7元，增值税金额1666.3元）",
            "14484",
        ]
        hits = contract_amount.extract_from_lines(lines, "合同.doc")
        self.assertTrue(hits)
        best = max(hits, key=lambda h: h["score"])
        self.assertEqual(best["amount"], 14484.0)
        self.assertIn("大写", best["label"])
        self.assertNotIn(12817.7, [h["amount"] for h in hits])
        self.assertNotIn(1666.3, [h["amount"] for h in hits])

    def test_06_weak_label_and_followup_number(self):
        hits = contract_amount.extract_from_lines(["合计 | | 98765.43"])
        self.assertEqual(hits[0]["amount"], 98765.43)
        hits = contract_amount.extract_from_lines(["总计：", "", "14484"])
        self.assertTrue(any(h["amount"] == 14484.0 and h["kind"] == "followup"
                            for h in hits))

    def test_07_best_amount_picks_highest_confidence(self):
        a = {"amount": 100.0, "score": 55, "label": "总计", "source": "a.txt"}
        b = {"amount": 14484.0, "score": 95, "label": "人民币大写",
             "source": "b.doc"}
        self.assertEqual(contract_amount.best_amount([a, b])["amount"], 14484.0)
        empty = contract_amount.best_amount([{}, None])
        self.assertIsNone(empty["amount"])

    # ④ 各格式文件 --------------------------------------------------

    def test_08_csv_and_txt(self):
        csv = self._text("CG3460001--国能沈西热电厂--采购合同.csv",
                         "合同编号,CG3460001\n签订日期,2026-11-10\n"
                         "合同金额,234567.89\n")
        self.assertEqual(contract_amount.read_contract_amount(csv)["amount"],
                         234567.89)
        txt = self._text("CG3460002--厦门龙净环保--采购合同.txt",
                         "工业产品买卖合同\n总计：\n"
                         "人民币：伍仟陆佰玖拾贰圆整\n"
                         "（其中不含税金额5037.17元，增值税金额654.83元）\n")
        self.assertEqual(contract_amount.read_contract_amount(txt)["amount"],
                         5692.0)

    def test_09_xlsx_ignores_detail_rows(self):
        p = self._xlsx("CG3460003--北电电力--销售排产单.xlsx", [
            ["序号", "物资名称", "数量", "单价", "总价（元）"],
            [1, "双金属复合弯头", 2, 3552, 7104],
            [2, "双金属复合三通", 2, 3690, 7380],
            ["合计", "", "", "", 14484],
            ["合同总金额", "", "", "", 234567.89],
        ])
        hit = contract_amount.read_contract_amount(p)
        self.assertEqual(hit["amount"], 234567.89,
                         "明确总金额标签应压过明细与合计行")
        self.assertNotIn(7104.0, [c["amount"] for c in hit["candidates"]])

    def test_10_docx_kv_table(self):
        p = self._docx("CG3460004--京东平台--采购合同.docx",
                       ["采购合同", "合同编号：CG3460004"],
                       [("供方", "某某机械有限公司"),
                        ("合同金额", "88,888.88"),
                        ("币种", "CNY")])
        hit = contract_amount.read_contract_amount(p)
        self.assertEqual(hit["amount"], 88888.88)
        self.assertEqual(hit["label"], "合同金额")

    def test_11_legacy_doc_binary_without_office(self):
        """旧版 .doc 的 OLE 正文扫描：无需 Word COM 也能读大写金额"""
        p = self._ole_doc(
            "20261107KYCN-B-027委托加工合同-浙江-河北久通.doc",
            "委托加工采购合同\r编号：20261110KYCN-B-027\r"
            "总计：\r人民币：壹万肆仟肆佰捌拾肆圆整\r"
            "（含税不含运，其中不含税金额12817.7元，增值税金额1666.3元）\r"
            "14484\r")
        with mock.patch.object(contract_amount, "_lines_from_word_com",
                               return_value=[]):
            hit = contract_amount.read_contract_amount(p, allow_ocr=False)
        self.assertEqual(hit["amount"], 14484.0)
        self.assertIn("二进制", hit["note"])
        self.assertNotIn(12817.7, [c["amount"] for c in hit["candidates"]])

    def test_12_unsupported_and_missing_files_never_raise(self):
        p = self._write("CG3460005--扫描件.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        hit = contract_amount.read_contract_amount(p, allow_ocr=False)
        self.assertIsNone(hit["amount"])
        hit = contract_amount.read_contract_amount(str(self.tmp / "nope.docx"),
                                                   allow_ocr=False)
        self.assertIsNone(hit["amount"])
        hit = contract_amount.read_contract_amount(
            self._write("CG3460006--压缩包.zip", b"PK\x03\x04xxxx"),
            allow_ocr=False)
        self.assertIsNone(hit["amount"], "不支持的类型不报错、不猜数")
        hit = contract_amount.read_contract_amount(
            self._write("CG3460007--坏表格.xlsx", b"not-a-zip" * 20),
            allow_ocr=False)
        self.assertIsNone(hit["amount"], "损坏文件不得阻断导入")

    def test_13_parser_is_read_only(self):
        p = self._text("CG3460008--合同.csv", "合同总金额,12345.67\n")
        f = Path(p)
        before = (f.stat().st_size, f.stat().st_mtime_ns, f.read_bytes())
        contract_amount.read_contract_amount(p)
        self.assertEqual((f.stat().st_size, f.stat().st_mtime_ns,
                          f.read_bytes()), before)

    @unittest.skipUnless(REAL_DOC.is_file(), "本机部署合同样例不存在")
    def test_14_real_deployed_doc_is_14484(self):
        """真实现场：合同总额 14484，订单总额 18778，且解析不得改动源文件"""
        before = (REAL_DOC.stat().st_size, REAL_DOC.stat().st_mtime_ns)
        hit = contract_amount.read_contract_amount(str(REAL_DOC),
                                                   allow_ocr=False)
        self.assertEqual(hit["amount"], 14484.0)
        self.assertEqual((REAL_DOC.stat().st_size,
                          REAL_DOC.stat().st_mtime_ns), before)


# ── 服务层 ─────────────────────────────────────────────────

class ContractAmountServiceTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v346_svc_"))
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.db = Database(":memory:")
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.order = OrderService(self.db).create_order(
            customer="国能沈西热电厂", project_no="SO-346-001",
            status=models.OrderStatus.CONFIRMED)
        self.db.update("orders", {"amount_total": 18778.0},
                       "id = ?", (self.order["id"],))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name: str, text: str, encoding: str = "utf-8") -> str:
        p = self.src / name
        p.write_bytes(text.encode(encoding))
        return str(p)

    def _contract_csv(self, no: str, amount: str = "234567.89",
                      name: str = "国能沈西热电厂") -> str:
        return self._file(
            f"{no}--{name}--采购合同.csv",
            f"合同编号,{no}\n签订日期,2026-11-10\n合同金额,{amount}\n")

    def _rows(self):
        return self.svc.list(order_id=self.order["id"])

    # ① 核心口径 ---------------------------------------------------

    def test_15_import_uses_document_amount_not_order_total(self):
        p = self._contract_csv("CG3460001", "234567.89")
        res = self.svc.import_contract_files(self.order["id"], [p], "测试")
        c = res["contracts"][0]
        self.assertEqual(c["amount"], 234567.89)
        self.assertNotEqual(c["amount"], 18778.0)
        self.assertEqual(c["status"], STATUS_SIGNED)
        self.assertEqual(c["doc_no"], "CG3460001")
        self.assertEqual(res["amount_missing"], [])
        self.assertIn("234,567.89", amount_summary(res))
        self.assertIn("合同金额", res["amounts"]["CG3460001"]["reason"])
        self.assertEqual(self.db.get_by_id(
            "orders", self.order["id"])["amount_total"], 18778.0,
            "订单总额不得被改写")

    def test_16_legacy_doc_upper_amount(self):
        ole = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 64
        text = ("委托加工采购合同\r总计：\r"
                "人民币：壹万肆仟肆佰捌拾肆圆整\r"
                "（其中不含税金额12817.7元，增值税金额1666.3元）\r14484\r")
        p = self.src / "20261107KYCN-B-027委托加工合同-浙江-河北久通.doc"
        p.write_bytes(ole + text.encode("utf-16-le"))
        with mock.patch.object(contract_amount, "_lines_from_word_com",
                               return_value=[]):
            res = self.svc.import_contract_files(self.order["id"],
                                                 [str(p)], "测试")
        c = res["contracts"][0]
        self.assertEqual(c["doc_no"], "20261107KYCN-B-027")
        self.assertEqual(c["amount"], 14484.0)
        self.assertIn("14484", str(res["amounts"]))
        self.assertNotEqual(c["amount"], 18778.0)

    def test_17_unrecognized_amount_stays_zero(self):
        p = self._file("CG3460002--扫描件--采购合同.png",
                       "不是图片内容，但解析应安全跳过")
        res = self.svc.import_contract_files(self.order["id"], [p], "测试")
        c = res["contracts"][0]
        self.assertEqual(c["amount"], 0.0)
        self.assertEqual(res["amount_missing"], ["CG3460002"])
        self.assertIn("识别到总金额", amount_summary(res))
        self.assertEqual(c["status"], STATUS_SIGNED,
                         "金额识别失败不得影响编号 / 状态 / 归档")

    def test_18_draft_upgrade_replaces_order_total(self):
        c = self.svc.create({
            "contract_name": "待升格合同", "order_id": self.order["id"],
            "amount": 18778.0, "status": STATUS_DRAFT,
            "remark": "由生产排产操作模块按订单自动登记",
        }, "测试")
        self.svc.generate(int(c["id"]), "测试")
        p = self._contract_csv("CG3460003", "98765.43")
        res = self.svc.import_contract_files(
            self.order["id"], [p], "测试", contract_id=int(c["id"]))
        after = res["contracts"][0]
        self.assertEqual(after["id"], c["id"], "不制造第二份台账")
        self.assertEqual(after["doc_no"], "CG3460003")
        self.assertEqual(after["amount"], 98765.43)
        self.assertEqual(after["status"], STATUS_SIGNED)
        self.assertTrue(all(f["exists"] for f in
                            self.svc.files(int(after["id"]))),
                        "附件随编号迁移后不能变死链")

    def test_19_historical_signed_contract_is_corrected(self):
        c = self.svc.create({
            "contract_name": "历史合同", "doc_no": "CG3460004",
            "order_id": self.order["id"], "amount": 18778.0,
            "status": STATUS_SIGNED,
            "remark": "由采购合同导入自动登记",
        }, "测试")
        res = self.svc.import_contract_files(
            self.order["id"], [self._contract_csv("CG3460004", "34567.89")],
            "测试")
        self.assertEqual(len(self.svc.list()), 1, "同编号复用，不重复建档")
        self.assertEqual(res["contracts"][0]["id"], c["id"])
        self.assertEqual(res["contracts"][0]["amount"], 34567.89)
        self.assertTrue(res["amounts"]["CG3460004"]["changed"])

    def test_20_manual_amount_is_preserved(self):
        c = self.svc.create({
            "contract_name": "手工金额", "doc_no": "CG3460005",
            "order_id": self.order["id"], "amount": 99999.0,
            "status": STATUS_SIGNED,
        }, "测试")
        res = self.svc.import_contract_files(
            self.order["id"], [self._contract_csv("CG3460005", "23456.78")],
            "测试")
        self.assertEqual(res["contracts"][0]["amount"], 99999.0)
        info = res["amounts"]["CG3460005"]
        self.assertTrue(info["manual_kept"])
        self.assertIn("手工填写", info["reason"])

    def test_21_unrecognized_clears_auto_order_total(self):
        c = self.svc.create({
            "contract_name": "历史错误金额", "doc_no": "CG3460006",
            "order_id": self.order["id"], "amount": 18778.0,
            "status": STATUS_SIGNED,
        }, "测试")
        p = self._file("CG3460006--扫描件--合同.png", "无文字层")
        res = self.svc.import_contract_files(self.order["id"], [p], "测试")
        self.assertEqual(res["contracts"][0]["amount"], 0.0)
        self.assertTrue(res["amounts"]["CG3460006"]["changed"])
        logs = self.db.query(
            "SELECT action FROM audit_log WHERE entity='purchase_contract' "
            "AND action='clear_order_amount'")
        self.assertEqual(len(logs), 1)

    # ② 分组 / 摘要 / 重读 ------------------------------------------

    def test_22_group_amounts_are_independent(self):
        p1 = self._contract_csv("CG3460007", "11111.11")
        p2 = self._contract_csv("CG3460008", "22222.22", "厦门龙净环保")
        res = self.svc.import_contract_files(self.order["id"],
                                             [p1, p2], "测试")
        got = {c["doc_no"]: c["amount"] for c in res["contracts"]}
        self.assertEqual(got, {"CG3460007": 11111.11,
                               "CG3460008": 22222.22})
        self.assertEqual(len(res["amounts"]), 2)

    def test_23_same_group_picks_best_hit(self):
        p1 = self._contract_csv("CG3460009", "33333.33")
        p2 = self._file("CG3460009--附件--说明.txt",
                        "本说明无金额字段，仅有编号 CG3460009")
        res = self.svc.import_contract_files(self.order["id"],
                                             [p2, p1], "测试")
        self.assertEqual(res["contracts"][0]["amount"], 33333.33)

    def test_24_audit_log_records_amount_source(self):
        self.svc.import_contract_files(
            self.order["id"], [self._contract_csv("CG3460010", "45678.90")],
            "测试")
        rows = self.db.query(
            "SELECT after_json FROM audit_log WHERE entity='purchase_contract' "
            "AND action='set_amount_from_doc'")
        self.assertEqual(len(rows), 1)
        self.assertIn("45678.9", str(rows[0]["after_json"]))
        rows = self.db.query(
            "SELECT after_json FROM audit_log WHERE entity='purchase_contract' "
            "AND action='import_signed'")
        self.assertIn("amount_source", str(rows[0]["after_json"]))

    def test_25_refresh_amount_from_archived_files(self):
        res = self.svc.import_contract_files(
            self.order["id"], [self._contract_csv("CG3460011", "56789.01")],
            "测试")
        cid = int(res["contracts"][0]["id"])
        self.svc.update(cid, {"amount": 18778.0}, "测试")
        out = self.svc.refresh_amount_from_files(cid, "测试")
        self.assertTrue(out["changed"])
        self.assertEqual(out["before"], 18778.0)
        self.assertEqual(out["amount"], 56789.01)
        self.assertEqual(self.svc.get(cid)["amount"], 56789.01)
        # 手工金额不覆盖
        self.svc.update(cid, {"amount": 88888.0}, "测试")
        out2 = self.svc.refresh_amount_from_files(cid, "测试")
        self.assertFalse(out2["changed"])
        self.assertTrue(out2["manual_kept"])
        self.assertEqual(self.svc.get(cid)["amount"], 88888.0)

    def test_26_refresh_without_files_is_safe(self):
        c = self.svc.create({"contract_name": "无附件", "amount": 18778.0,
                             "order_id": self.order["id"]}, "测试")
        out = self.svc.refresh_amount_from_files(int(c["id"]), "测试")
        self.assertEqual(out["files"], 0)
        self.assertFalse(out["changed"])
        self.assertEqual(self.svc.get(int(c["id"]))["amount"], 18778.0)

    def test_27_amount_summary_and_empty_result(self):
        res = self.svc.import_contract_files(
            self.order["id"], [self._contract_csv("CG3460012", "12345.67")],
            "测试")
        text = amount_summary(res)
        self.assertIn("CG3460012", text)
        self.assertIn("¥12,345.67", text)
        self.assertEqual(amount_summary({}), "")

    def test_28_create_and_generate_paths_unchanged(self):
        """新建草稿 / 生成模板链路不受导入金额改造影响"""
        c = self.svc.create({"contract_name": "手工新建", "amount": 100.0},
                            "测试")
        self.assertEqual(c["status"], STATUS_DRAFT)
        self.assertEqual(c["amount"], 100.0)
        g = self.svc.generate(int(c["id"]), "测试")
        self.assertTrue(Path(g["path"]).is_file())
        self.assertEqual(self.db.schema_version(), 7)


# ── UI 层 ──────────────────────────────────────────────────

@unittest.skipUnless(PYSIDE, "PySide6 未安装")
class ContractAmount346UITest(unittest.TestCase):

    def setUp(self):
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="v346_ui_"))
        self.db = Database(":memory:")
        self.svc = PurchaseContractService(self.db, str(self.tmp / "data"))
        self.owner = Owner(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_29_dialog_has_refresh_button_and_amount_hint(self):
        from v2.ui.contract_dialog import PurchaseContractDialog
        dlg = PurchaseContractDialog(self.owner, self.svc)
        try:
            labels = [b.text() for b in dlg.findChildren(QPushButton)]
            self.assertIn("重读金额", labels)
            self.assertIn("导入合同", labels)
            self.assertTrue(hasattr(dlg, "_do_refresh_amount"))
            dlg._reload_contracts()
            self.assertIn("合同金额取合同文档总金额", dlg.hint.text() or "")
        finally:
            dlg.close()

    def test_30_wiring_in_actions_and_sources(self):
        self.assertIn("amount_summary", _src("v2/ui/panels/contract_actions.py"))
        self.assertIn("合同金额取自文档", _src("v2/ui/panels/contract_actions.py"))
        self.assertIn("refresh_amount_from_files",
                      _src("v2/ui/contract_dialog.py"))
        self.assertIn("amount_summary", _src("v2/ui/contract_dialog.py"))
        self.assertIn("不再取订单", _src("README.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

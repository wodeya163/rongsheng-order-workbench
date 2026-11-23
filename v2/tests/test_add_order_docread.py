#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「添加订单」多格式读取 v2.5 回归（2026-11-07）

覆盖：
  · 表格 / CSV / Word 表格 / Word 键值 / PDF 词框 / 图片 OCR 六条通道
  · 来源行统一摊成 17 列标准标签、缺列补空、未识别列上报
  · 无项目编号时整份文件合并为一张订单（避免被拆成 N 张）
  · 对话框 17 列可编辑预览、改单元格回流、建单后 Q12 与数量守恒
  · 主窗口「添加订单」按钮位于「刷新」左侧且只在订单视图可见
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import docread, integrity, order_fields as ofdefs    # noqa: E402
from v2.core.db import Database                                   # noqa: E402
from v2.core import models                                        # noqa: E402

try:
    from PySide6.QtWidgets import QApplication, QPushButton
    PYSIDE = True
except ImportError:                                               # pragma: no cover
    PYSIDE = False

HEAD = ["物资名称", "规格", "单位", "数量", "单价", "参数描述"]
ROWS = [["耐磨弯头", "DN250", "件", "10", "2100", "耐磨层4mm"],
        ["波纹补偿器", "DN300", "件", "5", "4200", "双层波"]]


def _make_docx(path: Path, with_table: bool = True) -> Path:
    from docx import Document
    doc = Document()
    doc.add_paragraph("生产订单通知单")
    if with_table:
        table = doc.add_table(rows=1, cols=len(HEAD))
        for i, head in enumerate(HEAD):
            table.rows[0].cells[i].text = head
        for row in ROWS:
            cells = table.add_row().cells
            for i, value in enumerate(row):
                cells[i].text = value
    else:
        for line in ["订单项目编号：SO-2026-007", "需方单位：国能沈西热电厂",
                     "物资名称：耐磨弯头", "规格：DN250", "单位：件",
                     "数量：10", "单价：2100",
                     "物资名称：波纹补偿器", "规格：DN300", "数量：5", "单价：4200"]:
            doc.add_paragraph(line)
    doc.save(str(path))
    return path


def _make_xlsx(path: Path) -> Path:
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "销售订单"
    sheet.append(["销售订单汇总表"])          # 标题行，非表头
    sheet.append([])                          # 空行
    sheet.append(HEAD)
    for row in ROWS:
        sheet.append(row)
    book.save(str(path))
    return path


def _make_pdf(path: Path) -> Path:
    import fitz
    font = "C:/Windows/Fonts/msyh.ttc"
    doc = fitz.open()
    page = doc.new_page(width=1100, height=520)
    page.insert_text((40, 60), "生产订单  SO-2026-009", fontsize=15,
                     fontname="china", fontfile=font)
    page.insert_text((40, 105), "物资名称        规格     单位   数量    单价",
                     fontsize=13, fontname="china", fontfile=font)
    page.insert_text((40, 140), "耐磨弯头        DN250    件     10      2100",
                     fontsize=13, fontname="china", fontfile=font)
    page.insert_text((40, 175), "波纹补偿器      DN300    件     5       4200",
                     fontsize=13, fontname="china", fontfile=font)
    doc.save(str(path))
    doc.close()
    return path


def _make_image(path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1300, 560), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 40)
    rows = [("物资名称", "规格", "单位", "数量", "单价"),
            ("耐磨弯头", "DN250", "件", "10", "2100"),
            ("波纹补偿器", "DN300", "件", "5", "4200")]
    xs = [60, 500, 790, 930, 1105]
    for row_index, row in enumerate(rows):
        y = 70 + row_index * 120
        for col, text in enumerate(row):
            draw.text((xs[col], y), text, fill="black", font=font)
    image.save(str(path))
    return path


class DocreadFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v25_docread_"))

    def _read(self, path, grouping=True):
        """grouping=True 复现对话框行为：一份文件默认合并成一张订单"""
        source = docread.read_source(str(path))
        if grouping:
            docread.ensure_grouping(source["rows"], Path(path).stem)
        return source, ofdefs.parse_rows(source["rows"])


class TestDocreadExtraction(DocreadFixture):

    def test_01_xlsx_skips_title_rows_and_hits_header(self):
        path = _make_xlsx(self.tmp / "订单.xlsx")
        source, report = self._read(path)
        self.assertEqual(source["kind"], "table")
        self.assertEqual(len(source["rows"]), 2)
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["item_count"], 2)
        self.assertEqual(report["groups"][0]["items"][0]["item_name"], "耐磨弯头")
        self.assertTrue(any("工作表" in n for n in source["notes"]))

    def test_02_csv_reads_with_header_aliases(self):
        path = self.tmp / "订单.csv"
        path.write_text("物资名称,规格,单位,数量,单价\n耐磨弯头,DN250,件,10,2100\n",
                        encoding="utf-8-sig")
        source, report = self._read(path)
        self.assertEqual(source["kind"], "table")
        self.assertEqual(report["item_count"], 1)
        self.assertEqual(report["groups"][0]["items"][0]["qty_ordered"], 10.0)

    def test_03_docx_table(self):
        path = _make_docx(self.tmp / "通知.docx", with_table=True)
        source, report = self._read(path)
        self.assertEqual(source["kind"], "word")
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["item_count"], 2)
        self.assertEqual(report["groups"][0]["items"][1]["unit_price"], 4200.0)

    def test_04_docx_key_value_without_table(self):
        path = _make_docx(self.tmp / "正文.docx", with_table=False)
        source, report = self._read(path)
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["item_count"], 2)
        head = report["groups"][0]["head"]
        self.assertEqual(head["project_no"], "SO-2026-007")
        self.assertEqual(head["customer"], "国能沈西热电厂")
        self.assertEqual([i["item_name"] for i in report["groups"][0]["items"]],
                         ["耐磨弯头", "波纹补偿器"])

    @unittest.skipUnless((Path("C:/Windows/Fonts/msyh.ttc")).is_file(),
                         "缺少中文字体，无法生成中文 PDF 样本")
    def test_05_pdf_restores_columns_from_word_boxes(self):
        try:
            import fitz  # noqa: F401
        except ImportError:                                   # pragma: no cover
            self.skipTest("未安装 PyMuPDF，跳过 PDF 样本")
        path = _make_pdf(self.tmp / "文字型.pdf")
        source, report = self._read(path)
        self.assertEqual(source["kind"], "pdf")
        self.assertEqual(report["item_count"], 2)
        items = report["groups"][0]["items"]
        self.assertEqual(items[0]["item_name"], "耐磨弯头")
        self.assertEqual(items[0]["spec"], "DN250")
        self.assertEqual(items[0]["qty_ordered"], 10.0)

    @unittest.skipUnless(docread.ocr_available(), "未安装 Tesseract-OCR，跳过图片识别")
    def test_06_image_ocr(self):
        path = _make_image(self.tmp / "订单照片.png")
        source, report = self._read(path)
        self.assertEqual(source["kind"], "image")
        self.assertGreaterEqual(len(source["rows"]), 1)
        self.assertTrue(any(str(r.get("物资名称") or "").strip()
                            for r in source["rows"]))
        self.assertTrue(any("OCR" in n for n in source["notes"]))

    def test_07_unsupported_extension_raises(self):
        path = self.tmp / "说明.zzz"
        path.write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            docread.read_source(str(path))
        self.assertIn("不支持的文件类型", str(ctx.exception))

    def test_08_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            docread.read_source(str(self.tmp / "不存在.docx"))

    def test_09_canonical_rows_are_17_columns(self):
        path = _make_xlsx(self.tmp / "订单.xlsx")
        source = docread.read_source(str(path))
        self.assertEqual(docread.CANONICAL_LABELS, ofdefs.headers())
        for row in source["rows"]:
            self.assertEqual(list(row.keys()), ofdefs.headers())
            self.assertEqual(row["订单项目编号"], "")     # 缺列补空
            self.assertEqual(row["单位"], "件")

    def test_10_ensure_grouping_merges_single_document(self):
        rows = [{"物资名称": "耐磨弯头", "数量": "10"},
                {"物资名称": "波纹补偿器", "数量": "5"}]
        self.assertTrue(docread.ensure_grouping(rows, "订单通知"))
        self.assertTrue(all(r["订单分组"] == "订单通知" for r in rows))
        report = ofdefs.parse_rows(rows)
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["item_count"], 2)

    def test_11_ensure_grouping_keeps_existing_project_numbers(self):
        rows = [{"订单项目编号": "SO-1", "物资名称": "A", "数量": "1"},
                {"订单项目编号": "SO-2", "物资名称": "B", "数量": "2"}]
        self.assertFalse(docread.ensure_grouping(rows, "x"))
        report = ofdefs.parse_rows(rows)
        self.assertEqual(report["group_count"], 2)


@unittest.skipUnless(PYSIDE, "需要 PySide6")
class TestAddOrderDialog(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)

    def setUp(self):
        from v2.ui import theme
        self.tmp = Path(tempfile.mkdtemp(prefix="v25_dialog_"))
        self.db = Database(":memory:")
        self.notices = []
        self._orig_notify = theme.notify
        theme.notify = lambda *a, **k: self.notices.append(
            (a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        self._theme = theme

    def tearDown(self):
        self._theme.notify = self._orig_notify
        self.db.close()

    def _dialog(self, path):
        from v2.ui import dialogs
        dialog = dialogs.AddOrderDialog(None, self.db)
        dialog._load_blocking(str(path))
        return dialog

    def test_12_preview_is_editable_17_columns(self):
        dialog = self._dialog(_make_docx(self.tmp / "通知.docx"))
        self.assertEqual(dialog.table.columnCount(), 17)
        self.assertEqual([dialog.table.horizontalHeaderItem(i).text()
                          for i in range(17)], ofdefs.headers())
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertEqual(dialog._ok_btn.text(), "添加 1 张订单 / 2 行明细")
        self.assertTrue(dialog._ok_btn.isEnabled())
        self.assertIn("添加 1 张订单", dialog._ok_btn.text())

    def test_13_cell_edit_flows_back_into_rows(self):
        dialog = self._dialog(_make_docx(self.tmp / "通知.docx"))
        col = ofdefs.headers().index("数量")
        dialog.table.item(0, col).setText("12")
        self.assertEqual(dialog.rows[0]["数量"], "12")
        self.assertIn("明细 2 行", dialog.summary.text())
        dialog.table.item(0, ofdefs.headers().index("客户名称")).setText("国能沈西热电厂")
        self.assertIn("订单 1 张", dialog.summary.text())

    def test_14_add_creates_order_with_q12_and_conservation(self):
        dialog = self._dialog(_make_docx(self.tmp / "通知.docx"))
        col = ofdefs.headers().index("客户名称")
        dialog.table.item(0, col).setText("国能沈西热电厂")
        dialog._do_add()
        self.assertIsNotNone(dialog.result)
        self.assertEqual(dialog.result["order_count"], 1)
        self.assertEqual(dialog.result["item_count"], 2)
        self.assertEqual(dialog.result["qty_ordered_real"], 15.0)
        self.assertEqual(dialog.result["amount_total"], 42000.0)

        order = self.db.query("SELECT * FROM orders")[0]
        self.assertEqual(order["customer"], "国能沈西热电厂")
        self.assertEqual(order["status"], models.OrderStatus.DRAFT)
        self.assertEqual(order["item_qty_total"], 2.0)      # Q12：物资项目数
        self.assertEqual(order["amount_total"], 42000.0)
        self.assertTrue(integrity.check(self.db)["ok"])
        self.assertTrue(any("添加完成" in n[0] for n in self.notices))

    def test_15_fatal_rows_disable_ok_button(self):
        path = self.tmp / "空表.csv"
        path.write_text("物资名称,数量\n,0\n", encoding="utf-8-sig")
        from v2.ui import dialogs
        dialog = dialogs.AddOrderDialog(None, self.db)
        dialog._load_blocking(str(path))
        self.assertFalse(dialog._ok_btn.isEnabled())
        self.assertIn("未解析出订单", dialog.issues.toPlainText())


@unittest.skipUnless(PYSIDE, "需要 PySide6")
class TestMainWindowEntry(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)

    def setUp(self):
        from v2.ui import theme
        self._orig_notify = theme.notify
        theme.notify = lambda *a, **k: None
        self._theme = theme
        from v2.core.db import Database
        from v2 import demo_data
        from v2.ui.main_window_v2 import MainWindowV2
        self.db = Database(":memory:")
        demo_data.seed(self.db)
        self.win = MainWindowV2(self.db)

    def tearDown(self):
        self._theme.notify = self._orig_notify
        self.win.close()
        self.db.close()

    def test_16_button_sits_left_of_refresh_and_only_in_orders(self):
        buttons = {b.text(): b for b in
                   self.win.btn_add_order.parentWidget().findChildren(QPushButton)}
        self.assertIn("添加订单", buttons)
        panel = self.win.btn_add_order.parentWidget()
        head = None
        for i in range(panel.layout().count()):
            item = panel.layout().itemAt(i)
            layout = item.layout()
            if layout and any(layout.itemAt(j).widget() is self.win.btn_add_order
                              for j in range(layout.count())):
                head = layout
                break
        self.assertIsNotNone(head, "未找到结果页头部按钮栏")
        texts = [head.itemAt(j).widget().text() for j in range(head.count())
                 if head.itemAt(j).widget()]
        self.assertLess(texts.index("添加订单"), texts.index("刷新"))

        self.win.switch_view("orders")
        self.assertFalse(self.win.btn_add_order.isHidden())
        self.win.switch_view("bids")
        self.assertTrue(self.win.btn_add_order.isHidden())

    def test_17_action_group_lists_add_order(self):
        from v2.ui.main_window_v2 import ACTION_GROUPS
        labels = [a[0] for g in ACTION_GROUPS if g[1] == "orders" for a in g[2]]
        self.assertIn("单据识别建单", labels)
        self.assertLess(labels.index("新增订单"),
                        labels.index("单据识别建单"))
        self.assertTrue(hasattr(self.win, "_add_order"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

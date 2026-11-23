#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单排产 v3.1.2 回归（2026-11-08 · 用户第 11 轮）

口径：
  ① 删除「导入图片」「导入图纸」按钮，只留「导入生产排产审批」一个入口；
  ② 这一个入口可以一次导入任意类型的文件，按扩展名自动归类；
  ③ 至少存在 1 个 Word / Excel 格式的审批文件 → 保存即默认本批物资已排产；
     图片 / 图纸 / PDF / 其它格式只登记，不触发状态流转；
  ④ 「新建生产排产审批」只生成并登记 .docx 模板，不再自动启动本机 Word。

    python -m pytest v2/tests/test_schedule_import_v312.py -q
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
from v2.domains import schedule as sched_mod                   # noqa: E402
from v2.domains.schedule import (                              # noqa: E402
    APPROVAL_DOC_EXTS, IMPORT_FILTER, KIND_APPROVAL, KIND_DRAWING,
    KIND_IMAGE, KIND_OTHER, classify_file, is_approval_doc,
)

try:
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


def _file(tmp: Path, name: str, payload: bytes = b"fake-content") -> str:
    p = Path(tmp) / name
    p.write_bytes(payload)
    return str(p)


class ClassifyTest(unittest.TestCase):
    """分类口径：界面、服务层、状态判定共用一个函数"""

    def test_01_word_and_excel_are_approval_docs(self):
        for ext in (".doc", ".docx", ".wps", ".rtf", ".odt",
                    ".xls", ".xlsx", ".xlsm", ".et", ".csv", ".ods"):
            name = "审批" + ext
            self.assertEqual(classify_file(name), KIND_APPROVAL, name)
            self.assertTrue(is_approval_doc(name), name)

    def test_02_pdf_is_approval_kind_but_not_trigger(self):
        self.assertEqual(classify_file("扫描审批.pdf"), KIND_APPROVAL)
        self.assertFalse(is_approval_doc("扫描审批.pdf"))
        self.assertNotIn(".pdf", APPROVAL_DOC_EXTS)

    def test_03_images_drawings_and_other(self):
        self.assertEqual(classify_file("现场.PNG"), KIND_IMAGE)
        self.assertEqual(classify_file("弯头.dwg"), KIND_DRAWING)
        self.assertEqual(classify_file("管路.step"), KIND_DRAWING)
        self.assertEqual(classify_file("打包.zip"), KIND_OTHER)
        self.assertEqual(classify_file("无扩展名"), KIND_OTHER)
        self.assertEqual(classify_file(r"D:\图纸\三通.DXF"), KIND_DRAWING)

    def test_04_import_filter_is_plain_string_and_open_to_all(self):
        # v3.0.1 老 bug：过滤器常量变元组 → setNameFilter 抛异常被 Qt 吞掉
        self.assertIsInstance(IMPORT_FILTER, str)
        for token in ("*.docx", "*.xlsx", "*.pdf", "*.png", "*.dwg",
                      "所有文件 (*.*)"):
            self.assertIn(token, IMPORT_FILTER)


class Fixture(unittest.TestCase):
    """三物资订单，全部可建单；附件落在临时 data 根"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v312_import_")
        self.db = Database(":memory:")
        self.orders = OrderService(self.db)
        self.svc = OrderScheduleService(self.db, self.tmp)
        self.order = self.orders.create_order(
            customer="国能沈西热电", project_no="SO-2026-112",
            status=models.OrderStatus.CONFIRMED)
        self.i1 = self.orders.add_item(
            self.order["id"], item_name="耐磨弯头", spec="DN250",
            unit="件", qty_ordered=10, unit_price=2100.0)
        self.i2 = self.orders.add_item(
            self.order["id"], item_name="波纹补偿器", spec="DN300",
            unit="台", qty_ordered=5, unit_price=5200.0)
        self.i3 = self.orders.add_item(
            self.order["id"], item_name="落煤管", spec="DN400",
            unit="根", qty_ordered=3, unit_price=800.0)
        self.ids = [self.i1["id"], self.i2["id"], self.i3["id"]]

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        return self.db.get_by_id("orders", self.order["id"])["status"]

    def whole(self):
        res = self.svc.ensure_scope(self.order["id"], self.ids, "测试", "整单")
        return [s["id"] for s in res["schedules"]]


class MixedImportTest(Fixture):

    def test_05_one_import_handles_all_file_types(self):
        sids = self.whole()
        paths = [
            _file(Path(self.tmp), "外部审批.docx"),
            _file(Path(self.tmp), "排产清单.xlsx"),
            _file(Path(self.tmp), "现场照片.png"),
            _file(Path(self.tmp), "弯头图纸.dwg"),
            _file(Path(self.tmp), "盖章扫描.pdf"),
            _file(Path(self.tmp), "资料包.zip"),
        ]
        res = self.svc.import_files(sids, paths, "测试")
        self.assertEqual(res["files"], 6)
        self.assertEqual(res["kinds"], {
            KIND_APPROVAL: 3, KIND_IMAGE: 1,
            KIND_DRAWING: 1, KIND_OTHER: 1})
        # 合并/整单：同一物理文件登记到每张排产单，但只存一份
        for sid in sids:
            self.assertEqual(len(self.svc.files(sid)), 6)
            self.assertEqual(sorted(self.svc.file_kinds(sid)),
                             sorted([KIND_APPROVAL, KIND_IMAGE,
                                     KIND_DRAWING, KIND_OTHER]))
        merged = self.svc.scope_files(sids)
        self.assertEqual(len(merged), 6)
        self.assertTrue(all(f["shared"] == len(sids) for f in merged))
        for f in merged:
            self.assertTrue(self.svc.to_abs(f["file_path"]).is_file())

    def test_06_approval_docs_only_count_word_excel(self):
        sids = self.whole()
        self.svc.import_files(
            sids,
            [_file(Path(self.tmp), "清单.png"),
             _file(Path(self.tmp), "扫描.pdf"),
             _file(Path(self.tmp), "图纸.dxf"),
             _file(Path(self.tmp), "说明.zip")], "测试")
        self.assertFalse(self.svc.has_approval_doc(sids[0]))
        self.assertEqual(self.svc.scope_approval_docs(sids), [])
        self.svc.import_files(
            sids, [_file(Path(self.tmp), "审批定稿.XLSX")], "测试")
        docs = self.svc.approval_docs(sids[0])
        self.assertEqual(docs, ["审批定稿.XLSX"])
        self.assertEqual(self.svc.scope_approval_docs(sids), ["审批定稿.XLSX"])

    def test_07_import_filter_allows_unknown_extension(self):
        sids = self.whole()
        res = self.svc.import_files(
            sids, [_file(Path(self.tmp), "厂家回执.dat")], "测试")
        self.assertEqual(res["kinds"], {KIND_OTHER: 1})
        self.assertEqual(self.svc.files(sids[0])[0]["kind"], KIND_OTHER)


class AutoScheduleTest(Fixture):

    def _lower_qty(self, sids):
        """把可排量人为放出来：模拟还没排满"""
        for sid in sids:
            self.svc.set_allocated_qty(sid, 1.0, "测试")

    def test_08_images_drawings_pdf_do_not_trigger(self):
        sids = self.whole()
        self._lower_qty(sids)
        self.svc.import_files(
            sids,
            [_file(Path(self.tmp), "a.png"),
             _file(Path(self.tmp), "b.dwg"),
             _file(Path(self.tmp), "c.pdf"),
             _file(Path(self.tmp), "d.zip")], "测试")
        for sid in sids:
            filled, kinds = self.svc.auto_fill_if_ready(sid, "测试")
            self.assertFalse(filled)
            self.assertEqual(kinds, 4)     # 4 类也不再触发（v2.9.3 老口径废止）
            self.assertAlmostEqual(float(self.svc.get(sid)["allocated_qty"]),
                                   1.0, places=6)

    def test_09_excel_approval_triggers_fill(self):
        sids = self.whole()
        self._lower_qty(sids)
        self.assertEqual(self.status(), models.OrderStatus.CONFIRMED)
        self.svc.import_files(
            sids, [_file(Path(self.tmp), "生产排产单.xlsx")], "测试")
        for sid in sids:
            filled, _ = self.svc.auto_fill_if_ready(sid, "测试")
            self.assertTrue(filled)
        self.svc.recheck_order(self.order["id"], "测试")
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        for sid in sids:
            item = self.db.get_by_id(
                "order_items", self.svc.get(sid)["order_item_id"])
            self.assertAlmostEqual(float(self.svc.get(sid)["allocated_qty"]),
                                   float(item["qty_ordered"]), places=6)

    def test_10_word_approval_also_triggers(self):
        sids = self.whole()
        self._lower_qty(sids)
        self.svc.import_files(
            sids, [_file(Path(self.tmp), "生产排产审批.doc")], "测试")
        self.assertTrue(self.svc.has_approval_doc(sids[0]))
        self.assertTrue(self.svc.auto_fill_if_ready(sids[0], "测试")[0])

    def test_11_new_approval_template_counts_as_approval_doc(self):
        sids = self.whole()
        path, created = self.svc.build_scope_approval(
            self.order["id"], sids, "整单", "测试")
        self.assertTrue(created)
        self.assertTrue(Path(path).is_file())
        self.assertTrue(Path(path).name.endswith(".docx"))
        for sid in sids:
            self.assertTrue(self.svc.has_approval_doc(sid))
            self.assertTrue(self.svc.auto_fill_if_ready(sid, "测试")[0])


@unittest.skipUnless(PYSIDE, "PySide6 不可用")
class DialogV312Test(Fixture):

    def _dialog(self, **kw):
        _app()
        from v2.ui import dialogs
        return dialogs.OrderScheduleDialog(None, self.svc, **kw)

    def test_12_only_two_action_buttons_and_no_word_launch(self):
        d = self._dialog(order_id=self.order["id"], scope="整单")
        texts = [b.text() for b in d.findChildren(QPushButton)]
        # 整单范围下按钮会带范围后缀（「（整单 · 全部物资共用一张审批单）」）
        self.assertTrue(any(t.startswith("新建生产排产审批") for t in texts))
        self.assertIn("导入生产排产审批", texts)
        self.assertNotIn("导入图片", texts)
        self.assertNotIn("导入图纸", texts)
        self.assertFalse(any("启动本机 Word" in t for t in texts))
        self.assertFalse(d.chk_fill.isChecked())   # 不绕过审批文件判定
        d.close()

    def test_13_new_approval_does_not_call_open_in_word(self):
        d = self._dialog(order_id=self.order["id"], scope="整单")
        with mock.patch.object(self.svc, "open_approval") as opened, \
                mock.patch("v2.ui.schedule_dialog.theme.notify") as notify:
            d._do_approval()
        opened.assert_not_called()
        self.assertTrue(notify.called)
        sids = [s["id"] for s in self.svc.list(self.order["id"])]
        self.assertEqual(len(sids), 3)
        self.assertTrue(all(self.svc.has_approval_doc(s) for s in sids))
        self.assertIn("不再自动启动本机 Word",
                      notify.call_args.args[2] if len(notify.call_args.args) > 2
                      else notify.call_args.kwargs.get("text", ""))
        d.close()

    def test_14_import_button_imports_mixed_types(self):
        d = self._dialog(order_id=self.order["id"], scope="整单")
        paths = [
            _file(Path(self.tmp), "外部审批.docx"),
            _file(Path(self.tmp), "排产表.xlsx"),
            _file(Path(self.tmp), "照片.jpg"),
            _file(Path(self.tmp), "图纸.dwg"),
        ]
        with mock.patch("v2.ui.schedule_dialog.theme.pick_paths",
                        return_value=paths), \
                mock.patch("v2.ui.schedule_dialog.theme.notify") as notify:
            d._do_import()
        self.assertTrue(notify.called)
        sids = [s["id"] for s in self.svc.list(self.order["id"])]
        kinds = {f["kind"] for f in self.svc.scope_files(sids)}
        self.assertEqual(kinds, {KIND_APPROVAL, KIND_IMAGE, KIND_DRAWING})
        self.assertIn("已排产", d.kind_hint.text())
        self.assertIn("Word/Excel", d.kind_hint.text())
        d.close()

    def test_15_save_after_excel_approval_sets_order_scheduled(self):
        sids = self.whole()
        for sid in sids:
            self.svc.set_allocated_qty(sid, 1.0, "测试")
        self.assertEqual(self.status(), models.OrderStatus.CONFIRMED)
        self.svc.import_files(
            sids, [_file(Path(self.tmp), "审批.xlsx")], "测试")
        d = self._dialog(order_id=self.order["id"], scope="整单")
        self.assertTrue(d._save())
        self.assertEqual(self.status(), models.OrderStatus.SCHEDULED)
        d.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 界面基础设施回归：异步解析（P1-7）、操作人（P1-9）、审计查询分页

    python -m pytest v2/tests/test_p1_ui_infra.py -q
"""
import csv
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import current_user                              # noqa: E402
from v2.core.db import Database                               # noqa: E402
from v2.domains import MaintenanceService, OrderService       # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    PYSIDE = True
except ImportError:                                            # pragma: no cover
    PYSIDE = False


def _make_csv(path: Path) -> Path:
    rows = [
        ["订单项目编号", "客户名称", "订单类型", "订单状态", "价税合计",
         "交货日期", "物资数量", "ERP订单编号",
         "物资代码", "物资名称", "实际生产物资名称", "规格", "单位",
         "数量", "单价", "总价", "参数描述"],
        ["ASYNC-1", "异步测试客户", "外部订单", "已确认", "", "2026-12-31", "",
         "", "M001", "耐磨弯头", "", "DN200", "件", "2", "100", "", ""],
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


class OperatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1user_"))
        current_user.reset_cache()
        os.environ.pop(current_user.ENV_KEY, None)

    def tearDown(self):
        os.environ.pop(current_user.ENV_KEY, None)
        current_user.configure(None)
        current_user.reset_cache()

    def test_01_default_is_login_name(self):
        name = current_user.name(self.tmp)
        self.assertTrue(name)
        self.assertNotEqual(name, "操作员")

    def test_02_file_override_and_clear(self):
        current_user.set_name("张哥", self.tmp)
        self.assertEqual(current_user.name(self.tmp), "张哥")
        # configure 后不传参也能读到
        current_user.configure(self.tmp)
        self.assertEqual(current_user.name(), "张哥")
        self.assertEqual(current_user.set_name("", self.tmp),
                         current_user.name(self.tmp))
        self.assertNotEqual(current_user.name(self.tmp), "张哥")

    def test_03_env_wins(self):
        current_user.set_name("张哥", self.tmp)
        os.environ[current_user.ENV_KEY] = "临时操作员"
        current_user.reset_cache()          # 环境变量只在进程启动时读一次，缓存后需重置
        self.assertEqual(current_user.name(self.tmp), "临时操作员")

    def test_04_resolve_empty(self):
        current_user.set_name("张哥", self.tmp)
        self.assertEqual(current_user.resolve("", self.tmp), "张哥")
        self.assertEqual(current_user.resolve("李工", self.tmp), "李工")

    def test_05_audit_log_fills_operator(self):
        db = Database(str(self.tmp / "erp.db"))
        current_user.configure(db.data_dir)
        current_user.set_name("张哥", db.data_dir)
        db.log_action("order", 1, "create", after={"doc_no": "DD-1"}, operator="")
        row = db.query_one("SELECT operator FROM audit_log ORDER BY id DESC")
        self.assertEqual(row["operator"], "张哥")
        db.close()


class AuditQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1audit_"))
        self.db = Database(str(self.tmp / "erp.db"))
        self.maint = MaintenanceService(self.db)
        for i in range(7):
            self.db.log_action("order" if i % 2 == 0 else "bid_project", i + 1,
                               "create" if i < 5 else "edit",
                               after={"n": i}, operator="张哥" if i < 4 else "李工")

    def tearDown(self):
        self.db.close()

    def test_01_filters(self):
        res = self.maint.audit_page(entity="order")
        self.assertEqual(res["total"], 4)
        res = self.maint.audit_page(action="edit")
        self.assertEqual(res["total"], 2)
        res = self.maint.audit_page(operator="李工")
        self.assertEqual(res["total"], 3)
        res = self.maint.audit_page(keyword='"n": 6')
        self.assertEqual(res["total"], 1)

    def test_02_paging(self):
        page1 = self.maint.audit_page(limit=3)
        self.assertEqual(page1["total"], 7)
        self.assertEqual(len(page1["rows"]), 3)
        page3 = self.maint.audit_page(offset=6, limit=3)
        self.assertEqual(len(page3["rows"]), 1)
        # 倒序：最新一条在首页
        self.assertGreater(page1["rows"][0]["id"], page1["rows"][-1]["id"])

    def test_03_entities_and_actions(self):
        ents = {e["key"] for e in self.maint.audit_entities()}
        self.assertEqual(ents, {"order", "bid_project"})
        acts = {a["key"] for a in self.maint.audit_actions()}
        self.assertEqual(acts, {"create", "edit"})
        # 中文标签可直接展示
        labels = {e["key"]: e["label"] for e in self.maint.audit_entities()}
        self.assertEqual(labels["order"], "订单")


@unittest.skipUnless(PYSIDE, "未安装 PySide6")
class AsyncLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1async_"))
        self.db = Database(":memory:")
        from v2.ui import theme
        self._theme = theme
        self._orig_notify = theme.notify
        theme.notify = lambda *a, **k: None

    def tearDown(self):
        self._theme.notify = self._orig_notify
        self.db.close()

    def test_01_async_load_fills_preview(self):
        from v2.ui import dialogs, async_task
        path = _make_csv(self.tmp / "异步订单.csv")
        dlg = dialogs.AddOrderDialog(None, self.db)
        dlg._load(str(path))                      # 异步入口
        self.assertFalse(dlg.btn_file.isEnabled(), "识别期间应禁用选择按钮")
        self.assertIn("正在识别", dlg.path_label.text())
        async_task.wait_all(dlg, timeout_ms=20000)
        self.assertEqual(dlg.table.rowCount(), 1, "回调后应填入预览表")
        self.assertEqual(dlg.table.item(0, 1).text(), "异步测试客户")
        self.assertTrue(dlg.btn_file.isEnabled(), "完成后按钮应恢复")
        self.assertTrue(dlg._ok_btn.isEnabled())

    def test_02_async_failure_is_reported(self):
        from v2.ui import dialogs, async_task
        dlg = dialogs.AddOrderDialog(None, self.db)
        dlg._load(str(self.tmp / "不存在.pdf"))
        async_task.wait_all(dlg, timeout_ms=20000)
        self.assertIn("读取失败", dlg.path_label.text())
        self.assertTrue(dlg.btn_file.isEnabled())
        self.assertEqual(dlg.table.rowCount(), 0)

    def test_03_async_task_tracks_owner(self):
        from v2.ui import async_task

        class Owner:
            pass

        owner = Owner()
        seen = []
        async_task.run_task(owner, lambda: 21 * 2, on_done=seen.append)
        async_task.wait_all(owner, timeout_ms=10000)
        self.assertEqual(seen, [42])
        self.assertFalse(async_task.busy(owner), "完成后应从 owner 上摘除任务")


if __name__ == "__main__":
    unittest.main(verbosity=2)

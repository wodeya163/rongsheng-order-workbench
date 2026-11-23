#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据安全 P0 回归（v2.6）

覆盖 P0-2 / P0-3 / P0-4：
  · schema_meta 版本号：新库直接到 CURRENT；v2.5 老库以 1 为基线并升到 2
  · 版本 2 迁移把附件绝对路径裁剪为相对 data/ 的路径
  · backup / list_backups / prune / auto_backup 每日一次 / restore_from 回滚
  · 日志落盘、异常堆栈可查、日志目录可定位
"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from v2.core import applog
from v2.core.db import CURRENT_SCHEMA_VERSION, Database


class SchemaVersionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v26_schema_"))
        self.path = self.tmp / "erp.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_new_db_is_current_version(self):
        db = Database(str(self.path))
        self.assertEqual(db.schema_version(), CURRENT_SCHEMA_VERSION)
        db.close()

    def test_02_legacy_db_upgrades_and_paths_become_relative(self):
        # 造一个 v2.5 老库：结构完整但无版本记录，附件存绝对路径
        legacy = Database(str(self.path))
        legacy.execute("DELETE FROM schema_meta")
        legacy.conn.execute("PRAGMA foreign_keys = OFF")
        legacy.insert("order_schedules", {
            "doc_no": "DDPS-202611-0001", "order_id": 1, "order_item_id": 1,
            "status": "待审批", "status_before": "已确认",
            "created_at": "2026-11-01T09:00:00", "updated_at": "2026-11-01T09:00:00",
        })
        # 外键依赖先关掉，直接插附件行（只验证路径迁移，不验证业务链路）
        abs_file = self.tmp / "订单排产附件" / "DDPS-202611-0001" / "图.png"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_bytes(b"PNG")
        legacy.insert("order_schedule_files", {
            "schedule_id": 1, "kind": "图片", "file_name": "图.png",
            "file_path": str(abs_file), "file_size": 3,
            "created_at": "2026-11-01T09:00:00",
        })
        legacy.close()

        db = Database(str(self.path))
        self.assertEqual(db.schema_version(), CURRENT_SCHEMA_VERSION)
        row = db.query_one("SELECT file_path FROM order_schedule_files")
        self.assertEqual(row["file_path"], "订单排产附件/DDPS-202611-0001/图.png")
        self.assertFalse(Path(row["file_path"]).is_absolute())
        db.close()

    def test_03_relative_paths_are_left_alone(self):
        db = Database(str(self.path))
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.insert("order_schedules", {
            "doc_no": "DDPS-202611-0002", "order_id": 1, "order_item_id": 1,
            "created_at": "2026-11-01T09:00:00", "updated_at": "2026-11-01T09:00:00",
        })
        db.insert("order_schedule_files", {
            "schedule_id": 1, "kind": "图纸", "file_name": "x.dwg",
            "file_path": "其他目录/外部图纸.dwg", "file_size": 1,
            "created_at": "2026-11-01T09:00:00",
        })
        db.execute("DELETE FROM schema_meta")
        db.close()

        db2 = Database(str(self.path))
        row = db2.query_one("SELECT file_path FROM order_schedule_files")
        # 不在 data/ 下的相对路径保持原样，不被误改
        self.assertEqual(row["file_path"], "其他目录/外部图纸.dwg")
        db2.close()


class BackupTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v26_backup_"))
        self.db = Database(str(self.tmp / "erp.db"))
        self.db.insert("bid_projects", {
            "doc_no": "ZB-202611-0001", "project_name": "备份测试",
            "status": "草稿", "created_at": "2026-11-01T09:00:00",
            "updated_at": "2026-11-01T09:00:00",
        })

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_11_backup_creates_readable_file(self):
        path = self.db.backup(tag="manual")
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, "backup")
        self.assertTrue(path.parent.is_dir())
        with sqlite3.connect(str(path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bid_projects").fetchone()[0], 1)

    def test_12_auto_backup_once_per_day(self):
        first = self.db.auto_backup()
        second = self.db.auto_backup()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.db.list_backups()), 1)

    def test_13_prune_keeps_latest(self):
        for i in range(5):
            self.db.backup(tag=f"m{i}", keep=0)
        self.assertEqual(len(self.db.list_backups()), 5)
        self.db.prune_backups(keep=2)
        self.assertEqual(len(self.db.list_backups()), 2)

    def test_14_restore_rolls_data_back(self):
        self.db.backup(tag="point")
        self.db.insert("bid_projects", {
            "doc_no": "ZB-202611-0002", "project_name": "备份之后新增",
            "status": "草稿", "created_at": "2026-11-02T09:00:00",
            "updated_at": "2026-11-02T09:00:00",
        })
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM bid_projects"), 2)
        target = self.db.list_backups()[0]["path"]
        self.db.restore_from(target)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM bid_projects"), 1)
        # 恢复前的现库也留了一份安全备份
        self.assertTrue(any("before-restore" in b["name"] for b in self.db.list_backups()))

    def test_15_memory_db_skips_backup(self):
        mem = Database(":memory:")
        self.assertIsNone(mem.backup())
        self.assertIsNone(mem.auto_backup())
        self.assertEqual(mem.list_backups(), [])
        mem.close()


class AppLogTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v26_log_"))
        applog.reset()

    def tearDown(self):
        applog.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_21_log_file_written_with_context(self):
        applog.setup(directory=str(self.tmp), context={"version": "test", "db": "x.db"})
        applog.get_logger("unit").warning("测试告警 %s", 42)
        log_file = next(self.tmp.glob("app-*.log"))
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("测试告警 42", text)
        self.assertIn("version=test", text)

    def test_22_exception_logged_with_traceback(self):
        applog.setup(directory=str(self.tmp))
        try:
            1 / 0
        except ZeroDivisionError as exc:
            applog.log_exception("单元测试动作", exc)
        text = next(self.tmp.glob("app-*.log")).read_text(encoding="utf-8")
        self.assertIn("操作失败：单元测试动作", text)
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("Traceback", text)

    def test_23_log_dir_is_deterministic(self):
        d = applog.setup(directory=str(self.tmp))
        self.assertEqual(d, self.tmp)
        self.assertEqual(applog.log_dir(), self.tmp)
        self.assertTrue(str(applog.current_log_file()).endswith(".log"))


if __name__ == "__main__":
    unittest.main()

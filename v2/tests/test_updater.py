#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动更新回归（v3.1.0 · 方案 B：清单 + 全量 zip + 目录交换）

覆盖：
  ① 版本比较（含位数不齐 / 非数字段）；
  ② 清单拉取（file 源 / 相对 zip 解析 / 缺字段 / 坏 JSON / 未配置）；
  ③ check() 三态（update / latest / error）；
  ④ 下载校验（file 源复制、进度回调、SHA256 不过 → 报错且删残留）；
  ⑤ 解压（zip 顶层为程序文件夹）；
  ⑥ apply_update_from_marker：目录交换成功路径 —— data 原位保留、旧目录进 _old_、
     staging 就位（launch=False，不真启动进程）；
  ⑦ 失败回滚 —— staging 缺失时旧目录与 data 完整还原；
  ⑧ 历史备份清理；
  ⑨ 更新源配置读写；
  ⑩ UI 冒烟：UpdateDialog 构建 / UpdateSourceDialog 保存。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import updater                                   # noqa: E402
from v2.core.errors import ValidationError                    # noqa: E402


def _sha(path: Path) -> str:
    return updater.sha256_of(path)


def _make_zip(root: Path, version: str) -> Path:
    """模拟 release 产物：顶层 ProductSuite_v2.2/ 内含 exe + 程序文件"""
    program = root / f"ProductSuite_v2.2_v{version}"
    program.mkdir(parents=True, exist_ok=True)
    (program / "ProductSuite_v2.2.exe").write_bytes(b"NEW-EXE-" + version.encode())
    (program / "v2_core_placeholder.dll").write_bytes(b"payload")
    zip_path = root / f"update_{version}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in program.rglob("*"):
            zf.write(f, Path("ProductSuite_v2.2") / f.relative_to(program))
    return zip_path


class VersionTest(unittest.TestCase):

    def test_01_parse_and_compare(self):
        self.assertEqual(updater.parse_version("3.0.1"), (3, 0, 1))
        self.assertEqual(updater.parse_version("3.1"), (3, 1, 0))
        self.assertEqual(updater.parse_version("v2.9.4-beta"), (2, 9, 4))
        self.assertLess(updater.compare_versions("3.0.1", "3.1"), 0)
        self.assertEqual(updater.compare_versions("3.0.1", "3.0.1"), 0)
        self.assertGreater(updater.compare_versions("3.1", "3.0.9"), 0)
        self.assertLess(updater.compare_versions("3.1", "3.1.0.1"), 0)


class ManifestTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd_manifest_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_02_fetch_file_manifest_resolves_relative_zip(self):
        zip_path = _make_zip(self.tmp, "9.9.9")
        manifest_file = self.tmp / "manifest.json"
        manifest_file.write_text(json.dumps({
            "version": "9.9.9", "zip": zip_path.name,
            "sha256": _sha(zip_path), "note": "测试", "date": "2026-11-08",
        }, ensure_ascii=False), encoding="utf-8")
        m = updater.fetch_manifest(str(manifest_file))
        self.assertEqual(m["version"], "9.9.9")
        self.assertTrue(Path(m["zip"]).is_file(), "相对 zip 应按 manifest 目录解析")
        self.assertEqual(m["note"], "测试")

    def test_03_missing_field_rejected(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"version": "1.0", "zip": "x.zip"}))
        with self.assertRaises(ValidationError):
            updater.fetch_manifest(str(bad))

    def test_04_bad_json_rejected(self):
        bad = self.tmp / "worse.json"
        bad.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValidationError):
            updater.fetch_manifest(str(bad))

    def test_05_empty_source_rejected_with_hint(self):
        with self.assertRaises(ValidationError) as ctx:
            updater.fetch_manifest("")
        self.assertIn("更新源", str(ctx.exception))

    def test_06_check_three_states(self):
        zip_path = _make_zip(self.tmp, "3.2")
        src = self.tmp / "manifest.json"
        src.write_text(json.dumps({
            "version": "3.2", "zip": zip_path.name, "sha256": _sha(zip_path)}))
        s = str(src)
        self.assertEqual(updater.check(s, "3.0.1")["status"], "update")
        self.assertEqual(updater.check(s, "3.2")["status"], "latest")
        self.assertEqual(updater.check(s, "4.0")["status"], "latest")
        self.assertEqual(updater.check(str(self.tmp / "nope.json"),
                                       "3.0.1")["status"], "error")


class DownloadUnpackTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd_dl_"))
        self.zip_path = _make_zip(self.tmp, "3.1")
        self.manifest = {"version": "3.1", "zip": str(self.zip_path),
                         "sha256": _sha(self.zip_path), "note": "", "date": ""}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_07_download_copies_and_reports_progress(self):
        dest = self.tmp / "dl"
        seen = []
        out = updater.download_zip(self.manifest, dest,
                                   progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(out.is_file())
        self.assertEqual(_sha(out), self.manifest["sha256"])
        self.assertTrue(seen and seen[-1][0] > 0)

    def test_08_bad_sha256_rejected_and_cleaned(self):
        dest = self.tmp / "dl2"
        bad = dict(self.manifest, sha256="0" * 64)
        with self.assertRaises(ValidationError) as ctx:
            updater.download_zip(bad, dest)
        self.assertIn("SHA256", str(ctx.exception))
        self.assertFalse((dest / "update_3.1.zip").exists(),
                         "校验失败必须删除残留文件")

    def test_09_unpack_returns_program_dir(self):
        staging = self.tmp / "staging"
        program = updater.unpack_zip(self.zip_path, staging)
        self.assertEqual(program.name, "ProductSuite_v2.2")
        self.assertTrue((program / "ProductSuite_v2.2.exe").is_file())


class ApplyUpdateTest(unittest.TestCase):
    """目录交换：成功路径 / 回滚路径（launch=False，不真启动进程）"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="upd_swap_"))
        # 旧程序目录（target）
        self.target = self.base / "ProductSuite_v2.2"
        self.target.mkdir()
        (self.target / "ProductSuite_v2.2.exe").write_bytes(b"OLD-EXE")
        (self.target / "old_payload.txt").write_text("old", encoding="utf-8")
        self.data_dir = self.target / "data"
        self.data_dir.mkdir()
        (self.data_dir / "erp.db").write_bytes(b"PRECIOUS-DB")
        (self.data_dir / "订单排产附件").mkdir()
        # staging（新程序目录）
        self.staging = self.base / "_update_staging" / "ProductSuite_v2.2"
        self.staging.mkdir(parents=True)
        (self.staging / "ProductSuite_v2.2.exe").write_bytes(b"NEW-EXE")
        (self.staging / "new_payload.txt").write_text("new", encoding="utf-8")
        self.marker = {
            "target_dir": str(self.target), "staging_dir": str(self.staging),
            "exe_name": "ProductSuite_v2.2.exe", "base_dir": str(self.base),
            "log": str(self.base / "_update_apply.log"), "launch": False,
        }

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_10_swap_success_keeps_data(self):
        res = updater.apply_update_from_marker(self.marker, launch=False)
        self.assertTrue(res["ok"], res["message"])
        # 新程序就位
        self.assertEqual((self.target / "ProductSuite_v2.2.exe").read_bytes(),
                         b"NEW-EXE")
        self.assertTrue((self.target / "new_payload.txt").is_file())
        # data 原位保留、内容原样
        self.assertTrue((self.target / "data" / "erp.db").is_file())
        self.assertEqual((self.target / "data" / "erp.db").read_bytes(),
                         b"PRECIOUS-DB")
        self.assertTrue((self.target / "data" / "订单排产附件").is_dir())
        # 旧目录进 _old_ 备份
        olds = list(self.base.glob("_old_*"))
        self.assertEqual(len(olds), 1)
        self.assertEqual((olds[0] / "old_payload.txt").read_text(), "old")
        # v3.4.4：staging 由「移动」改为「复制」，更新器进程仍运行其中，
        # 不能自删，留待下次 prepare_update 清空（规避 WinError 5）
        self.assertTrue(self.staging.exists())

    def test_11_failure_rolls_back(self):
        # staging 目录丢失 → ③ 必然失败 → 必须还原旧目录与 data
        shutil.rmtree(self.staging)
        res = updater.apply_update_from_marker(self.marker, launch=False)
        self.assertFalse(res["ok"])
        self.assertTrue((self.target / "ProductSuite_v2.2.exe").is_file())
        self.assertEqual((self.target / "ProductSuite_v2.2.exe").read_bytes(),
                         b"OLD-EXE")
        self.assertTrue((self.target / "data" / "erp.db").is_file())
        self.assertEqual((self.target / "data" / "erp.db").read_bytes(),
                         b"PRECIOUS-DB")

    def test_12_run_apply_update_with_marker_file(self):
        marker_path = self.base / "marker.json"
        marker_path.write_text(json.dumps(self.marker), encoding="utf-8")
        self.assertEqual(updater.run_apply_update(str(marker_path)), 0)
        self.assertTrue((self.target / "data" / "erp.db").is_file())

    def test_13_cleanup_old_backups(self):
        for name in ("_old_20260101_000000", "_old_20260102_000000", "keep_me"):
            d = self.base / name
            d.mkdir()
            (d / "x").write_text("x")
        n = updater.cleanup_old_backups(self.base, keep="_old_20260102_000000")
        self.assertEqual(n, 1)
        self.assertTrue((self.base / "_old_20260102_000000").is_dir())
        self.assertFalse((self.base / "_old_20260101_000000").exists())
        self.assertTrue((self.base / "keep_me").is_dir())


class SourceConfigTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd_src_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_14_source_roundtrip(self):
        self.assertEqual(updater.load_source(self.tmp), "")
        updater.save_source(self.tmp, r"\\server\share\manifest.json")
        self.assertEqual(updater.load_source(self.tmp),
                         r"\\server\share\manifest.json")
        updater.save_source(self.tmp, "")
        self.assertEqual(updater.load_source(self.tmp), "")


class _UiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("未安装 PySide6")
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)


class UpdateDialogSmokeTest(_UiBase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd_dlg_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_15_dialog_builds_with_manifest(self):
        from v2.ui.update_dialog import UpdateDialog
        from v2 import __version__
        manifest = {"version": "3.2", "note": "修复若干问题", "date": "2026-11-08"}
        dlg = UpdateDialog(None, manifest)
        texts = " ".join(w.text() for w in dlg.findChildren(type(dlg.hint)))
        self.assertIn("3.2", texts)
        self.assertIn(__version__, texts)       # 当前版本对比出现在界面上
        self.assertTrue(dlg.btn_update.isEnabled())
        dlg.close()

    def test_16_source_dialog_saves(self):
        from v2.ui.update_dialog import UpdateSourceDialog
        dlg = UpdateSourceDialog(None, self.tmp)
        dlg.edit.setText(r"\\server\share\manifest.json")
        dlg._save()
        self.assertEqual(updater.load_source(self.tmp),
                         r"\\server\share\manifest.json")


if __name__ == "__main__":
    unittest.main()

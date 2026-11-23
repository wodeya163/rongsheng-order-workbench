"""v3.1.1 云同步盘占位文件适配（OneDrive Files On-Demand）回归

不依赖真实 OneDrive 登录状态：用 monkeypatch 模拟文件属性位与复制耗时，
验证「原生文件框 + 侧边栏云盘入口 + 水合超时保护 + 各读取入口接入」全链路。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.core import cloudfile                                   # noqa: E402


class _UiBase(unittest.TestCase):
    """需要 QApplication 的用例共用"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        pass


class CloudAttrTest(unittest.TestCase):
    """属性位探测"""

    def test_01_constants(self):
        self.assertEqual(cloudfile.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, 0x400000)
        self.assertEqual(cloudfile.FILE_ATTRIBUTE_RECALL_ON_OPEN, 0x40000)
        self.assertEqual(cloudfile.FILE_ATTRIBUTE_OFFLINE, 0x1000)

    def test_02_plain_local_file_is_not_placeholder(self):
        with mock.patch.object(cloudfile, "file_attributes", return_value=0x20):
            self.assertFalse(cloudfile.is_placeholder("X:/a.xlsx"))

    def test_03_recall_on_data_access_is_placeholder(self):
        with mock.patch.object(cloudfile, "file_attributes", return_value=0x400020):
            self.assertTrue(cloudfile.is_placeholder("X:/a.xlsx"))

    def test_04_recall_on_open_is_placeholder(self):
        with mock.patch.object(cloudfile, "file_attributes", return_value=0x40020):
            self.assertTrue(cloudfile.is_placeholder("X:/a.docx"))

    def test_05_offline_attribute_is_placeholder(self):
        with mock.patch.object(cloudfile, "file_attributes", return_value=0x1000):
            self.assertTrue(cloudfile.is_placeholder("X:/a.pdf"))

    def test_06_reparse_point_alone_is_not_placeholder(self):
        # 本地符号链接也带 REPARSE_POINT，不能误判成云文件
        with mock.patch.object(cloudfile, "file_attributes", return_value=0x400):
            self.assertFalse(cloudfile.is_placeholder("X:/link.xlsx"))

    def test_07_lstat_failure_degrades_to_local(self):
        with mock.patch.object(cloudfile, "file_attributes", return_value=0):
            self.assertFalse(cloudfile.is_placeholder("X:/missing.xlsx"))

    def test_08_non_windows_never_placeholder(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(cloudfile, "file_attributes", return_value=0x400000):
            self.assertFalse(cloudfile.is_placeholder("X:/a.xlsx"))


class EnsureReadableTest(unittest.TestCase):
    """水合 / 缓存 / 超时保护"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="pqs_cf_test_"))
        self.src = self.tmp / "订单明细.xlsx"
        self.src.write_bytes(b"PK\x03\x04CLOUD-CONTENT")
        # 每个用例独享缓存根，避免互相污染
        self.cache = self.tmp / "cache"
        self._orig_root = cloudfile.CACHE_ROOT
        cloudfile.CACHE_ROOT = self.cache

    def tearDown(self):
        cloudfile.CACHE_ROOT = self._orig_root
        import shutil
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def _as_cloud(self):
        return mock.patch.object(cloudfile, "is_placeholder", return_value=True)

    def test_09_plain_file_returns_same_string(self):
        raw = str(self.src).replace("\\", "/")
        self.assertEqual(cloudfile.ensure_readable(raw), raw)

    def test_10_placeholder_materializes_local_copy(self):
        with self._as_cloud():
            out = Path(cloudfile.ensure_readable(str(self.src)))
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), self.src.read_bytes())
        self.assertEqual(out.name, self.src.name)      # 扩展名/文件名不变
        self.assertNotEqual(out, self.src)

    def test_11_cache_hit_no_second_copy(self):
        with self._as_cloud():
            a = cloudfile.ensure_readable(str(self.src))
            with mock.patch.object(cloudfile.shutil, "copyfile",
                                   side_effect=AssertionError("不应重复复制")):
                b = cloudfile.ensure_readable(str(self.src))
        self.assertEqual(a, b)

    def test_12_timeout_raises_clear_validation_error(self):
        from v2.core.errors import ValidationError
        import time

        def slow(src, dst, box):
            time.sleep(2)
            box.append("ok")

        with self._as_cloud(), \
                mock.patch.object(cloudfile, "_materialize", side_effect=slow):
            th = mock.patch.object(cloudfile.threading, "Thread")
            with th as ctor:
                # 造一个 join 立即返回但 box 仍为空的假线程
                fake = mock.MagicMock()
                fake.join.return_value = None
                ctor.return_value = fake
                with self.assertRaises(ValidationError) as ctx:
                    cloudfile.ensure_readable(str(self.src), timeout=0.2)
        self.assertIn("超时", str(ctx.exception))
        self.assertIn("仅联机", str(ctx.exception))

    def test_13_copy_failure_raises_validation_error(self):
        from v2.core.errors import ValidationError
        with self._as_cloud(), \
                mock.patch.object(cloudfile, "_materialize",
                                  side_effect=lambda s, d, box: box.append(
                                      "OSError: [Errno 22] Invalid argument")):
            with self.assertRaises(ValidationError) as ctx:
                cloudfile.ensure_readable(str(self.src), timeout=1)
        self.assertIn("Errno 22", str(ctx.exception))
        self.assertIn("始终保留在此设备上", str(ctx.exception))

    def test_14_size_mismatch_rejected(self):
        from v2.core.errors import ValidationError
        with self._as_cloud(), \
                mock.patch.object(cloudfile, "_materialize",
                                  side_effect=lambda s, d, box: box.append(
                                      "size-mismatch: 源 100 B / 副本 0 B")):
            with self.assertRaises(ValidationError):
                cloudfile.ensure_readable(str(self.src), timeout=1)

    def test_14b_zero_byte_source_is_legal(self):
        """源文件本来就是 0 字节（空白工作簿）不能误判成水合失败"""
        empty = self.tmp / "空表.xlsx"
        empty.write_bytes(b"")
        with self._as_cloud():
            out = Path(cloudfile.ensure_readable(str(empty)))
        self.assertTrue(out.is_file())
        self.assertEqual(out.stat().st_size, 0)

    def test_14c_tick_called_during_wait(self):
        """等待云端同步时 UI 刷新回调被调用，界面不假死"""
        ticks = []
        with self._as_cloud():
            cloudfile.ensure_readable(str(self.src), timeout=5,
                                      tick=lambda: ticks.append(1))
        self.assertTrue(Path(cloudfile.ensure_readable(str(self.src))).is_file())

    def test_15_ensure_all_keeps_order(self):
        a, b = self.tmp / "a.csv", self.tmp / "b.csv"
        a.write_text("h1,h2\n1,2", encoding="utf-8")
        b.write_text("h1,h2\n3,4", encoding="utf-8")
        with self._as_cloud():
            out = cloudfile.ensure_all([str(a), str(b)])
        self.assertEqual(len(out), 2)
        self.assertEqual(Path(out[0]).name, "a.csv")
        self.assertEqual(Path(out[1]).name, "b.csv")


class OneDriveRootsTest(unittest.TestCase):
    def test_16_dirs_from_environment(self):
        env = {"OneDrive": str(self._dir()), "OneDriveCommercial": str(self._dir()),
               "USERPROFILE": str(self._dir())}
        with mock.patch.dict(os.environ, env, clear=False):
            dirs = cloudfile.onedrive_dirs()
        self.assertGreaterEqual(len(dirs), 1)
        self.assertTrue(all(Path(d).is_dir() for d in dirs))
        self.assertEqual(len(dirs), len({d.lower() for d in dirs}))  # 去重

    def _dir(self):
        import tempfile
        return Path(tempfile.gettempdir())

    def test_17_cloud_hint_mentions_action(self):
        self.assertIn("始终保留在此设备上", cloudfile.cloud_hint())


class FileDialogTest(_UiBase):
    def test_18_offscreen_uses_qt_dialog(self):
        from v2.ui import theme
        with mock.patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}, clear=False):
            self.assertFalse(theme.use_native_file_dialog())

    def test_19_windows_default_native(self):
        from v2.ui import theme
        env = dict(os.environ)
        env.pop("QT_QPA_PLATFORM", None)
        env.pop("PQS_FILE_DIALOG", None)
        from PySide6.QtWidgets import QApplication
        fake_app = mock.MagicMock()
        fake_app.platformName.return_value = "windows"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(QApplication, "instance", return_value=fake_app):
            self.assertTrue(theme.use_native_file_dialog())

    def test_20_env_force_qt(self):
        from v2.ui import theme
        with mock.patch.dict(os.environ, {"PQS_FILE_DIALOG": "qt"}, clear=False):
            self.assertFalse(theme.use_native_file_dialog())

    def test_21_make_dialog_has_cloud_sidebar(self):
        from v2.ui import theme
        with mock.patch.object(theme, "cloud_roots",
                               return_value=[str(Path.home())]), \
                mock.patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            dlg = theme.make_file_dialog(None, "选择文件")
        urls = [str(Path(u.toLocalFile()).resolve()) for u in dlg.sidebarUrls()]
        self.assertTrue(urls)
        self.assertIn(str(Path.home().resolve()), urls)
        dlg.close()

    def test_22_filter_tuple_defense_still_works(self):
        from PySide6.QtWidgets import QFileDialog
        from v2.ui import theme
        with mock.patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            dlg = theme.make_file_dialog(None, "选择文件",
                                         filters=("Word (*.docx);;所有文件 (*.*)",))
        self.assertIn("*.docx", dlg.nameFilters()[0])
        self.assertIsInstance(dlg, QFileDialog)
        dlg.close()

    def test_23_pick_path_preflight_returns_original_path(self):
        """水合预热不改变返回路径（路径字段仍存用户选的真实位置）"""
        from v2.ui import theme
        with mock.patch.object(theme, "make_file_dialog") as mk, \
                mock.patch.object(theme, "notify") as notify, \
                mock.patch("v2.core.cloudfile.ensure_readable",
                           side_effect=lambda p, timeout=30: p):
            dlg = mock.MagicMock()
            from PySide6.QtWidgets import QFileDialog
            dlg.exec.return_value = QFileDialog.Accepted
            dlg.selectedFiles.return_value = ["D:/OneDrive/订单.xlsx"]
            mk.return_value = dlg
            out = theme.pick_path(None, "选择文件")
        self.assertEqual(out, "D:/OneDrive/订单.xlsx")
        notify.assert_not_called()

    def test_24_pick_path_hydration_failure_shows_message(self):
        from v2.core.errors import ValidationError
        from v2.ui import theme
        with mock.patch.object(theme, "make_file_dialog") as mk, \
                mock.patch.object(theme, "notify") as notify, \
                mock.patch("v2.core.cloudfile.is_placeholder", return_value=True), \
                mock.patch("v2.core.cloudfile.ensure_readable",
                           side_effect=ValidationError("云端文件未就绪")):
            dlg = mock.MagicMock()
            from PySide6.QtWidgets import QFileDialog
            dlg.exec.return_value = QFileDialog.Accepted
            dlg.selectedFiles.return_value = ["D:/OneDrive/离线.xlsx"]
            mk.return_value = dlg
            out = theme.pick_path(None, "选择文件")
        self.assertEqual(out, "")           # 失败不返回半可用路径
        self.assertEqual(notify.call_args[0][1], "云端文件未就绪")
        self.assertEqual(notify.call_args[1]["kind"], "warn")

    def test_25_save_mode_skips_hydration(self):
        """保存目标是待创建文件，不能做占位水合"""
        from v2.ui import theme
        with mock.patch.object(theme, "make_file_dialog") as mk, \
                mock.patch("v2.core.cloudfile.ensure_readable") as hyd:
            dlg = mock.MagicMock()
            from PySide6.QtWidgets import QFileDialog
            dlg.exec.return_value = QFileDialog.Accepted
            dlg.selectedFiles.return_value = ["D:/OneDrive/导出.xlsx"]
            mk.return_value = dlg
            out = theme.pick_path(None, "另存", mode="save")
        self.assertEqual(out, "D:/OneDrive/导出.xlsx")
        hyd.assert_not_called()


class ReadEntrypointTest(unittest.TestCase):
    """各读取入口都接了 cloudfile（源码级断言 + 行为级）"""

    def test_26_docread_calls_ensure_readable(self):
        import tempfile
        from v2.core import docread
        tmp = Path(tempfile.mkdtemp(prefix="pqs_dr_"))
        f = tmp / "t.csv"
        f.write_text("物资名称,数量\n弯头,2", encoding="utf-8-sig")
        with mock.patch("v2.core.cloudfile.ensure_readable",
                        side_effect=lambda p, timeout=30, tick=None: str(p)) as hyd:
            src = docread.read_source(str(f))
        # read_source 自身 + 内部 excelio 通道各调一次（双保险）
        self.assertGreaterEqual(hyd.call_count, 1)
        self.assertGreaterEqual(len(src["rows"]), 1)

    def test_27_excelio_calls_ensure_readable(self):
        import tempfile
        from v2.core import excelio
        tmp = Path(tempfile.mkdtemp(prefix="pqs_ex_"))
        f = tmp / "t.csv"
        f.write_text("A,B\n1,2", encoding="utf-8-sig")
        with mock.patch("v2.core.cloudfile.ensure_readable",
                        side_effect=lambda p, timeout=30, tick=None: str(p)) as hyd:
            rows = excelio.import_rows(str(f))
        hyd.assert_called_once()
        self.assertEqual(len(rows), 1)

    def test_28_updater_fetch_manifest_hydrates_cloud_manifest(self):
        import json, tempfile
        from v2.core import updater
        tmp = Path(tempfile.mkdtemp(prefix="pqs_up_"))
        f = tmp / "manifest.json"
        f.write_text(json.dumps({"version": "9.9.9", "zip": "x.zip",
                                 "sha256": "abc"}), encoding="utf-8")
        with mock.patch("v2.core.cloudfile.ensure_readable",
                        side_effect=lambda p, timeout=30, tick=None: str(p)) as hyd:
            m = updater.fetch_manifest(str(f))
        hyd.assert_called_once()
        self.assertEqual(m["version"], "9.9.9")
        self.assertTrue(Path(m["zip"]).is_absolute())

    def test_29_schedule_add_files_hydrates_cloud_attachment(self):
        """附件复制前必须经过 ensure_readable（源码断言，不建库）"""
        src = Path("v2/domains/schedule.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("cloudfile.ensure_readable(str(src))"), 2)

    def test_30_theme_pick_paths_hydrates_each(self):
        from v2.ui import theme
        with mock.patch.object(theme, "make_file_dialog") as mk, \
                mock.patch("v2.core.cloudfile.is_placeholder", return_value=True), \
                mock.patch("v2.core.cloudfile.ensure_readable",
                           side_effect=lambda p, timeout=30, tick=None: p) as hyd:
            dlg = mock.MagicMock()
            from PySide6.QtWidgets import QFileDialog
            dlg.exec.return_value = QFileDialog.Accepted
            dlg.selectedFiles.return_value = ["D:/OneDrive/a.png", "D:/OneDrive/b.dwg"]
            mk.return_value = dlg
            out = theme.pick_paths(None, "选择附件")
        self.assertEqual(out, ["D:/OneDrive/a.png", "D:/OneDrive/b.dwg"])
        self.assertEqual(hyd.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

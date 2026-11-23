#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.5.4 GitHub Releases 自动更新回归

覆盖：
  ① github:owner/repo[@tag] 源解析与资产直链生成（中文资产名 / 路径穿越拒绝）；
  ② fetch_manifest(github:) —— 绝对 zip URL、相对名补全、域名白名单、
     缺字段 / 网络失败的可读提示；
  ③ download_zip 走 HTTP 分支下载 GitHub 资产 + 进度回调 + SHA256；
  ④ check() 主源失败自动回退备用清单；
  ⑤ updater.json 的 fallback 读写与旧版本地源一次性迁移；
  ⑥ 设置更新源对话框默认推荐 GitHub 源、切换时保留旧源为备用。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core import updater                                   # noqa: E402
from v2.core.errors import ValidationError                    # noqa: E402

OWNER, REPO = "wodeya163", "rongsheng-order-workbench"
GH = f"github:{OWNER}/{REPO}"


class _Resp:
    """可同时用于 fetch_manifest(read()) 与 download_zip(read(CHUNK)) 的假响应"""

    def __init__(self, body: bytes, length=None):
        self._body = body
        self._pos = 0
        self.headers = {"Content-Length": str(
            len(body) if length is None else length)}

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._pos = self._body[self._pos:], len(self._body)
            return out
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _manifest_bytes(zip_ref: str, version: str = "9.9.9") -> bytes:
    return json.dumps({
        "version": version,
        "zip": zip_ref,
        "sha256": "a" * 64,
        "note": "GitHub 发布测试",
        "date": "2026-11-24",
    }, ensure_ascii=False).encode("utf-8")


class GithubSourceParseTest(unittest.TestCase):

    def test_01_parse_plain(self):
        self.assertEqual(updater.parse_github_source(GH),
                         {"owner": OWNER, "repo": REPO, "tag": ""})

    def test_02_parse_tag_and_spaces(self):
        self.assertEqual(
            updater.parse_github_source("  github: wodeya163/rongsheng-order-workbench @v3.5.4  "),
            {"owner": OWNER, "repo": REPO, "tag": "v3.5.4"})

    def test_03_non_github_sources_are_none(self):
        for s in ("", "github:onlyowner", "https://github.com/a/b",
                  r"D:\发布\manifest.json", "github:a/b/c"):
            self.assertIsNone(updater.parse_github_source(s), s)

    def test_04_asset_url_latest_and_tag(self):
        self.assertEqual(
            updater.github_asset_url(OWNER, REPO, "manifest.json"),
            "https://github.com/wodeya163/rongsheng-order-workbench/"
            "releases/latest/download/manifest.json")
        self.assertEqual(
            updater.github_asset_url(OWNER, REPO, "订单管理工作台_v3.5.4.zip", "v3.5.4"),
            "https://github.com/wodeya163/rongsheng-order-workbench/"
            "releases/download/v3.5.4/"
            "%E8%AE%A2%E5%8D%95%E7%AE%A1%E7%90%86%E5%B7%A5%E4%BD%9C%E5%8F%B0_v3.5.4.zip")

    def test_05_asset_url_rejects_path_traversal(self):
        for bad in ("", "../evil.zip", "a/b.zip", r"a\b.zip", "..", "x\x00y"):
            with self.assertRaises(ValidationError):
                updater.github_asset_url(OWNER, REPO, bad)

    def test_06_default_source_constant(self):
        self.assertEqual(updater.DEFAULT_GITHUB_SOURCE, GH)
        self.assertIsNotNone(updater.parse_github_source(
            updater.DEFAULT_GITHUB_SOURCE))


class GithubManifestTest(unittest.TestCase):

    def _fetch(self, body: bytes, source: str = GH):
        with patch.object(updater, "_http_get",
                          side_effect=lambda *a, **kw: _Resp(body)) as m:
            result = updater.fetch_manifest(source)
        return result, m

    def test_07_absolute_zip_url_kept(self):
        url = ("https://github.com/wodeya163/rongsheng-order-workbench/"
               "releases/download/v9.9.9/update.zip")
        res, mocked = self._fetch(_manifest_bytes(url))
        self.assertEqual(res["version"], "9.9.9")
        self.assertEqual(res["zip"], url)
        self.assertEqual(res["sha256"], "a" * 64)
        self.assertEqual(
            mocked.call_args[0][0],
            updater.github_asset_url(OWNER, REPO, "manifest.json"))

    def test_08_relative_zip_name_resolved_as_asset(self):
        res, _ = self._fetch(_manifest_bytes("订单管理工作台_v9.9.9.zip"))
        self.assertEqual(res["zip"], updater.github_asset_url(
            OWNER, REPO, "订单管理工作台_v9.9.9.zip"))

    def test_09_fixed_tag_source(self):
        res, mocked = self._fetch(_manifest_bytes("update.zip"),
                                  source=f"{GH}@v3.5.4")
        self.assertIn("/releases/download/v3.5.4/manifest.json",
                      mocked.call_args[0][0])
        self.assertIn("/releases/download/v3.5.4/update.zip", res["zip"])

    def test_10_foreign_zip_host_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self._fetch(_manifest_bytes("https://evil.example.com/u.zip"))
        self.assertIn("GitHub 域名", str(ctx.exception))

    def test_11_missing_field(self):
        with self.assertRaises(ValidationError) as ctx:
            self._fetch(json.dumps({"version": "9.9.9"}).encode("utf-8"))
        self.assertIn("缺少必填字段", str(ctx.exception))

    def test_12_network_error_is_readable(self):
        with patch.object(updater, "_http_get",
                          side_effect=OSError("HTTP Error 404")):
            with self.assertRaises(ValidationError) as ctx:
                updater.fetch_manifest(GH)
        msg = str(ctx.exception)
        self.assertIn("GitHub", msg)
        self.assertIn("manifest.json", msg)

    def test_13_local_manifest_still_works(self):
        tmp = Path(tempfile.mkdtemp(prefix="upd_gh_local_"))
        try:
            (tmp / "manifest.json").write_bytes(
                _manifest_bytes("update.zip", "1.2.3"))
            res = updater.fetch_manifest(str(tmp / "manifest.json"))
            self.assertEqual(res["version"], "1.2.3")
            self.assertEqual(res["zip"], str(tmp / "update.zip"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_14_plain_http_relative_zip_uses_urljoin(self):
        body = _manifest_bytes("update.zip", "2.0.0")
        with patch.object(updater, "_http_get",
                          side_effect=lambda *a, **kw: _Resp(body)):
            res = updater.fetch_manifest("https://host/dir/manifest.json")
        self.assertEqual(res["zip"], "https://host/dir/update.zip")


class GithubDownloadTest(unittest.TestCase):

    def test_15_download_and_progress(self):
        payload = b"GITHUB-ASSET-PAYLOAD" * 4096
        digest = hashlib.sha256(payload).hexdigest()
        tmp = Path(tempfile.mkdtemp(prefix="upd_gh_dl_"))
        try:
            manifest = {"version": "9.9.9",
                        "zip": "https://github.com/o/r/releases/download/v9/u.zip",
                        "sha256": digest}
            seen = []
            with patch.object(updater, "_http_get",
                              side_effect=lambda *a, **kw: _Resp(payload)) as m:
                out = updater.download_zip(manifest, tmp,
                                           progress=lambda d, t: seen.append((d, t)))
            self.assertEqual(updater.sha256_of(out), digest)
            self.assertEqual(seen[-1][0], len(payload))
            self.assertEqual(m.call_args[0][1], updater.DOWNLOAD_TIMEOUT)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_16_broken_download_removes_partial_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="upd_gh_fail_"))
        try:
            manifest = {"version": "9.9.9", "zip": "https://github.com/o/r/u.zip",
                        "sha256": "b" * 64}
            with patch.object(updater, "_http_get",
                              side_effect=OSError("connection reset")):
                with self.assertRaises(ValidationError) as ctx:
                    updater.download_zip(manifest, tmp)
            self.assertIn("下载中断", str(ctx.exception))
            self.assertEqual(list(tmp.glob("*.zip")), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SourceMigrationTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd_gh_cfg_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_17_fallback_roundtrip(self):
        self.assertEqual(updater.load_fallback(self.tmp), "")
        updater.save_source(self.tmp, GH, fallback=r"D:\发布\manifest.json")
        self.assertEqual(updater.load_source(self.tmp), GH)
        self.assertEqual(updater.load_fallback(self.tmp), r"D:\发布\manifest.json")
        # fallback=None 时保留备用源
        updater.save_source(self.tmp, GH)
        self.assertEqual(updater.load_fallback(self.tmp), r"D:\发布\manifest.json")
        updater.save_source(self.tmp, "https://x/manifest.json", fallback="")
        self.assertEqual(updater.load_fallback(self.tmp), "")

    def test_18_legacy_local_source_migrates_once(self):
        legacy = r"D:\Programs\ProductSuite_发布\manifest.json"
        updater.save_source(self.tmp, legacy)
        self.assertEqual(updater.migrate_source(self.tmp),
                         updater.DEFAULT_GITHUB_SOURCE)
        self.assertEqual(updater.load_fallback(self.tmp), legacy)
        self.assertEqual(updater.migrate_source(self.tmp),
                         updater.DEFAULT_GITHUB_SOURCE)   # 幂等

    def test_19_custom_source_untouched(self):
        custom = r"\\server\share\my\manifest.json"
        updater.save_source(self.tmp, custom)
        self.assertEqual(updater.migrate_source(self.tmp), custom)
        self.assertEqual(updater.load_fallback(self.tmp), "")

    def test_20_check_falls_back_offline(self):
        good = self.tmp / "manifest.json"
        good.write_bytes(_manifest_bytes("update.zip", "7.7.7"))
        with patch.object(updater, "fetch_manifest",
                          side_effect=[OSError("GitHub 不可达"),
                                       updater.fetch_manifest(str(good))]):
            res = updater.check(GH, "3.5.4", fallback=str(good))
        self.assertEqual(res["status"], "update")
        self.assertEqual(res["manifest"]["version"], "7.7.7")

    def test_21_check_error_without_fallback(self):
        with patch.object(updater, "fetch_manifest",
                          side_effect=OSError("offline")):
            res = updater.check(GH, "3.5.4")
        self.assertEqual(res["status"], "error")


class SourceDialogTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("未安装 PySide6")
        cls.app = QApplication.instance() or QApplication([])
        from v2.ui import theme
        theme.apply_theme(cls.app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd_gh_ui_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_22_dialog_suggests_github_by_default(self):
        from v2.ui.update_dialog import UpdateSourceDialog
        dlg = UpdateSourceDialog(None, self.tmp)
        self.assertEqual(dlg.edit.text(), updater.DEFAULT_GITHUB_SOURCE)
        dlg.close()

    def test_23_switch_to_github_keeps_old_source_as_fallback(self):
        from v2.ui.update_dialog import UpdateSourceDialog
        legacy = r"D:\Programs\ProductSuite_发布\manifest.json"
        updater.save_source(self.tmp, legacy)
        dlg = UpdateSourceDialog(None, self.tmp)
        self.assertEqual(dlg.edit.text(), legacy)
        dlg.edit.setText(updater.DEFAULT_GITHUB_SOURCE)
        dlg._save()
        self.assertEqual(updater.load_source(self.tmp),
                         updater.DEFAULT_GITHUB_SOURCE)
        self.assertEqual(updater.load_fallback(self.tmp), legacy)
        dlg.close()


if __name__ == "__main__":
    unittest.main()

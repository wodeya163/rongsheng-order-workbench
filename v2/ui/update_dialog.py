#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查更新对话框（v3.1.0 · 方案 B 半自动更新）

入口：帮助 → 检查更新… / 设置更新源… ；启动 3 秒后自动后台检查一次。
流程：check()（子线程）→ 有新版 → 本对话框展示版本对比与更新说明
      → 「立即更新」→ prepare_update()（子线程：下载 + SHA256 + 解压 + 写 marker）
      → spawn_apply_update() 启动更新器进程 → 主程序退出，更新器交换目录后拉起新版。
失败兜底：下载/校验失败只提示不动作；目录交换失败自动回滚，旧程序照常可用。
"""
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QVBoxLayout,
)

from .. import __version__ as APP_VERSION
from ..core import current_user as cur
from ..core import updater
from . import theme
from . import async_task
from .base_dialog import ThemedDialog


class _Bridge(QObject):
    """子线程回调 → 主线程 UI 的信号桥（prepare_update 的两个回调都在工作线程触发）"""
    stage_text = Signal(str)
    bytes_progress = Signal(int, int)


class UpdateDialog(ThemedDialog):
    """展示清单版本对比；「立即更新」走 prepare → swap 全流程"""

    def __init__(self, parent, manifest: Dict[str, Any]):
        super().__init__(parent, "发现新版本",
                         f"当前 v{APP_VERSION}　→　最新 v{manifest.get('version')}",
                         width=560, height=420)
        self.manifest = manifest
        self._updating = False
        v = self.body_layout

        # 版本对比卡
        info = QFrame()
        info.setObjectName("Card")
        grid = QGridLayout(info)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        pairs = [
            ("当前版本", f"v{APP_VERSION}", "最新版本", f"v{manifest.get('version')}"),
            ("发布日期", manifest.get("date") or "—", "更新包", "全量 zip（SHA256 校验）"),
        ]
        for r, (k1, v1, k2, v2) in enumerate(pairs):
            grid.addWidget(theme.label(k1, "DialogHint"), r, 0)
            grid.addWidget(theme.label(str(v1), "DialogCount"), r, 1)
            grid.addWidget(theme.label(k2, "DialogHint"), r, 2)
            grid.addWidget(theme.label(str(v2), "DialogCount"), r, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        v.addWidget(info)

        note = str(manifest.get("note") or "").strip() or "（发布人未填写更新说明）"
        note_box = QLabel(note)
        note_box.setObjectName("DialogSubTitle")
        note_box.setWordWrap(True)
        note_box.setMinimumHeight(72)
        v.addWidget(theme.label("更新说明", "DialogHint"))
        v.addWidget(note_box, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        v.addWidget(self.progress)
        self.hint = theme.label("", "DialogHint", word_wrap=True)
        v.addWidget(self.hint)

        self.btn_update = QPushButton("立即更新（自动重启到新版）")
        self.btn_update.setObjectName("Primary")
        self.btn_update.clicked.connect(self._do_update)
        self.btn_check_again = QPushButton("重新检查")
        self.btn_check_again.clicked.connect(self._recheck)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_update)
        btn_row.addWidget(self.btn_check_again)
        btn_row.addStretch(1)
        btn_close = QPushButton("稍后再说")
        btn_close.clicked.connect(self.reject)
        self.footer_layout.addLayout(btn_row)
        self.footer_layout.addWidget(btn_close)
        theme.polish_tree(self)
        self._mark_source(parent)

    def _mark_source(self, parent):
        db = getattr(parent, "db", None)
        if db is not None:
            src = updater.migrate_source(db.data_dir)
            self.hint.setText(f"更新源：{src}" if src else "未配置更新源")

    # ── 动作 ──────────────────────────────────────────────

    def _recheck(self):
        db = getattr(self.parent(), "db", None)
        if db is None:
            return
        self.btn_check_again.setEnabled(False)
        source = updater.migrate_source(db.data_dir)

        def job():
            return updater.check(source, APP_VERSION,
                                 fallback=updater.load_fallback(db.data_dir))

        def done(res):
            self.btn_check_again.setEnabled(True)
            if res["status"] == "update":
                self.hint.setText("清单已是最新（当前对话框即最新版本信息）。")
                self.manifest = res["manifest"]
            elif res["status"] == "latest":
                self.hint.setText(f"远端仍是 v{res['manifest']['version']}，无更新。")
            else:
                self.hint.setText(f"检查失败：{res['message']}")

        async_task.run_task(self, job, on_done=done,
                            on_error=lambda m: (
                                self.btn_check_again.setEnabled(True),
                                self.hint.setText(f"检查失败：{m}")))

    def _do_update(self):
        if self._updating:
            return
        import sys
        db = getattr(self.parent(), "db", None)
        if db is None:
            theme.notify(self, "无法更新", "未找到数据目录上下文", kind="error")
            return
        if not getattr(sys, "frozen", False):
            theme.notify(self, "开发环境", "源码运行不支持自动更新，请用 tools/release.py 流程。",
                         kind="warn")
            return
        self._updating = True
        self.btn_update.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)            # 下载开始前的忙碌条
        self.hint.setText("正在准备更新…")

        bridge = _Bridge(self)
        self._bridge = bridge                    # 防 GC
        bridge.stage_text.connect(self.hint.setText)
        bridge.bytes_progress.connect(self._on_bytes)

        def job():
            return updater.prepare_update(
                self.manifest,
                progress=bridge.stage_text.emit,
                bytes_progress=bridge.bytes_progress.emit)

        def done(res):
            try:
                updater.spawn_apply_update(res["staging_exe"], res["marker"])
                self.hint.setText("更新器已启动，程序即将自动重启到新版本…")
                parent = self.parent()
                if parent is not None:
                    parent.close()
            except Exception as exc:            # noqa: BLE001
                self._failed(str(exc))

        async_task.run_task(self, job, on_done=done, on_error=self._failed)

    def _on_bytes(self, done: int, total: int):
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total) if total > 0 else 0)

    def _failed(self, msg: str):
        self._updating = False
        self.btn_update.setEnabled(True)
        self.progress.setVisible(False)
        self.hint.setText(f"更新失败：{msg}\n程序未做任何改动，可稍后重试。")


class UpdateSourceDialog(ThemedDialog):
    """设置更新源：GitHub 仓库 / manifest.json 路径或网址（存 data/updater.json）"""

    def __init__(self, parent, data_dir):
        super().__init__(parent, "设置更新源",
                         "推荐用 GitHub Releases 自动更新；也可用本机 / 共享路径或 http(s) 网址",
                         width=640, height=320)
        self.data_dir = data_dir
        v = self.body_layout
        v.addWidget(theme.label("更新源", "DialogHint"))
        from PySide6.QtWidgets import QLineEdit
        current = updater.load_source(data_dir)
        # v3.5.4：首次配置直接给推荐值，省得用户手敲仓库名
        self.edit = QLineEdit(current or updater.DEFAULT_GITHUB_SOURCE)
        self.edit.setPlaceholderText(
            "github:owner/repo 或 https://…/manifest.json 或 D:\\发布\\manifest.json")
        v.addWidget(self.edit)

        btn_row = QHBoxLayout()
        btn_github = QPushButton("填入 GitHub 自动更新（推荐）")
        btn_github.clicked.connect(
            lambda: self.edit.setText(updater.DEFAULT_GITHUB_SOURCE))
        btn_row.addWidget(btn_github)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        v.addWidget(theme.label(
            "· github:owner/repo —— 读取该仓库最新 Release 里的 manifest.json 与安装包，"
            "公开仓库免登录、无需 token；加 @v3.5.4 可锁定版本。\n"
            "· 本机 / 共享 / HTTP —— 填 manifest.json 的完整位置，"
            "由发布人用 tools/release.py 生成（字段 version / zip / sha256 / note / date）。\n"
            "· 切到 GitHub 后旧的本地源会留作离线备用；清空并保存 = 关闭自动更新检查。",
            "DialogHint", word_wrap=True))
        self.hint = theme.label("", "DialogHint")
        v.addWidget(self.hint)
        self.add_buttons(ok_text="保存", on_ok=self._save)

    def _save(self):
        src = self.edit.text().strip()
        old = updater.load_source(self.data_dir)
        # 从本地/HTTP 切到 GitHub 时，把旧源留作断网回退
        fallback = (old if updater.parse_github_source(src)
                    and old and not updater.parse_github_source(old) else None)
        try:
            updater.save_source(self.data_dir, src, fallback=fallback)
        except Exception as exc:                    # noqa: BLE001
            self.hint.setText(f"保存失败：{exc}")
            return
        self.accept()

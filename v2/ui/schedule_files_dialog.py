#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单「排产文件」窗口（v3.4.0 · 用户第 14 轮需求 1）

入口：订单管理 → 选中订单 → 右键「排产文件」；也可从操作导航 / 快捷栏进入。

一屏内完成「查看 + 编辑 + 更改」：
  · 上半区：该订单全部订单排产单（单号 / 物资 / 范围 / 批次 / 状态 / 备注）
  · 下半区：随单附件按物理路径去重（合并 / 整单共用一份文件只显示一行）
  · 文件动作：打开（本机 Word/Excel/看图软件直接编辑内容）、重命名、替换内容、
    更改类别、移除、打开所在目录、导入新文件
  · 排产单动作：编辑备注 / 本次排产数量 / 审批状态（数量仍走守恒校验）

★ 关键口径：合并 / 整单排产时同一物理文件登记在多张排产单名下，
  所有写操作都通过服务层按 file_path 作用于整组兄弟登记，绝不留下死链。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import current_user as cur
from ..core import models
from ..core.db import now
from ..core.errors import NotFoundError
from ..domains.schedule import IMPORT_FILTER, KINDS
from . import theme
from .base_dialog import ThemedDialog

SCHED_HEADERS = ["订单排产单号", "物资", "范围", "批次", "状态", "附件数", "备注"]
FILE_HEADERS = ["文件名", "类别", "大小(KB)", "在盘", "共用排产单", "登记时间"]


class ScheduleFilesDialog(ThemedDialog):
    """查看 / 编辑 / 更改某张订单的全部排产文件"""

    def __init__(self, parent, svc, order_id: int, badge: str = theme.BADGE):
        self.svc = svc
        self.db = svc.db
        self.order_id = int(order_id)
        self.order = self.db.get_by_id("orders", self.order_id)
        if not self.order:
            raise NotFoundError(f"订单 {self.order_id} 不存在")
        self.changes: List[str] = []
        super().__init__(parent, "排产文件",
                         f"{self.order['doc_no']}　·　{self.order.get('customer') or '—'}",
                         width=1080, height=760, badge=badge)
        v = self.body_layout

        v.addWidget(self._build_head())
        self.hint = theme.label("", "DialogHint", word_wrap=True)
        v.addWidget(self.hint)

        self.sched_table = theme.style_table(QTableWidget())
        self.sched_table.setColumnCount(len(SCHED_HEADERS))
        self.sched_table.setHorizontalHeaderLabels(SCHED_HEADERS)
        self.file_table = theme.style_table(QTableWidget())
        self.file_table.setColumnCount(len(FILE_HEADERS))
        self.file_table.setHorizontalHeaderLabels(FILE_HEADERS)
        self.file_table.doubleClicked.connect(self._open_file)

        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(5)
        tv.addWidget(theme.label("订单排产单 · 双击或点选后可编辑备注与排产数量", "KpiLabel"))
        tv.addWidget(self.sched_table, 1)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(5)
        bv.addWidget(theme.label("随单文件 · 双击用本机程序打开编辑；共用文件只显示一行",
                                 "KpiLabel"))
        bv.addWidget(self.file_table, 1)
        v.addWidget(top, 2)
        v.addWidget(bottom, 3)

        v.addLayout(self._build_file_actions())
        v.addLayout(self._build_schedule_actions())
        self.add_buttons(ok_text="关闭", show_cancel=False)
        self._reload()

    # ── 顶部信息 / 动作区 ──────────────────────────────────

    def _build_head(self) -> QFrame:
        info = QFrame()
        info.setObjectName("Card")
        grid = QGridLayout(info)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        pairs = [
            ("订单系统单号", self.order["doc_no"],
             "订单状态", self.order.get("status") or "—"),
            ("订单项目编号", self.order.get("project_no") or "—",
             "交货日期", self.order.get("required_date") or "—"),
        ]
        for r, (k1, v1, k2, v2) in enumerate(pairs):
            grid.addWidget(theme.label(k1, "DialogHint"), r, 0)
            grid.addWidget(theme.label(str(v1), "DialogCount"), r, 1)
            grid.addWidget(theme.label(k2, "DialogHint"), r, 2)
            grid.addWidget(theme.label(str(v2), "DialogCount"), r, 3)
        return info

    def _build_file_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        for text, slot in (("打开", self._open_file),
                           ("重命名", self._do_rename),
                           ("替换内容", self._do_replace),
                           ("更改类别", self._do_kind),
                           ("移除", self._do_remove),
                           ("打开目录", self._open_folder)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        b = QPushButton("导入排产文件")
        b.setObjectName("Primary")
        b.setToolTip("一次可多选 Word/Excel/PDF/图片/图纸等任意格式，按扩展名自动归类")
        b.clicked.connect(self._do_import)
        row.addWidget(b)
        row.addStretch(1)
        return row

    def _build_schedule_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        b = QPushButton("编辑选中排产单")
        b.setToolTip("更改备注、本次排产数量（走数量守恒校验）与审批状态")
        b.clicked.connect(self._do_edit_schedule)
        row.addWidget(b)
        row.addStretch(1)
        row.addWidget(theme.label("文件存于 data/订单排产附件/<订单号>/", "DialogHint"))
        return row

    # ── 数据渲染 ──────────────────────────────────────────

    def _reload(self) -> None:
        scheds = self.svc.order_schedule_rows(self.order_id)
        files = self.svc.order_files(self.order_id)
        self.sched_table.blockSignals(True)
        self.sched_table.clearContents()
        self.sched_table.setRowCount(len(scheds))
        for r, s in enumerate(scheds):
            values = (s["doc_no"], f"第{s['line_no']:02d}项 {s['item_name']}",
                      s.get("scope") or "分项", s.get("batch_no") or "—",
                      s.get("status") or "—", s["file_count"],
                      s.get("remark") or "—")
            for c, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if c == 0:
                    cell.setData(Qt.UserRole, s["id"])
                self.sched_table.setItem(r, c, cell)
        self.sched_table.resizeColumnsToContents()
        self.sched_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.sched_table.blockSignals(False)

        self.file_table.blockSignals(True)
        self.file_table.clearContents()
        self.file_table.setRowCount(len(files))
        for r, f in enumerate(files):
            name = f["file_name"] + (f"（{f['shared']} 张共用）" if f["shared"] > 1 else "")
            values = (name, f["kind"],
                      f"{int(f.get('file_size') or 0) / 1024:.1f}",
                      "在盘" if f["exists"] else "缺失",
                      f["shared"], str(f.get("created_at") or "")[:19])
            for c, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if c == 0:
                    cell.setData(Qt.UserRole, f["file_ids"][0])
                if c == 3 and not f["exists"]:
                    cell.setForeground(QColor(theme.DANGER))
                self.file_table.setItem(r, c, cell)
        self.file_table.resizeColumnsToContents()
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.blockSignals(False)

        missing = sum(1 for f in files if not f["exists"])
        self.hint.setText(
            f"共 {len(scheds)} 张订单排产单 / {len(files)} 份随单文件"
            + (f"，其中 {missing} 份在盘上找不到（可用「替换内容」重新挂接）"
               if missing else "，全部文件在盘")
            + "。打开后即可用本机 Word / Excel 直接编辑文件内容，改完保存即可，"
              "无需重新导入；替换内容则保留原文件名与登记关系。")
        if not scheds:
            self.hint.setText(
                "该订单还没有订单排产单。先在订单明细上右键「订单排产」建立排产单，"
                "再回到本窗口查看、编辑排产文件。")

    def _selected_file(self) -> Optional[Dict[str, Any]]:
        row = self.file_table.currentRow()
        cell = self.file_table.item(row, 0) if row >= 0 else None
        fid = cell.data(Qt.UserRole) if cell else None
        if fid is None:
            return None
        return next((f for f in self.svc.order_files(self.order_id)
                     if int(fid) in f["file_ids"]), None)

    def _selected_schedule(self) -> Optional[Dict[str, Any]]:
        row = self.sched_table.currentRow()
        cell = self.sched_table.item(row, 0) if row >= 0 else None
        sid = cell.data(Qt.UserRole) if cell else None
        return self.svc.get(int(sid)) if sid is not None else None

    def _need_file(self) -> Optional[Dict[str, Any]]:
        f = self._selected_file()
        if f is None:
            theme.notify(self, "请先选择", "请点选下方文件表里的一行", kind="warn")
        return f

    # ── 文件动作 ──────────────────────────────────────────

    def _open_file(self, *_args):
        f = self._selected_file()
        if f is None:
            return
        if not f["exists"]:
            theme.notify(self, "文件缺失", f"{f['file_name']} 在盘上找不到，"
                         "可用「替换内容」重新挂接。", kind="warn")
            return
        ok, msg = self.svc.open_file(int(f["file_ids"][0]))
        if not ok:
            theme.notify(self, "无法打开", msg, kind="warn")

    def _do_rename(self):
        f = self._need_file()
        if f is None:
            return
        from . import dialogs
        data = dialogs.ask(self, "重命名排产文件", [
            {"key": "name", "label": "文件名", "type": "text",
             "default": f["file_name"], "required": True},
        ], subtitle="扩展名不可更改；合并 / 整单共用的文件会同步更新所有排产单登记。")
        if not data:
            return
        self._apply(lambda: self.svc.rename_file(int(f["file_ids"][0]),
                                                 str(data["name"]).strip(), cur.name()),
                    f"重命名：{f['file_name']} → {data['name']}")

    def _do_replace(self):
        f = self._need_file()
        if f is None:
            return
        path = theme.pick_path(self, "选择用于替换的文件", "open", filters=IMPORT_FILTER)
        if not path:
            return
        self._apply(lambda: self.svc.replace_file(int(f["file_ids"][0]), path, cur.name()),
                    f"替换内容：{f['file_name']} ← {Path(path).name}")

    def _do_kind(self):
        f = self._need_file()
        if f is None:
            return
        from . import dialogs
        data = dialogs.ask(self, "更改附件类别", [
            {"key": "kind", "label": "类别", "type": "combo",
             "options": [(k, k) for k in KINDS], "default": f["kind"]},
        ], subtitle="类别用于检索与统计；同一共用文件的全部登记会一并更新。")
        if not data:
            return
        self._apply(lambda: self.svc.set_file_kind(int(f["file_ids"][0]),
                                                   str(data["kind"]), cur.name()),
                    f"更改类别：{f['file_name']} → {data['kind']}")

    def _do_remove(self):
        f = self._need_file()
        if f is None:
            return
        if not theme.confirm(self, "移除排产文件",
                             f"确认移除「{f['file_name']}」？",
                             "合并 / 整单排产时该文件在多张排产单下共用，将一并移除；"
                             "物理文件也会被清理，不影响排产单与数量守恒。"):
            return
        self._apply(lambda: self.svc.remove_file(int(f["file_ids"][0]), cur.name()),
                    f"移除：{f['file_name']}")

    def _do_import(self):
        paths = theme.pick_paths(self, "导入排产文件（可多选 · 任意格式）", IMPORT_FILTER)
        if not paths:
            return
        self._apply(lambda: self.svc.add_files_for_order(self.order_id, paths, cur.name()),
                    f"导入 {len(paths)} 个文件")

    def _open_folder(self):
        folder = self.svc.order_dir(self.order["doc_no"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ── 排产单编辑 ────────────────────────────────────────

    def _do_edit_schedule(self):
        s = self._selected_schedule()
        if s is None:
            theme.notify(self, "请先选择", "请点选上方订单排产单表里的一行", kind="warn")
            return
        from . import dialogs
        data = dialogs.ask(self, "编辑订单排产单", [
            {"key": "qty", "label": "本次排产数量", "type": "float",
             "default": float(s.get("allocated_qty") or 0), "decimals": 3,
             "minimum": 0.001},
            {"key": "status", "label": "审批状态", "type": "combo",
             "options": [(x, x) for x in models.ScheduleStatus.ALL],
             "default": s.get("status") or models.ScheduleStatus.DRAFT},
            {"key": "remark", "label": "备注", "type": "textarea",
             "default": s.get("remark") or ""},
        ], subtitle=f"{s['doc_no']} · 数量仍受「Σ已排量 ≤ 订单数量」守恒约束。")
        if not data:
            return

        def _save():
            op = cur.name()
            qty = float(data["qty"])
            if abs(qty - float(s.get("allocated_qty") or 0)) > 1e-9:
                self.svc.set_allocated_qty(int(s["id"]), qty, op)
            self.svc.set_remark(int(s["id"]), str(data["remark"] or ""), op)
            if str(data["status"]) != (s.get("status") or ""):
                with self.db.tx():
                    self.db.update("order_schedules",
                                   {"status": str(data["status"]),
                                    "updated_at": now()},
                                   "id = ?", (s["id"],))
                    self.db.log_action("order_schedule", s["id"], "set_status",
                                       before={"status": s.get("status")},
                                       after={"status": str(data["status"])},
                                       operator=op)
            return s["doc_no"]

        self._apply(_save, f"编辑排产单：{s['doc_no']}")

    # ── 统一执行 / 结果 ───────────────────────────────────

    def _apply(self, fn, label: str):
        try:
            fn()
        except Exception as exc:                                # noqa: BLE001
            theme.notify(self, f"{label} · 失败", str(exc), kind="error")
            return
        self.changes.append(label)
        self._reload()
        self.hint.setText(f"{label} · 已完成。{self.hint.text()}")

    def summary_text(self) -> str:
        return (f"{self.order['doc_no']} 排产文件："
                + ("；".join(self.changes) if self.changes else "仅查看，未做更改"))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交期提醒弹窗（v3.3.0）

两个对话框：
  · DueReminderDialog     临期清单：按紧急度排序、一键标记已发货 / 定位订单 /
                          今日不再提醒 / 打开设置
  · ReminderSettingsDialog  启用开关、提前天数、逾期上限、启动弹窗、托盘气泡

所有业务动作通过回调交回主窗口执行，本模块不直接写库，保证
「服务层 → 审计 → 刷新」链路只有一条。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from . import theme
from .base_dialog import ThemedDialog
from ..core.duereminder import DEFAULT_DAYS, DEFAULT_OVERDUE_MAX, notice_text

_COLUMNS = ["系统单号", "订单项目编号", "客户名称", "交货日期",
            "交期", "订单状态"]

_LEVEL_COLOR = {
    "overdue": QColor("#B91C1C"),
    "today": QColor("#C2410C"),
    "soon": QColor("#1D4ED8"),
}


class DueReminderDialog(ThemedDialog):
    """启动自动弹出 / 状态栏徽标点击 / 导航「临期订单」共用"""

    def __init__(self, parent, report: Dict[str, Any], *,
                 on_mark_shipped: Optional[Callable] = None,
                 on_view_order: Optional[Callable] = None,
                 on_settings: Optional[Callable] = None,
                 on_snooze: Optional[Callable] = None,
                 auto: bool = False):
        super().__init__(
            parent, "交期提醒 · 临期与逾期订单",
            "距交货期进入提醒窗口且尚未发货的订单；标记已发货后即时停止提醒",
            width=1000, height=640)
        self.setObjectName("DueReminderDialog")
        self._callbacks = {
            "mark": on_mark_shipped, "view": on_view_order,
            "settings": on_settings, "snooze": on_snooze,
        }
        self.auto = auto
        self.hint = theme.label("", "DialogHint", word_wrap=True)
        self.body_layout.addWidget(self.hint)

        self.table = theme.style_table(QTableWidget())
        self.table.setColumnCount(len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMinimumHeight(260)
        self.table.verticalHeader().setDefaultSectionSize(31)
        self.table.doubleClicked.connect(lambda _: self._do_view())
        self.body_layout.addWidget(self.table, 1)

        self.rule = theme.label(
            "口径：进入「提前天数」窗口即提醒，逾期同样催交；"
            "超过「逾期上限」的陈旧单只在状态栏计数、不进本清单。"
            "已发货 / 发货中 / 已交付 / 已验收 / 关闭 / 作废 一律不再提醒。",
            "Hint", word_wrap=True)
        self.body_layout.addWidget(self.rule)

        self._build_buttons()
        self.reload(report)

    def _build_buttons(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_mark = QPushButton("标记已发货")
        self.btn_mark.setObjectName("Primary")
        self.btn_mark.setToolTip("把选中订单状态改为「已发货」，本单立即从提醒清单移除")
        self.btn_mark.clicked.connect(self._do_mark)
        self.btn_view = QPushButton("查看订单")
        self.btn_view.clicked.connect(self._do_view)
        self.btn_snooze = QPushButton("今日不再提醒")
        self.btn_snooze.clicked.connect(self._do_snooze)
        self.btn_settings = QPushButton("提醒设置")
        self.btn_settings.clicked.connect(self._do_settings)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        for b in (self.btn_mark, self.btn_view, self.btn_snooze,
                  self.btn_settings, self.btn_close):
            row.addWidget(b)
        self.footer_layout.addLayout(row)
        theme.polish_tree(self)

    def reload(self, report: Dict[str, Any]):
        self.report = report or {}
        rows = self.report.get("rows") or []
        s = self.report.get("summary") or {}
        long_n = int(s.get("long_overdue") or 0)
        self.hint.setText(
            f"共 {len(rows)} 单需关注：逾期 {s.get('overdue', 0)} / "
            f"今天到期 {s.get('today', 0)} / 临期 {s.get('soon', 0)}"
            + (f"　·　另有 {long_n} 单长期逾期未发货（不进清单）" if long_n else "")
            + f"　|　{notice_text(self.report)}")
        self._fill(rows)
        has = bool(rows)
        self.btn_mark.setEnabled(has)
        self.btn_view.setEnabled(has)
        if has:
            self.table.selectRow(0)

    def _fill(self, rows):
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            values = [r.get("doc_no") or "—", r.get("project_no") or "—",
                      r.get("customer") or "—", r.get("required_date") or "—",
                      r.get("label") or "—", r.get("status") or "—"]
            color = _LEVEL_COLOR.get(r.get("level"))
            for col, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setData(Qt.UserRole, r.get("id"))
                if color is not None and col in (0, 4):
                    item.setForeground(color)
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

    def selected_order_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    def _require_row(self):
        oid = self.selected_order_id()
        if oid is None:
            theme.notify(self, "请先选择", "请点选清单里的一张订单", kind="warn")
        return oid

    def _do_mark(self):
        oid = self._require_row()
        if oid is None:
            return
        fn = self._callbacks.get("mark")
        if fn is None:
            return
        try:
            report = fn(oid)
        except Exception as exc:
            theme.notify(self, "标记失败", str(exc), kind="error")
            return
        self.reload(report or {"rows": [], "summary": {}})

    def _do_view(self):
        oid = self._require_row()
        if oid is None:
            return
        fn = self._callbacks.get("view")
        if fn is not None:
            fn(oid)
        self.accept()

    def _do_snooze(self):
        fn = self._callbacks.get("snooze")
        if fn is not None:
            fn()
        self.accept()

    def _do_settings(self):
        fn = self._callbacks.get("settings")
        if fn is None:
            return
        report = fn()
        self.reload(report or {"rows": [], "summary": {}})


class ReminderSettingsDialog(ThemedDialog):
    """提醒偏好；返回 values()，由主窗口写 data/reminder.json"""

    def __init__(self, parent, prefs: Dict[str, Any]):
        super().__init__(parent, "交期提醒设置",
                         "设置保存在 data/reminder.json，程序升级不会丢失",
                         width=620, height=460)
        self.setObjectName("ReminderSettingsDialog")
        p = prefs or {}

        self.chk_enabled = QCheckBox("启用交期提醒")
        self.chk_enabled.setChecked(bool(p.get("enabled", True)))
        self.chk_popup = QCheckBox("启动与定时复查时弹出临期清单")
        self.chk_popup.setChecked(bool(p.get("popup", True)))
        self.chk_tray = QCheckBox("显示 Windows 托盘气泡（系统托盘可用时）")
        self.chk_tray.setChecked(bool(p.get("tray", True)))
        for c in (self.chk_enabled, self.chk_popup, self.chk_tray):
            self.body_layout.addWidget(c)

        form = QHBoxLayout()
        form.addWidget(QLabel("提前提醒天数"))
        self.spin_days = QSpinBox()
        self.spin_days.setRange(1, 365)
        self.spin_days.setValue(int(p.get("days", DEFAULT_DAYS)))
        self.spin_days.setSuffix(" 天")
        form.addWidget(self.spin_days)
        form.addStretch(1)
        self.body_layout.addLayout(form)

        form2 = QHBoxLayout()
        form2.addWidget(QLabel("逾期上限"))
        self.spin_overdue = QSpinBox()
        self.spin_overdue.setRange(0, 3650)
        self.spin_overdue.setValue(int(p.get("overdue_max_days",
                                             DEFAULT_OVERDUE_MAX)))
        self.spin_overdue.setSuffix(" 天")
        form2.addWidget(self.spin_overdue)
        form2.addStretch(1)
        self.body_layout.addLayout(form2)

        self.body_layout.addWidget(theme.label(
            "· 提前天数：距交货期 ≤ N 天即进入提醒窗口（用户口径默认 10 天）。\n"
            "· 逾期上限：逾期超过 N 天的单归入「长期逾期」，只在状态栏计数、不逐单弹窗；"
            "填 0 表示不设上限。真实库里的陈年历史单靠这一条避免刷屏。\n"
            "· 已发货 / 发货中 / 已交付 / 已验收 / 关闭 / 作废 一律不再提醒；"
            "在清单里点「标记已发货」即可即时停止。\n"
            "· 同一天同一批订单只弹一次；清单变化或跨天会重新提醒。",
            "Hint", word_wrap=True))
        self.body_layout.addStretch(1)
        self.add_buttons("保存设置", "取消")
        theme.polish_tree(self)

    def values(self) -> Dict[str, Any]:
        return {
            "enabled": self.chk_enabled.isChecked(),
            "popup": self.chk_popup.isChecked(),
            "tray": self.chk_tray.isChecked(),
            "days": int(self.spin_days.value()),
            "overdue_max_days": int(self.spin_overdue.value()),
        }

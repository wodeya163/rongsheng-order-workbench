#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一日期选择控件（v3.2.0）

用户口径：所有日期输入框一律「弹出日历选择」，不再手工敲 YYYY-MM-DD。

设计要点：
  · DatePicker = 只读 QLineEdit + 「选择」按钮 + 「清空」按钮；输入框本身
    设 readOnly，点击输入框/选择按钮都弹出 QCalendarWidget 日历对话框。
  · 保持 QLineEdit 的 text()/setText()/clear()/setPlaceholderText() 接口，
    因此 FormDialog、QueryPanel、审计查询、新建/编辑订单里原有的
    .text().strip() 取值代码一行都不用改。
  · 日期为空是合法状态（交期、过滤下限等大量字段可留空），清空按钮和
    日历里的「清空」都支持；历史脏数据（非法日期串）按原文显示，不丢数据。
  · 日历弹窗走 ThemedDialog 外壳，浅色 QSS 全局生效，版本徽标与主窗口同源。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional, Union

from PySide6.QtCore import QDate, QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCalendarWidget, QDialog, QHBoxLayout, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .base_dialog import ThemedDialog
from . import theme

__all__ = ["DatePicker", "parse_date", "DATE_FORMAT"]

DATE_FORMAT = "yyyy-MM-dd"
_EMPTY_HINT = "点击选择日期（可留空）"

DateLike = Union[str, QDate, _dt.date, _dt.datetime, None]


def parse_date(value: DateLike) -> Optional[QDate]:
    """把常见日期写法解析成 QDate；解析不了返回 None（不抛异常）"""
    if value is None:
        return None
    if isinstance(value, QDate):
        return value if value.isValid() else None
    if isinstance(value, _dt.datetime):
        return QDate(value.year, value.month, value.day)
    if isinstance(value, _dt.date):
        return QDate(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    # "2026-11-08 12:30:00" / "2026-11-08T12:30:00" 只取日期段
    head = s.replace("T", " ").split(" ")[0]
    for text in (s[:10], head):
        for fmt in (DATE_FORMAT, "yyyy/M/d", "yyyy/MM/dd", "yyyy.M.d",
                    "yyyyMMdd"):
            d = QDate.fromString(text, fmt)
            if d.isValid():
                return d
    return None


class _DateCalendarDialog(ThemedDialog):
    """日历弹窗：月份导航 + 今天 / 清空 / 确定 / 取消"""

    def __init__(self, parent: QWidget, current: Optional[QDate],
                 title: str = "选择日期"):
        super().__init__(parent, title,
                         "单击选中日期，双击或点「确定」完成；不需要日期可点「清空」",
                         width=430, height=430)
        self.cleared = False
        self._accepted = False

        self.cal = QCalendarWidget()
        self.cal.setGridVisible(True)
        self.cal.setFirstDayOfWeek(Qt.Monday)
        self.cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.cal.setMinimumDate(QDate(1900, 1, 1))
        self.cal.setMaximumDate(QDate(2100, 12, 31))
        self.cal.setSelectedDate(current or QDate.currentDate())
        self.cal.activated.connect(self._accept_date)
        self.body_layout.addWidget(self.cal, 1)

        tip = theme.label("提示：日期只能从日历选择，输入框不接受手工填写。",
                          "DialogHint", word_wrap=True)
        self.body_layout.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(8)
        btn_today = QPushButton("今天")
        btn_clear = QPushButton("清空")
        btn_ok = QPushButton("确定")
        btn_ok.setObjectName("Primary")
        btn_cancel = QPushButton("取消")
        btn_today.clicked.connect(
            lambda: self.cal.setSelectedDate(QDate.currentDate()))
        btn_clear.clicked.connect(self._clear)
        btn_ok.clicked.connect(self._accept_date)
        btn_cancel.clicked.connect(self.reject)
        for b in (btn_today, btn_clear, btn_ok, btn_cancel):
            row.addWidget(b)
        self.footer_layout.addLayout(row)
        theme.polish_tree(self)

    def _clear(self):
        self.cleared = True
        self._accepted = True
        self.accept()

    def _accept_date(self, *_args):
        self.cleared = False
        self._accepted = True
        self.accept()

    def chosen(self) -> Optional[QDate]:
        """确定且未清空 → 选中日期；否则 None"""
        return None if (not self._accepted or self.cleared) else self.cal.selectedDate()


class DatePicker(QWidget):
    """只读日期输入框 + 弹出日历（对外仍是 QLineEdit 式的 text 接口）"""

    dateChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, value: DateLike = ""):
        super().__init__(parent)
        self.setObjectName("DatePicker")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(5)

        self.edit = QLineEdit()
        self.edit.setObjectName("DateField")
        self.edit.setReadOnly(True)          # ★ 禁止手工输入，只能从日历选
        self.edit.setPlaceholderText(_EMPTY_HINT)
        self.edit.setToolTip("点击输入框或「选择」按钮打开日历")
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self.dateChanged.emit)

        self.btn_pick = QPushButton("选择")
        self.btn_pick.setObjectName("DatePick")
        self.btn_pick.setToolTip("打开日历选择日期")
        self.btn_pick.clicked.connect(self.open_calendar)

        self.btn_clear = QPushButton("×")
        self.btn_clear.setObjectName("DateClear")
        self.btn_clear.setFixedWidth(28)
        self.btn_clear.setToolTip("清空日期")
        self.btn_clear.clicked.connect(self.clear)

        h.addWidget(self.edit, 1)
        h.addWidget(self.btn_pick)
        h.addWidget(self.btn_clear)
        self.setText(value)
        self._sync_clear()

    # ── QLineEdit 兼容接口 ───────────────────────────────
    def text(self) -> str:
        return self.edit.text()

    def setText(self, value: DateLike) -> None:
        d = parse_date(value)
        if d is not None:
            self.edit.setText(d.toString(DATE_FORMAT))
        else:
            # 历史数据里可能有非标准日期串：按原文显示，绝不悄悄清空
            self.edit.setText("" if value is None else str(value).strip())
        self._sync_clear()

    def clear(self) -> None:
        self.edit.clear()
        self._sync_clear()

    def setPlaceholderText(self, text: str) -> None:
        self.edit.setPlaceholderText(text or _EMPTY_HINT)

    def placeholderText(self) -> str:
        return self.edit.placeholderText()

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self.edit.setToolTip(text)

    def date(self) -> Optional[QDate]:
        return parse_date(self.text())

    def setDate(self, d: QDate) -> None:
        self.edit.setText(d.toString(DATE_FORMAT) if d and d.isValid() else "")
        self._sync_clear()

    def isReadOnly(self) -> bool:
        return self.edit.isReadOnly()

    def _sync_clear(self):
        self.btn_clear.setEnabled(bool(self.text()))

    # ── 日历弹窗 ───────────────────────────────────────
    def open_calendar(self):
        """弹出日历；确定回填，清空置空，取消保持原值"""
        dlg = _DateCalendarDialog(self, self.date())
        if dlg.exec() == QDialog.Accepted:
            if dlg.cleared:
                self.clear()
            else:
                chosen = dlg.chosen()
                if chosen is not None:
                    self.setDate(chosen)
        dlg.deleteLater()
        return self.text()

    def eventFilter(self, obj, event):
        # 点击只读输入框也弹日历，避免用户以为控件坏了；
        # 用 0 延迟把模态循环挪出鼠标事件处理栈，避免嵌套事件循环卡住 Qt
        if (obj is self.edit and event.type() == QEvent.MouseButtonRelease
                and getattr(event, "button", lambda: None)() == Qt.LeftButton):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.open_calendar)
        return super().eventFilter(obj, event)

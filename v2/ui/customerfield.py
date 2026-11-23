#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户名称智能输入（v3.4.2 · 用户第 16 轮）

一条口径：**库里字段名仍是 customer，界面统一叫「客户名称」**。

客户候选不另建字典表（不制造第二个主数据源、零迁移）：
  · 主来源 orders.customer —— 用户明确要求「从订单中收集客户名称」；
  · 补充 bid_projects.customer / code_projects.customer —— 只建过项目还没下单的客户也能带出；
  · 按出现次数降序 + 名称升序排，常用的排前面；空白 / 纯空格 / 「—」一律丢弃。

控件 CustomerComboBox：可编辑下拉 + 包含匹配补全器，
  · 输入任意关键字（如「北仑」「国能」）即时过滤候选列表；
  · 选中候选项自动填入，也可直接手输新客户名称（NoInsert，不会污染候选库）；
  · 兼容 QLineEdit 的 text()/setText() 接口，老表单取值代码一行不用改。
"""
from typing import Any, Iterable, List, Optional

from PySide6.QtCore import Qt, QStringListModel
from PySide6.QtWidgets import QComboBox, QCompleter

# 收集来源：订单为主，项目表兜底（表名固定，不做动态 SQL 拼接）
_SOURCES = ("orders", "bid_projects", "code_projects")
_PLACEHOLDERS = {"", "—", "-", "无", "n/a", "null", "none"}

HINT = "输入关键字检索历史客户名称，也可直接输入新客户"


def clean_names(values: Iterable[Any]) -> List[str]:
    """去空白、去占位、去重并按「出现次数降序 + 名称升序」排"""
    counts: dict = {}
    for value in values:
        name = str(value or "").strip()
        if name.lower() in _PLACEHOLDERS:
            continue
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], n))


def collect_customer_names(db) -> List[str]:
    """从真实库收集历史客户名称；表不存在/字段缺失时自动跳过，绝不抛异常"""
    values: List[Any] = []
    if db is None:
        return []
    for table in _SOURCES:
        try:
            rows = db.query(f"SELECT customer FROM {table}")
            values.extend(r["customer"] for r in rows)
        except Exception:                                    # noqa: BLE001
            continue
    return clean_names(values)


def db_from_parent(parent) -> Optional[Any]:
    """表单的 parent 一般就是主窗口 / Mixin（带 self.db）；取不到就退化成纯输入"""
    return getattr(parent, "db", None)


class CustomerComboBox(QComboBox):
    """客户名称输入：可编辑 + 关键字包含匹配补全 + 历史候选下拉"""

    def __init__(self, parent=None, db=None, value: str = "",
                 names: Optional[List[str]] = None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMinimumWidth(260)
        self._completer = QCompleter(QStringListModel([], self), self)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(self._completer)
        # Qt 的可编辑 QComboBox 不会自动同步补全前缀，必须显式接一根线
        self.lineEdit().textChanged.connect(self._completer.setCompletionPrefix)
        self.lineEdit().setPlaceholderText(HINT)
        self.setToolTip("从订单及项目台账自动汇总的历史客户名称；输入关键字即可筛选，"
                        "也可直接输入新客户名称")
        self.reload(names if names is not None
                    else collect_customer_names(db or db_from_parent(parent)))
        if value:
            self.setText(value)

    # ── QLineEdit 兼容接口（老表单直接 .text() / .setText()）──
    def text(self) -> str:
        return self.currentText().strip()

    def setText(self, value: str) -> None:
        self.setEditText(str(value or ""))

    def clear(self) -> None:
        # 只清输入，不清候选列表（QComboBox.clear() 会连候选一起清空）
        self.setEditText("")

    # ── 候选 ────────────────────────────────────────────
    def reload(self, names: Iterable[str]) -> None:
        items = list(names or [])
        super().clear()
        self.addItems(items)
        # 复用同一个 model，避免每次刷新都挂一个新的 QStringListModel 到父对象
        model = self._completer.model()
        model.setStringList(items)
        self.setCurrentIndex(-1)
        self.setEditText("")

    def names(self) -> List[str]:
        return [self.itemText(i) for i in range(self.count())]

    def matching(self, keyword: str) -> List[str]:
        """给测试/调用方用的包含匹配视图（不依赖弹窗是否可见）"""
        kw = str(keyword or "").strip().casefold()
        return [n for n in self.names() if not kw or kw in n.casefold()]

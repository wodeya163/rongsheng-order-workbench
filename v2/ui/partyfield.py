#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采购合同「需方 / 供方」智能输入（v3.4.5 · 用户第 20 轮需求 2）

口径：**不另建往来单位字典表**（避免制造第二个主数据源、零迁移）。
  · 供方候选 ← purchase_contracts.supplier 历史值；
  · 需方候选 ← purchase_contracts.buyer 历史值 + orders.customer 补充
    （老订单本来就是需方，能在合同台账还没积累时立刻带出）；
  · 每次打开「新建 / 编辑采购合同」表单时现查现填，合同越加越多，
    候选也自动越攒越全；空白 / 占位值丢弃，按「出现次数降序 + 名称升序」排。

控件 PartyComboBox 复用 v3.4.2 的 CustomerComboBox 交互契约：
  可编辑下拉 + QCompleter(MatchContains) + 自由新输 + text()/setText()/clear()，
  输入任意关键字即弹出包含匹配的历史名称，不改动数据库。
"""
from typing import Any, List, Optional

from .customerfield import CustomerComboBox, clean_names, db_from_parent

# 只允许这两个固定列名进 SQL，杜绝动态拼接注入
_PARTY_COLUMNS = {"buyer", "supplier"}
_HINTS = {
    "supplier": "输入关键字检索历史供方名称，也可直接输入新供方",
    "buyer": "输入关键字检索历史需方名称，也可直接输入新需方",
}
_TIPS = {
    "supplier": "从历史采购合同自动汇总的供方；输入关键字即可筛选，也可直接输入新供方名称",
    "buyer": "从历史采购合同（及订单客户名称）自动汇总的需方；输入关键字即可筛选，也可直接输入新需方名称",
}


def collect_party_names(db, column: str) -> List[str]:
    """收集某一方的历史名称；表 / 字段不存在时静默跳过，绝不抛异常"""
    if column not in _PARTY_COLUMNS:
        raise ValueError(f"不支持的往来单位列：{column}")
    if db is None:
        return []
    values: List[Any] = []
    try:
        rows = db.query(f"SELECT {column} FROM purchase_contracts")
        values.extend(r[column] for r in rows)
    except Exception:                                        # noqa: BLE001
        pass
    if column == "buyer":
        try:
            values.extend(r["customer"] for r in db.query(
                "SELECT customer FROM orders"))
        except Exception:                                    # noqa: BLE001
            pass
    return clean_names(values)


class PartyComboBox(CustomerComboBox):
    """供方 / 需方输入：历史候选 + 关键字包含匹配补全 + 自由新输"""

    def __init__(self, parent=None, db=None, column: str = "supplier",
                 value: str = ""):
        if column not in _PARTY_COLUMNS:
            raise ValueError(f"不支持的往来单位列：{column}")
        self.party_column = column
        # names 显式传入 → 基类不会再去查客户表
        super().__init__(parent=parent, db=None, value="",
                         names=collect_party_names(db or db_from_parent(parent),
                                                   column))
        self.lineEdit().setPlaceholderText(_HINTS[column])
        self.setToolTip(_TIPS[column])
        if value:
            self.setText(value)

    def reload_from_db(self, db=None) -> None:
        """台账更新后手动刷新候选（表单每次新建时会自动收，一般不必调用）"""
        self.reload(collect_party_names(db or db_from_parent(self.parent()),
                                        self.party_column))

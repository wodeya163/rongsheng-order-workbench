#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询条件下推（P1-10）

背景：v2.5 的 _filtered() 是「全表读进内存再逐行匹配」，
      数据量一上来（订单破万）就会拖慢界面，而且分页无从谈起。

做法：把界面 filters 翻译成 SQL WHERE / HAVING 片段，交给 SQLite 过滤 + 分页；
      聚合口径（明细数、实值合计、已排量）仍在 SQL 里算，不在 Python 里二次遍历。

约定：
  · 关键字是「多列 OR LIKE」，字段名由调用方给，别名必须与 FROM 一致；
  · 空条件一律跳过，不产生 `LIKE '%%'` 这种全表扫描；
  · 所有值走参数绑定，绝不拼进 SQL 文本（除列名白名单）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

# LIKE 特殊字符转义：用户输入 100% 时不应退化成通配
_ESCAPE = "\\"


def like_value(text: str) -> str:
    """把用户输入转成 LIKE 参数，转义 % _ \\ 三个特殊符"""
    escaped = (text.replace(_ESCAPE, _ESCAPE * 2)
                   .replace("%", _ESCAPE + "%")
                   .replace("_", _ESCAPE + "_"))
    return f"%{escaped}%"


def keyword_clause(keyword: str, columns: Sequence[str]) -> Tuple[str, List[Any]]:
    """多列 OR LIKE；columns 必须是白名单列名（可带表别名）"""
    kw = (keyword or "").strip()
    if not kw or not columns:
        return "", []
    part = " OR ".join(f"IFNULL({c},'') LIKE ? ESCAPE '{_ESCAPE}'" for c in columns)
    return f"({part})", [like_value(kw)] * len(columns)


def date_clause(column: str, date_from: str = "", date_to: str = "") -> Tuple[str, List[Any]]:
    """按天比较（字段里可能带时分秒，统一取前 10 位）"""
    sql, params = "", []
    d1 = (date_from or "").strip()[:10]
    d2 = (date_to or "").strip()[:10]
    if d1:
        sql += f" AND substr(IFNULL({column},''),1,10) >= ?"
        params.append(d1)
    if d2:
        sql += f" AND substr(IFNULL({column},''),1,10) <= ?"
        params.append(d2)
    return sql, params


def build_where(filters: Dict[str, Any],
                keyword_columns: Sequence[str],
                status_column: str = "",
                customer_column: str = "",
                date_column: str = "",
                type_column: str = "",
                project_column: str = "",
                extra: Iterable[Tuple[str, List[Any]]] = ()) -> Tuple[str, List[Any]]:
    """
    返回 (" WHERE a=? AND b=?", params)。
    filters 直接来自 QueryPanel.values()，多余的键自动忽略。
    """
    f = filters or {}
    parts: List[str] = []
    params: List[Any] = []

    kw_sql, kw_params = keyword_clause(str(f.get("keyword") or ""), keyword_columns)
    if kw_sql:
        parts.append(kw_sql)
        params += kw_params

    status = str(f.get("status") or "").strip()
    if status and status_column:
        parts.append(f"{status_column} = ?")
        params.append(status)

    customer = str(f.get("customer") or "").strip()
    if customer and customer_column:
        parts.append(f"IFNULL({customer_column},'') LIKE ? ESCAPE '{_ESCAPE}'")
        params.append(like_value(customer))

    # v2.9.1：订单项目编号独立筛选（模糊，与关键字同口径）
    project = str(f.get("project_no") or "").strip()
    if project and project_column:
        parts.append(f"IFNULL({project_column},'') LIKE ? ESCAPE '{_ESCAPE}'")
        params.append(like_value(project))

    if date_column:
        d_sql, d_params = date_clause(date_column, str(f.get("date_from") or ""),
                                      str(f.get("date_to") or ""))
        if d_sql:
            parts.append(d_sql.strip()[4:])       # 去掉前导 "AND "
            params += d_params

    otype = str(f.get("order_type") or "").strip()
    if otype and type_column:
        parts.append(f"{type_column} = ?")
        params.append(otype)

    if f.get("won_only") and status_column:
        parts.append(f"{status_column} = '中标'")

    for sql, extra_params in extra:
        if sql:
            parts.append(sql)
            params += extra_params

    return (" WHERE " + " AND ".join(parts), params) if parts else ("", [])


def having_capacity() -> str:
    """has_capacity=true：还有可排量（qty_real - qty_scheduled > 0）。

    需配合 GROUP BY 且 SELECT 里已有 qty_real / qty_scheduled 两个别名
    （SQLite 允许 HAVING 引用 SELECT 别名）。
    """
    return " HAVING qty_real - qty_scheduled > 1e-9"


# ── 交期预警（P2-6）──────────────────────────────────────────
# 口径：以订单「交货日期 required_date」为准，发货及以后的订单不再催交。
# v3.3.0：与 models.OrderStatus.NO_REMIND 单一来源一致，补上「发货中/已发货」，
# 否则订单表「交期状态」列会把已发货单仍标成临期，与交期提醒自相矛盾。
# 日期按本地日期比较（date('now','localtime')），与界面显示用的是同一天。
from . import models as _models                                  # noqa: E402

_DUE_DONE_STATUS = tuple(_models.OrderStatus.NO_REMIND)

DUE_LABELS = {"": "全部", "overdue": "已逾期", "soon": "7 天内到期"}


def due_clause(state: str, column: str = "o.required_date",
               status_column: str = "o.status") -> Tuple[str, List[Any]]:
    """交期状态 → SQL 片段。state: overdue / soon / 其它=不过滤"""
    state = (state or "").strip()
    if state not in ("overdue", "soon"):
        return "", []
    done = ", ".join("?" for _ in _DUE_DONE_STATUS)
    has_date = f"substr(IFNULL({column},''),1,10) <> ''"
    if state == "overdue":
        return (f"({has_date} AND date(substr({column},1,10)) < date('now','localtime')"
                f" AND IFNULL({status_column},'') NOT IN ({done}))",
                list(_DUE_DONE_STATUS))
    return (f"({has_date} AND date(substr({column},1,10)) >= date('now','localtime')"
            f" AND date(substr({column},1,10)) <= date('now','localtime','+7 day')"
            f" AND IFNULL({status_column},'') NOT IN ({done}))",
            list(_DUE_DONE_STATUS))


def due_state(required_date: str, status: str = "", today: Any = None) -> str:
    """
    Python 侧判定（表格显示用，与 SQL 口径保持一致）：
    返回 'overdue' / 'soon' / 'normal' / 'none'
    """
    from datetime import date, datetime, timedelta

    text = (required_date or "").strip()[:10]
    if not text:
        return "none"
    if (status or "") in _DUE_DONE_STATUS:
        return "normal"
    try:
        d = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return "none"
    base = today or date.today()
    if d < base:
        return "overdue"
    if d <= base + timedelta(days=7):
        return "soon"
    return "normal"


DUE_TEXT = {"overdue": "已逾期", "soon": "临期", "normal": "正常", "none": "—"}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交期提醒引擎（v3.3.0 · 2026-11-08）

用户口径：
  · 从「离交货期 10 天」开始自动提醒；
  · 一旦订单状态改为「已发货」就不再提醒。

实现要点（都是被真实数据逼出来的）：
  1. 单一状态口径：`models.OrderStatus.NO_REMIND` 是唯一「不再提醒」集合，
     含「已发货 / 发货中 / 已交付 / 已验收 / 关闭 / 作废」。
     真实库里 2024~2025 年的历史单大量是「发货中」「已验收」，
     若只认「已发货」，一启动就会把几百张陈年单当成待催交。
  2. 日期容错：required_date 可能是 `YYYY-MM-DD` / `YYYY/M/D` / `YYYYMMDD` /
     带时间后缀 / datetime 对象（Excel 导入），脏数据一律跳过而不是崩界面。
  3. 逾期分栏：逾期仍在提醒窗口内（更该催），但「逾期 > overdue_max_days」
     的单归入 `long_overdue`，不进弹窗、不计入提醒数，只在状态栏报个总数。
     真实库 306 单全部逾期逾年，没有这条保护，功能上线即刷屏。
     `overdue_max_days=0` 表示不设上限（全部逾期都进清单）。
  4. 纯逻辑、零 Qt 依赖：可被 pytest 直接跑，也可被后台任务脚本复用。
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from . import models
from .db import now
from .errors import NotFoundError, StateError

DEFAULT_DAYS = 10                 # 提前天数：距交货期 ≤10 天进入提醒窗口
DEFAULT_OVERDUE_MAX = 30          # 逾期超过 30 天转「长期逾期」，不再逐单弹窗

LEVEL_OVERDUE = "overdue"
LEVEL_TODAY = "today"
LEVEL_SOON = "soon"

_LEVEL_TEXT = {
    LEVEL_OVERDUE: "已逾期",
    LEVEL_TODAY: "今天到期",
    LEVEL_SOON: "临期",
}

_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def parse_date(value: Any) -> Optional[date]:
    """宽松解析交货日期；无法识别返回 None（调用方跳过该单，不抛异常）"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    m = _DATE_RE.match(text)                     # 前 10 位即可，后面的时间后缀忽略
    if not m:
        m2 = _COMPACT_RE.match(text[:8])
        if m2:
            m = m2
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def days_left(required_date: Any, today: Optional[date] = None) -> Optional[int]:
    """距交货期天数：正=还剩几天，0=今天到期，负=已逾期几天"""
    d = parse_date(required_date)
    if d is None:
        return None
    return (d - (today or date.today())).days


def level_of(delta: int) -> str:
    if delta < 0:
        return LEVEL_OVERDUE
    if delta == 0:
        return LEVEL_TODAY
    return LEVEL_SOON


def label_of(delta: int) -> str:
    if delta < 0:
        return f"已逾期 {-delta} 天"
    if delta == 0:
        return "今天到期"
    return f"剩余 {delta} 天"


def is_quiet(status: Any) -> bool:
    """该状态是否不再提醒（发货及以后 / 作废）"""
    return str(status or "").strip() in models.OrderStatus.NO_REMIND


def scan(db, days: int = DEFAULT_DAYS,
         overdue_max_days: int = DEFAULT_OVERDUE_MAX,
         today: Optional[date] = None) -> Dict[str, Any]:
    """
    扫描全库订单，返回：
      {
        "rows": [临期/逾期清单，按紧急度升序],
        "long_overdue": [逾期超过上限的清单],
        "summary": {"total", "overdue", "today", "soon", "long_overdue"},
        "today": "YYYY-MM-DD", "days": 10, "overdue_max_days": 30,
      }
    每行含 id / doc_no / project_no / customer / order_type / status /
    required_date / amount_total / days_left / level / label。
    """
    base = today or date.today()
    try:
        days = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    try:
        cap = max(0, min(3650, int(overdue_max_days)))
    except (TypeError, ValueError):
        cap = DEFAULT_OVERDUE_MAX

    try:
        raw = db.query(
            "SELECT id, doc_no, project_no, customer, order_type, status, "
            "required_date, amount_total FROM orders")
    except Exception:                                   # noqa: BLE001
        raw = []

    rows: List[Dict[str, Any]] = []
    long_overdue: List[Dict[str, Any]] = []
    for o in raw:
        if is_quiet(o.get("status")):
            continue
        delta = days_left(o.get("required_date"), base)
        if delta is None or delta > days:
            continue
        item = dict(o)
        item["days_left"] = delta
        item["level"] = level_of(delta)
        item["label"] = label_of(delta)
        item["level_text"] = _LEVEL_TEXT[item["level"]]
        if cap and delta < -cap:
            long_overdue.append(item)
        else:
            rows.append(item)

    rows.sort(key=lambda r: (r["days_left"], str(r.get("doc_no") or "")))
    long_overdue.sort(key=lambda r: (r["days_left"], str(r.get("doc_no") or "")))

    summary = {
        "total": len(rows),
        "overdue": sum(1 for r in rows if r["level"] == LEVEL_OVERDUE),
        "today": sum(1 for r in rows if r["level"] == LEVEL_TODAY),
        "soon": sum(1 for r in rows if r["level"] == LEVEL_SOON),
        "long_overdue": len(long_overdue),
    }
    return {"rows": rows, "long_overdue": long_overdue, "summary": summary,
            "today": base.isoformat(), "days": days, "overdue_max_days": cap,
            "signature": signature(rows)}


def signature(rows: Sequence[Dict[str, Any]]) -> str:
    """清单指纹：同一天同一批单只弹一次；改状态/改交期/跨天都会变"""
    if not rows:
        return ""
    payload = "|".join(
        f"{r.get('id')}:{r.get('required_date')}:{r.get('status')}:"
        f"{r.get('days_left')}" for r in rows)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def notice_text(report: Dict[str, Any]) -> str:
    """托盘气泡 / 摘要行文本"""
    s = report.get("summary") or {}
    bits = []
    if s.get("overdue"):
        bits.append(f"逾期 {s['overdue']} 单")
    if s.get("today"):
        bits.append(f"今天到期 {s['today']} 单")
    if s.get("soon"):
        bits.append(f"{report.get('days', DEFAULT_DAYS)} 天内到期 {s['soon']} 单")
    head = "、".join(bits) if bits else "暂无临期订单"
    if s.get("long_overdue"):
        head += f"；另有 {s['long_overdue']} 单长期逾期未发货"
    return head


def mark_shipped(db, order_id: int, operator: str = "") -> Dict[str, Any]:
    """
    把订单状态置为「已发货」（v3.3.0 停止提醒的正规通道）：
      · 走状态机校验，终态/已发货后状态不允许倒退；
      · 事务内写状态 + 写一条 audit_log，留痕可在「审计留痕」里查；
      · 幂等：已经是「已发货」直接返回原记录，不重复写审计。
    """
    o = db.get_by_id("orders", int(order_id))
    if not o:
        raise NotFoundError(f"订单 {order_id} 不存在")
    old = str(o.get("status") or "")
    if old == models.OrderStatus.SHIPPED:
        return o
    if old in models.OrderStatus.NO_REMIND:
        raise StateError(
            f"订单 {o.get('doc_no')} 当前为「{old}」，已属发货后状态，无需再标记")
    models.assert_transition("order", old, models.OrderStatus.SHIPPED)
    with db.tx():
        db.update("orders",
                  {"status": models.OrderStatus.SHIPPED, "updated_at": now()},
                  "id = ?", (int(order_id),))
        db.log_action("order", int(order_id), "mark_shipped",
                      before={"status": old},
                      after={"status": models.OrderStatus.SHIPPED,
                             "reason": "交期提醒 · 标记已发货"},
                      operator=operator)
    return db.get_by_id("orders", int(order_id))

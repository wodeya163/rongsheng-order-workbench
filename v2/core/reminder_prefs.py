#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交期提醒偏好（v3.3.0）

落在 data/reminder.json（与数据库同目录）：更新器做目录交换时 data/ 原位保留，
所以升级不会重置用户的提醒设置。任何读写异常都吞掉并回退默认值——
提醒是辅助功能，绝不该影响业务启动。

去重口径：
  · 同一天 + 同一份清单指纹只弹一次（signature 见 core/duereminder.py）；
  · 「今日不再提醒」写 snooze_date，当天一律静默（状态栏徽标仍显示数量）；
  · 跨天 / 改交期 / 改状态导致清单变化 → 指纹变化，会再提醒一次。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict

FILENAME = "reminder.json"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,             # 总开关
    "days": 10,                  # 提前天数
    "overdue_max_days": 30,      # 逾期超过 N 天转长期逾期；0 = 不限
    "popup": True,               # 启动 / 复查时弹清单
    "tray": True,                # Windows 托盘气泡
    "snooze_date": "",           # 当日不再提醒
    "last_date": "",             # 上次弹窗日期
    "last_signature": "",        # 上次弹窗清单指纹
}


def state_path(data_dir) -> Path:
    return Path(data_dir) / FILENAME


def load(data_dir) -> Dict[str, Any]:
    state = dict(DEFAULTS)
    if data_dir is None:                # 内存库 / 离屏冒烟：不落盘
        return state
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in ("enabled", "popup", "tray"):
                if key in raw:
                    state[key] = bool(raw[key])
            for key, lo, hi in (("days", 1, 365), ("overdue_max_days", 0, 3650)):
                if key in raw:
                    try:
                        state[key] = max(lo, min(hi, int(raw[key])))
                    except (TypeError, ValueError):
                        pass
            for key in ("snooze_date", "last_date", "last_signature"):
                if key in raw:
                    state[key] = str(raw[key] or "")
    except Exception:                                   # noqa: BLE001
        pass
    return state


def save(data_dir, state: Dict[str, Any]) -> bool:
    if data_dir is None:
        return False
    try:
        p = state_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: state.get(k, v) for k, v in DEFAULTS.items()}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:                                   # noqa: BLE001
        return False


def should_popup(state: Dict[str, Any], signature: str,
                 today: date | None = None) -> bool:
    """是否该弹清单：总开关 + 弹窗开关 + 今日静默 + 同日同指纹去重"""
    if not signature or not state.get("enabled") or not state.get("popup"):
        return False
    day = (today or date.today()).isoformat()
    if state.get("snooze_date") == day:
        return False
    return (state.get("last_date"), state.get("last_signature")) != (day, signature)


def mark_popped(state: Dict[str, Any], signature: str,
                today: date | None = None) -> Dict[str, Any]:
    state["last_date"] = (today or date.today()).isoformat()
    state["last_signature"] = signature
    return state


def snooze_today(state: Dict[str, Any],
                 today: date | None = None) -> Dict[str, Any]:
    state["snooze_date"] = (today or date.today()).isoformat()
    return state

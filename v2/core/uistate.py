#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面偏好持久化（v3.2.0 · 操作导航折叠 / 隐藏状态）

单机绿色部署：状态落在 data/ui_state.json（与数据库同目录），
更新器做目录交换时 data/ 原位保留，所以升级后用户的导航习惯不丢。
任何读写异常都吞掉并回退默认值——界面偏好绝不该影响业务启动。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

FILENAME = "ui_state.json"

DEFAULTS: Dict[str, Any] = {
    "nav_visible": True,
    "nav_expanded": {},     # {分组标题: bool}
}


def state_path(data_dir) -> Path:
    return Path(data_dir) / FILENAME


def load(data_dir) -> Dict[str, Any]:
    """读取界面偏好；文件缺失/损坏 → 返回一份默认值副本"""
    state = dict(DEFAULTS)
    state["nav_expanded"] = {}
    if data_dir is None:                # 内存库 / 离屏冒烟：不落盘
        return state
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            state["nav_visible"] = bool(raw.get("nav_visible", True))
            exp = raw.get("nav_expanded")
            if isinstance(exp, dict):
                state["nav_expanded"] = {
                    str(k): bool(v) for k, v in exp.items()}
    except Exception:                                   # noqa: BLE001
        pass
    return state


def save(data_dir, state: Dict[str, Any]) -> bool:
    """写入界面偏好；返回是否成功（只读目录/内存库路径会返回 False）"""
    if data_dir is None:
        return False
    try:
        p = state_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "nav_visible": bool(state.get("nav_visible", True)),
            "nav_expanded": {
                str(k): bool(v)
                for k, v in (state.get("nav_expanded") or {}).items()},
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:                                   # noqa: BLE001
        return False

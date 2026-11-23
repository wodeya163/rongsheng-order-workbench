#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采购合同导入偏好（v3.5.2 · 导入即自动应用）

落在 data/contract_prefs.json（与数据库同目录）：更新器做目录交换时
data/ 原位保留，所以升级不会重置开关；不同导入入口（采购合同管理窗口 /
生产排产快捷导入）共用同一份文件，行为一致。

设计边界：
  · 这个开关只决定「满足条件时是否替用户多点一次保存」，不放宽任何
    判定条件；严格条件全部写在 PurchaseContractService._auto_apply_items()，
    配置文件无法把它们关掉，避免误把「不猜不摊」口径配没了。
  · 内存库 / 离屏冒烟时 data_dir 传 None，不落盘、全用默认值。
  · 任何读写异常都吞掉并回退默认值——辅助偏好绝不该影响业务启动。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

FILENAME = "contract_prefs.json"

DEFAULTS: Dict[str, Any] = {
    # 导入合同文件成功后，若解析/匹配/对账全部通过，自动写入物资分配
    "auto_apply_items": True,
}


def state_path(data_dir) -> Path:
    return Path(data_dir) / FILENAME


def load(data_dir) -> Dict[str, Any]:
    state = dict(DEFAULTS)
    if data_dir is None:
        return state
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "auto_apply_items" in raw:
            state["auto_apply_items"] = bool(raw["auto_apply_items"])
    except Exception:                                   # noqa: BLE001
        pass
    return state


def save(data_dir, state: Optional[Dict[str, Any]] = None) -> bool:
    """写入偏好；返回是否成功（内存库 / 只读目录返回 False）"""
    if data_dir is None:
        return False
    src = state if isinstance(state, dict) else {}
    try:
        p = state_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "auto_apply_items": bool(
                src.get("auto_apply_items", DEFAULTS["auto_apply_items"])),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:                                   # noqa: BLE001
        return False


def is_enabled(data_dir) -> bool:
    return bool(load(data_dir).get("auto_apply_items"))

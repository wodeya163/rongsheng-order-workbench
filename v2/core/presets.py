#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询方案（P2-5）

把「上月待发货」「我的逾期单」这类常用筛选条件存进库，一键切换，
不必每次重填。方案是**展示层配置**，不参与任何业务计算。

约定：
  · name 唯一；同视图内重名即覆盖（overwrite=True），否则报重复；
  · filters 存 JSON，结构等同 QueryPanel.values()；
  · 方案只存条件，不存结果，也不写审计（属于个人视图偏好）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .db import Database, now
from .errors import DuplicateError, NotFoundError, ValidationError

TABLE = "query_presets"
MAX_NAME = 40


class QueryPresetService:
    def __init__(self, db: Database):
        self.db = db

    # ── 读 ──────────────────────────────────────────────────
    def list(self, view_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出方案；给 view_key 时只返回该视图 + 通用方案"""
        if view_key:
            rows = self.db.query(
                f"SELECT * FROM {TABLE} WHERE view_key = ? OR view_key = '' "
                "ORDER BY sort_order, id", (view_key,))
        else:
            rows = self.db.query(f"SELECT * FROM {TABLE} ORDER BY sort_order, id")
        for r in rows:
            r["filters"] = self._parse(r.get("filters"))
        return rows

    def get(self, preset_id: int) -> Dict[str, Any]:
        row = self.db.get_by_id(TABLE, preset_id)
        if not row:
            raise NotFoundError(f"查询方案 {preset_id} 不存在")
        row["filters"] = self._parse(row.get("filters"))
        return row

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(f"SELECT * FROM {TABLE} WHERE name = ?", (name.strip(),))
        if row:
            row["filters"] = self._parse(row.get("filters"))
        return row

    # ── 写 ──────────────────────────────────────────────────
    def save(self, name: str, view_key: str, filters: Dict[str, Any],
             overwrite: bool = False, operator: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValidationError("方案名不能为空")
        if len(name) > MAX_NAME:
            raise ValidationError(f"方案名最长 {MAX_NAME} 个字符")
        payload = json.dumps(self._clean(filters), ensure_ascii=False)
        exist = self.find_by_name(name)
        if exist:
            if not overwrite:
                raise DuplicateError(f"已存在同名方案「{name}」，请换个名字或选择覆盖")
            with self.db.tx():
                self.db.update(TABLE, {
                    "view_key": view_key or "",
                    "filters": payload,
                    "created_by": operator or exist.get("created_by") or "",
                }, "id = ?", (exist["id"],))
            return self.get(exist["id"])
        with self.db.tx():
            pid = self.db.insert(TABLE, {
                "name": name,
                "view_key": view_key or "",
                "filters": payload,
                "sort_order": 100,
                "created_by": operator,
                "created_at": now(),
            })
        return self.get(pid)

    def delete(self, preset_id: int) -> None:
        row = self.db.get_by_id(TABLE, preset_id)
        if not row:
            return
        with self.db.tx():
            self.db.execute(f"DELETE FROM {TABLE} WHERE id = ?", (preset_id,))

    def rename(self, preset_id: int, new_name: str) -> Dict[str, Any]:
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValidationError("方案名不能为空")
        clash = self.find_by_name(new_name)
        if clash and clash["id"] != preset_id:
            raise DuplicateError(f"已存在同名方案「{new_name}」")
        with self.db.tx():
            self.db.update(TABLE, {"name": new_name}, "id = ?", (preset_id,))
        return self.get(preset_id)

    # ── 内部 ────────────────────────────────────────────────
    @staticmethod
    def _parse(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _clean(filters: Dict[str, Any]) -> Dict[str, Any]:
        """只保留非空条件，避免存一堆 '' / False 让方案看起来有内容"""
        out: Dict[str, Any] = {}
        for k, v in (filters or {}).items():
            if v in (None, "", False):
                continue
            out[str(k)] = v
        return out

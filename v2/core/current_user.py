#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
当前操作人（P1-9）

个人单机使用，不引入账号体系：
    优先级  ① 环境变量 PRODUCT_SUITE_OPERATOR（便于脚本/批量）
            ② data/operator.txt 里手工设置的名字（菜单「当前操作人…」写入）
            ③ Windows 登录名（getpass.getuser）
            ④ 兜底 "操作员"

用途：审计留痕 operator、订单 owner、排产附件 created_by。
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Optional

ENV_KEY = "PRODUCT_SUITE_OPERATOR"
FILE_NAME = "operator.txt"
_cache: Optional[str] = None
_cached_dir: Optional[Path] = None
_default_dir: Optional[Path] = None


def configure(data_dir: Optional[Path]) -> None:
    """启动时绑定数据目录，之后 name() 不传参也能读到 operator.txt 覆盖值"""
    global _default_dir, _cache, _cached_dir
    _default_dir = Path(data_dir) if data_dir else None
    _cache, _cached_dir = None, None


def _read_file(data_dir: Optional[Path]) -> str:
    if not data_dir:
        return ""
    p = Path(data_dir) / FILE_NAME
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
    return ""


def name(data_dir: Optional[Path] = None) -> str:
    """当前操作人（带进程内缓存；data_dir 变化时重算）"""
    global _cache, _cached_dir
    if data_dir is None:
        data_dir = _default_dir
    if _cache and _cached_dir == data_dir:
        return _cache
    value = (os.environ.get(ENV_KEY) or "").strip()
    if not value:
        value = _read_file(data_dir)
    if not value:
        try:
            value = getpass.getuser()
        except (OSError, KeyError):
            value = ""
    value = value.strip() or "操作员"
    _cache, _cached_dir = value, data_dir
    return value


def resolve(operator: str = "", data_dir: Optional[Path] = None) -> str:
    """空占位一律换成真实操作人；显式传入的名字原样保留"""
    value = (operator or "").strip()
    if value:
        return value
    return name(data_dir)


def set_name(value: str, data_dir: Path) -> str:
    """持久化到 data/operator.txt（空值 = 清除，回到 Windows 登录名）"""
    global _cache, _cached_dir
    text = (value or "").strip()
    target = Path(data_dir) / FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if text:
        target.write_text(text + "\n", encoding="utf-8")
    else:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    _cache, _cached_dir = None, None
    return name(data_dir)


def reset_cache() -> None:
    global _cache, _cached_dir
    _cache, _cached_dir = None, None

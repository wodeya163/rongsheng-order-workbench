#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路径与目录管理
兼容开发模式（源码同目录）与 PyInstaller 打包模式（APPDATA）。
v1.5 起支持用户自定义数据目录（通过 config.json 持久化）。
"""
import os
import sys
import json
import shutil
from pathlib import Path


# ── 固定路径（不随数据目录变化） ──────────────────────────────

def get_base_dir() -> str:
    """程序基础目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_config_dir() -> str:
    """配置文件所在目录（固定，不随数据目录移动）"""
    if getattr(sys, 'frozen', False):
        return os.path.join(os.environ.get('APPDATA', ''), '产品询报价管理系统')
    # 本文件位于 app/ 下，项目根目录为上一级
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.normpath(os.path.join(project_root, 'data'))


def get_config_path() -> str:
    """config.json 完整路径"""
    return os.path.join(get_config_dir(), 'config.json')


# ── 配置读写 ────────────────────────────────────────────────

def load_config() -> dict:
    """读取 config.json，失败返回空字典"""
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    """写入 config.json"""
    config_dir = get_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    with open(get_config_path(), 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ── 数据目录（动态，可由用户自定义） ──────────────────────────

def get_default_data_dir() -> str:
    """默认数据目录（未自定义时使用）"""
    return get_config_dir()


def get_data_dir() -> str:
    """
    获取当前数据存储目录。
    优先读取 config.json 中的 data_dir；若无效则回退到默认目录。
    """
    config = load_config()
    custom = config.get('data_dir', '').strip()
    if custom and os.path.isdir(custom):
        return os.path.normpath(custom)
    return get_default_data_dir()


def is_custom_data_dir() -> bool:
    """当前是否使用了自定义数据目录"""
    config = load_config()
    custom = config.get('data_dir', '').strip()
    return bool(custom) and os.path.isdir(custom)


def set_data_dir(new_dir: str, migrate: bool = True) -> dict:
    """
    切换数据目录并（可选）迁移已有数据。

    返回::

        {
            'success': bool,
            'migrated': bool,        # 是否执行了迁移
            'old_dir': str | None,
            'new_dir': str,
            'message': str,
        }
    """
    new_dir = os.path.normpath(new_dir)
    old_dir = get_data_dir()

    # 目标与当前相同 — 无需操作
    if os.path.normpath(old_dir) == new_dir:
        return {
            'success': True,
            'migrated': False,
            'old_dir': old_dir,
            'new_dir': new_dir,
            'message': '新目录与当前目录相同，无需切换。',
        }

    # 确保新目录存在
    os.makedirs(new_dir, exist_ok=True)

    # 写入配置
    config = load_config()
    config['data_dir'] = new_dir
    save_config(config)

    migrated = False
    if migrate:
        migrated = _migrate_data(old_dir, new_dir)

    return {
        'success': True,
        'migrated': migrated,
        'old_dir': old_dir,
        'new_dir': new_dir,
        'message': f'数据目录已切换到：{new_dir}' + ('（已迁移数据）' if migrated else ''),
    }


def reset_data_dir() -> dict:
    """恢复默认数据目录（不清除已迁移的数据）"""
    config = load_config()
    old_dir = get_data_dir()
    config.pop('data_dir', None)
    save_config(config)
    new_dir = get_default_data_dir()
    os.makedirs(new_dir, exist_ok=True)
    os.makedirs(os.path.join(new_dir, 'attachments'), exist_ok=True)
    return {
        'success': True,
        'old_dir': old_dir,
        'new_dir': new_dir,
        'message': f'已恢复默认数据目录：{new_dir}',
    }


# ── 数据迁移 ────────────────────────────────────────────────

def _migrate_data(old_dir: str, new_dir: str) -> bool:
    """
    将 old_dir 中的 products.db 和 attachments/ 复制到 new_dir。
    若 new_dir 中已存在 products.db 则跳过（不覆盖）。
    """
    moved_any = False

    # 迁移数据库
    old_db = os.path.join(old_dir, 'products.db')
    new_db = os.path.join(new_dir, 'products.db')
    if os.path.exists(old_db) and not os.path.exists(new_db):
        shutil.copy2(old_db, new_db)
        moved_any = True

    # 迁移附件目录
    old_att = os.path.join(old_dir, 'attachments')
    new_att = os.path.join(new_dir, 'attachments')
    if os.path.isdir(old_att):
        os.makedirs(new_att, exist_ok=True)
        for item in os.listdir(old_att):
            src = os.path.join(old_att, item)
            dst = os.path.join(new_att, item)
            if not os.path.exists(dst):
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                    moved_any = True
                elif os.path.isdir(src):
                    shutil.copytree(src, dst)
                    moved_any = True

    return moved_any


# ── 目录初始化 ──────────────────────────────────────────────

def ensure_dirs() -> None:
    """确保数据与附件目录存在"""
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, 'attachments'), exist_ok=True)


def attachments_dir() -> Path:
    """附件存储目录"""
    ensure_dirs()
    return Path(get_data_dir()) / 'attachments'


def db_path() -> str:
    """数据库文件完整路径"""
    return os.path.join(get_data_dir(), 'products.db')


# ── 向后兼容（旧代码可能直接引用模块级常量） ──────────────────
BASE_DIR = get_base_dir()
# 注意：DATA_DIR 不再是静态常量，请使用 get_data_dir() 获取实时路径
DATA_DIR = get_data_dir()

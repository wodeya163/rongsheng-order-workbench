#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库管理（SQLite 单文件）
保留旧表迁移逻辑，确保向后兼容。
"""
import os
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

from .paths import get_data_dir, db_path as get_db_path, ensure_dirs
from . import calc


class DatabaseManager:
    """数据库管理类"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            ensure_dirs()
            db_path = get_db_path()
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """初始化数据库表结构并执行兼容迁移"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                spec TEXT,
                category TEXT,
                quote_ratio REAL,
                inquiry_qty INTEGER,
                purchase_price REAL,
                inquiry_unit TEXT,
                description TEXT,
                project TEXT,
                is_won TEXT,
                quoter TEXT,
                image_path TEXT,
                drawing_path TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        ''')

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_name ON products(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_category ON products(category)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_project ON products(project)')

        # 迁移：旧表若存在 quote_price 列则重建（SQLite 不支持 DROP COLUMN）
        cursor.execute("PRAGMA table_info(products)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'quote_price' in columns:
            cursor.execute('''
                CREATE TABLE products_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    spec TEXT,
                    category TEXT,
                    quote_ratio REAL,
                    inquiry_qty INTEGER,
                    purchase_price REAL,
                    inquiry_unit TEXT,
                    description TEXT,
                    project TEXT,
                    is_won TEXT,
                    quoter TEXT,
                    image_path TEXT,
                    drawing_path TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            ''')
            cursor.execute('''
                INSERT INTO products_new (id, name, spec, category, quote_ratio,
                    inquiry_qty, purchase_price, inquiry_unit, description, project,
                    is_won, quoter, image_path, drawing_path, created_at, updated_at)
                SELECT id, name, spec, category, quote_ratio,
                    inquiry_qty, purchase_price, inquiry_unit, description, project,
                    is_won, quoter, image_path, drawing_path, created_at, updated_at
                FROM products
            ''')
            cursor.execute('DROP TABLE products')
            cursor.execute('ALTER TABLE products_new RENAME TO products')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_name ON products(name)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_category ON products(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_project ON products(project)')

        # 迁移：添加 quoter 列（兼容更早版本）
        if 'quoter' not in columns:
            cursor.execute('ALTER TABLE products ADD COLUMN quoter TEXT')

        # 迁移：添加 inquiry_unit（询价单位）列
        if 'inquiry_unit' not in columns:
            cursor.execute('ALTER TABLE products ADD COLUMN inquiry_unit TEXT')

        # 迁移：添加 is_won（是否中标）列，用于中标分析
        if 'is_won' not in columns:
            cursor.execute('ALTER TABLE products ADD COLUMN is_won TEXT')

        conn.commit()
        conn.close()

    def add_product(self, product_data: Dict[str, Any]) -> int:
        """添加新产品，返回自增 id"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        now = datetime.now().isoformat()
        product_data = dict(product_data)
        product_data['created_at'] = now
        product_data['updated_at'] = now

        columns = ', '.join(product_data.keys())
        placeholders = ', '.join(['?' for _ in product_data])
        values = list(product_data.values())

        cursor.execute(f'INSERT INTO products ({columns}) VALUES ({placeholders})', values)
        product_id = cursor.lastrowid

        conn.commit()
        conn.close()
        return product_id

    def update_product(self, product_id: int, product_data: Dict[str, Any]):
        """更新产品信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        product_data = dict(product_data)
        product_data['updated_at'] = datetime.now().isoformat()

        set_clause = ', '.join([f'{k} = ?' for k in product_data.keys()])
        values = list(product_data.values()) + [product_id]

        cursor.execute(f'UPDATE products SET {set_clause} WHERE id = ?', values)

        conn.commit()
        conn.close()

    def delete_product(self, product_id: int):
        """删除产品"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM products WHERE id = ?', (product_id,))
        conn.commit()
        conn.close()

    def get_product(self, product_id: int) -> Optional[Dict[str, Any]]:
        """获取单个产品"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM products WHERE id = ?', (product_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        return None

    def search_products(self, filters: Dict[str, str] = None) -> List[Dict[str, Any]]:
        """多字段模糊搜索"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        query = 'SELECT * FROM products'
        params = []

        if filters:
            conditions = []
            for key, value in filters.items():
                if value and key in ['name', 'spec', 'category', 'project', 'quoter', 'is_won', 'inquiry_unit']:
                    conditions.append(f'{key} LIKE ?')
                    params.append(f'%{value}%')
            if conditions:
                query += ' WHERE ' + ' AND '.join(conditions)

        query += ' ORDER BY updated_at DESC'

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return []

        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_all_products(self) -> List[Dict[str, Any]]:
        """获取全部产品（用于汇总/导入校验）"""
        return self.search_products()

    def get_summary(self, filters: Dict[str, str] = None) -> Dict[str, float]:
        """价格汇总（委托纯函数计算）"""
        products = self.search_products(filters)
        return calc.compute_summary(products)

    def get_project_summary(self) -> List[Dict[str, Any]]:
        """按投标项目分组汇总"""
        products = self.get_all_products()
        return calc.compute_project_summary(products)

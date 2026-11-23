#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表名集中声明（P2-4）

背景：`Database.insert/update/get_by_id` 需要把表名拼进 SQL（SQLite 不支持参数化
表名），此前靠各业务模块自觉传字面量。只要有一处传了外部字符串（比如从界面下拉
或配置读出来的值），就是注入面。这里把全部合法表名收进一个 frozenset，CRUD 入口
统一校验，非法表名直接抛 ValidationError，拼 SQL 前就被拦下。

新增表时：① 在 schema.sql 加 DDL；② 在本文件 TABLES 里登记；③ 若属于业务实体，
同步维护层 ENTITY_SPECS。`check_schema()` 会在测试里兜底核对「DDL 有、清单没有」。
"""
from pathlib import Path
from typing import Iterable, Set

# 物理表清单（与 schema.sql 一一对应，按业务分组便于检索）
SEQUENCE = "doc_sequence"
AUDIT = "audit_log"
SCHEMA_META = "schema_meta"

BID_PROJECTS = "bid_projects"
BID_ITEMS = "bid_items"
CODE_PROJECTS = "code_projects"
CODE_ITEMS = "code_items"
ORDERS = "orders"
ORDER_ITEMS = "order_items"
PRODUCTION_PROJECTS = "production_projects"
PRODUCTION_TASKS = "production_tasks"
PRODUCTION_LINKS = "production_links"
ORDER_SCHEDULES = "order_schedules"
ORDER_SCHEDULE_FILES = "order_schedule_files"
QUERY_PRESETS = "query_presets"
# v3.4.0 采购合同管理（生产排产模块）
PURCHASE_CONTRACTS = "purchase_contracts"
PURCHASE_CONTRACT_FILES = "purchase_contract_files"
# v3.5.0 合同↔订单明细桥表（订单盈亏 / 多供应商采购归集）
PURCHASE_CONTRACT_ITEMS = "purchase_contract_items"

TABLES: Set[str] = frozenset({
    SEQUENCE, AUDIT, SCHEMA_META,
    BID_PROJECTS, BID_ITEMS,
    CODE_PROJECTS, CODE_ITEMS,
    ORDERS, ORDER_ITEMS,
    PRODUCTION_PROJECTS, PRODUCTION_TASKS, PRODUCTION_LINKS,
    ORDER_SCHEDULES, ORDER_SCHEDULE_FILES,
    QUERY_PRESETS,
    PURCHASE_CONTRACTS, PURCHASE_CONTRACT_FILES,
    PURCHASE_CONTRACT_ITEMS,
})

# 业务实体表（不含审计/序号/结构元数据等基础设施表）
BUSINESS_TABLES: Set[str] = frozenset(TABLES - {AUDIT, SCHEMA_META})


class TableNameError(ValueError):
    """非法表名（不在白名单内），继承 ValueError 便于上层统一按参数错误处理"""


def check(name: str) -> str:
    """校验表名并原样返回；非法即抛 TableNameError"""
    if not isinstance(name, str) or name not in TABLES:
        raise TableNameError(f"非法表名：{name!r}（未在 core/tables.py 登记）")
    return name


def is_valid(name: str) -> bool:
    return isinstance(name, str) and name in TABLES


def ddl_tables(schema_path: Path) -> Set[str]:
    """从 schema.sql 里扫出 CREATE TABLE 的表名，用于一致性校验"""
    import re

    text = Path(schema_path).read_text(encoding="utf-8")
    return set(re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z_][\w]*)",
                          text, flags=re.I))


def missing_from_declaration(schema_path: Path) -> Set[str]:
    """DDL 里有、但本清单没登记的表（测试用；应为空集）"""
    return ddl_tables(schema_path) - set(TABLES)


def undeclared_in_ddl(schema_path: Path) -> Set[str]:
    """本清单有、但 DDL 里没有的表（测试用；应为空集）"""
    return set(TABLES) - ddl_tables(schema_path) - {SCHEMA_META}


def sorted_names(names: Iterable[str] = None) -> list:
    return sorted(TABLES if names is None else names)

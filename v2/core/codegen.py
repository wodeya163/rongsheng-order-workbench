#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单号生成：事务内取号，避免同月并发重号（v2.2）"""
from datetime import datetime
from typing import Optional

from .db import Database


PREFIX_BID = "ZB"              # 招投标项目（询报价统计口径）
PREFIX_CODE_PROJECT = "XPM"    # 询配码项目 / 文件
PREFIX_ORDER = "DD"            # 外部订单（用户按询配码文件下单）
PREFIX_INTERNAL_ORDER = "ND"   # 内部订单（中标项目生成）
PREFIX_PROJECT = "SC"          # 生产排产项目
PREFIX_SCHEDULE = "DDPS"       # 订单排产单（v2.4，一条订单明细一张）
PREFIX_CONTRACT = "CG"         # 采购合同（v3.4.0，生产排产操作模块）


def next_doc_no(db: Database, prefix: str, at: Optional[datetime] = None) -> str:
    """取下一个单号，如 DD-202611-0001；调用方应包在 db.tx() 内"""
    at = at or datetime.now()
    period = at.strftime("%Y%m")
    db.execute(
        "INSERT OR IGNORE INTO doc_sequence(prefix, period, next_val) VALUES (?, ?, 1)",
        (prefix, period),
    )
    db.execute(
        "UPDATE doc_sequence SET next_val = next_val + 1 WHERE prefix = ? AND period = ?",
        (prefix, period),
    )
    row = db.query_one(
        "SELECT next_val - 1 AS seq FROM doc_sequence WHERE prefix = ? AND period = ?",
        (prefix, period),
    )
    return f"{prefix}-{period}-{int(row['seq']):04d}"


def item_code_of(code_project_no: str, line_no: int) -> str:
    """询配码明细项号：<项目号>-<两位行号>"""
    return f"{code_project_no}-{line_no:02d}"


def order_line_no(order_no: str, line_no: int) -> str:
    """订单明细行标识：<订单号>-<两位行号>"""
    return f"{order_no}-{line_no:02d}"


def bid_line_no(bid_project_no: str, line_no: int) -> str:
    """中标项目明细行标识：<项目号>-<两位行号>（内部订单溯源用）"""
    return f"{bid_project_no}-{line_no:02d}"


def task_code_of(project_no: str, seq: int) -> str:
    return f"{project_no}-T{seq:02d}"

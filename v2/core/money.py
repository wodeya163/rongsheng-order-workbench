#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金额精度统一（P1-6）

背景：
    金额列在 SQLite 里是 REAL（双精度）。单行 0.1 无法精确表示，
    多行累加后会出现 12345.679999999998 这类尾数，导出/对账被质疑。

约定：
    1. 所有金额【写入】一律先 q2() 量化到分，再转 float 落库；
       落库值的 str() 必然还原成两位小数（Python 最短往返表示），
       因此从库读回来做 Decimal 运算不带二进制尾数。
    2. 所有金额【求和】一律用 sum2()（Decimal 累加），不做 float 相加。
    3. 单价可以是 4 位小数（行业报价常见），用 q4()；金额只到分。
    4. 不用 Decimal 直接落库（sqlite3 不认 Decimal），只做中间计算。

★ 这是「REAL + 两位量化」方案，不是整数分存储。
   选择理由：金额列语义不变、无需迁移存量数据、读写路径零改动；
   代价是极值（>2^53 分）不可表示，业务上不可能出现。
   若将来要彻底转整数分，只需把 to_db() 换成 *100 取整 + 改列名。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

CENT = Decimal("0.01")
TEN_THOUSANDTH = Decimal("0.0001")
ZERO = Decimal("0")


def to_dec(value: Any, default: Decimal = ZERO) -> Decimal:
    """任意输入 → Decimal；空/非法 → default（不抛异常，金额字段不该阻断业务）"""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return default


def q2(value: Any) -> Decimal:
    """量化到分（四舍五入，银行/财务惯例的 HALF_UP）"""
    return to_dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def q4(value: Any) -> Decimal:
    """量化到 4 位（单价用）"""
    return to_dec(value).quantize(TEN_THOUSANDTH, rounding=ROUND_HALF_UP)


def to_db(value: Any) -> float:
    """金额 → 落库 float（已量化到分）"""
    return float(q2(value))


def to_db_price(value: Any) -> float:
    """单价 → 落库 float（4 位）"""
    return float(q4(value))


def mul(qty: Any, price: Any) -> Decimal:
    """数量 × 单价 → 量化到分的金额"""
    return q2(to_dec(qty) * to_dec(price))


def sum2(values: Iterable[Any]) -> Decimal:
    """Decimal 累加后量化到分（避免 float 累加尾数）"""
    total = ZERO
    for v in values:
        total += to_dec(v)
    return q2(total)


def eq(a: Any, b: Any, tol: str = "0.005") -> bool:
    """金额相等判定（默认容差半分）"""
    return abs(to_dec(a) - to_dec(b)) <= to_dec(tol)


def fmt(value: Any, digits: int = 2) -> str:
    """展示用：千分位 + 固定小数位"""
    d = q2(value) if digits == 2 else to_dec(value).quantize(CENT, rounding=ROUND_HALF_UP)
    return f"{d:,.{digits}f}"

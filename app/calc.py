#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务计算（纯函数，便于单元测试）
核心规则：对外报价 = 采购价格 ÷ 报价比率
"""
from typing import Dict, Any, List, Optional


def safe_float(value: Any, default: float = 0.0) -> float:
    """安全转为 float，失败返回默认值"""
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """安全转为 int，失败返回默认值"""
    try:
        if value is None or value == '':
            return default
        return int(float(value))
    except (ValueError, TypeError):
        return default


def compute_quote_price(purchase_price: Any, quote_ratio: Any) -> float:
    """对外报价 = 采购价格 ÷ 报价比率"""
    pp = safe_float(purchase_price)
    qr = safe_float(quote_ratio, 1.0)
    return pp / qr if qr else 0.0


def compute_summary(products: List[Dict[str, Any]]) -> Dict[str, float]:
    """汇总：采购总额 / 报价总额 / 毛利 / 毛利率"""
    count = len(products)
    total_purchase = 0.0
    total_quote = 0.0

    for p in products:
        pp = safe_float(p.get('purchase_price'))
        qr = safe_float(p.get('quote_ratio'), 1.0)
        qty = safe_int(p.get('inquiry_qty'))
        qp = pp / qr if qr else 0.0
        total_purchase += pp * qty
        total_quote += qp * qty

    profit = total_quote - total_purchase
    profit_rate = (profit / total_quote * 100) if total_quote else 0.0

    return {
        'count': count,
        'total_purchase': total_purchase,
        'total_quote': total_quote,
        'profit': profit,
        'profit_rate': profit_rate,
    }


def compute_project_summary(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按投标项目分组汇总，返回按报价总额降序的列表"""
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for p in products:
        project = (p.get('project') or '').strip() or '（未分配项目）'
        if project not in groups:
            groups[project] = {
                'project': project,
                'count': 0,
                'total_purchase': 0.0,
                'total_quote': 0.0,
                'won_count': 0,
                'lost_count': 0,
            }
            order.append(project)

        g = groups[project]
        pp = safe_float(p.get('purchase_price'))
        qr = safe_float(p.get('quote_ratio'), 1.0)
        qty = safe_int(p.get('inquiry_qty'))
        qp = pp / qr if qr else 0.0
        g['count'] += 1
        g['total_purchase'] += pp * qty
        g['total_quote'] += qp * qty
        is_won = (p.get('is_won') or '').strip()
        if is_won == '是':
            g['won_count'] += 1
        elif is_won == '否':
            g['lost_count'] += 1

    for g in groups.values():
        g['profit'] = g['total_quote'] - g['total_purchase']
        g['profit_rate'] = (g['profit'] / g['total_quote'] * 100) if g['total_quote'] else 0.0
        decided = g['won_count'] + g['lost_count']
        g['win_rate'] = (g['won_count'] / decided * 100) if decided else 0.0

    return sorted(groups.values(), key=lambda x: x['total_quote'], reverse=True)

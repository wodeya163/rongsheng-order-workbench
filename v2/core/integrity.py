#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据一致性检查（纯函数，可离线测试）

六条不变量：
  1. 数量守恒：Σ production_links.allocated_qty ≤ order_items.qty_ordered
  2. 来源互斥：一条订单明细不能同时挂询配码项和中标明细项
  3. 外键完整：排产关联必须指向存在的订单明细
  4. 内部订单唯一：一个中标项目最多一张内部订单
  5. 采购数量守恒（v3.5.0）：Σ purchase_contract_items.qty ≤ order_items.qty_ordered
  6. 合同金额对齐（v3.5.0，仅告警）：Σ 明细金额与合同总额偏差 > 0.01 时列出；
     文档金额识别失败留 0 属合法状态，因此不计入 problem_count。
"""
from typing import Any, Dict, List

from .db import Database


def check(db: Database) -> Dict[str, Any]:
    over: List[Dict[str, Any]] = db.query(
        """SELECT oi.id, oi.qty_ordered,
                  COALESCE(SUM(pl.allocated_qty), 0) AS allocated
           FROM order_items oi
           LEFT JOIN production_links pl ON pl.order_item_id = oi.id
           GROUP BY oi.id
           HAVING allocated > oi.qty_ordered + 1e-9""")
    mixed = db.query(
        """SELECT id FROM order_items
           WHERE source_code_item_id IS NOT NULL AND source_bid_item_id IS NOT NULL""")
    orphan = db.query(
        """SELECT pl.id FROM production_links pl
           LEFT JOIN order_items oi ON oi.id = pl.order_item_id
           WHERE oi.id IS NULL""")
    multi = db.query(
        """SELECT source_bid_project_id, COUNT(*) AS n FROM orders
           WHERE order_type = '内部订单'
           GROUP BY source_bid_project_id HAVING n > 1""")
    # v3.5.0：合同物资分配的数量守恒。一张订单明细可分给多份合同（分批采购），
    # 但采购总量不得超过订单实值，否则采购成本会被重复计入。
    over_buy = db.query(
        """SELECT oi.id, oi.qty_ordered,
                  COALESCE(SUM(pci.qty), 0) AS purchased
           FROM order_items oi
           LEFT JOIN purchase_contract_items pci ON pci.order_item_id = oi.id
           GROUP BY oi.id
           HAVING purchased > oi.qty_ordered + 1e-9""")
    # 金额对齐只作告警：合同金额可能尚未识别（0）或含运费等未分摊项。
    amount_gap = db.query(
        """SELECT c.id, c.doc_no, c.amount,
                  COALESCE(SUM(pci.amount), 0) AS items_total
           FROM purchase_contracts c
           JOIN purchase_contract_items pci ON pci.contract_id = c.id
           WHERE c.status IN ('已签订', '执行中', '已完成') AND c.amount > 0
           GROUP BY c.id
           HAVING ABS(items_total - c.amount) > 0.011""")
    problems = (len(over) + len(mixed) + len(orphan) + len(multi)
                + len(over_buy))
    parts = [f"超排记录 {len(over)} 条", f"来源混挂明细 {len(mixed)} 条",
             f"孤立关联 {len(orphan)} 条", f"一中标多内部订单 {len(multi)} 组",
             f"超采记录 {len(over_buy)} 条"]
    summary = (" / ".join(parts) + "\n"
               + ("✅ 全部通过：数量守恒、来源互斥、外键完整、内部订单唯一、采购数量守恒"
                  if problems == 0 else "⚠ 存在异常，请核查上述记录"))
    if amount_gap:
        summary += (f"\n· 金额偏差告警 {len(amount_gap)} 份合同"
                    "（合同总额与已分配明细金额不一致，可在合同物资分配页核对）")
    return {
        "ok": problems == 0,
        "problem_count": problems,
        "over_alloc": over,
        "mixed_source": mixed,
        "orphan_links": orphan,
        "duplicate_internal": multi,
        "over_purchase": over_buy,
        "contract_amount_gap": amount_gap,
        "summary": summary,
    }

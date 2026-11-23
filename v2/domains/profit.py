#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 7：订单盈亏分析（v3.5.0 · 张哥 2026-11-18 三条口径拍板）

口径（唯一来源，UI / 图表 / 导出一律走这里，禁止在别处重算）：
  1. 销售额 = Σ 订单明细金额（逐行 amount；为 0 时回退 数量 × 单价），
     与订单头「价税合计」差额 > 0.01 时给 warning，绝不静默挑一个用；
  2. 金额统一【含税】：采购额 = Σ 本合同已分配明细的含税金额，
     仅统计【已签订 / 执行中 / 已完成】合同（草稿 / 作废不计）；
     若参与统计的合同被标成不含税，只出金额、profit_rate = None 并告警；
  3. 未覆盖（还有物资没关联已签合同）→ complete=False，利润率标「不完整」，
     绝不把未采购的物资当 0 成本算虚高利润。

为什么不在 order_items 上加采购单价：一条物资可能分两家供应商、分两批采购，
单值列表达不了；采购成本是按 purchase_contract_items 汇总出来的【派生值】。
一张订单 → 多项物资 → 多份供应商合同，以及一份合同跨多张订单（合并采购），
都由桥表的 order_item_id 天然归集，不会重复计入。

金额一律 Decimal（core/money.py 的 q2 / sum2），不做 float 连加。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from ..core import money
from ..core.db import Database
from ..core.errors import NotFoundError
from .contract import SIGNED_STATUSES

TAX_INCLUDED = 1                 # 统一含税口径
ZERO = Decimal("0")
RATE_GAP = Decimal("0.01")      # 表头/明细差额告警阈值（分）


def _f(value: Decimal) -> float:
    return float(money.q2(value))


class OrderProfitService:
    """订单级销售 / 采购 / 毛利 / 利润率汇总，兼作图表的唯一数据出口"""

    def __init__(self, db: Database):
        self.db = db

    # ── 一次性取数（309 单 / 738 行量级，全表拉进内存比 N+1 快得多）──

    def _collect(self) -> Dict[str, Any]:
        orders = {
            int(r["id"]): dict(r)
            for r in self.db.query(
                "SELECT id, doc_no, project_no, customer, order_date, status, "
                "amount_total, amount_manual, required_date FROM orders")
        }
        items: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        item_index: Dict[int, Dict[str, Any]] = {}
        for r in self.db.query(
                "SELECT id, order_id, line_no, item_name, spec, unit, "
                "qty_ordered, unit_price, amount FROM order_items "
                "ORDER BY order_id, line_no"):
            row = dict(r)
            qty = money.to_dec(row.get("qty_ordered"))
            price = money.to_dec(row.get("unit_price"))
            declared = money.q2(row.get("amount") or 0)
            # amount 为 0（旧数据留空）= 派生值，按 数量 × 单价 补
            row["_sales"] = declared if declared != ZERO else money.mul(qty, price)
            row["_qty"] = qty
            items[int(row["order_id"])].append(row)
            item_index[int(row["id"])] = row

        placeholders = ",".join("?" for _ in SIGNED_STATUSES)
        allocs = [dict(r) for r in self.db.query(
            "SELECT pci.id, pci.contract_id, pci.order_item_id, pci.qty, "
            "pci.unit_price, pci.amount, pci.currency, "
            "c.doc_no AS contract_no, c.supplier, c.status, c.tax_included, "
            "c.sign_date, c.order_id AS contract_order_id "
            "FROM purchase_contract_items pci "
            "JOIN purchase_contracts c ON c.id = pci.contract_id "
            f"WHERE c.status IN ({placeholders}) ORDER BY pci.id",
            tuple(SIGNED_STATUSES))]
        alloc_by_item: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for a in allocs:
            alloc_by_item[int(a["order_item_id"])].append(a)
        return {"orders": orders, "items": items, "item_index": item_index,
                "allocs": allocs, "alloc_by_item": alloc_by_item}

    @staticmethod
    def _line_sales(row: Dict[str, Any]) -> Decimal:
        """某条订单明细按其已采购数量分摊的销售额（按供应商/时间维度用，不重复计数）"""
        qty = row["_qty"]
        if qty <= ZERO:
            return ZERO
        return money.q2(row["_sales"] * money.to_dec(row.get("_buy_qty") or 0) / qty)

    # ── 单订单完整口径 ────────────────────────────────────────

    def order_summary(self, order_id: int) -> Dict[str, Any]:
        data = self._collect()
        o = data["orders"].get(int(order_id))
        if not o:
            raise NotFoundError(f"订单 {order_id} 不存在")
        return self._one(o, data)

    def _one(self, o: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        oid = int(o["id"])
        lines = data["items"].get(oid, [])
        sales = money.sum2(r["_sales"] for r in lines)
        head = money.q2(o.get("amount_total") or 0)
        warnings: List[str] = []
        gap = money.q2(sales - head)
        if abs(gap) > RATE_GAP:
            warnings.append(
                f"订单表头价税合计 ¥{_f(head):,.2f} 与明细汇总 ¥{_f(sales):,.2f} "
                f"相差 ¥{_f(gap):,.2f}，销售额按【明细为准】口径")

        by_item: List[Dict[str, Any]] = []
        by_supplier: Dict[str, Decimal] = defaultdict(lambda: ZERO)
        purchase = ZERO
        covered = 0
        tax_bad: List[str] = []
        for r in lines:
            iid = int(r["id"])
            bought = ZERO
            suppliers: List[str] = []
            for a in data["alloc_by_item"].get(iid, []):
                amt = money.q2(a.get("amount") or 0)
                bought += amt
                purchase += amt
                name = str(a.get("supplier") or "未填供方").strip() or "未填供方"
                by_supplier[name] += amt
                if name not in suppliers:
                    suppliers.append(name)
                tax_flag = a.get("tax_included")
                tax_flag = 1 if tax_flag is None else int(tax_flag)
                if tax_flag != TAX_INCLUDED:
                    tax_bad.append(str(a.get("contract_no") or "?"))
            if bought > ZERO:
                covered += 1
            profit = money.q2(r["_sales"] - bought)
            by_item.append({
                "item_id": iid, "line_no": r["line_no"],
                "item_name": r["item_name"], "spec": r.get("spec") or "",
                "qty_ordered": _f(r["_qty"]),
                "sales_amount": _f(r["_sales"]),
                "purchase_amount": _f(bought),
                "profit": _f(profit),
                "profit_rate": (float(money.q2(profit / r["_sales"] * 100))
                                if r["_sales"] > ZERO else None),
                "suppliers": "、".join(suppliers),
                "covered": bought > ZERO,
            })

        purchase = money.q2(purchase)
        gross = money.q2(sales - purchase)
        complete = bool(lines) and covered == len(lines)
        if not complete:
            warnings.append(
                f"还有 {len(lines) - covered} 项物资未关联已签订采购合同，"
                "利润率标为「不完整」（未采购物资不计 0 成本）")
        tax_bad = sorted(set(tax_bad))
        if tax_bad:
            warnings.append(
                f"合同 {'、'.join(tax_bad)} 标记为不含税，与统一含税口径不一致："
                "只出金额，利润率不出数")
        rate: Optional[float] = (None if tax_bad or sales <= ZERO
                                 else float(money.q2(gross / sales * 100)))
        return {
            "order_id": oid,
            "doc_no": o.get("doc_no") or "",
            "project_no": o.get("project_no") or "",
            "customer": o.get("customer") or "",
            "order_date": o.get("order_date") or "",
            "status": o.get("status") or "",
            "sales_total": _f(sales),
            "sales_head": _f(head),
            "sales_head_gap": _f(gap),
            "purchase_total": _f(purchase),
            "gross_profit": _f(gross),
            "profit_rate": rate,
            "complete": complete,
            "coverage": round(covered / len(lines) * 100, 1) if lines else 0.0,
            "item_count": len(lines),
            "covered_items": covered,
            "supplier_count": len(by_supplier),
            "by_item": by_item,
            "by_supplier": [
                {"supplier": k, "purchase_amount": _f(v)}
                for k, v in sorted(by_supplier.items(), key=lambda x: -x[1])
            ],
            "warnings": warnings,
        }

    # ── 批量（订单页列 / 图表 / 导出）──────────────────────────

    def overview(self, order_ids: Optional[Sequence[int]] = None,
                 limit: int = 0) -> List[Dict[str, Any]]:
        data = self._collect()
        want = set(int(x) for x in order_ids) if order_ids is not None else None
        ids = [i for i in data["orders"] if want is None or i in want]
        ids.sort(key=lambda i: (data["orders"][i].get("order_date") or "", i))
        rows = [self._one(data["orders"][i], data) for i in ids]
        return rows[-limit:] if limit and limit > 0 else rows

    def breakdown(self, dimension: str = "order",
                  limit: int = 0) -> List[Dict[str, Any]]:
        """图表维度：order 按订单 / supplier 按供应商 / month 按订单月份。

        supplier 与 month 用「按已采购数量分摊的销售额」聚合，避免一张订单
        多供应商时把整单销售额重复算进每个供应商。
        """
        data = self._collect()
        buckets: Dict[str, Dict[str, Decimal]] = defaultdict(
            lambda: {"sales": ZERO, "purchase": ZERO})
        if dimension == "supplier":
            for a in data["allocs"]:
                row = data["item_index"].get(int(a["order_item_id"]))
                if not row:
                    continue
                key = str(a.get("supplier") or "未填供方").strip() or "未填供方"
                share = dict(row)
                share["_buy_qty"] = a.get("qty") or 0
                b = buckets[key]
                b["sales"] += self._line_sales(share)
                b["purchase"] += money.q2(a.get("amount") or 0)
        elif dimension == "month":
            for a in data["allocs"]:
                row = data["item_index"].get(int(a["order_item_id"]))
                if not row:
                    continue
                o = data["orders"].get(int(row["order_id"]))
                key = str((o or {}).get("order_date") or a.get("sign_date") or "未填日期")[:7]
                if key == "未填日":
                    key = "未填日期"
                share = dict(row)
                share["_buy_qty"] = a.get("qty") or 0
                b = buckets[key]
                b["sales"] += self._line_sales(share)
                b["purchase"] += money.q2(a.get("amount") or 0)
        else:
            for one in self.overview():
                b = buckets[str(one["project_no"] or one["doc_no"] or one["order_id"])]
                b["sales"] += money.q2(one["sales_total"])
                b["purchase"] += money.q2(one["purchase_total"])

        out: List[Dict[str, Any]] = []
        for label, b in sorted(buckets.items()):
            profit = money.q2(b["sales"] - b["purchase"])
            out.append({
                "label": label,
                "sales_total": _f(b["sales"]),
                "purchase_total": _f(b["purchase"]),
                "gross_profit": _f(profit),
                "profit_rate": (float(money.q2(profit / b["sales"] * 100))
                                if b["sales"] > ZERO else None),
            })
        return out[-limit:] if limit and limit > 0 else out

    # ── 导出（与 core/excelio.py 的 SheetData 格式一致）──────────

    def export_sheets(self, rows: Optional[Sequence[Dict[str, Any]]] = None
                      ) -> Dict[str, Any]:
        rows = list(rows if rows is not None else self.overview())
        heads = ["订单项目编号", "客户名称", "下单时间", "订单状态",
                 "销售额(含税)", "采购额(含税)", "毛利", "利润率(%)",
                 "覆盖度(%)", "完整性", "告警"]
        body = [[r["project_no"] or r["doc_no"], r["customer"], r["order_date"],
                 r["status"], r["sales_total"], r["purchase_total"],
                 r["gross_profit"],
                 f"{r['profit_rate']:.2f}" if r["profit_rate"] is not None else "不出数",
                 r["coverage"], "完整" if r["complete"] else "不完整",
                 "；".join(r["warnings"])] for r in rows]
        details = ["订单项目编号", "客户名称", "行号", "物资名称", "规格",
                   "数量", "销售额(含税)", "采购额(含税)", "毛利", "利润率(%)",
                   "供方", "是否已采购"]
        data = self._collect()
        lines = []
        for r in rows:
            o = data["orders"].get(int(r["order_id"]))
            if not o:
                continue
            for it in self._one(o, data)["by_item"]:
                lines.append([
                    r["project_no"] or r["doc_no"], r["customer"], it["line_no"],
                    it["item_name"], it["spec"], it["qty_ordered"],
                    it["sales_amount"], it["purchase_amount"], it["profit"],
                    f"{it['profit_rate']:.2f}" if it["profit_rate"] is not None else "—",
                    it["suppliers"], "是" if it["covered"] else "否"])
        suppliers = ["供方", "采购额(含税)", "对应物资销售额(含税)", "毛利", "利润率(%)"]
        sup_rows = [[s["label"], s["purchase_total"], s["sales_total"],
                     s["gross_profit"],
                     f"{s['profit_rate']:.2f}" if s["profit_rate"] is not None else "—"]
                    for s in self.breakdown("supplier")]
        return {
            "订单盈亏汇总": (heads, body),
            "物资盈亏明细": (details, lines),
            "供方采购分析": (suppliers, sup_rows),
        }

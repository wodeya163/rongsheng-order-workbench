#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 1：询报价 / 招投标项目统计

职责边界：只统计招投标项目及其报价数据；只有「中标」项目允许关联到生产排产。
★ 本模块与询配码域【没有任何关联】，不提供任何"转询配码"功能。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core import codegen, models
from ..core.db import Database, now
from ..core.errors import NotFoundError, ValidationError


class BidService:
    def __init__(self, db: Database):
        self.db = db

    def create_project(
        self,
        project_name: str,
        customer: str = "",
        tender_no: str = "",
        bid_date: str = "",
        owner: str = "",
        remark: str = "",
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not project_name.strip():
            raise ValidationError("招投标项目名称不能为空")
        with self.db.tx():
            doc_no = codegen.next_doc_no(self.db, codegen.PREFIX_BID, at)
            ts = now()
            pid = self.db.insert("bid_projects", {
                "doc_no": doc_no,
                "project_name": project_name,
                "customer": customer,
                "tender_no": tender_no,
                "bid_date": bid_date or ts[:10],
                "status": models.BidStatus.DRAFT,
                "owner": owner,
                "remark": remark,
                "created_at": ts,
                "updated_at": ts,
            })
            self.db.log_action("bid_project", pid, "create",
                               after={"doc_no": doc_no}, operator=owner)
        return self.db.get_by_id("bid_projects", pid)

    def add_item(
        self,
        bid_project_id: int,
        product_name: str,
        spec: str = "",
        category: str = "",
        unit: str = "件",
        qty: float = 1.0,
        purchase_price: float = 0.0,
        quote_ratio: float = 1.0,
        is_won: str = "",
        image_path: str = "",
        drawing_path: str = "",
        legacy_product_id: Optional[int] = None,
        remark: str = "",
    ) -> Dict[str, Any]:
        if not self.db.get_by_id("bid_projects", bid_project_id):
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        if not product_name.strip():
            raise ValidationError("产品名称不能为空")
        if qty <= 0:
            raise ValidationError("询价数量必须大于 0")
        if quote_ratio <= 0:
            raise ValidationError("报价比率必须大于 0")
        with self.db.tx():
            line_no = int(self.db.scalar(
                "SELECT COALESCE(MAX(line_no), 0) + 1 FROM bid_items WHERE bid_project_id = ?",
                (bid_project_id,), default=1,
            ))
            ts = now()
            iid = self.db.insert("bid_items", {
                "bid_project_id": bid_project_id,
                "line_no": line_no,
                "product_name": product_name,
                "spec": spec,
                "category": category,
                "unit": unit,
                "qty": qty,
                "purchase_price": purchase_price,
                "quote_ratio": quote_ratio,
                "is_won": is_won,
                "image_path": image_path,
                "drawing_path": drawing_path,
                "legacy_product_id": legacy_product_id,
                "remark": remark,
                "created_at": ts,
                "updated_at": ts,
            })
        return self.db.get_by_id("bid_items", iid)

    def list_items(self, bid_project_id: int) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM bid_items WHERE bid_project_id = ? ORDER BY line_no",
            (bid_project_id,),
        )
        for r in rows:
            r["quote_price"] = self.quote_price(r)
        return rows

    @staticmethod
    def quote_price(item: Dict[str, Any]) -> float:
        return (float(item.get("purchase_price") or 0.0)
                / (float(item.get("quote_ratio") or 1.0) or 1.0))

    def summary(self, bid_project_id: int) -> Dict[str, float]:
        items = self.list_items(bid_project_id)
        total_purchase = sum(float(i["purchase_price"]) * float(i["qty"]) for i in items)
        total_quote = sum(i["quote_price"] * float(i["qty"]) for i in items)
        profit = total_quote - total_purchase
        return {
            "count": len(items),
            "total_purchase": round(total_purchase, 2),
            "total_quote": round(total_quote, 2),
            "profit": round(profit, 2),
            "profit_rate": round(profit / total_quote * 100, 2) if total_quote else 0.0,
        }

    def set_status(self, bid_project_id: int, new_status: str,
                   operator: str = "") -> Dict[str, Any]:
        p = self.db.get_by_id("bid_projects", bid_project_id)
        if not p:
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        models.assert_transition("bid_project", p["status"], new_status)
        with self.db.tx():
            self.db.update("bid_projects", {"status": new_status, "updated_at": now()},
                           "id = ?", (bid_project_id,))
            self.db.log_action("bid_project", bid_project_id, "status",
                               before={"status": p["status"]},
                               after={"status": new_status}, operator=operator)
        return self.db.get_by_id("bid_projects", bid_project_id)

    def mark_quoted(self, bid_project_id: int, operator: str = "") -> Dict[str, Any]:
        return self.set_status(bid_project_id, models.BidStatus.QUOTED, operator)

    def mark_won(self, bid_project_id: int, won: bool = True,
                 operator: str = "") -> Dict[str, Any]:
        """便捷入口：草稿→已报价→中标/未中标"""
        p = self.db.get_by_id("bid_projects", bid_project_id)
        if not p:
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        if p["status"] == models.BidStatus.DRAFT:
            self.mark_quoted(bid_project_id, operator)
        result = self.set_status(bid_project_id,
                                 models.BidStatus.WON if won else models.BidStatus.LOST,
                                 operator)
        if won:
            with self.db.tx():
                self.db.execute("UPDATE bid_items SET is_won = '是' WHERE bid_project_id = ?",
                                (bid_project_id,))
        return result

    def won_projects(self) -> List[Dict[str, Any]]:
        """供生产排产选择关联的中标项目"""
        return self.db.query(
            "SELECT * FROM bid_projects WHERE status = ? ORDER BY id",
            (models.BidStatus.WON,),
        )

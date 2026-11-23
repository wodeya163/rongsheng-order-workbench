#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打通域：穿透查询（v2.2 双来源）

数量轨道（唯一）：生产项目 → production_links.allocated_qty → order_items.qty_ordered(实值)
溯源轨道 A：外部订单明细 → 询配码明细项(虚值) → 询配码项目   【一码多单】
溯源轨道 B：内部订单明细 → 中标明细项 → 中标项目             【一项目一张内部订单】
来源标签：   生产项目.source_bid_project_id → 中标项目

★ 输出里 qty_ordered / allocated_qty 是实值；qty_reference 是虚值，
   只作统计对照，绝不参与守恒判断。
"""
from typing import Any, Dict, List

from ..core import models
from ..core.db import Database
from ..core.errors import NotFoundError


class TraceService:
    def __init__(self, db: Database):
        self.db = db

    def from_production(self, project_id: int) -> List[Dict[str, Any]]:
        """点一个生产项目：看到订单实值 + 来源是哪个询配码项 / 哪个中标明细项"""
        if not self.db.get_by_id("production_projects", project_id):
            raise NotFoundError(f"生产项目 {project_id} 不存在")
        rows = self.db.query(
            """
            SELECT
                pp.id AS project_id, pp.doc_no AS project_no, pp.title AS project_title,
                pp.status AS project_status, pp.plan_start, pp.plan_end, pp.workshop,
                pl.id AS link_id, pl.allocated_qty,
                pl.order_no_snapshot, pl.order_line_snapshot,
                pl.order_type_snapshot, pl.source_type_snapshot,
                pl.item_name_snapshot, pl.spec_snapshot,
                pl.code_no_snapshot, pl.item_code_snapshot, pl.qty_reference_snapshot,
                pl.bid_no_snapshot, pl.bid_line_snapshot, pl.bid_item_name_snapshot,
                oi.id AS order_item_id, oi.line_no AS order_line_no,
                oi.item_name, oi.spec, oi.unit, oi.qty_ordered, oi.unit_price,
                o.id AS order_id, o.doc_no AS order_no, o.customer AS order_customer,
                o.status AS order_status, o.required_date,
                o.order_type, o.batch_seq, o.source_bid_project_id AS order_source_bid_id,
                ci.id AS code_item_id, ci.item_code, ci.qty_reference,
                COALESCE(cp.doc_no, cp_head.doc_no) AS code_no,
                COALESCE(cp.project_name, cp_head.project_name) AS code_project_name,
                bi.id AS bid_item_id, bi.line_no AS bid_line_no,
                bi.product_name AS bid_item_name, bi.qty AS bid_item_qty,
                bp.doc_no AS bid_project_no, bp.project_name AS bid_project_name,
                bp.status AS bid_status,
                bp_src.doc_no AS source_bid_project_no,
                bp_src.project_name AS source_bid_project_name
            FROM production_projects pp
            JOIN production_links pl ON pl.project_id = pp.id
            JOIN order_items oi ON oi.id = pl.order_item_id
            JOIN orders o ON o.id = oi.order_id
            LEFT JOIN code_items ci ON ci.id = oi.source_code_item_id
            LEFT JOIN code_projects cp ON cp.id = ci.code_project_id
            LEFT JOIN code_projects cp_head ON cp_head.id = o.source_code_project_id
            LEFT JOIN bid_items bi ON bi.id = oi.source_bid_item_id
            LEFT JOIN bid_projects bp ON bp.id = pp.source_bid_project_id
            LEFT JOIN bid_projects bp_src ON bp_src.id = o.source_bid_project_id
            WHERE pp.id = ?
            ORDER BY pl.id
            """,
            (project_id,),
        )
        for r in rows:
            r["source_origin"] = self._origin(r)
        return rows

    @staticmethod
    def _origin(r: Dict[str, Any]) -> str:
        """一行说清来源：询配码项目/项 · 中标项目/项 · 订单新增"""
        if r.get("order_type") == models.OrderType.INTERNAL:
            no = r.get("source_bid_project_no") or r.get("bid_no_snapshot") or "?"
            line = r.get("bid_line_snapshot") or (
                f"{no}-{int(r['bid_line_no']):02d}" if r.get("bid_line_no") else "—")
            return f"中标项目 {no} / {line}"
        code_no = r.get("code_no") or r.get("code_no_snapshot")
        item_code = r.get("item_code") or r.get("item_code_snapshot")
        if code_no and item_code:
            return f"询配码 {code_no} / {item_code}"
        if code_no:
            return f"询配码 {code_no}（订单新增物资）"
        return "订单新增物资（无询配码来源）"

    def from_order_item(self, order_item_id: int) -> List[Dict[str, Any]]:
        """订单明细被哪些生产项目领用、各领多少（实值）"""
        item = self.db.get_by_id("order_items", order_item_id)
        if not item:
            raise NotFoundError(f"订单明细 {order_item_id} 不存在")
        rows = self.db.query(
            """SELECT pp.id AS project_id, pp.doc_no AS project_no,
                      pp.title AS project_title, pp.status AS project_status,
                      pp.plan_start, pp.plan_end, pp.actual_end, pp.workshop,
                      pl.id AS link_id, pl.allocated_qty, pl.created_at AS linked_at,
                      pl.code_no_snapshot, pl.item_code_snapshot,
                      pl.bid_no_snapshot, pl.bid_line_snapshot,
                      pl.order_type_snapshot
               FROM production_links pl
               JOIN production_projects pp ON pp.id = pl.project_id
               WHERE pl.order_item_id = ? ORDER BY pl.created_at, pl.id""",
            (order_item_id,),
        )
        allocated = sum(float(r["allocated_qty"]) for r in rows)
        for r in rows:
            r["qty_ordered_real"] = item["qty_ordered"]
            r["allocated_total"] = allocated
            r["available_qty"] = round(float(item["qty_ordered"]) - allocated, 6)
        return rows

    def from_code_item(self, code_item_id: int) -> List[Dict[str, Any]]:
        """
        从询配码项正查：用户下了哪些订单（可多张）、各下了多少（实值）、排了多少产。
        返回里 qty_reference 是虚值，仅作对照展示。
        """
        ci = self.db.get_by_id("code_items", code_item_id)
        if not ci:
            raise NotFoundError(f"询配码项 {code_item_id} 不存在")
        rows = self.db.query(
            """SELECT oi.id AS order_item_id, oi.qty_ordered, oi.item_name,
                      o.id AS order_id, o.doc_no AS order_no, o.status AS order_status,
                      o.batch_seq, o.order_type, o.customer AS order_customer,
                      COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                WHERE pl.order_item_id = oi.id), 0) AS qty_scheduled,
                      (SELECT COUNT(*) FROM production_links pl
                       WHERE pl.order_item_id = oi.id) AS project_count
               FROM order_items oi JOIN orders o ON o.id = oi.order_id
               WHERE oi.source_code_item_id = ? ORDER BY oi.id""",
            (code_item_id,),
        )
        for r in rows:
            r["item_code"] = ci["item_code"]
            r["qty_reference_virtual"] = ci.get("qty_reference")
            r["note"] = "订单/排产数量为实值，与询配码虚值不对应；同一项可分批多次下单"
        return rows

    def from_code_project(self, code_project_id: int) -> List[Dict[str, Any]]:
        """★ 一码多单：一个询配码项目 → 多张订单 → 每行实值 → 已排量"""
        if not self.db.get_by_id("code_projects", code_project_id):
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        rows = self.db.query(
            """SELECT o.id AS order_id, o.doc_no AS order_no, o.batch_seq,
                      o.status AS order_status, o.order_date, o.required_date,
                      oi.id AS order_item_id, oi.line_no, oi.item_name, oi.spec,
                      oi.qty_ordered, oi.unit,
                      ci.item_code, ci.qty_reference AS qty_reference_virtual,
                      COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                WHERE pl.order_item_id = oi.id), 0) AS qty_scheduled
               FROM orders o JOIN order_items oi ON oi.order_id = o.id
               LEFT JOIN code_items ci ON ci.id = oi.source_code_item_id
               WHERE o.source_code_project_id = ? AND o.order_type = ?
               ORDER BY o.batch_seq, o.id, oi.line_no""",
            (code_project_id, models.OrderType.EXTERNAL),
        )
        for r in rows:
            r["source_origin"] = (
                f"询配码项 {r['item_code']}" if r["item_code"] else "订单新增物资")
        return rows

    def from_bid_project(self, bid_project_id: int) -> List[Dict[str, Any]]:
        """中标项目 → 内部订单 → 关联到了哪些生产排产项目、排了多少（实值）"""
        if not self.db.get_by_id("bid_projects", bid_project_id):
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        io = self.db.query_one(
            "SELECT * FROM orders WHERE source_bid_project_id = ? AND order_type = ?",
            (bid_project_id, models.OrderType.INTERNAL),
        )
        rows = self.db.query(
            """SELECT pp.id AS project_id, pp.doc_no AS project_no, pp.title AS project_title,
                      pp.status AS project_status, pp.plan_start, pp.plan_end,
                      COALESCE(SUM(pl.allocated_qty), 0) AS total_allocated,
                      COUNT(pl.id) AS link_count
               FROM production_projects pp
               LEFT JOIN production_links pl ON pl.project_id = pp.id
               WHERE pp.source_bid_project_id = ? GROUP BY pp.id ORDER BY pp.id""",
            (bid_project_id,),
        )
        for r in rows:
            r["internal_order_no"] = io["doc_no"] if io else ""
            r["internal_order_status"] = io["status"] if io else ""
            r["internal_order_qty_real"] = (
                self.db.scalar("SELECT COALESCE(SUM(qty_ordered), 0) FROM order_items "
                               "WHERE order_id = ?", (io["id"],)) if io else 0.0)
            r["internal_scheduled_qty"] = (
                self.db.scalar(
                    "SELECT COALESCE(SUM(pl.allocated_qty), 0) FROM production_links pl "
                    "JOIN order_items oi ON oi.id = pl.order_item_id WHERE oi.order_id = ?",
                    (io["id"],)) if io else 0.0)
        return rows

    def bid_internal_order(self, bid_project_id: int) -> List[Dict[str, Any]]:
        """中标项目 → 内部订单明细 → 每一行实值 / 已排量 / 来源中标明细行"""
        if not self.db.get_by_id("bid_projects", bid_project_id):
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        return self.db.query(
            """SELECT bp.doc_no AS bid_project_no, bp.status AS bid_status,
                      o.id AS order_id, o.doc_no AS order_no, o.status AS order_status,
                      oi.id AS order_item_id, oi.line_no, oi.item_name, oi.spec,
                      oi.qty_ordered, oi.unit, oi.unit_price,
                      bi.id AS bid_item_id, bi.line_no AS bid_line_no,
                      bi.qty AS bid_item_qty,
                      COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                WHERE pl.order_item_id = oi.id), 0) AS qty_scheduled,
                      (SELECT COUNT(*) FROM production_links pl
                       WHERE pl.order_item_id = oi.id) AS project_count
               FROM orders o JOIN order_items oi ON oi.order_id = o.id
               LEFT JOIN bid_items bi ON bi.id = oi.source_bid_item_id
               JOIN bid_projects bp ON bp.id = o.source_bid_project_id
               WHERE o.source_bid_project_id = ? AND o.order_type = ?
               ORDER BY oi.line_no""",
            (bid_project_id, models.OrderType.INTERNAL),
        )

    def full_chain(self, project_id: int) -> Dict[str, Any]:
        """一个生产项目的完整来源清单：按订单 / 按询配码项目 / 按来源类型分组"""
        rows = self.from_production(project_id)
        by_order: Dict[str, Dict[str, Any]] = {}
        by_code: Dict[str, Dict[str, Any]] = {}
        by_source: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            ok = r["order_no"] or "(未知订单)"
            o = by_order.setdefault(ok, {"order_no": ok, "order_type": r["order_type"],
                                         "batch_seq": r["batch_seq"],
                                         "item_count": 0, "allocated_qty": 0.0, "items": []})
            o["item_count"] += 1
            o["allocated_qty"] += float(r["allocated_qty"])
            o["items"].append({
                "order_line": r["order_line_no"],
                "item_name": r["item_name"],
                "qty_ordered_real": r["qty_ordered"],
                "allocated_qty": r["allocated_qty"],
                "code_no": r["code_no"] or r["code_no_snapshot"],
                "item_code": r["item_code"] or r["item_code_snapshot"],
                "bid_no": r["source_bid_project_no"] or r["bid_no_snapshot"],
                "bid_line": r["bid_line_snapshot"],
                "qty_reference_virtual": r["qty_reference"],
                "source_origin": r["source_origin"],
            })
            ck = r["code_no"] or r["code_no_snapshot"] or "(未关联询配码)"
            c = by_code.setdefault(ck, {"code_no": ck, "item_count": 0,
                                        "allocated_qty": 0.0})
            c["item_count"] += 1
            c["allocated_qty"] += float(r["allocated_qty"])
            sk = r["source_type_snapshot"] or "订单新增"
            s = by_source.setdefault(sk, {"source_type": sk, "item_count": 0,
                                          "allocated_qty": 0.0})
            s["item_count"] += 1
            s["allocated_qty"] += float(r["allocated_qty"])
        project = self.db.get_by_id("production_projects", project_id)
        return {
            "project": project,
            "bid_project_no": rows[0]["bid_project_no"] if rows else (project or {}).get("source_bid_project_id"),
            "link_count": len(rows),
            "total_allocated_real": round(sum(float(r["allocated_qty"]) for r in rows), 6),
            "orders": list(by_order.values()),
            "code_projects": list(by_code.values()),
            "source_types": list(by_source.values()),
            "rows": rows,
            "note": "数量以订单 qty_ordered 为准；询配码 qty_reference 为虚值，仅供对照；"
                    "中标项目经内部订单产生数量，不直接挂数量",
        }

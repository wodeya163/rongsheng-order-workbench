#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 4：生产排产 + 打通核心（v2.2）

两条链路汇入同一个订单枢纽，共用同一套实值守恒：
  A. 询配码项目 → 多张外部订单(实值) → 排产；溯源到询配码项目的哪一项
  B. 中标项目 → 一张内部订单(实值) → 排产；溯源到中标项目的哪一项
  来源标签：排产项目另可挂中标项目（不携带数量）

★ 中标项目【不直接产生排产数量】，必须经内部订单；
  数量守恒唯一锚点始终是 order_items.qty_ordered。
"""
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core import codegen, models
from ..core.db import Database, now
from ..core.errors import (DuplicateError, NotFoundError, OverAllocationError,
                           StateError, ValidationError)


class ProductionService:
    def __init__(self, db: Database):
        self.db = db
        from .order import OrderService
        self.orders = OrderService(db)

    # ── 中标项目关联（只有中标才能挂） ───────────────────────

    def _assert_won(self, bid_project_id: int) -> Dict[str, Any]:
        p = self.db.get_by_id("bid_projects", bid_project_id)
        if not p:
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        if p["status"] != models.BidStatus.WON:
            raise StateError(
                f"招投标项目 {p['doc_no']} 状态为 {p['status']}；只有「中标」项目可关联生产排产"
            )
        return p

    def link_bid_project(self, project_id: int, bid_project_id: int,
                         operator: str = "") -> Dict[str, Any]:
        """把中标项目作为来源标签挂到生产排产项目（不携带数量）"""
        if not self.db.get_by_id("production_projects", project_id):
            raise NotFoundError(f"生产项目 {project_id} 不存在")
        bp = self._assert_won(bid_project_id)
        with self.db.tx():
            self.db.update("production_projects",
                           {"source_bid_project_id": bid_project_id, "updated_at": now()},
                           "id = ?", (project_id,))
            self.db.log_action("production_project", project_id, "link_won_bid",
                               after={"bid_project_no": bp["doc_no"]}, operator=operator)
        return self.db.get_by_id("production_projects", project_id)

    def create_project(
        self,
        title: str,
        source_bid_project_id: Optional[int] = None,
        plan_start: str = "",
        plan_end: str = "",
        workshop: str = "",
        owner: str = "",
        priority: int = 3,
        remark: str = "",
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not title.strip():
            raise ValidationError("生产项目名称不能为空")
        if source_bid_project_id is not None:
            self._assert_won(source_bid_project_id)
        with self.db.tx():
            doc_no = codegen.next_doc_no(self.db, codegen.PREFIX_PROJECT, at)
            ts = now()
            pid = self.db.insert("production_projects", {
                "doc_no": doc_no,
                "title": title,
                "source_bid_project_id": source_bid_project_id,
                "status": models.ProjectStatus.PENDING,
                "plan_start": plan_start,
                "plan_end": plan_end,
                "workshop": workshop,
                "owner": owner,
                "priority": priority,
                "remark": remark,
                "created_at": ts,
                "updated_at": ts,
            })
            self.db.log_action("production_project", pid, "create",
                               after={"doc_no": doc_no}, operator=owner)
        return self.db.get_by_id("production_projects", pid)

    # ── 排产关联：挂订单明细（不是询配码明细） ───────────────

    def _snapshots(self, item: Dict[str, Any], order: Dict[str, Any]) -> Dict[str, Any]:
        """快照：订单实值信息 + 溯源信息（按订单类型分流；虚值仅记录，不参与校验）"""
        order_no = order["doc_no"]
        result = {
            "order_no_snapshot": order_no,
            "order_line_snapshot": codegen.order_line_no(order_no, item["line_no"]),
            "order_type_snapshot": order.get("order_type") or models.OrderType.EXTERNAL,
            "source_type_snapshot": "订单新增",
            "item_name_snapshot": item["item_name"],
            "spec_snapshot": item.get("spec") or "",
            "code_no_snapshot": "",
            "item_code_snapshot": "",
            "code_item_name_snapshot": "",
            "qty_reference_snapshot": None,
            "bid_no_snapshot": "",
            "bid_line_snapshot": "",
            "bid_item_name_snapshot": "",
        }
        # A 外部订单：询配码项目 / 哪一项（虚值仅展示）
        code_item_id = item.get("source_code_item_id")
        if code_item_id:
            ci = self.db.get_by_id("code_items", code_item_id)
            if ci:
                cp = self.db.get_by_id("code_projects", ci["code_project_id"])
                result.update({
                    "source_type_snapshot": "询配码项目",
                    "code_no_snapshot": cp["doc_no"] if cp else "",
                    "item_code_snapshot": ci["item_code"],
                    "code_item_name_snapshot": ci["item_name"],
                    "qty_reference_snapshot": ci.get("qty_reference"),  # 虚值，仅展示
                })
        if not result["code_no_snapshot"] and order.get("source_code_project_id"):
            cp = self.db.get_by_id("code_projects", order["source_code_project_id"])
            if cp:
                result["code_no_snapshot"] = cp["doc_no"]
        # B 内部订单：中标项目 / 哪一项（实值来自中标明细数量，经订单带过来）
        bid_item_id = item.get("source_bid_item_id")
        if bid_item_id:
            bi = self.db.get_by_id("bid_items", bid_item_id)
            if bi:
                bp = self.db.get_by_id("bid_projects", bi["bid_project_id"])
                result.update({
                    "source_type_snapshot": "中标项目",
                    "bid_no_snapshot": bp["doc_no"] if bp else "",
                    "bid_line_snapshot": codegen.bid_line_no(
                        bp["doc_no"] if bp else "", bi["line_no"]),
                    "bid_item_name_snapshot": bi["product_name"],
                })
        if not result["bid_no_snapshot"] and order.get("source_bid_project_id"):
            bp = self.db.get_by_id("bid_projects", order["source_bid_project_id"])
            if bp:
                result["bid_no_snapshot"] = bp["doc_no"]
        return result

    def create_project_from_won_bid(
        self,
        bid_project_id: int,
        title: str = "",
        plan_start: str = "",
        plan_end: str = "",
        workshop: str = "",
        owner: str = "",
        priority: int = 1,
        operator: str = "",
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        ★ 一键把中标项目投入排产：中标项目 → (自动生成)一张内部订单 → 排产项目，
          内部订单全部明细按【剩余可排实值】挂到 production_links。

        这是「中标项目也要生成排产数量」的落地入口；
        数量守恒仍由 link_order_item 统一校验，绝不新开第二条数量链路。
        """
        bp = self._assert_won(bid_project_id)
        order = self.orders.internal_order_of(bid_project_id)
        created_order = False
        if order is None:
            order = self.orders.create_internal_from_bid(
                bid_project_id, owner=operator or owner, at=at)
            created_order = True
        elif order["status"] == models.OrderStatus.DRAFT:
            order = self.orders.confirm(order["id"], operator)

        project = self.create_project(
            title=title or f"{bp['doc_no']} 中标项目排产",
            source_bid_project_id=bid_project_id,
            plan_start=plan_start, plan_end=plan_end, workshop=workshop,
            owner=owner, priority=priority, at=at,
            remark=f"由中标项目 {bp['doc_no']} 的内部订单 {order['doc_no']} 驱动",
        )
        links = []
        for item in self.orders.list_items(order["id"]):
            available = self.orders.available_qty(item["id"])
            if available > 0:
                links.append(self.link_order_item(
                    project["id"], item["id"], available, operator=operator))
        self.db.log_action("production_project", project["id"], "from_won_bid",
                           after={"bid_project_no": bp["doc_no"],
                                  "internal_order_no": order["doc_no"],
                                  "created_order": created_order,
                                  "link_count": len(links)}, operator=operator)
        return {
            "project": self.db.get_by_id("production_projects", project["id"]),
            "order": order,
            "created_order": created_order,
            "links": links,
            "total_allocated_real": round(
                sum(float(l["allocated_qty"]) for l in links), 6),
        }

    def link_order_item(
        self,
        project_id: int,
        order_item_id: int,
        allocated_qty: Optional[float] = None,
        operator: str = "",
        strict: bool = True,
    ) -> Dict[str, Any]:
        """
        【打通核心】把订单明细（实值物资+数量）关联到生产排产项目。

        数量守恒：Σallocated_qty ≤ order_items.qty_ordered
        询配码明细的虚值数量【完全不参与】此校验。
        """
        project = self.db.get_by_id("production_projects", project_id)
        if not project:
            raise NotFoundError(f"生产项目 {project_id} 不存在")
        item = self.db.get_by_id("order_items", order_item_id)
        if not item:
            raise NotFoundError(f"订单明细 {order_item_id} 不存在")
        order = self.db.get_by_id("orders", item["order_id"])
        if not order:
            raise NotFoundError(f"订单 {item['order_id']} 不存在")

        # 2026-11-06 v2.3：并入真实销售订单表的 待发货/发货中/已验收 状态
        allowed_head = (models.OrderStatus.CONFIRMED, models.OrderStatus.WAIT_SHIP,
                        models.OrderStatus.SCHEDULED, models.OrderStatus.RUNNING,
                        models.OrderStatus.SHIPPING, models.OrderStatus.DELIVERED,
                        models.OrderStatus.ACCEPTED)
        if strict and order["status"] not in allowed_head:
            raise StateError(f"订单 {order['doc_no']} 状态为 {order['status']}，需先「确认」才可排产")

        available = self.orders.available_qty(order_item_id)
        qty = available if allocated_qty is None else float(allocated_qty)
        if qty <= 0:
            raise ValidationError("分配数量必须大于 0")
        if qty > available + 1e-9:
            raise OverAllocationError(
                codegen.order_line_no(order["doc_no"], item["line_no"]), qty, available
            )

        with self.db.tx():
            if self.orders.available_qty(order_item_id) + 1e-9 < qty:
                raise OverAllocationError(
                    codegen.order_line_no(order["doc_no"], item["line_no"]),
                    qty, self.orders.available_qty(order_item_id),
                )
            snap = self._snapshots(item, order)
            snap.update({
                "project_id": project_id,
                "order_item_id": order_item_id,
                "allocated_qty": qty,
                "created_by": operator,
                "created_at": now(),
            })
            try:
                lid = self.db.insert("production_links", snap)
            except sqlite3.IntegrityError as exc:
                raise DuplicateError(
                    f"生产项目 {project['doc_no']} 已关联订单明细 "
                    f"{snap['order_line_snapshot']}：{exc}"
                ) from exc

            ts = now()
            if order["status"] == models.OrderStatus.CONFIRMED:
                self.db.update("orders", {"status": models.OrderStatus.SCHEDULED,
                                          "updated_at": ts}, "id = ?", (order["id"],))
            if project["status"] == models.ProjectStatus.PENDING:
                self.db.update("production_projects",
                               {"status": models.ProjectStatus.SCHEDULED, "updated_at": ts},
                               "id = ?", (project_id,))
            self.db.log_action("production_link", lid, "link_order_item",
                               after={"project_no": project["doc_no"],
                                      "order_line": snap["order_line_snapshot"],
                                      "qty": qty,
                                      "code_item": snap["item_code_snapshot"]},
                               operator=operator)
        return self.db.get_by_id("production_links", lid)

    def update_allocation(self, link_id: int, new_qty: float,
                          operator: str = "") -> Dict[str, Any]:
        link = self.db.get_by_id("production_links", link_id)
        if not link:
            raise NotFoundError(f"关联 {link_id} 不存在")
        new_qty = float(new_qty)
        if new_qty <= 0:
            raise ValidationError("分配数量必须大于 0")
        with self.db.tx():
            available = (self.orders.available_qty(link["order_item_id"])
                         + float(link["allocated_qty"]))
            if new_qty > available + 1e-9:
                item = self.db.get_by_id("order_items", link["order_item_id"])
                raise OverAllocationError(f"订单明细 {item['line_no']}", new_qty, available)
            self.db.update("production_links", {"allocated_qty": new_qty},
                           "id = ?", (link_id,))
            self.db.log_action("production_link", link_id, "update_allocation",
                               before={"qty": link["allocated_qty"]},
                               after={"qty": new_qty}, operator=operator)
        return self.db.get_by_id("production_links", link_id)

    def unlink(self, link_id: int, operator: str = "") -> None:
        link = self.db.get_by_id("production_links", link_id)
        if not link:
            raise NotFoundError(f"关联 {link_id} 不存在")
        with self.db.tx():
            self.db.execute("DELETE FROM production_links WHERE id = ?", (link_id,))
            self.db.log_action("production_link", link_id, "unlink",
                               before=link, operator=operator)

    def list_links(self, project_id: int) -> List[Dict[str, Any]]:
        return self.db.query(
            """SELECT pl.*, oi.item_name, oi.qty_ordered, o.doc_no AS order_no
               FROM production_links pl
               JOIN order_items oi ON oi.id = pl.order_item_id
               JOIN orders o ON o.id = oi.order_id
               WHERE pl.project_id = ? ORDER BY pl.id""",
            (project_id,),
        )

    def add_task(
        self, project_id: int, process_name: str, workcenter: str = "",
        qty: float = 0.0, plan_start: str = "", plan_end: str = "", owner: str = "",
    ) -> Dict[str, Any]:
        project = self.db.get_by_id("production_projects", project_id)
        if not project:
            raise NotFoundError(f"生产项目 {project_id} 不存在")
        with self.db.tx():
            seq = int(self.db.scalar(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM production_tasks WHERE project_id = ?",
                (project_id,), default=1,
            ))
            ts = now()
            tid = self.db.insert("production_tasks", {
                "project_id": project_id,
                "seq": seq,
                "doc_no": codegen.task_code_of(project["doc_no"], seq),
                "process_name": process_name,
                "workcenter": workcenter,
                "qty": qty,
                "plan_start": plan_start,
                "plan_end": plan_end,
                "owner": owner,
                "status": "待开工",
                "created_at": ts,
                "updated_at": ts,
            })
        return self.db.get_by_id("production_tasks", tid)

    def set_status(self, project_id: int, new_status: str,
                   operator: str = "") -> Dict[str, Any]:
        p = self.db.get_by_id("production_projects", project_id)
        if not p:
            raise NotFoundError(f"生产项目 {project_id} 不存在")
        models.assert_transition("production_project", p["status"], new_status)
        with self.db.tx():
            self.db.update("production_projects", {"status": new_status, "updated_at": now()},
                           "id = ?", (project_id,))
            self.db.log_action("production_project", project_id, "status",
                               before={"status": p["status"]},
                               after={"status": new_status}, operator=operator)
        return self.db.get_by_id("production_projects", project_id)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 3：订单枢纽（v2.2）

两种订单，共用同一套 qty_ordered 实值与排产守恒规则：
  外部订单(DD-)：用户按询配码文件下单；★ 一个询配码项目可产生【多张】，
                 以 batch_seq 标记第几单，分批下单互不约束
  内部订单(ND-)：★ 中标项目生成【一张】，明细实值取 bid_items.qty，
                 中标项目由此产生排产数量，但不直接产生排产数量

★ 询配码明细 qty_reference 是【虚值】，本模块绝不读取它来生成订单数量。
★ 只有 order_items.qty_ordered(实值) 进入排产数量守恒。
★ 2026-11-06 v2.3 订单 8+9 字段：新增的 价税合计 / 物资数量 / 总价 都是
  【金额与展示字段】，绝不参与数量守恒，也不改变 qty_ordered 实值锚点。
  明细总价留空自动 = 数量 × 单价；订单头价税合计留空 = Σ 明细总价；
★ 2026-11-07 Q12 已确认：「物资数量」= 物资项目数（明细行数），
  留空自动 = len(items)，**不是** Σ 明细数量；Σ 明细数量请看结果表「实值合计」列。
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..core import codegen, models, money
from ..core.db import Database, now
from ..core.errors import (DuplicateError, NotFoundError, StateError,
                           ValidationError)
from ..core.order_fields import ORDER_SOURCES


class OrderService:
    def __init__(self, db: Database):
        self.db = db

    # ── v2.3 金额/数量派生的小工具 ──────────────────────────

    @staticmethod
    def _number_or_zero(value: Any) -> float:
        """空 / 非数字 → 0；不抛异常，派生字段不该阻断建单"""
        if value in (None, ""):
            return 0.0
        return float(money.to_dec(value))

    @staticmethod
    def _optional_amount(value: Any) -> Optional[float]:
        """明细总价：留空 → None（随后按 数量×单价 计算）；非空一律量化到分"""
        if value in (None, ""):
            return None
        return money.to_db(value)

    def _sync_totals(self, order_id: int, amount_total: Any = None,
                     item_qty_total: Any = None) -> None:
        """
        订单头「价税合计 / 物资数量」：显式声明值优先，留空则自动派生。
          · auto_amount = Σ 明细总价（Decimal 累加，量化到分）
          · auto_qty    = 明细行数（物资项目数，Q12 2026-11-07 已确认）

        ★ P1-5：显式声明过的字段会被打上 *_manual = 1，
          之后增删明细只重算未锁字段，不再把手工值冲掉；
          传空字符串仍可显式「清空 → 回到自动」，reset_totals() 是批量入口。
        ★ 只写金额与展示列，绝不触碰 qty_ordered / production_links。
        """
        o = self.db.get_by_id("orders", order_id) or {}
        items = self.list_items(order_id)
        auto_amount = money.sum2(
            i["amount"] if i.get("amount") is not None
            else money.mul(i["qty_ordered"], i["unit_price"] or 0)
            for i in items)
        # Q12：物资数量是「物资项目数」= 明细行数，不是 Σ qty_ordered
        auto_qty = float(len(items))

        data: Dict[str, Any] = {"updated_at": now()}
        if amount_total not in (None, ""):
            data["amount_total"] = money.to_db(amount_total)
            data["amount_manual"] = 1
        elif not int(o.get("amount_manual") or 0):
            data["amount_total"] = float(auto_amount)
        if item_qty_total not in (None, ""):
            data["item_qty_total"] = float(money.to_dec(item_qty_total))
            data["item_qty_manual"] = 1
        elif not int(o.get("item_qty_manual") or 0):
            data["item_qty_total"] = auto_qty
        self.db.update("orders", data, "id = ?", (order_id,))

    def reset_totals(self, order_id: int, amount: bool = True,
                     qty: bool = True, operator: str = "") -> Dict[str, Any]:
        """解除手工锁定并立即按明细重算（界面「恢复自动汇总」）"""
        if not self.db.get_by_id("orders", order_id):
            raise NotFoundError(f"订单 {order_id} 不存在")
        patch: Dict[str, Any] = {}
        if amount:
            patch["amount_manual"] = 0
        if qty:
            patch["item_qty_manual"] = 0
        with self.db.tx():
            if patch:
                patch["updated_at"] = now()
                self.db.update("orders", patch, "id = ?", (order_id,))
            self._sync_totals(order_id)
            after = self.db.get_by_id("orders", order_id)
            self.db.log_action("order", order_id, "reset_totals",
                               after={"amount_total": after["amount_total"],
                                      "item_qty_total": after["item_qty_total"],
                                      "amount": amount, "qty": qty},
                               operator=operator)
        return after

    # ── 建单头 ──────────────────────────────────────────────

    def create_order(
        self,
        customer: str = "",
        source_code_project_id: Optional[int] = None,
        source_bid_project_id: Optional[int] = None,
        order_type: str = models.OrderType.EXTERNAL,
        order_date: str = "",
        required_date: str = "",
        owner: str = "",
        remark: str = "",
        at: Optional[datetime] = None,
        # ── v2.3 订单头 8 字段中的新增列 ──────────────────
        project_no: str = "",
        erp_order_no: str = "",
        status: str = "",
        amount_total: Any = None,
        item_qty_total: Any = None,
        order_source: str = "",
    ) -> Dict[str, Any]:
        if order_type not in (models.OrderType.EXTERNAL, models.OrderType.INTERNAL):
            raise ValidationError(f"未知订单类型：{order_type}")
        status = status or models.OrderStatus.DRAFT
        if not models.is_order_status(status):
            raise ValidationError(
                f"未知订单状态「{status}」；可用：{'、'.join(models.OrderStatus.CREATABLE)}")

        prefix = (codegen.PREFIX_INTERNAL_ORDER if order_type == models.OrderType.INTERNAL
                  else codegen.PREFIX_ORDER)
        batch_seq = 1

        if order_type == models.OrderType.INTERNAL:
            if source_code_project_id is not None:
                raise ValidationError("内部订单不得来源于询配码项目")
            if source_bid_project_id is None:
                raise ValidationError("内部订单必须指定来源中标项目")
            bp = self.db.get_by_id("bid_projects", source_bid_project_id)
            if not bp:
                raise NotFoundError(f"招投标项目 {source_bid_project_id} 不存在")
            if bp["status"] != models.BidStatus.WON:
                raise StateError(
                    f"招投标项目 {bp['doc_no']} 状态为 {bp['status']}；只有「中标」项目可生成内部订单"
                )
            if self.internal_order_of(source_bid_project_id):
                raise DuplicateError(
                    f"中标项目 {bp['doc_no']} 已生成内部订单；一个中标项目只能生成一张"
                )
        else:
            if source_bid_project_id is not None:
                raise ValidationError("外部订单不得来源于中标项目")
            if source_code_project_id is not None:
                cp = self.db.get_by_id("code_projects", source_code_project_id)
                if not cp:
                    raise NotFoundError(f"来源询配码项目 {source_code_project_id} 不存在")
                allowed = (models.CodeProjectStatus.SUBMITTED,
                           models.CodeProjectStatus.PARTIAL_ORDERED,
                           models.CodeProjectStatus.ORDERED)
                if cp["status"] not in allowed:
                    raise StateError(
                        f"询配码项目 {cp['doc_no']} 状态为 {cp['status']}，需先提交给用户才可下单"
                    )
                # ★ 一码多单：第几单 = 该项目已有外部订单数 + 1
                batch_seq = int(self.db.scalar(
                    "SELECT COUNT(*) FROM orders WHERE source_code_project_id = ? "
                    "AND order_type = ?",
                    (source_code_project_id, models.OrderType.EXTERNAL), default=0,
                )) + 1

        with self.db.tx():
            doc_no = codegen.next_doc_no(self.db, prefix, at)
            ts = now()
            oid = self.db.insert("orders", {
                "doc_no": doc_no,
                "order_type": order_type,
                "source_code_project_id": source_code_project_id,
                "source_bid_project_id": source_bid_project_id,
                "batch_seq": batch_seq,
                # v2.3 订单头 8 字段
                "project_no": project_no,
                "customer": customer,
                # v2.9：订单来源（销售渠道）；自由文本，预设值见 ofdefs.ORDER_SOURCES
                "order_source": (order_source or "").strip() or None,
                "status": status,
                "amount_total": self._number_or_zero(amount_total),
                "required_date": required_date,
                "item_qty_total": self._number_or_zero(item_qty_total),
                "erp_order_no": erp_order_no,
                # P1-5：建单时显式给出 → 打手工标记，后续增删明细不覆盖
                "amount_manual": 0 if amount_total in (None, "") else 1,
                "item_qty_manual": 0 if item_qty_total in (None, "") else 1,
                # 系统溯源 / 兼容字段
                "order_date": order_date or ts[:10],
                "owner": owner,
                "remark": remark,
                "created_at": ts,
                "updated_at": ts,
            })
            self.db.log_action("order", oid, "create",
                               after={"doc_no": doc_no, "order_type": order_type,
                                      "batch_seq": batch_seq,
                                      "source_code_project_id": source_code_project_id,
                                      "source_bid_project_id": source_bid_project_id},
                               operator=owner)
        return self.db.get_by_id("orders", oid)

    # ── 建明细 ──────────────────────────────────────────────

    def add_item(
        self,
        order_id: int,
        item_name: str,
        qty_ordered: float,
        spec: str = "",
        material: str = "",
        unit: str = "件",
        unit_price: float = 0.0,
        source_code_item_id: Optional[int] = None,
        source_bid_item_id: Optional[int] = None,
        remark: str = "",
        # ── v2.3 订单明细 9 字段中的新增列 ────────────────
        item_code: str = "",
        production_name: str = "",
        amount: Any = None,
        param_desc: str = "",
    ) -> Dict[str, Any]:
        """订单明细：数量必须显式传入（实值），不自动取询配码虚值"""
        if not self.db.get_by_id("orders", order_id):
            raise NotFoundError(f"订单 {order_id} 不存在")
        if not item_name.strip():
            raise ValidationError("订单物资名称不能为空")
        if qty_ordered is None or float(qty_ordered) <= 0:
            raise ValidationError("订单数量必须显式填写且大于 0（不得取自询配码虚值）")
        if source_code_item_id is not None and source_bid_item_id is not None:
            raise ValidationError("一条订单明细不能同时来源于询配码项和中标明细项")
        src = None
        if source_code_item_id is not None:
            src = self.db.get_by_id("code_items", source_code_item_id)
            if not src:
                raise NotFoundError(f"来源询配码项 {source_code_item_id} 不存在")
        if source_bid_item_id is not None and not self.db.get_by_id(
                "bid_items", source_bid_item_id):
            raise NotFoundError(f"来源中标明细项 {source_bid_item_id} 不存在")

        # 来源询配码项只补全【描述类字段】；数量与金额一律不读虚值
        if src:
            item_code = item_code or str(src.get("material_code")
                                         or src.get("goods_code") or "")
            param_desc = param_desc or str(src.get("item_detail")
                                          or src.get("tech_requirement") or "")
        production_name = production_name or item_name
        line_amount = self._optional_amount(amount)
        if line_amount is None:
            line_amount = float(money.mul(qty_ordered, unit_price or 0))

        with self.db.tx():
            line_no = int(self.db.scalar(
                "SELECT COALESCE(MAX(line_no), 0) + 1 FROM order_items WHERE order_id = ?",
                (order_id,), default=1,
            ))
            ts = now()
            iid = self.db.insert("order_items", {
                "order_id": order_id,
                "line_no": line_no,
                # v2.3 明细 9 字段
                "item_code": item_code,
                "item_name": item_name,
                "production_name": production_name,
                "spec": spec,
                "unit": unit or "件",
                "qty_ordered": float(qty_ordered),
                "unit_price": money.to_db_price(unit_price or 0),
                "amount": line_amount,
                "param_desc": param_desc,
                # 系统溯源 / 兼容字段
                "material": material,
                "source_code_item_id": source_code_item_id,
                "source_bid_item_id": source_bid_item_id,
                "remark": remark,
                "created_at": ts,
                "updated_at": ts,
            })
            self._sync_totals(order_id)
        return self.db.get_by_id("order_items", iid)

    # ── v2.9.2：订单编辑（更改 + 删除）统一入口 ─────────────

    def apply_edits(
        self,
        order_id: int,
        head: Optional[Dict[str, Any]] = None,
        item_updates: Optional[List[Any]] = None,
        new_items: Optional[List[Dict[str, Any]]] = None,
        deleted_item_ids: Optional[List[int]] = None,
        operator: str = "",
    ) -> Dict[str, Any]:
        """
        「订单编辑」对话框的落库入口：一次提交 = 改头 + 改明细 + 增行 + 删行。

        约束（写之前先整体预检，避免删一半留下半成品）：
          · 已作废订单不可改；
          · 要删的明细若已被排产领用（production_links）→ 整单拒绝，先解链；
          · 明细数量改小不得低于已排量（Σ分配 ≤ 实值 守恒）；
          · 每行仍走 MaintenanceService，字段白名单 + 审计留痕不变。
        价税合计 / 物资数量传空串 = 解除手工锁、回到自动汇总（reset_totals）。
        """
        from .maintenance import MaintenanceService

        maint = MaintenanceService(self.db)
        order = self.db.get_by_id("orders", order_id)
        if not order:
            raise NotFoundError(f"订单 {order_id} 不存在")
        if order.get("status") == models.OrderStatus.VOID:
            raise StateError("已作废订单不可编辑；如需重开请新建订单")

        head = dict(head or {})
        updates = [(int(i), dict(c)) for i, c in (item_updates or [])]
        news = [dict(x) for x in (new_items or [])]
        deletes = [int(i) for i in (deleted_item_ids or [])]

        # ① 预检：删除行是否被排产领用
        for iid in deletes:
            refs = maint.references("order_item", iid)
            if refs:
                detail = "；".join(f"{r['label']} {r['count']} 条" for r in refs)
                raise ValidationError(
                    f"明细（ID {iid}）已被排产领用（{detail}），不能删除；"
                    f"请先在④生产排产解除关联，或改为把数量改到 0 以下以外的处理方式。")

        # ② 预检：改小数量不得低于已排量
        for iid, changes in updates:
            if "qty_ordered" not in changes:
                continue
            allocated = float(self.db.scalar(
                "SELECT COALESCE(SUM(allocated_qty), 0) FROM production_links "
                "WHERE order_item_id = ?", (iid,), default=0.0))
            qty = float(changes["qty_ordered"] or 0)
            if qty <= 0:
                raise ValidationError("明细数量必须 > 0（订单实值是排产唯一锚点）")
            if qty + 1e-9 < allocated:
                raise ValidationError(
                    f"明细数量不能改为 {qty:g}：已排产 {allocated:g}，"
                    f"会破坏「Σ排产分配 ≤ 订单实值」守恒。")

        # ③ 预检：新增行必填项
        for idx, it in enumerate(news, 1):
            if not str(it.get("item_name") or "").strip():
                raise ValidationError(f"新增第 {idx} 行：物资名称不能为空")
            if float(it.get("qty_ordered") or 0) <= 0:
                raise ValidationError(f"新增第 {idx} 行：数量必须 > 0")

        # ④ 落库：头字段（空串 = 解除手工锁，单独走 reset_totals）
        clear_amount = head.get("amount_total", None) == ""
        if clear_amount:
            head.pop("amount_total", None)
        clear_qty = head.get("item_qty_total", None) == ""
        if clear_qty:
            head.pop("item_qty_total", None)
        if head:
            maint.edit("order", order_id, head, operator=operator)

        for iid, changes in updates:
            if changes:
                maint.edit("order_item", iid, changes, operator=operator)

        for iid in deletes:
            maint.delete("order_item", iid, operator=operator)

        for it in news:
            self.add_item(
                order_id,
                item_name=str(it.get("item_name") or ""),
                qty_ordered=float(it.get("qty_ordered") or 0),
                spec=str(it.get("spec") or ""),
                material=str(it.get("material") or ""),
                unit=str(it.get("unit") or "件"),
                unit_price=float(it.get("unit_price") or 0),
                item_code=str(it.get("item_code") or ""),
                production_name=str(it.get("production_name") or ""),
                amount=it.get("amount"),
                param_desc=str(it.get("param_desc") or ""),
                remark=str(it.get("remark") or ""),
            )

        if clear_amount or clear_qty:
            self.reset_totals(order_id, amount=clear_amount, qty=clear_qty,
                              operator=operator)
        else:
            self._sync_totals(order_id)      # 只补未锁字段，手工值不会被打掉

        return {
            "order": self.db.get_by_id("orders", order_id),
            "head": len(head), "updated": len(updates),
            "added": len(news), "deleted": len(deletes),
            "cleared": [k for k, v in (("amount", clear_amount),
                                       ("item_qty", clear_qty)) if v],
        }

    # ── 外部订单：一码多单 ──────────────────────────────────

    def create_from_code_project(
        self,
        code_project_id: int,
        items: List[Dict[str, Any]],
        customer: str = "",
        required_date: str = "",
        owner: str = "",
        order_date: str = "",
        at: Optional[datetime] = None,
        head: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        用户按询配码文件下单（可重复调用 = 分批下单，每张订单独立计实值）。

        items 每项：
          必填 qty_ordered（实值，用户本次实际下单量）
          可选 source_code_item_id（来自哪个询配码项）、item_name、spec、unit…
          以及 v2.3 的 item_code / production_name / amount / param_desc
        只借 source_code_item_id 补全【物资描述】；
        ★ 绝不读取 code_items.qty_reference，虚值与订单数量不对应。
        ★ 同一询配码项可在多张订单中重复下单，虚值不做任何扣减。
        head 可选：v2.3 订单头字段（project_no / status / amount_total …）。
        """
        cp = self.db.get_by_id("code_projects", code_project_id)
        if not cp:
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        if not items:
            raise ValidationError("订单至少需要一个明细项")

        head = head or {}
        with self.db.tx():
            order = self.create_order(
                customer=customer or cp.get("customer") or "",
                source_code_project_id=code_project_id,
                order_type=models.OrderType.EXTERNAL,
                required_date=required_date, owner=owner, at=at,
                order_date=order_date,
                project_no=head.get("project_no", ""),
                erp_order_no=head.get("erp_order_no", ""),
                status=head.get("status") or "",
                amount_total=head.get("amount_total"),
                item_qty_total=head.get("item_qty_total"),
                order_source=head.get("order_source") or "",
            )
            for spec_item in items:
                if spec_item.get("qty_ordered") is None:
                    raise ValidationError(
                        "订单数量必须显式填写；询配码明细数量是虚值，不能自动带入订单"
                    )
                src = None
                if spec_item.get("source_code_item_id") is not None:
                    src = self.db.get_by_id("code_items", spec_item["source_code_item_id"])
                    if not src or src["code_project_id"] != code_project_id:
                        raise ValidationError("来源询配码项不属于该询配码项目")
                self.add_item(
                    order["id"],
                    item_name=spec_item.get("item_name") or (src["item_name"] if src else ""),
                    qty_ordered=float(spec_item["qty_ordered"]),
                    spec=spec_item.get("spec") or (src.get("spec") if src else "") or "",
                    material=spec_item.get("material") or (src.get("material") if src else "") or "",
                    unit=spec_item.get("unit") or (src.get("unit") if src else "件") or "件",
                    unit_price=float(spec_item.get("unit_price") or 0),
                    source_code_item_id=spec_item.get("source_code_item_id"),
                    remark=spec_item.get("remark") or "",
                    # v2.3 明细 9 字段
                    item_code=spec_item.get("item_code") or "",
                    production_name=spec_item.get("production_name") or "",
                    amount=spec_item.get("amount"),
                    param_desc=spec_item.get("param_desc") or "",
                )
            self._sync_totals(order["id"], head.get("amount_total"),
                              head.get("item_qty_total"))
            from .partcode import PartCodeService
            PartCodeService(self.db).refresh_status(code_project_id)
            self.db.log_action("order", order["id"], "from_code_project",
                               after={"code_project_id": code_project_id,
                                      "batch_seq": order["batch_seq"],
                                      "item_count": len(items)}, operator=owner)
        return self.db.get_by_id("orders", order["id"])

    def order_batches(self, code_project_id: int) -> List[Dict[str, Any]]:
        """★ 一码多单：一个询配码项目下所有外部订单及其各自实值汇总"""
        if not self.db.get_by_id("code_projects", code_project_id):
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        return self.db.query(
            """SELECT o.id, o.doc_no, o.project_no, o.erp_order_no,
                      o.amount_total, o.item_qty_total,
                      o.batch_seq, o.status, o.order_date,
                      o.required_date, o.customer,
                      COUNT(oi.id) AS item_count,
                      COALESCE(SUM(oi.qty_ordered), 0) AS qty_ordered_real,
                      COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                JOIN order_items oi2 ON oi2.id = pl.order_item_id
                                WHERE oi2.order_id = o.id), 0) AS qty_scheduled
               FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id
               WHERE o.source_code_project_id = ? AND o.order_type = ?
               GROUP BY o.id ORDER BY o.batch_seq, o.id""",
            (code_project_id, models.OrderType.EXTERNAL),
        )

    # ── 内部订单：中标项目 → 排产数量 ────────────────────────

    def create_internal_from_bid(
        self,
        bid_project_id: int,
        line_ids: Optional[List[int]] = None,
        qty_overrides: Optional[Dict[int, float]] = None,
        customer: str = "",
        required_date: str = "",
        owner: str = "",
        auto_confirm: bool = True,
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        ★ 中标项目生成【一张内部订单】，明细实值 = 中标明细数量 bid_items.qty。
        这是中标项目产生排产数量的【唯一入口】；中标项目本身不直接挂数量。

        line_ids     可选：只按指定中标明细行生成（默认全部）
        qty_overrides 可选：{bid_item_id: 实值数量} 覆盖（仍须 > 0）
        auto_confirm 默认 True：内部订单无需用户确认，直接可排产
        """
        bp = self.db.get_by_id("bid_projects", bid_project_id)
        if not bp:
            raise NotFoundError(f"招投标项目 {bid_project_id} 不存在")
        if bp["status"] != models.BidStatus.WON:
            raise StateError(
                f"招投标项目 {bp['doc_no']} 状态为 {bp['status']}；只有「中标」项目可生成内部订单"
            )
        bid_items = self.db.query(
            "SELECT * FROM bid_items WHERE bid_project_id = ? ORDER BY line_no",
            (bid_project_id,),
        )
        if line_ids:
            bid_items = [i for i in bid_items if i["id"] in set(line_ids)]
        if not bid_items:
            raise ValidationError(f"中标项目 {bp['doc_no']} 没有可用于生成内部订单的明细")

        qty_overrides = qty_overrides or {}
        with self.db.tx():
            order = self.create_order(
                customer=customer or bp.get("customer") or "",
                source_bid_project_id=bid_project_id,
                order_type=models.OrderType.INTERNAL,
                required_date=required_date, owner=owner, at=at,
                remark=f"由中标项目 {bp['doc_no']} 自动生成",
                # v2.9：内部订单只可能来自中标项目，来源语义唯一
                order_source="招投标",
            )
            for bi in bid_items:
                qty = float(qty_overrides.get(bi["id"], bi["qty"]))
                self.add_item(
                    order["id"],
                    item_name=bi["product_name"],
                    qty_ordered=qty,
                    spec=bi.get("spec") or "",
                    unit=bi.get("unit") or "件",
                    unit_price=BidQuote.price(bi),
                    source_bid_item_id=bi["id"],
                    remark=f"来源 {codegen.bid_line_no(bp['doc_no'], bi['line_no'])}",
                    param_desc=bi.get("remark") or "",
                )
            self._sync_totals(order["id"])
            if auto_confirm and order["status"] == models.OrderStatus.DRAFT:
                self.confirm(order["id"], owner)
            self.db.log_action("order", order["id"], "internal_from_won_bid",
                               after={"bid_project_no": bp["doc_no"],
                                      "item_count": len(bid_items),
                                      "qty_real": sum(float(qty_overrides.get(i["id"], i["qty"]))
                                                      for i in bid_items)},
                               operator=owner)
        result = self.db.get_by_id("orders", order["id"])
        result["item_count"] = len(bid_items)
        result["qty_ordered_real"] = self.summary(order["id"])["qty_ordered_real"]
        return result

    def internal_order_of(self, bid_project_id: int) -> Optional[Dict[str, Any]]:
        """查中标项目已生成的内部订单（幂等校验用）"""
        return self.db.query_one(
            "SELECT * FROM orders WHERE source_bid_project_id = ? AND order_type = ?",
            (bid_project_id, models.OrderType.INTERNAL),
        )

    # ── 通用 ────────────────────────────────────────────────

    def confirm(self, order_id: int, operator: str = "") -> Dict[str, Any]:
        o = self.db.get_by_id("orders", order_id)
        if not o:
            raise NotFoundError(f"订单 {order_id} 不存在")
        models.assert_transition("order", o["status"], models.OrderStatus.CONFIRMED)
        with self.db.tx():
            self.db.update("orders", {"status": models.OrderStatus.CONFIRMED,
                                      "updated_at": now()}, "id = ?", (order_id,))
            self.db.log_action("order", order_id, "confirm", operator=operator)
        return self.db.get_by_id("orders", order_id)

    def list_orders(self, order_type: Optional[str] = None,
                    source_code_project_id: Optional[int] = None,
                    source_bid_project_id: Optional[int] = None,
                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM orders WHERE 1=1", []
        for col, val in (("order_type", order_type),
                         ("source_code_project_id", source_code_project_id),
                         ("source_bid_project_id", source_bid_project_id),
                         ("status", status)):
            if val is not None:
                sql += f" AND {col} = ?"
                params.append(val)
        return self.db.query(sql + " ORDER BY id", tuple(params))

    def list_items(self, order_id: int) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY line_no", (order_id,)
        )

    # ── 新建订单 / 导入订单（2026-11-06 取代下单向导）────────

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    def _lookup_project(self, table: str, value: Any) -> Optional[int]:
        """来源项目支持传 int 主键或单号字符串；找不到明确报错，绝不静默建无源单"""
        if value in (None, ""):
            return None
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            row = self.db.get_by_id(table, int(value))
        else:
            row = self.db.query_one(f"SELECT * FROM {table} WHERE doc_no = ?",
                                    (self._text(value),))
        if not row:
            raise NotFoundError(f"来源项目「{value}」不存在，请检查项目号或直接留空"
                                f"（留空建立手工订单）")
        return int(row["id"])

    def _resolve_code_item(self, cp_id: Optional[int], item_no: Any) -> Optional[int]:
        no = self._text(item_no)
        if not no or cp_id is None:
            return None
        row = self.db.query_one(
            "SELECT * FROM code_items WHERE item_code = ? AND code_project_id = ?",
            (no, cp_id))
        if not row:
            raise NotFoundError(f"来源询配码项号「{no}」在该询配码项目中不存在")
        return int(row["id"])

    def _resolve_bid_item(self, bp_id: Optional[int], line_no: Any) -> Optional[int]:
        text = self._text(line_no)
        if not text or bp_id is None:
            return None
        m = re.search(r"(\d+)$", text)
        if not m:
            raise ValidationError(f"来源中标明细行号「{text}」无法识别（可填 1 / 02 / ZB-…-02）")
        row = self.db.query_one(
            "SELECT * FROM bid_items WHERE bid_project_id = ? AND line_no = ?",
            (bp_id, int(m.group(1))))
        if not row:
            raise NotFoundError(f"中标明细第 {m.group(1)} 行在该项目中不存在")
        return int(row["id"])

    def _resolve_code_item_by_code(self, cp_id: Optional[int], code: Any) -> Optional[int]:
        """v2.3：UI 明细表不再有「来源项号」列，用物资代码自动匹配来源询配码项"""
        text = self._text(code)
        if not text or cp_id is None:
            return None
        row = self.db.query_one(
            "SELECT * FROM code_items WHERE code_project_id = ? "
            "AND (item_code = ? OR material_code = ? OR goods_code = ?) "
            "ORDER BY line_no LIMIT 1", (cp_id, text, text, text))
        return int(row["id"]) if row else None

    def _resolve_bid_item_by_name(self, bp_id: Optional[int], name: Any) -> Optional[int]:
        """v2.3：内部订单按物资名称回连中标明细行"""
        text = self._text(name)
        if not text or bp_id is None:
            return None
        row = self.db.query_one(
            "SELECT * FROM bid_items WHERE bid_project_id = ? AND product_name = ? "
            "ORDER BY line_no LIMIT 1", (bp_id, text))
        return int(row["id"]) if row else None

    def create_from_spec(self, spec: Dict[str, Any], operator: str = "",
                         origin: str = "create_manual",
                         allow_empty_items: bool = False) -> Dict[str, Any]:
        """
        「新建订单」与「导入订单」共用的统一建单入口（v2.3 · 8+9 字段）。

        ★ v3.5.3 两段式建单：allow_empty_items=True 时允许 0 明细只建订单头，
          明细随后由「录入订单明细 / 导入订单明细」补录。导入订单（批量建单）
          与其余调用方仍走默认 False，必须带明细，避免制造空壳订单。

        spec = {
          # 订单头 8 字段
          project_no / customer / order_type / status / amount_total /
          required_date / item_qty_total / erp_order_no,
          # 系统溯源 / 兼容字段
          source_code_no | source_code_project_id,  # 外部订单可选
          source_bid_no  | source_bid_project_id,  # 内部订单必填
          order_date / owner / remark / auto_confirm,
          order_source,                             # v2.9 订单来源（可空，自由文本）
          # 明细 9 字段
          items: [{item_code, item_name, production_name, spec, unit,
                   qty_ordered(实值>0), unit_price, amount, param_desc,
                   source_code_item_no, source_bid_line_no}]
        }

        ★ 内部订单仍受「一个中标项目唯一一张」约束；默认直接建为「已确认」。
        ★ 外部订单来源可空 = 手工订单；有来源时走 create_from_code_project。
        ★ 状态：显式 status 优先 → 内部默认已确认 → 外部按 auto_confirm。
        ★ 价税合计（Σ明细总价）/ 物资数量（明细项数，Q12）/ 明细总价：
          留空由 _sync_totals 自动派生；显式值原样保留。
          绝不改写 qty_ordered 实值，也不参与排产数量守恒。
        """
        otype = spec.get("order_type") or models.OrderType.EXTERNAL
        if otype not in (models.OrderType.EXTERNAL, models.OrderType.INTERNAL):
            raise ValidationError(f"未知订单类型：{otype}")
        items = list(spec.get("items") or [])
        if not items and not allow_empty_items:
            raise ValidationError("订单至少需要一个明细项（数量必须显式 > 0）")

        cp_id = self._lookup_project(
            "code_projects",
            spec.get("source_code_project_id") or spec.get("source_code_no"))
        bp_id = self._lookup_project(
            "bid_projects",
            spec.get("source_bid_project_id") or spec.get("source_bid_no"))
        if otype == models.OrderType.INTERNAL and cp_id is not None:
            raise ValidationError("内部订单不得来源于询配码项目")
        if otype == models.OrderType.EXTERNAL and bp_id is not None:
            raise ValidationError("外部订单不得来源于中标项目；请改订单类型为「内部订单」")
        if otype == models.OrderType.INTERNAL and bp_id is None:
            raise ValidationError("内部订单必须指定来源中标项目")

        customer = self._text(spec.get("customer"))
        order_date = self._text(spec.get("order_date"))
        required_date = self._text(spec.get("required_date"))
        owner = self._text(spec.get("owner")) or operator
        remark = self._text(spec.get("remark"))
        project_no = self._text(spec.get("project_no"))
        erp_order_no = self._text(spec.get("erp_order_no"))
        order_source = self._text(spec.get("order_source"))
        if otype == models.OrderType.INTERNAL and not order_source:
            # 内部订单只可能由中标项目生成，来源语义唯一，无需用户再选
            order_source = ORDER_SOURCES[0]          # 「招投标」
        declared_amount = spec.get("amount_total")
        declared_qty = spec.get("item_qty_total")

        requested_status = self._text(spec.get("status"))
        if requested_status:
            if not models.is_order_status(requested_status):
                raise ValidationError(
                    f"未知订单状态「{requested_status}」；可用：" +
                    "、".join(models.OrderStatus.CREATABLE))
            status = requested_status
        elif otype == models.OrderType.INTERNAL:
            status = models.OrderStatus.CONFIRMED       # 与 create_internal_from_bid 一致
        else:
            status = (models.OrderStatus.CONFIRMED if spec.get("auto_confirm")
                      else models.OrderStatus.DRAFT)

        head_kwargs = {
            "project_no": project_no, "erp_order_no": erp_order_no,
            "status": status, "amount_total": declared_amount,
            "item_qty_total": declared_qty, "order_source": order_source,
        }

        def add_kwargs(it: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
            return {
                "item_code": self._text(it.get("item_code")),
                "item_name": self._text(it.get("item_name")),
                "production_name": self._text(it.get("production_name")),
                "spec": self._text(it.get("spec")),
                "unit": self._text(it.get("unit")) or "件",
                "qty_ordered": float(it["qty_ordered"]),
                "unit_price": float(it.get("unit_price") or 0),
                "amount": it.get("amount"),
                "param_desc": self._text(it.get("param_desc")),
                "remark": self._text(it.get("remark")),
                **extra,
            }

        with self.db.tx():
            if otype == models.OrderType.INTERNAL:
                order = self.create_order(
                    customer=customer, order_type=otype,
                    source_bid_project_id=bp_id, order_date=order_date,
                    required_date=required_date, owner=owner,
                    remark=remark or "由中标项目生成", **head_kwargs)
                for it in items:
                    self.add_item(
                        order["id"],
                        **add_kwargs(it, source_bid_item_id=(
                            self._resolve_bid_item(bp_id, it.get("source_bid_line_no"))
                            or self._resolve_bid_item_by_name(bp_id, it.get("item_name")))))
            else:
                resolved = [{**it,
                             "source_code_item_id": (
                                 self._resolve_code_item(
                                     cp_id, it.get("source_code_item_no"))
                                 or self._resolve_code_item_by_code(
                                     cp_id, it.get("item_code")))}
                            for it in items]
                if cp_id is not None:
                    order = self.create_from_code_project(
                        cp_id, resolved, customer=customer,
                        required_date=required_date, owner=owner,
                        order_date=order_date, head=head_kwargs)
                else:
                    order = self.create_order(
                        customer=customer, order_type=otype,
                        order_date=order_date, required_date=required_date,
                        owner=owner, remark=remark, **head_kwargs)
                    for it in resolved:
                        self.add_item(order["id"], **add_kwargs(
                            it, source_code_item_id=it["source_code_item_id"]))
            # 声明值优先；留空按明细自动汇总（总价留空 = 数量 × 单价）
            self._sync_totals(order["id"], declared_amount, declared_qty)
            self.db.log_action(
                "order", order["id"], origin,
                after={"doc_no": order["doc_no"], "order_type": otype,
                       "status": status, "project_no": project_no,
                       "erp_order_no": erp_order_no,
                       "order_source": order_source,
                       "source_code_project_id": cp_id,
                       "source_bid_project_id": bp_id,
                       "item_count": len(items), "origin": origin},
                operator=operator)
        return self.db.get_by_id("orders", order["id"])

    def add_items_bulk(self, order_id: int,
                       items: Sequence[Dict[str, Any]],
                       operator: str = "", origin: str = "add_items_bulk",
                       reset_manual_totals: bool = False) -> Dict[str, Any]:
        """
        v3.5.3 两段式建单：给【已存在】的订单批量追加明细。

        · 单事务：任一行不合法 → 整批回滚，绝不留半截明细；
        · 每行仍走 add_item()（字段白名单、实值 >0、line_no 递增、审计不变）；
        · 结束按明细重算订单头；默认不冲掉手工声明的价税合计 / 物资数量
          （P1-5 既有口径），导入向导可置 reset_manual_totals=True 强制按明细汇总；
        · 目标订单必须存在且未作废。
        """
        rows = [dict(x) for x in (items or [])]
        if not rows:
            raise ValidationError("没有可导入的订单明细")
        order = self.db.get_by_id("orders", order_id)
        if not order:
            raise NotFoundError(f"订单 {order_id} 不存在")
        if order.get("status") == models.OrderStatus.VOID:
            raise StateError("已作废订单不可追加明细；如需重开请新建订单")

        prepared: List[Dict[str, Any]] = []
        for idx, it in enumerate(rows, 1):
            name = str(it.get("item_name") or "").strip()
            qty = float(it.get("qty_ordered") or 0)
            if not name:
                raise ValidationError(f"第 {idx} 行：物资名称不能为空")
            if qty <= 0:
                raise ValidationError(f"第 {idx} 行：数量(实值)必须 > 0")
            prepared.append({
                "item_code": str(it.get("item_code") or ""),
                "item_name": name,
                "production_name": str(it.get("production_name") or ""),
                "spec": str(it.get("spec") or ""),
                "unit": str(it.get("unit") or "件"),
                "qty_ordered": qty,
                "unit_price": float(it.get("unit_price") or 0),
                "amount": it.get("amount"),
                "param_desc": str(it.get("param_desc") or ""),
                "remark": str(it.get("remark") or ""),
            })

        before_count = len(self.list_items(order_id))
        with self.db.tx():
            for it in prepared:
                self.add_item(order_id, **it)
            if reset_manual_totals:
                self.reset_totals(order_id, amount=True, qty=True,
                                  operator=operator)
            else:
                self._sync_totals(order_id)
            after = self.db.get_by_id("orders", order_id)
            self.db.log_action(
                "order", order_id, origin,
                after={"doc_no": order["doc_no"], "added": len(prepared),
                       "item_count": before_count + len(prepared),
                       "amount_total": after["amount_total"],
                       "origin": origin},
                operator=operator)
        s = self.summary(order_id)
        return {"count": len(prepared), "order": after,
                "item_count": s["item_count"],
                "qty_ordered_real": s["qty_ordered_real"],
                "amount_total": s["amount_total"]}

    def import_orders(self, groups: List[Dict[str, Any]], operator: str = "",
                      auto_confirm: bool = False) -> Dict[str, Any]:
        """
        「导入订单」批量入口：groups 来自 core/order_fields.parse_rows()。
        ★ 单事务：任一张订单失败 → 全部回滚，不留半截订单。
        ★ 内部订单若同一中标项目出现多组，第二组会触发唯一约束整体回滚。
        """
        if not groups:
            raise ValidationError("没有可导入的订单数据")
        created: List[Dict[str, Any]] = []
        with self.db.tx():
            for g in groups:
                spec = {**g["head"], "items": g["items"],
                        "auto_confirm": auto_confirm}
                # 兼容旧调用：文件里根本没有「订单状态」列且要求自动确认时，
                # 把缺省的草稿态提升为已确认；显式填了状态列则一切以文件为准。
                if (auto_confirm and not spec.get("_status_given")
                        and spec.get("status") == models.OrderStatus.DRAFT):
                    spec["status"] = models.OrderStatus.CONFIRMED
                created.append(self.create_from_spec(
                    spec, operator=operator, origin="import_order"))
            self.db.log_action(
                "order_batch", None, "import_orders",
                after={"order_count": len(created),
                       "doc_nos": [o["doc_no"] for o in created],
                       "item_count": sum(len(g["items"]) for g in groups)},
                operator=operator)
        summaries = [self.summary(o["id"]) for o in created]
        qty = sum(s["qty_ordered_real"] for s in summaries)
        return {
            "orders": created,
            "order_count": len(created),
            "item_count": sum(len(g["items"]) for g in groups),
            "qty_ordered_real": round(qty, 2),
            "amount_total": float(money.sum2(s["amount_total"] for s in summaries)),
            "doc_nos": [o["doc_no"] for o in created],
        }

    # ── 数量守恒（锚点始终是 qty_ordered 实值） ─────────────

    def allocated_qty(self, order_item_id: int) -> float:
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(allocated_qty), 0) FROM production_links WHERE order_item_id = ?",
            (order_item_id,), default=0.0,
        ))

    def available_qty(self, order_item_id: int) -> float:
        item = self.db.get_by_id("order_items", order_item_id)
        if not item:
            raise NotFoundError(f"订单明细 {order_item_id} 不存在")
        return round(float(item["qty_ordered"]) - self.allocated_qty(order_item_id), 6)

    def summary(self, order_id: int) -> Dict[str, float]:
        o = self.db.get_by_id("orders", order_id)
        if not o:
            raise NotFoundError(f"订单 {order_id} 不存在")
        items = self.list_items(order_id)
        qty = sum(float(i["qty_ordered"]) for i in items)
        # P1-6：金额一律 Decimal 累加，避免 float 尾数（如 12345.679999999998）
        amount = money.sum2(money.mul(i["qty_ordered"], i["unit_price"] or 0)
                            for i in items)
        allocated = sum(self.allocated_qty(i["id"]) for i in items)
        return {
            "order_type": o["order_type"],
            "batch_seq": o["batch_seq"],
            "item_count": len(items),
            "qty_ordered_real": round(qty, 2),
            "qty_scheduled": round(allocated, 2),
            "qty_remaining": round(qty - allocated, 2),
            "amount": float(amount),             # Σ 数量×单价（旧口径，保留）
            "amount_total": float(money.q2(o.get("amount_total") or 0)),  # v2.3 价税合计
            "item_qty_total": float(money.q2(o.get("item_qty_total") or 0)),  # v2.3 物资数量
            "project_no": o.get("project_no") or "",
            "erp_order_no": o.get("erp_order_no") or "",
            "order_source": o.get("order_source") or "",
        }


class BidQuote:
    """内部订单单价：沿用询报价口径「对外报价 = 采购价 ÷ 报价比率」"""

    @staticmethod
    def price(bid_item: Dict[str, Any]) -> float:
        ratio = float(bid_item.get("quote_ratio") or 1.0) or 1.0
        return round(float(bid_item.get("purchase_price") or 0.0) / ratio, 2)

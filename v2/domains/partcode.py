#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 2：询配码项目（独立域）

★ 与询报价/招投标域零关联：本表无 source_* 外键，也不提供转换接口。
★ qty_reference 是【虚值】：只做统计展示，不参与任何数量守恒或排产校验。
   真正的排产数量来自 order_items.qty_ordered（实值）。
业务流程：建询配码项目/明细 → 提交文件给用户 → 用户下单 → 订单 → 排产。
"""
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core import codegen, fields, models
from ..core.db import Database, now
from ..core.errors import (DuplicateError, NotFoundError, ValidationError)


class PartCodeService:
    def __init__(self, db: Database):
        self.db = db

    def create_project(
        self,
        project_name: str,
        customer: str = "",
        file_path: str = "",
        version: str = "V1",
        submit_date: str = "",
        owner: str = "",
        remark: str = "",
        at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not project_name.strip():
            raise ValidationError("询配码项目名称不能为空")
        with self.db.tx():
            doc_no = codegen.next_doc_no(self.db, codegen.PREFIX_CODE_PROJECT, at)
            ts = now()
            pid = self.db.insert("code_projects", {
                "doc_no": doc_no,
                "project_name": project_name,
                "customer": customer,
                "status": models.CodeProjectStatus.DRAFT,
                "submit_date": submit_date,
                "file_path": file_path,
                "version": version,
                "owner": owner,
                "remark": remark,
                "created_at": ts,
                "updated_at": ts,
            })
            self.db.log_action("code_project", pid, "create",
                               after={"doc_no": doc_no}, operator=owner)
        return self.db.get_by_id("code_projects", pid)

    def add_item(
        self,
        code_project_id: int,
        item_name: str,
        spec: str = "",
        material: str = "",
        std_code: str = "",
        drawing_no: str = "",
        tech_requirement: str = "",
        unit: str = "件",
        qty_reference: float = 0.0,
        remark: str = "",
        inquiry_item_name: str = "",
        goods_code: str = "",
        material_code: str = "",
        factory_price: float = 0.0,
        item_detail: str = "",
        operator: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        """
        新增一行询配码明细。

        业务字段规范见 v2/core/fields.py（10 列，顺序与导出/导入模板一致）：
        询价物料名称 / 商品编码 / 物料编码 / 物料名称 / 单位 /
        厂家价格(元) / 物料具体情况 / 参考数量 / 备注 / 技术要求。
        spec / material / std_code / drawing_no 为兼容旧模板的历史字段。

        ★ qty_reference 是【虚值】：允许 0、允许任意值，不复制到订单、不约束排产。
        ★ factory_price 只是询价信息，不是订单单价；订单金额以 order_items.unit_price 为准。
        ★ item_name 是主名称（订单明细与溯源快照沿用），只填询价名称时自动兜底。
        """
        bad = (set(extra) - fields.WRITABLE_KEYS)
        if bad:
            raise ValidationError(
                f"询配码明细不接受这些字段：{'、'.join(sorted(bad))}；"
                f"可写字段见 v2/core/fields.py")
        data: Dict[str, Any] = {
            "inquiry_item_name": inquiry_item_name,
            "goods_code": goods_code,
            "material_code": material_code,
            "item_name": item_name,
            "unit": unit,
            "factory_price": factory_price,
            "item_detail": item_detail,
            "qty_reference": qty_reference,
            "remark": remark,
            "tech_requirement": tech_requirement,
            "spec": spec, "material": material,
            "std_code": std_code, "drawing_no": drawing_no,
        }
        for key, value in extra.items():
            data[key] = value
        return self._insert_one(code_project_id, data, operator=operator)

    def _insert_one(self, code_project_id: int, data: Dict[str, Any],
                    operator: str = "", audit: bool = True) -> Dict[str, Any]:
        project = self.db.get_by_id("code_projects", code_project_id)
        if not project:
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        name = str(data.get("item_name") or "").strip()
        if not name:
            name = str(data.get("inquiry_item_name") or "").strip()
        data["item_name"] = name
        if not name:
            raise ValidationError("询配码项「物料名称」不能为空（询价物料名称也为空）")
        for key in fields.NUMERIC_KEYS:
            value = data.get(key)
            if value in (None, ""):
                data[key] = 0.0
            else:
                try:
                    data[key] = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{key} 必须是数字，收到：{value!r}") from exc
        if not str(data.get("unit") or "").strip():
            data["unit"] = "件"
        with self.db.tx():
            line_no = int(self.db.scalar(
                "SELECT COALESCE(MAX(line_no), 0) + 1 FROM code_items WHERE code_project_id = ?",
                (code_project_id,), default=1,
            ))
            item_code = codegen.item_code_of(project["doc_no"], line_no)
            ts = now()
            try:
                iid = self.db.insert("code_items", {
                    "code_project_id": code_project_id,
                    "line_no": line_no,
                    "item_code": item_code,
                    "status": models.CodeItemStatus.PENDING,
                    "created_at": ts,
                    "updated_at": ts,
                    **{k: v for k, v in data.items()
                       if k in fields.WRITABLE_KEYS},
                })
            except sqlite3.IntegrityError as exc:
                raise DuplicateError(f"询配码项号 {item_code} 已存在：{exc}") from exc
            if audit:
                self.db.log_action("code_item", iid, "create",
                                   after={"item_code": item_code, "item_name": name},
                                   operator=operator)
        return self.db.get_by_id("code_items", iid)

    def add_items_bulk(
        self,
        code_project_id: int,
        items: List[Dict[str, Any]],
        operator: str = "",
    ) -> Dict[str, Any]:
        """
        CSV / Excel 批量导入：单事务、全成功或全回滚，写一条汇总审计留痕。
        items 为 fields.parse_rows() 的输出（可含历史兼容字段）。
        """
        project = self.db.get_by_id("code_projects", code_project_id)
        if not project:
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        if not items:
            return {"count": 0, "ids": [], "doc_no": project["doc_no"]}
        ids: List[int] = []
        # 先做一遍纯校验（不写库），避免半途失败留下语义不明的错误信息
        for i, item in enumerate(items, start=1):
            bad = set(item) - fields.WRITABLE_KEYS
            if bad:
                raise ValidationError(f"第 {i} 行含不可写字段：{'、'.join(sorted(bad))}")
            if not str(item.get("item_name") or item.get("inquiry_item_name") or "").strip():
                raise ValidationError(f"第 {i} 行缺少物料名称")
        with self.db.tx():
            for item in items:
                ids.append(self._insert_one(code_project_id, dict(item),
                                            operator=operator, audit=False)["id"])
            self.db.log_action(
                "code_project", code_project_id, "import_code_items",
                after={"doc_no": project["doc_no"], "count": len(ids),
                       "first_items": [str(x.get("item_name") or "") for x in items[:3]]},
                operator=operator,
            )
        return {"count": len(ids), "ids": ids, "doc_no": project["doc_no"]}

    def submit(self, code_project_id: int, file_path: str = "",
               submit_date: str = "", operator: str = "") -> Dict[str, Any]:
        """询配码文件提交给用户；提交后才允许下单"""
        p = self.db.get_by_id("code_projects", code_project_id)
        if not p:
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        models.assert_transition("code_project", p["status"],
                                 models.CodeProjectStatus.SUBMITTED)
        with self.db.tx():
            self.db.update("code_projects", {
                "status": models.CodeProjectStatus.SUBMITTED,
                "submit_date": submit_date or now()[:10],
                "file_path": file_path or p.get("file_path") or "",
                "updated_at": now(),
            }, "id = ?", (code_project_id,))
            self.db.log_action("code_project", code_project_id, "submit_to_customer",
                               operator=operator)
        return self.db.get_by_id("code_projects", code_project_id)

    def list_items(self, code_project_id: int) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM code_items WHERE code_project_id = ? ORDER BY line_no",
            (code_project_id,),
        )

    def refresh_status(self, code_project_id: int) -> Dict[str, Any]:
        """
        按「是否被订单明细引用」回写下单状态（只看有没有下单，不看数量；虚值不参与）。
        """
        p = self.db.get_by_id("code_projects", code_project_id)
        if not p:
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        rows = self.db.query(
            """SELECT ci.id,
                      (SELECT COUNT(*) FROM order_items oi
                       WHERE oi.source_code_item_id = ci.id) AS order_cnt
               FROM code_items ci WHERE ci.code_project_id = ? ORDER BY ci.line_no""",
            (code_project_id,),
        )
        with self.db.tx():
            for r in rows:
                status = (models.CodeItemStatus.ORDERED if r["order_cnt"] > 0
                          else models.CodeItemStatus.PENDING)
                self.db.update("code_items", {"status": status, "updated_at": now()},
                               "id = ?", (r["id"],))
            if p["status"] in (models.CodeProjectStatus.SUBMITTED,
                               models.CodeProjectStatus.PARTIAL_ORDERED,
                               models.CodeProjectStatus.ORDERED):
                ordered = sum(1 for r in rows if r["order_cnt"] > 0)
                head = (models.CodeProjectStatus.ORDERED if rows and ordered == len(rows)
                        else models.CodeProjectStatus.PARTIAL_ORDERED if ordered
                        else models.CodeProjectStatus.SUBMITTED)
                self.db.update("code_projects", {"status": head, "updated_at": now()},
                               "id = ?", (code_project_id,))
        return self.db.get_by_id("code_projects", code_project_id)

    def statistics(self, code_project_id: int) -> Dict[str, Any]:
        """询配码项目统计：虚值合计 vs 多张订单实值合计（分开列，绝不混算）"""
        from .order import OrderService

        p = self.db.get_by_id("code_projects", code_project_id)
        if not p:
            raise NotFoundError(f"询配码项目 {code_project_id} 不存在")
        items = self.list_items(code_project_id)
        ref_total = sum(float(i.get("qty_reference") or 0) for i in items)
        order_rows = self.db.query(
            """SELECT oi.*, o.doc_no AS order_no, o.batch_seq FROM order_items oi
               JOIN orders o ON o.id = oi.order_id
               WHERE oi.source_code_item_id IN (
                   SELECT id FROM code_items WHERE code_project_id = ?)
               ORDER BY oi.id""",
            (code_project_id,),
        )
        ordered_total = sum(float(r["qty_ordered"]) for r in order_rows)
        # ★ 一码多单：按批次列出每张订单的实值合计与已排量
        batches = OrderService(self.db).order_batches(code_project_id)
        return {
            "code_no": p["doc_no"],
            "project_name": p["project_name"],
            "status": p["status"],
            "item_count": len(items),
            "qty_reference_total_virtual": round(ref_total, 2),   # 虚值
            "order_count": len(batches),                          # 该项目下了几张单
            "order_item_count": len(order_rows),
            "qty_ordered_total_real": round(ordered_total, 2),    # 实值
            "qty_scheduled_total_real": round(
                sum(float(b["qty_scheduled"]) for b in batches), 2),
            "batches": batches,
            "note": "虚值仅统计展示，与订单/排产数量不对应，不参与任何校验；"
                    "一个询配码项目可产生多张订单，各单实值独立累加",
        }

    def order_batches(self, code_project_id: int):
        """便捷入口：询配码项目 → 多张订单（分批下单）"""
        from .order import OrderService
        return OrderService(self.db).order_batches(code_project_id)

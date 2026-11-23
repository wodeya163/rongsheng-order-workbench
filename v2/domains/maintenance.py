#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单据维护域（v2.2 · P0）：编辑 / 作废 / 删除 + 引用拦截 + 审计留痕

设计原则（改动必须守住这四条，否则会破坏 v2.2 的口径）：
  1. 单号、行号、来源外键、订单类型、状态一律【不可编辑】——
     它们是溯源链与数量守恒的锚点，改了链路就断了。
  2. 删除/作废前先查【跨实体引用】：被下游用了就拦，绝不静默级联，
     也不允许出现"订单作废了、排产还在跑"这种脏状态。
  3. order_items.qty_ordered 是唯一实值锚点：改小不得小于已排量
     Σ production_links.allocated_qty，否则数量守恒立刻被破坏。
  4. 所有写操作走 db.tx() 事务，并写 audit_log（before/after 全量 JSON 快照 + 操作人）。

作废是独立于状态机的通道：不改动 models.TRANSITIONS 的正常流转，
只把 status 置为「作废」（终态，不可再编辑/流转）。
明细行没有状态字段，不提供作废，只能删除（且同样受引用拦截）。

本模块零第三方依赖，纯逻辑环境可直接跑测试。
"""
from typing import Any, Dict, List, Optional

from ..core import models
from ..core import fields as fdefs
from ..core import order_fields as ofdefs
from ..core.db import Database, now
from ..core.errors import (NotFoundError, ReferencedError, StateError,
                           ValidationError)


# ── 常量 ────────────────────────────────────────────────────

VOID = models.VOID_STATUS

# 已经在跑的状态：禁止删除（作废另论，作废也拦引用）
RUNNING_STATUSES = {"中标", "已排产", "生产中", "已交付", "完工", "关闭",
                    "已下单", "部分下单"}

ACTION_LABELS = {
    "create": "新建", "edit": "编辑", "void": "作废", "delete": "删除",
    "delete_child": "级联删除明细", "status": "状态流转", "confirm": "确认",
    "submit_to_customer": "提交用户", "from_code_project": "由询配码下单",
    "internal_from_won_bid": "由中标生成内部订单", "link_won_bid": "挂中标标签",
    "from_won_bid": "中标一键排产", "link_order_item": "关联排产",
    "update_allocation": "调整分配量", "unlink": "解除关联",
}

# 可编辑字段白名单。每项：(key, 中文标签, 控件类型, 必填, 附加参数)
# 控件类型与 ui/dialogs.py::FormDialog 一致：text/textarea/date/float/int
def _f(key, label, typ="text", required=False, **kw):
    return {"key": key, "label": label, "type": typ,
            "required": required, **kw}


ENTITY_SPECS: Dict[str, Dict[str, Any]] = {
    "bid_project": {
        "table": "bid_projects", "label": "招投标项目",
        "no_field": "doc_no", "name_field": "project_name", "status_field": "status",
        "voidable": True,
        "fields": [
            _f("project_name", "招投标项目名称", required=True),
            _f("customer", "客户名称"), _f("tender_no", "招标编号"),
            _f("bid_date", "投标日期", "date"), _f("owner", "负责人"),
            _f("remark", "备注", "textarea"),
        ],
        "children": [("bid_items", "bid_project_id", "投标明细")],
        "refs": [
            {"table": "orders", "column": "source_bid_project_id",
             "label": "内部订单", "display": "doc_no"},
            {"table": "production_projects", "column": "source_bid_project_id",
             "label": "生产排产项目（来源标签）", "display": "doc_no"},
        ],
    },
    "bid_item": {
        "table": "bid_items", "label": "投标明细",
        "no_field": "line_no", "name_field": "product_name", "status_field": None,
        "fields": [
            _f("product_name", "产品名称", required=True),
            _f("spec", "规格"), _f("category", "类别"), _f("unit", "单位"),
            _f("qty", "数量（实值）", "float", True, minimum=0.001, decimals=3),
            _f("purchase_price", "采购单价 ¥", "float", False, minimum=0, decimals=2),
            _f("quote_ratio", "报价比率", "float", True, minimum=0.0001, decimals=4),
            _f("remark", "备注", "textarea"),
        ],
        "children": [],
        "refs": [
            {"table": "order_items", "column": "source_bid_item_id",
             "label": "内部订单明细", "display": "item_name"},
        ],
    },
    "code_project": {
        "table": "code_projects", "label": "询配码项目",
        "no_field": "doc_no", "name_field": "project_name", "status_field": "status",
        "voidable": True,
        "fields": [
            _f("project_name", "询配码项目名称", required=True),
            _f("customer", "客户名称"), _f("file_path", "询配码文件路径"),
            _f("version", "版本"), _f("submit_date", "提交日期", "date"),
            _f("owner", "负责人"), _f("remark", "备注", "textarea"),
        ],
        "children": [("code_items", "code_project_id", "询配码明细")],
        "refs": [
            {"table": "orders", "column": "source_code_project_id",
             "label": "外部订单", "display": "doc_no"},
        ],
    },
    "code_item": {
        "table": "code_items", "label": "询配码明细",
        "no_field": "item_code", "name_field": "item_name", "status_field": "status",
        # 10 个业务字段取自 core/fields.py；末尾 4 个为兼容历史数据的旧列
        "fields": fdefs.form_fields() + [
            _f("spec", "规格〔兼容字段〕"), _f("material", "材质〔兼容字段〕"),
            _f("std_code", "执行标准〔兼容字段〕"),
            _f("drawing_no", "图号〔兼容字段〕"),
        ],
        "children": [],
        "refs": [
            {"table": "order_items", "column": "source_code_item_id",
             "label": "外部订单明细", "display": "item_name"},
        ],
    },
    "order": {
        "table": "orders", "label": "订单",
        "no_field": "doc_no", "name_field": "customer", "status_field": "status",
        "voidable": True,
        # v2.3 订单头 8 字段中可编辑的业务字段；状态/订单类型仍走状态机，不开放编辑
        "fields": [
            _f("project_no", "订单项目编号"),
            _f("customer", "客户名称"),
            # v2.9：订单来源可改（渠道会变、也会新增），预设值与新建订单同源
            _f("order_source", "订单来源（可下拉或自填）", "combo", False,
               editable=True,
               options=[("", "（未填）")] +
                       [(s, s) for s in ofdefs.ORDER_SOURCES]),
            _f("amount_total", "价税合计（留空按明细汇总）", "float",
               minimum=0, decimals=2),
            _f("required_date", "交货日期", "date"),
            _f("item_qty_total", "物资数量〔项数〕（留空自动=明细行数）",
               "float", minimum=0, decimals=3),
            _f("erp_order_no", "ERP订单编号"),
            _f("order_date", "下单日期〔兼容字段〕", "date"),
            _f("owner", "负责人〔兼容字段〕"),
            _f("remark", "备注〔兼容字段〕", "textarea"),
        ],
        "children": [("order_items", "order_id", "订单明细")],
        "refs": [
            {"table": "production_links",
             "where": "order_item_id IN (SELECT id FROM order_items WHERE order_id = ?)",
             "label": "排产关联", "display": "order_line_snapshot"},
        ],
    },
    "order_item": {
        "table": "order_items", "label": "订单明细",
        "no_field": "line_no", "name_field": "item_name", "status_field": None,
        # v2.3 明细 9 字段（顺序与 core/order_fields.py 一致）
        "fields": [
            _f("item_code", "物资代码"),
            _f("item_name", "物资名称", required=True),
            _f("production_name", "实际生产物资名称"),
            _f("spec", "规格"),
            _f("unit", "单位"),
            # ★ 实值锚点：改小不得小于已排量，见 _guard_edit()
            _f("qty_ordered", "数量（实值 · 排产锚点）", "float", True,
               minimum=0.001, decimals=3),
            _f("unit_price", "单价 ¥", "float", False, minimum=0, decimals=2),
            _f("amount", "总价（留空按 数量×单价）", "float", False,
               minimum=0, decimals=2),
            _f("param_desc", "参数描述", "textarea"),
            _f("material", "材质〔兼容字段〕"),
            _f("remark", "备注〔兼容字段〕", "textarea"),
        ],
        "children": [],
        "refs": [
            {"table": "production_links", "column": "order_item_id",
             "label": "排产关联", "display": "order_line_snapshot"},
        ],
    },
    "production_project": {
        "table": "production_projects", "label": "生产排产项目",
        "no_field": "doc_no", "name_field": "title", "status_field": "status",
        "voidable": True,
        "fields": [
            _f("title", "生产项目名称", required=True),
            _f("plan_start", "计划开工", "date"), _f("plan_end", "计划完工", "date"),
            _f("actual_start", "实际开工", "date"), _f("actual_end", "实际完工", "date"),
            _f("workshop", "车间"), _f("owner", "负责人"),
            _f("priority", "优先级", "int", False, minimum=1, maximum=9),
            _f("remark", "备注", "textarea"),
        ],
        "children": [
            ("production_links", "project_id", "排产关联"),
            ("production_tasks", "project_id", "工序任务"),
        ],
        "refs": [],
    },
    "production_link": {
        "table": "production_links", "label": "排产关联",
        "no_field": "order_line_snapshot", "name_field": "item_name_snapshot",
        "status_field": None,
        "fields": [
            _f("allocated_qty", "分配量（实值）", "float", True,
               minimum=0.001, decimals=3),
        ],
        "children": [], "refs": [],
    },
}


class MaintenanceService:
    """统一的单据维护入口；界面只调 edit / void / delete / references / audit_trail"""

    def __init__(self, db: Database):
        self.db = db

    # ── 元数据 ──────────────────────────────────────────────

    @staticmethod
    def spec(entity: str) -> Dict[str, Any]:
        if entity not in ENTITY_SPECS:
            raise ValidationError(f"未知单据类型：{entity}")
        return ENTITY_SPECS[entity]

    def _columns(self, table: str) -> set:
        """表实际列集合（production_links 无 updated_at，不能无条件写）"""
        return {r["name"] for r in self.db.query(f"PRAGMA table_info({table})")}

    def _touch(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if "updated_at" in self._columns(table):
            data["updated_at"] = now()
        return data

    def get(self, entity: str, entity_id: int) -> Dict[str, Any]:
        s = self.spec(entity)
        row = self.db.get_by_id(s["table"], entity_id)
        if not row:
            raise NotFoundError(f"{s['label']} {entity_id} 不存在")
        return row

    @staticmethod
    def describe(entity: str, record: Dict[str, Any]) -> str:
        s = ENTITY_SPECS[entity]
        no = record.get(s["no_field"])
        name = record.get(s["name_field"])
        return f"{s['label']} {no or '—'}" + (f"  {name}" if name else "")

    def field_defs(self, entity: str, entity_id: int) -> List[Dict[str, Any]]:
        """生成 FormDialog 字段定义（带当前值），供编辑对话框使用"""
        record = self.get(entity, entity_id)
        out = []
        for f in self.spec(entity)["fields"]:
            item = dict(f)
            value = record.get(f["key"])
            item["default"] = "" if value is None else value
            out.append(item)
        return out

    # ── 引用检查 ────────────────────────────────────────────

    def references(self, entity: str, entity_id: int) -> List[Dict[str, Any]]:
        """
        跨实体下游引用。返回非空即【禁止作废/删除】。
        每项含 label / table / count / samples（单号或行号，最多 5 条）。
        """
        self.get(entity, entity_id)          # 先确认实体存在
        found = []
        for ref in self.spec(entity)["refs"]:
            if ref.get("column"):
                where, param = f"{ref['column']} = ?", entity_id
            else:
                where, param = ref["where"], entity_id
            count = int(self.db.scalar(
                f"SELECT COUNT(*) FROM {ref['table']} WHERE {where}", (param,), default=0))
            if not count:
                continue
            rows = self.db.query(
                f"SELECT * FROM {ref['table']} WHERE {where} LIMIT 5", (param,))
            disp = ref.get("display") or "id"
            samples = [str(r.get(disp) or r.get("id")) for r in rows]
            found.append({"label": ref["label"], "table": ref["table"],
                          "count": count, "samples": samples})
        return found

    @staticmethod
    def _assert_no_refs(refs: List[Dict[str, Any]], action: str) -> None:
        if not refs:
            return
        lines = []
        for r in refs:
            sample = "、".join(r["samples"])
            more = "…" if r["count"] > len(r["samples"]) else ""
            lines.append(f"{r['label']} {r['count']} 条（{sample}{more}）")
        raise ReferencedError(
            f"不能{action}：已被下游引用 —— " + "；".join(lines)
            + "。请先处理下游单据（解除关联 / 作废 / 删除）后重试。", refs)

    def child_counts(self, entity: str, entity_id: int) -> Dict[str, int]:
        """本级明细（删除时级联，但会逐条留审计）"""
        self.get(entity, entity_id)
        out = {}
        for table, column, _label in self.spec(entity)["children"]:
            n = int(self.db.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                (entity_id,), default=0))
            if n:
                out[table] = n
        return out

    # ── 编辑 ────────────────────────────────────────────────

    def _guard_edit(self, entity: str, record: Dict[str, Any],
                    changes: Dict[str, Any]) -> Dict[str, Any]:
        """字段白名单 + 业务硬约束。返回归一化后的变更字典"""
        s = self.spec(entity)
        status_field = s["status_field"]
        if status_field and record.get(status_field) == VOID:
            raise StateError("已作废单据不可编辑；如需重开请新建单据")

        allowed = {f["key"]: f for f in s["fields"]}
        illegal = [k for k in changes if k not in allowed]
        if illegal:
            raise ValidationError(
                "以下字段不允许编辑（单号 / 行号 / 来源 / 订单类型 / 状态是链路锚点）："
                + "、".join(illegal))

        clean: Dict[str, Any] = {}
        for key, raw in changes.items():
            f = allowed[key]
            value = raw
            if f["type"] in ("float", "int"):
                try:
                    value = float(value) if f["type"] == "float" else int(value)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{f['label']} 必须是数字") from exc
                if "minimum" in f and value < f["minimum"]:
                    raise ValidationError(
                        f"{f['label']} 不能小于 {f['minimum']}（当前填写 {value}）")
                if "maximum" in f and value > f["maximum"]:
                    raise ValidationError(
                        f"{f['label']} 不能大于 {f['maximum']}（当前填写 {value}）")
            else:
                value = "" if value is None else str(value).strip()
                if f["required"] and not value:
                    raise ValidationError(f"{f['label']} 不能为空")
            clean[key] = value

        # ★ 硬约束 1：订单实值不得改到小于已排量，否则立刻破坏数量守恒
        if entity == "order_item" and "qty_ordered" in clean:
            allocated = float(self.db.scalar(
                "SELECT COALESCE(SUM(allocated_qty), 0) FROM production_links "
                "WHERE order_item_id = ?", (record["id"],), default=0.0))
            if clean["qty_ordered"] + 1e-9 < allocated:
                raise ValidationError(
                    f"订单实值不能改为 {clean['qty_ordered']}："
                    f"已排产 {allocated}，会破坏「Σ排产分配 ≤ 订单实值」守恒。"
                    f"请先减少排产关联或在不低于 {allocated} 的范围内修改。")

        # ★ 硬约束 1b：排产关联的分配量同样不能超可排量
        if entity == "production_link" and "allocated_qty" in clean:
            item = self.db.get_by_id("order_items", record["order_item_id"])
            if item:
                others = float(self.db.scalar(
                    "SELECT COALESCE(SUM(allocated_qty), 0) FROM production_links "
                    "WHERE order_item_id = ? AND id <> ?",
                    (record["order_item_id"], record["id"]), default=0.0))
                ceiling = float(item["qty_ordered"]) - others
                if clean["allocated_qty"] > ceiling + 1e-9:
                    raise ValidationError(
                        f"分配量不能改为 {clean['allocated_qty']}："
                        f"该订单明细实值 {item['qty_ordered']}，其他关联已占 {others}，"
                        f"最多可分配 {round(ceiling, 6)}。")

        # ★ 硬约束 2：中标明细的实值/价格已被内部订单"翻译"，不得回改造成上下游不一致
        if entity == "bid_item" and any(
                k in clean for k in ("qty", "purchase_price", "quote_ratio")):
            used = int(self.db.scalar(
                "SELECT COUNT(*) FROM order_items WHERE source_bid_item_id = ?",
                (record["id"],), default=0))
            if used:
                raise ValidationError(
                    "该投标明细已进入内部订单（数量/单价已固化为订单实值），"
                    "不能回改数量或价格。请先删除对应内部订单，或改内部订单明细。")

        # 虚值字段：明确放行，不参与任何守恒（仅写注释说明，不做拦截）
        return clean

    def edit(self, entity: str, entity_id: int,
             changes: Dict[str, Any], operator: str = "") -> Dict[str, Any]:
        """按白名单编辑单据；单号/来源/行号/状态一律不可改；全量 before/after 留痕"""
        before = self.get(entity, entity_id)
        clean = self._guard_edit(entity, before, changes)
        if not clean:
            return before
        diff = {k: v for k, v in clean.items() if before.get(k) != v}
        if not diff:
            return before                      # 无实质变更，不写审计噪音
        s = self.spec(entity)
        # ★ P1-6：明细改数量/单价而未显式改总价时，总价随 数量×单价 重算（Decimal 两位），
        #   避免出现「单价改了、总价还是旧值」的自相矛盾数据。
        if entity == "order_item" and "amount" not in diff and (
                "qty_ordered" in diff or "unit_price" in diff):
            from ..core import money
            diff["amount"] = float(money.mul(
                diff.get("qty_ordered", before.get("qty_ordered")),
                diff.get("unit_price", before.get("unit_price"))))
        with self.db.tx():
            self.db.update(s["table"], self._touch(s["table"], dict(diff)),
                           "id = ?", (entity_id,))
            # v2.3：明细数量/单价/总价变更后，订单头同步重算：
            #   价税合计 = Σ 明细总价；物资数量 = 明细行数（Q12 项数口径，改数量不影响它）
            # ★ P1-5：只重算未被手工锁定的字段（amount_manual / item_qty_manual = 0），
            #   用户显式填过的值不会再被自动汇总冲掉。
            if entity == "order_item" and any(
                    k in diff for k in ("qty_ordered", "unit_price", "amount")):
                from .order import OrderService
                OrderService(self.db)._sync_totals(int(before["order_id"]))
            elif entity == "order" and any(
                    k in diff for k in ("amount_total", "item_qty_total")):
                # 界面改头字段 = 显式声明 → 加手工锁；清空走「恢复自动汇总」动作
                from .order import OrderService
                OrderService(self.db)._sync_totals(
                    int(entity_id),
                    diff.get("amount_total") if "amount_total" in diff else None,
                    diff.get("item_qty_total") if "item_qty_total" in diff else None)
            self.db.log_action(entity, entity_id, "edit",
                               before=before,
                               after={"changed": diff, "doc_no": before.get(s["no_field"])},
                               operator=operator)
        return self.db.get_by_id(s["table"], entity_id)

    # ── 作废 ────────────────────────────────────────────────

    def void(self, entity: str, entity_id: int, reason: str = "",
             operator: str = "") -> Dict[str, Any]:
        """
        主单据逻辑作废（status = 作废，终态，数据保留、可查、进审计）。
        被下游引用时硬拦截；明细行没有状态字段，不适用作废（请走删除）。
        """
        s = self.spec(entity)
        if not s.get("voidable"):
            raise ValidationError(
                f"{s['label']} 不是可作废的主单据；如确认录错请使用「删除选中记录」")
        before = self.get(entity, entity_id)
        if before[s["status_field"]] == VOID:
            raise StateError(f"{self.describe(entity, before)} 已作废，无需重复操作")
        self._assert_no_refs(self.references(entity, entity_id), "作废")
        with self.db.tx():
            self.db.update(s["table"],
                           self._touch(s["table"], {s["status_field"]: VOID}),
                           "id = ?", (entity_id,))
            self.db.log_action(entity, entity_id, "void", before=before,
                               after={"status": VOID, "reason": reason,
                                      "doc_no": before.get(s["no_field"])},
                               operator=operator)
        return self.db.get_by_id(s["table"], entity_id)

    # ── 删除 ────────────────────────────────────────────────

    def delete(self, entity: str, entity_id: int,
               operator: str = "") -> Dict[str, Any]:
        """
        物理删除，仅在【无跨实体引用】且【非运行态】时允许。
        本级明细会一并删除，但每行都写一条 delete_child 审计（可追溯、可复盘）。
        """
        s = self.spec(entity)
        before = self.get(entity, entity_id)
        # 引用优先于状态：被下游用了，先告诉用户"谁在用它"，比"状态不允许"更有用
        self._assert_no_refs(self.references(entity, entity_id), "删除")
        status = before.get(s["status_field"]) if s["status_field"] else None
        if status in RUNNING_STATUSES:
            raise StateError(
                f"{self.describe(entity, before)} 状态为「{status}」，已在执行中，"
                f"不能删除；请先把下游单据处理完或改用「作废」")

        child_rows: Dict[str, List[Dict[str, Any]]] = {}
        for table, column, _label in s["children"]:
            child_rows[table] = self.db.query(
                f"SELECT * FROM {table} WHERE {column} = ?", (entity_id,))

        # 删除订单后回写来源询配码项目的下单状态，避免状态滞后
        code_project_id = before.get("source_code_project_id") if entity == "order" else None

        with self.db.tx():
            for table, rows in child_rows.items():
                for row in rows:
                    self.db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
                    self.db.log_action(entity, entity_id, "delete_child",
                                       before={"table": table, "row": row},
                                       operator=operator)
            self.db.execute(f"DELETE FROM {s['table']} WHERE id = ?", (entity_id,))
            self.db.log_action(entity, entity_id, "delete", before=before,
                               after={"deleted_children": {k: len(v) for k, v in child_rows.items()}},
                               operator=operator)
            if code_project_id and self.db.get_by_id("code_projects", code_project_id):
                from .partcode import PartCodeService
                PartCodeService(self.db).refresh_status(code_project_id)

        return {
            "entity": entity, "entity_id": entity_id,
            "description": self.describe(entity, before),
            "deleted_children": {k: len(v) for k, v in child_rows.items()},
        }

    # ── 审计 ────────────────────────────────────────────────

    def audit_trail(self, entity: Optional[str] = None,
                    entity_id: Optional[int] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: List[Any] = []
        if entity:
            sql += " AND entity = ?"
            params.append(entity)
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        for r in rows:
            r["action_label"] = ACTION_LABELS.get(r["action"], r["action"])
            r["entity_label"] = ENTITY_SPECS.get(r["entity"], {}).get("label", r["entity"])
        return rows

    def audit_entities(self) -> List[Dict[str, str]]:
        """审计筛选下拉：库里实际出现过的实体（带中文标签）"""
        rows = self.db.query(
            "SELECT entity, COUNT(*) AS n FROM audit_log GROUP BY entity ORDER BY n DESC")
        return [{"key": r["entity"],
                 "label": ENTITY_SPECS.get(r["entity"], {}).get("label", r["entity"]),
                 "count": r["n"]} for r in rows if r["entity"]]

    def audit_actions(self) -> List[Dict[str, str]]:
        rows = self.db.query(
            "SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY n DESC")
        return [{"key": r["action"],
                 "label": ACTION_LABELS.get(r["action"], r["action"]),
                 "count": r["n"]} for r in rows if r["action"]]

    def audit_page(self, entity: str = "", action: str = "", operator: str = "",
                   keyword: str = "", date_from: str = "", date_to: str = "",
                   offset: int = 0, limit: int = 200) -> Dict[str, Any]:
        """P1-9：审计日志查询（条件下推 SQL + 分页）"""
        where = ["1=1"]
        params: List[Any] = []
        if entity:
            where.append("entity = ?")
            params.append(entity)
        if action:
            where.append("action = ?")
            params.append(action)
        if operator:
            where.append("operator LIKE ?")
            params.append(f"%{operator}%")
        if keyword:
            where.append("(IFNULL(before_json,'') LIKE ? OR IFNULL(after_json,'') LIKE ?)")
            params += [f"%{keyword}%", f"%{keyword}%"]
        if date_from:
            where.append("substr(at,1,10) >= ?")
            params.append(date_from[:10])
        if date_to:
            where.append("substr(at,1,10) <= ?")
            params.append(date_to[:10])
        clause = " AND ".join(where)
        total = int(self.db.scalar(
            f"SELECT COUNT(*) FROM audit_log WHERE {clause}", tuple(params), default=0))
        rows = self.db.query(
            f"SELECT * FROM audit_log WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (int(limit), int(max(0, offset))))
        for r in rows:
            r["action_label"] = ACTION_LABELS.get(r["action"], r["action"])
            r["entity_label"] = ENTITY_SPECS.get(r["entity"], {}).get("label", r["entity"])
        return {"rows": rows, "total": total, "offset": int(offset),
                "limit": int(limit)}

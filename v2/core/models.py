#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域实体与状态机（v2.2，纯定义，不依赖数据库）

四域 + 订单枢纽：
  询报价/招投标（中标项目 → 生成一张内部订单 → 产生排产数量）
  询配码（独立域，明细数量为虚值，一项目可产生多张外部订单）
  订单（两种类型共用 qty_ordered 实值，排产唯一数量来源）
  生产排产（挂订单明细；可挂中标项目作来源标签，不携带数量）
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from .errors import StateError


# ── 状态常量 ────────────────────────────────────────────────

# 作废是独立通道（不进 TRANSITIONS 正常流转），四类主单据共用的终态值
VOID_STATUS = "作废"


class BidStatus:
    DRAFT = "草稿"
    QUOTED = "已报价"
    WON = "中标"
    LOST = "未中标"
    VOID = VOID_STATUS


class CodeProjectStatus:
    DRAFT = "草稿"
    SUBMITTED = "已提交"
    PARTIAL_ORDERED = "部分下单"
    ORDERED = "已下单"
    CLOSED = "关闭"
    VOID = VOID_STATUS


class CodeItemStatus:
    PENDING = "待下单"
    ORDERED = "已下单"


class OrderType:
    """订单两种来源，共用同一套 qty_ordered 实值与排产守恒规则"""
    EXTERNAL = "外部订单"   # 用户按询配码文件下单；一个询配码项目可产生多张
    INTERNAL = "内部订单"   # 中标项目生成；一个中标项目仅一张


class OrderStatus:
    """订单状态：系统流转值 + 真实销售订单表常用状态（2026-11-06 v2.3 扩充）"""
    DRAFT = "草稿"
    CONFIRMED = "已确认"
    WAIT_SHIP = "待发货"       # 真实销售订单表：待发货
    SCHEDULED = "已排产"
    RUNNING = "生产中"
    SHIPPING = "发货中"        # 真实销售订单表：发货中（历史数据里等价于「已发货」）
    # v3.3.0 交期提醒：把「已发货」补成正式状态，用户可直接把订单改为已发货
    # 以停止提醒；历史库里归一到「发货中」的单同样不再提醒（见 NO_REMIND）。
    SHIPPED = "已发货"
    DELIVERED = "已交付"
    ACCEPTED = "已验收"        # 真实销售订单表：已验收
    CLOSED = "关闭"
    VOID = VOID_STATUS

    # 可用于「新建订单 / 导入订单」显式指定的状态（作废只能走维护通道）
    CREATABLE = (DRAFT, CONFIRMED, WAIT_SHIP, SCHEDULED, RUNNING,
                 SHIPPING, SHIPPED, DELIVERED, ACCEPTED, CLOSED)

    # 发货及以后（含作废）不再做交期提醒：这是「一旦已发货就不再提醒」的单一口径。
    # 列表里保留「发货中」，让 2024~2025 年导入的历史单不会被当成待催交。
    NO_REMIND = (SHIPPING, SHIPPED, DELIVERED, ACCEPTED, CLOSED, VOID)


def is_order_status(value: str) -> bool:
    return str(value or "").strip() in OrderStatus.CREATABLE


class ProjectStatus:
    PENDING = "待排"
    SCHEDULED = "已排产"
    RUNNING = "生产中"
    DONE = "完工"
    DELIVERED = "已交付"
    VOID = VOID_STATUS


class ScheduleStatus:
    """订单排产单状态（v2.4）：审批文档编制进度，不参与数量守恒"""
    DRAFT = "待审批"
    COMPILED = "已编制"
    APPROVED = "已审批"
    ALL = (DRAFT, COMPILED, APPROVED)


TRANSITIONS: Dict[str, Dict[str, List[str]]] = {
    "bid_project": {
        BidStatus.DRAFT:  [BidStatus.QUOTED, BidStatus.VOID],
        BidStatus.QUOTED: [BidStatus.WON, BidStatus.LOST, BidStatus.VOID],
        BidStatus.WON:    [BidStatus.VOID],
        BidStatus.LOST:   [],
        BidStatus.VOID:   [],
    },
    "code_project": {
        CodeProjectStatus.DRAFT:           [CodeProjectStatus.SUBMITTED],
        CodeProjectStatus.SUBMITTED:       [CodeProjectStatus.PARTIAL_ORDERED,
                                           CodeProjectStatus.ORDERED, CodeProjectStatus.CLOSED],
        CodeProjectStatus.PARTIAL_ORDERED: [CodeProjectStatus.ORDERED, CodeProjectStatus.CLOSED],
        CodeProjectStatus.ORDERED:         [CodeProjectStatus.CLOSED],
        CodeProjectStatus.CLOSED:          [],
    },
    "order": {
        # 2026-11-06 v2.3：并入真实销售订单表的 待发货/发货中/已验收，
        # 原有流转全部保留（超集），不影响已确认 → 已排产等旧链路。
        OrderStatus.DRAFT:     [OrderStatus.CONFIRMED, OrderStatus.WAIT_SHIP,
                                OrderStatus.SHIPPED],
        OrderStatus.CONFIRMED: [OrderStatus.WAIT_SHIP, OrderStatus.SCHEDULED,
                                OrderStatus.SHIPPING, OrderStatus.SHIPPED,
                                OrderStatus.CLOSED],
        OrderStatus.WAIT_SHIP: [OrderStatus.RUNNING, OrderStatus.SHIPPING,
                                OrderStatus.SHIPPED, OrderStatus.DELIVERED,
                                OrderStatus.CLOSED],
        OrderStatus.SCHEDULED: [OrderStatus.RUNNING, OrderStatus.SHIPPING,
                               OrderStatus.SHIPPED, OrderStatus.CLOSED],
        OrderStatus.RUNNING:   [OrderStatus.SHIPPING, OrderStatus.SHIPPED,
                                OrderStatus.DELIVERED, OrderStatus.CLOSED],
        OrderStatus.SHIPPING: [OrderStatus.SHIPPED, OrderStatus.DELIVERED,
                                OrderStatus.ACCEPTED, OrderStatus.CLOSED],
        # v3.3.0：发运完成后仍可推进到交付 / 验收 / 关闭
        OrderStatus.SHIPPED:   [OrderStatus.DELIVERED, OrderStatus.ACCEPTED,
                                OrderStatus.CLOSED],
        OrderStatus.DELIVERED: [OrderStatus.ACCEPTED, OrderStatus.CLOSED],
        OrderStatus.ACCEPTED:  [OrderStatus.CLOSED],
        OrderStatus.CLOSED:    [],
    },
    "production_project": {
        ProjectStatus.PENDING:   [ProjectStatus.SCHEDULED],
        ProjectStatus.SCHEDULED: [ProjectStatus.RUNNING],
        ProjectStatus.RUNNING:   [ProjectStatus.DONE],
        ProjectStatus.DONE:      [ProjectStatus.DELIVERED],
        ProjectStatus.DELIVERED: [],
    },
}


def assert_transition(entity: str, old: str, new: str) -> None:
    allowed = TRANSITIONS.get(entity, {}).get(old, [])
    if new not in allowed:
        raise StateError(f"{entity} 状态不允许 {old} → {new}（允许：{allowed or '终态'}）")


def order_item_status_by_qty(ordered: float, allocated: float) -> str:
    """订单明细的排产进度（按实值数量推导）"""
    if allocated <= 0:
        return "待排产"
    if allocated >= ordered:
        return "已排满"
    return "部分排产"


# ── 实体 ────────────────────────────────────────────────────

@dataclass
class BidProject:
    id: Optional[int] = None
    doc_no: str = ""
    project_name: str = ""
    customer: str = ""
    tender_no: str = ""
    bid_date: str = ""
    status: str = BidStatus.DRAFT
    owner: str = ""
    currency: str = "CNY"
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class BidItem:
    id: Optional[int] = None
    bid_project_id: int = 0
    line_no: int = 1
    product_name: str = ""
    spec: str = ""
    category: str = ""
    unit: str = "件"
    qty: float = 1.0
    purchase_price: float = 0.0
    quote_ratio: float = 1.0
    is_won: str = ""
    image_path: str = ""
    drawing_path: str = ""
    legacy_product_id: Optional[int] = None
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def quote_price(self) -> float:
        """对外报价 = 采购价 ÷ 报价比率"""
        return self.purchase_price / self.quote_ratio if self.quote_ratio else 0.0


@dataclass
class CodeProject:
    """询配码项目：直接提交给用户的文件/清单，与询报价零关联"""
    id: Optional[int] = None
    doc_no: str = ""
    project_name: str = ""
    customer: str = ""
    status: str = CodeProjectStatus.DRAFT
    submit_date: str = ""
    file_path: str = ""
    version: str = ""
    owner: str = ""
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class CodeItem:
    """询配码明细项；qty_reference 是虚值，禁止参与任何数量校验"""
    id: Optional[int] = None
    code_project_id: int = 0
    line_no: int = 1
    item_code: str = ""
    item_name: str = ""
    spec: str = ""
    material: str = ""
    std_code: str = ""
    drawing_no: str = ""
    tech_requirement: str = ""
    unit: str = "件"
    qty_reference: float = 0.0
    status: str = CodeItemStatus.PENDING
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Order:
    id: Optional[int] = None
    doc_no: str = ""
    # v2.3 订单头 8 字段（customer / order_type / status / required_date 复用旧列）
    project_no: str = ""           # 订单项目编号
    amount_total: float = 0.0     # 价税合计
    item_qty_total: float = 0.0    # 物资数量 = 物资项目数（明细行数，Q12 2026-11-07 确认）
    erp_order_no: str = ""         # ERP订单编号
    order_type: str = OrderType.EXTERNAL
    source_code_project_id: Optional[int] = None   # 外部订单来源
    source_bid_project_id: Optional[int] = None    # 内部订单来源
    batch_seq: int = 1                             # 同一询配码项目下第几单
    customer: str = ""
    order_date: str = ""
    required_date: str = ""
    status: str = OrderStatus.DRAFT
    owner: str = ""
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class OrderItem:
    """订单明细：qty_ordered(数量) 是实值，排产数量的唯一锚点"""
    id: Optional[int] = None
    order_id: int = 0
    line_no: int = 1
    # v2.3 明细 9 字段（item_name / spec / unit / qty_ordered / unit_price 复用旧列）
    item_code: str = ""              # 物资代码
    production_name: str = ""        # 实际生产物资名称
    amount: float = 0.0              # 总价
    param_desc: str = ""             # 参数描述
    item_name: str = ""
    spec: str = ""
    material: str = ""
    unit: str = "件"
    qty_ordered: float = 0.0
    unit_price: float = 0.0
    source_code_item_id: Optional[int] = None      # 外部订单溯源
    source_bid_item_id: Optional[int] = None       # 内部订单溯源
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ProductionProject:
    id: Optional[int] = None
    doc_no: str = ""
    title: str = ""
    source_bid_project_id: Optional[int] = None
    status: str = ProjectStatus.PENDING
    plan_start: str = ""
    plan_end: str = ""
    actual_start: str = ""
    actual_end: str = ""
    workshop: str = ""
    owner: str = ""
    priority: int = 3
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ProductionLink:
    id: Optional[int] = None
    project_id: int = 0
    order_item_id: int = 0
    allocated_qty: float = 0.0
    order_no_snapshot: str = ""
    order_line_snapshot: str = ""
    order_type_snapshot: str = OrderType.EXTERNAL
    source_type_snapshot: str = ""
    item_name_snapshot: str = ""
    spec_snapshot: str = ""
    code_no_snapshot: str = ""
    item_code_snapshot: str = ""
    code_item_name_snapshot: str = ""
    qty_reference_snapshot: Optional[float] = None
    bid_no_snapshot: str = ""
    bid_line_snapshot: str = ""
    bid_item_name_snapshot: str = ""
    created_by: str = ""
    created_at: str = ""


@dataclass
class OrderSchedule:
    """订单排产单（v2.4）：一条订单明细一张；project_id/link_id 复用既有守恒链路"""
    id: Optional[int] = None
    doc_no: str = ""
    order_id: int = 0
    order_item_id: int = 0
    project_id: Optional[int] = None
    link_id: Optional[int] = None
    project_created: int = 0
    allocated_qty: float = 0.0
    status: str = ScheduleStatus.DRAFT
    status_before: str = ""
    remark: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class OrderScheduleFile:
    """订单排产附件：图片 / 图纸 / 审批文档"""
    id: Optional[int] = None
    schedule_id: int = 0
    kind: str = ""
    file_name: str = ""
    file_path: str = ""
    file_size: int = 0
    created_by: str = ""
    created_at: str = ""


@dataclass
class TraceNode:
    """穿透查询一行：排产 → 订单明细(实值) → 询配码项目/项(虚值)"""
    project_id: int = 0
    project_no: str = ""
    project_title: str = ""
    project_status: str = ""
    allocated_qty: float = 0.0
    order_item_id: int = 0
    order_no: str = ""
    order_type: str = OrderType.EXTERNAL
    batch_seq: int = 1
    item_name: str = ""
    spec: str = ""
    qty_ordered: float = 0.0
    code_item_id: Optional[int] = None
    code_no: str = ""
    item_code: str = ""
    qty_reference: Optional[float] = None
    bid_project_no: str = ""          # 排产项目挂的来源标签
    bid_project_name: str = ""
    source_bid_project_no: str = ""   # 内部订单溯源到的中标项目
    source_bid_line_no: int = 0
    extras: Dict[str, str] = field(default_factory=dict)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单字段规范（v2.3 · 单一来源 · 2026-11-06 张哥给定字段）

订单头 8 字段（顺序即模板列顺序）：
    订单项目编号 / 客户名称 / 订单类型 / 订单状态 / 价税合计 /
    交货日期 / 物资数量 / ERP订单编号

订单明细 9 字段：
    物资代码 / 物资名称 / 实际生产物资名称 / 规格 / 单位 /
    数量 / 单价 / 总价 / 参数描述

「新建订单」表单、「导入订单」模板/预览、「导出订单」表头、服务层建单
全部从这里取，避免各改一处导致列错位。

v2.9 追加「订单来源」orders.order_source：预设 招投标 / 北电电力 / 厦门龙净 /
京东平台 四值，控件可编辑（新渠道直接手输，落原文，不加 CHECK 约束）。
它不属于 17 列业务模板，属订单头附加属性；导入文件若带「订单来源」列会自动识别。

── 字段映射（业务字段 → 数据库列）──────────────────────────────
  订单项目编号 → orders.project_no      客户名称 → orders.customer
  订单类型     → orders.order_type      订单状态 → orders.status
  价税合计     → orders.amount_total    交货日期 → orders.required_date
  物资数量     → orders.item_qty_total  ERP订单编号 → orders.erp_order_no
  物资代码     → order_items.item_code 物资名称 → order_items.item_name
  实际生产物资名称 → order_items.production_name
  规格/单位/数量/单价 → spec / unit / qty_ordered / unit_price
  总价         → order_items.amount     参数描述 → order_items.param_desc

★ 口径不变（三条硬约束）：
  1. 「数量」唯一映射 qty_ordered【实值】，必须显式 > 0；
     绝不读取询配码 qty_reference 虚值；总价/价税合计/物资数量【不参与】数量守恒。
     「物资数量」按 Q12 定义为【物资项目数】（明细行数），与 qty_ordered 无关。
  2. 生产排产唯一数量锚点仍是 order_items.qty_ordered；
     排产溯源仍挂 order_item_id，与新增字段无关。
  3. 系统单号 doc_no（DD-/ND-）、来源外键、batch_seq 仍由系统生成，
     不属于 8 个业务字段，编辑/导入均不可覆盖。

── 导入文件结构 ──────────────────────────────────────────────
  每行 = 一张订单的一条明细；订单头字段在每一行重复填写。
  分组键优先级：
    ① 显式「订单分组」列（可选，兼容旧模板/手工合并标记）
    ② 「订单项目编号」相同 → 合并为一张订单（真实销售订单表即此结构）
    ③ 「ERP订单编号」相同 → 合并为一张订单（订单项目编号留空时）
    ④ 都留空 → 每行各自生成一张订单（单明细）
  分组键只是文件内的合并标记，系统单号仍由 codegen 生成。

── 派生字段规则 ──────────────────────────────────────────────
  明细「总价」留空 → 自动 = 数量 × 单价
  头「价税合计」留空（或组内各行不一致）→ 自动 = Σ 明细总价
★ Q12（2026-11-07 张哥已确认）：「物资数量」= 物资项目数（明细行数），
  头「物资数量」留空（或组内各行不一致）→ 自动 = len(明细)，**不是** Σ 明细数量；
  需要「Σ 明细数量」请用结果表「实值合计」列或 summary()['qty_ordered_real']。
  若文件没有「总价」列但有「价税合计」列（真实销售订单表），
  逐行把「价税合计」当作该行总价，头金额再按明细汇总。
  显式填写且组内一致的值视为订单级声明值，原样保留。

── 真实销售订单表兼容 ────────────────────────────────────────
  「销售订单」工作表 22 列可直接导入：
    订单编号→订单项目编号、客户名称、订单状态、交货日期、ERP订单编号、
    产品代码→物资代码、产品名称→物资名称、含税单价→单价、数量、
    价税合计→行总价、单位；订单类型缺省=外部订单。
  状态 待发货/发货中/已验收 已并入 models.OrderStatus.CREATABLE。
"""
import re
from datetime import date, datetime
from typing import Any, Dict, List, Sequence, Tuple

from . import fields as _fdefs
from . import models as _models

EXTERNAL = _models.OrderType.EXTERNAL
INTERNAL = _models.OrderType.INTERNAL

# ── 订单来源（v2.9·2026-11-07 张哥给定）────────────────────
# 这里是【唯一来源】：新建订单下拉、维护编辑、导入识别全部读它。
# 预设四值只是「常用项」，控件可编辑 —— 新渠道直接手输，落库为原文本，
# 数据库不加 CHECK 约束（否则每加一个渠道都要重建表）。
ORDER_SOURCES: Tuple[str, ...] = ("招投标", "北电电力", "厦门龙净", "京东平台")


def _f(key: str, label: str, *, kind: str = "text", required: bool = False,
       default: Any = "", aliases: Sequence[str] = (), sample: Any = "") -> Dict[str, Any]:
    return {
        "key": key, "label": label, "kind": kind, "required": required,
        "default": default, "aliases": [label, *aliases], "sample": sample,
    }


# ── 订单头 8 字段 ───────────────────────────────────────────
HEAD_FIELDS: List[Dict[str, Any]] = [
    _f("project_no", "订单项目编号",
       aliases=("订单编号", "项目编号", "销售订单编号", "业务订单编号",
                "订单号(业务)", "CG订单号", "订单项目号"),
       sample="CG20261106000001"),
    _f("customer", "客户名称",
       aliases=("需方单位", "客户", "需方", "用户", "买方", "客户单位", "订货单位"),
       sample="国能沈西热电厂"),
    _f("order_type", "订单类型", default=EXTERNAL,
       aliases=("类型", "订单类别", "销售订单类型"), sample=EXTERNAL),
    _f("status", "订单状态", default=_models.OrderStatus.DRAFT,
       aliases=("状态", "订单当前状态"), sample=_models.OrderStatus.CONFIRMED),
    _f("amount_total", "价税合计", kind="float", default=0.0,
       aliases=("含税合计", "金额合计", "合计金额", "总金额", "订单金额"),
       sample=155100.0),
    _f("required_date", "交货日期", kind="date",
       aliases=("交期", "交付日期", "要求交期", "建议交货日期", "交付时间"),
       sample="2026-12-20"),
    _f("item_qty_total", "物资数量", kind="float", default=0.0,
       # Q12：项数口径，别名只收「项数」语义；不再把「数量合计/总数量」误映射进来
       aliases=("物资项数", "物资项目数", "明细项数", "物料项数", "项数",
                "品种数", "物资总数"),
       sample=2),
    _f("erp_order_no", "ERP订单编号",
       aliases=("ERP订单号", "ERP编号", "ERP单号", "ERP销售订单号"),
       sample="4507723757"),
]

# ── 订单明细 9 字段 ─────────────────────────────────────────
ITEM_FIELDS: List[Dict[str, Any]] = [
    _f("item_code", "物资代码",
       aliases=("产品代码", "物料编码", "物资编码", "商品编码", "物料代码",
                "产品编码", "物资编号", "物料号"),
       sample="6001238991"),
    _f("item_name", "物资名称", required=True,
       aliases=("产品名称", "物料名称", "品名", "名称", "标准物资名称"),
       sample="耐磨弯头"),
    _f("production_name", "实际生产物资名称",
       aliases=("生产名称", "实际生产名称", "生产物资名称", "实际生产物料名称"),
       sample="内壁堆焊耐磨弯头 90°"),
    _f("spec", "规格", aliases=("规格型号", "型号", "规格及型号"),
       sample="90° DN250 S=8"),
    _f("unit", "单位", default="件", aliases=("计量单位", "单位名称"),
       sample="件"),
    _f("qty_ordered", "数量", kind="float", required=True,
       aliases=("订单数量", "订单数量(实值)", "订单实值", "下单数量",
                "实值数量", "订货数量", "物资数量(明细)"),
       sample=30),
    _f("unit_price", "单价", kind="float", default=0.0,
       aliases=("含税单价", "单价(元)", "单价（元）", "订单单价", "含税价格"),
       sample=1850.0),
    _f("amount", "总价", kind="float", default=0.0,
       aliases=("金额", "行金额", "明细金额", "行价税合计", "小计"),
       sample=55500.0),
    _f("param_desc", "参数描述", kind="textarea",
       aliases=("参数", "描述", "配置描述", "技术参数", "规格参数",
                "参数说明", "配置参数"),
       sample="R=1.5D，内壁堆焊耐磨层≥4mm，HRC≥58"),
]

ORDER_FIELDS: List[Dict[str, Any]] = HEAD_FIELDS + ITEM_FIELDS
ORDER_KEYS: List[str] = [f["key"] for f in ORDER_FIELDS]
HEAD_KEYS: Tuple[str, ...] = tuple(f["key"] for f in HEAD_FIELDS)
ITEM_KEYS: Tuple[str, ...] = tuple(f["key"] for f in ITEM_FIELDS)

# 可选列：不在 17 列业务模板里，但允许出现在文件/表单中（旧模板兼容 + 系统溯源）
EXTRA_FIELDS: List[Dict[str, Any]] = [
    _f("group_key", "订单分组",
       aliases=("分组", "组号", "批次", "订单序号", "订单组", "订单批次")),
    _f("order_date", "下单日期",
       aliases=("订单日期", "制单日期")),
    _f("owner", "负责人", aliases=("业务员", "制单人")),
    _f("remark", "备注", aliases=("订单备注", "说明")),
    _f("source_code_no", "来源询配码项目号",
       aliases=("询配码项目号", "询配码项目", "来源询配码", "XPM项目号", "XPM单号")),
    _f("source_bid_no", "来源中标项目号",
       aliases=("中标项目号", "中标项目", "招投标项目号", "ZB项目号", "ZB单号")),
    _f("source_code_item_no", "来源询配码项号",
       aliases=("询配码项号", "项号", "来源项", "来源明细项", "XPM项号")),
    _f("source_bid_line_no", "来源中标明细行号",
       aliases=("中标明细行", "中标行号", "中标明细行号")),
    # v2.9：订单来源（销售渠道）。不进 17 列业务模板，但导入文件带该列可识别
    _f("order_source", "订单来源",
       aliases=("来源渠道", "订单渠道", "销售渠道", "渠道", "来源平台", "订单来源渠道")),
]

ALL_FIELDS: List[Dict[str, Any]] = ORDER_FIELDS + EXTRA_FIELDS
EXTRA_KEYS: Tuple[str, ...] = tuple(f["key"] for f in EXTRA_FIELDS)

# 订单头中「组内必须一致」的字段；聚合列（金额/数量）单独处理，不报冲突
STABLE_HEAD_KEYS: Tuple[str, ...] = (
    "project_no", "customer", "order_type", "status", "required_date",
    "erp_order_no", "order_date", "owner", "remark",
    "source_code_no", "source_bid_no", "order_source",
)

STATUS_ALIASES: Dict[str, str] = {
    "已下单": _models.OrderStatus.DRAFT,
    "待确认": _models.OrderStatus.DRAFT,
    "未确认": _models.OrderStatus.DRAFT,
    "已审核": _models.OrderStatus.CONFIRMED,
    # v3.3.0：「已发货」已是正式状态，不再归一成「发货中」；
    # 历史文件里写「部分发货」仍按发货中处理（两者同属 NO_REMIND，都不再提醒）。
    "已发货": _models.OrderStatus.SHIPPED,
    "部分发货": _models.OrderStatus.SHIPPING,
    "完成": _models.OrderStatus.ACCEPTED,
    "已完工": _models.OrderStatus.ACCEPTED,
    "已结束": _models.OrderStatus.CLOSED,
}


def headers() -> List[str]:
    """导入模板 / 预览表头（严格 17 列，顺序与用户给定一致）"""
    return [f["label"] for f in ORDER_FIELDS]


def form_head_fields() -> List[Dict[str, Any]]:
    """「新建订单」表单用的订单头字段定义（服务层字段 key → 输入控件）"""
    types = {"float": "float", "textarea": "textarea", "date": "date"}
    out: List[Dict[str, Any]] = []
    for f in HEAD_FIELDS:
        spec: Dict[str, Any] = {
            "key": f["key"], "label": f["label"], "required": f["required"],
            "default": f["default"],
            "type": types.get(f["kind"], "text"),
        }
        if f["kind"] == "float":
            spec.update(decimals=2, minimum=0)
        if f["key"] == "order_type":
            spec["type"] = "combo"
            spec["options"] = [(EXTERNAL, EXTERNAL), (INTERNAL, INTERNAL)]
        if f["key"] == "status":
            spec["type"] = "combo"
            spec["options"] = [(s, s) for s in _models.OrderStatus.CREATABLE]
        out.append(spec)
    # v2.9：订单来源不在 8 个业务字段里（不占导入模板列），但属于订单头属性：
    # 可编辑下拉 = 四个预设 + 自由输入，取值口径见模块顶部 ORDER_SOURCES。
    out.append({"key": "order_source", "label": "订单来源", "required": False,
                "default": "", "type": "combo", "editable": True,
                "options": [("", "（未填）")] + [(s, s) for s in ORDER_SOURCES]})
    return out


def form_item_columns() -> List[Dict[str, Any]]:
    """「新建订单」明细表格的 9 列定义（key / 表头 / 宽度）"""
    widths = {"item_code": 130, "item_name": 170, "production_name": 170,
              "spec": 150, "unit": 70, "qty_ordered": 100,
              "unit_price": 100, "amount": 110, "param_desc": 200}
    return [{"key": f["key"], "label": f["label"] +
            (" *" if f["required"] else ""), "width": widths.get(f["key"], 120)}
            for f in ITEM_FIELDS]


def sample_rows() -> List[List[Any]]:
    """示例：同一订单项目编号两行明细 + 另一个项目编号一行明细"""
    row1 = [f["sample"] for f in ORDER_FIELDS]
    row2 = list(row1)
    row2[8] = "6001240603"          # 物资代码
    row2[9] = "U形波纹补偿器"        # 物资名称
    row2[10] = "U形波纹管补偿器 DN300"
    row2[11] = "DN300 70B125-2002"
    row2[13] = 20
    row2[14] = 4980.0
    row2[15] = 99600.0
    row2[16] = "单式轴向型，3 波，法兰连接"
    row3 = list(row1)
    row3[0] = "CG20261106000002"
    row3[1] = "国能台州电厂"
    row3[4] = 21000.0
    row3[6] = 1                     # Q12：第二张订单只有 1 项物资
    row3[7] = "4507723903"
    row3[8] = "6001239342"
    row3[9] = "带颈对焊法兰"
    row3[10] = "带颈对焊法兰 DN250 PN16"
    row3[11] = "DN250 PN16"
    row3[12] = "片"
    row3[13] = 50
    row3[14] = 420.0
    row3[15] = 21000.0
    row3[16] = "材质 12Cr1MoV，密封面 RF"
    return [row1, row2, row3]


# ── 表头归一化与解析 ────────────────────────────────────────

def _norm(name: Any) -> str:
    return _fdefs._norm(name)


def _alias_index() -> Dict[str, str]:
    idx: Dict[str, str] = {}
    for f in ALL_FIELDS:
        for alias in f["aliases"]:
            idx.setdefault(_norm(alias), f["key"])
    return idx


ALIAS_INDEX = _alias_index()


def resolve_headers(header_names: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """返回 (字段key → 实际表头名, 未识别表头名列表)"""
    mapping: Dict[str, str] = {}
    ignored: List[str] = []
    for h in header_names:
        key = ALIAS_INDEX.get(_norm(h))
        if key and key not in mapping:
            mapping[key] = h
        elif not key and str(h or "").strip():
            ignored.append(str(h))
    return mapping, ignored


def _code(value: Any) -> str:
    """物资代码/ERP编号可能是 Excel 数字，去掉 .0 尾巴"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _to_number(value: Any) -> Tuple[float, bool, str]:
    """→ (数值, 是否合法, 提示)。空/面议/脏数据都视为 0，由调用方决定致命与否"""
    if value in (None, ""):
        return 0.0, True, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), True, ""
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    text = re.sub(r"(元|件|台|套|个)$", "", text).strip()
    if text in ("", "面议", "待定", "-", "—", "/", "无"):
        return 0.0, False, f"填的是「{value}」"
    try:
        return float(text), True, ""
    except ValueError:
        return 0.0, False, f"「{value}」不是数字"


def _date(value: Any) -> Tuple[str, str]:
    """支持 YYYY-MM-DD / YYYY/M/D / Excel datetime / date"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d"), ""
    if isinstance(value, date):
        return value.isoformat(), ""
    text = str(value or "").strip()
    if not text:
        return "", ""
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", ""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", ""
    return text, f"日期「{text}」不是 YYYY-MM-DD 格式，已按原文保存"


def _order_type(value: Any) -> Tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return EXTERNAL, ""
    if "内部" in text or text.upper() == "ND":
        return INTERNAL, ""
    if ("外部" in text or "销售" in text or "用户" in text
            or text.upper() == "DD"):
        return EXTERNAL, ""
    return "", f"订单类型「{text}」无法识别（应为 外部订单 / 内部订单）"


def _status(value: Any) -> Tuple[str, str]:
    """真实销售订单表状态兼容；无法识别时降级为草稿并给出报告"""
    text = str(value or "").strip()
    if not text:
        return _models.OrderStatus.DRAFT, ""
    if text in _models.OrderStatus.CREATABLE:
        return text, ""
    if text in STATUS_ALIASES:
        mapped = STATUS_ALIASES[text]
        return mapped, f"订单状态「{text}」已归一为「{mapped}」"
    return (_models.OrderStatus.DRAFT,
            f"订单状态「{text}」无法识别，已按「{_models.OrderStatus.DRAFT}」处理")


def _effective_amount(item: Dict[str, Any]) -> float:
    return (float(item["amount"]) if item.get("amount") is not None
            else round(float(item["qty_ordered"]) * float(item["unit_price"]), 2))


def parse_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    CSV/Excel 行 → 订单分组 + 逐行报告。

    返回 {
      groups:  [{key, head, items, rows}]（可直接交给 OrderService.import_orders）
      issues:  [{row, level, reason, fatal}]  行号从 2 开始（首行表头）
      matched / ignored / mapping / item_count / group_count
    }
    """
    header_names: List[str] = [str(k) for k in rows[0].keys() if str(k).strip()] if rows else []
    mapping, ignored = resolve_headers(header_names)
    issues: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []

    missing = [k for k in ("item_name", "qty_ordered") if k not in mapping]
    if missing:
        labels = {"item_name": "物资名称", "qty_ordered": "数量"}
        issues.append({"row": 1, "level": "error", "fatal": True,
                       "reason": "表头缺少必需列：" +
                                 "、".join(labels[k] for k in missing) +
                                 "；请使用「下载订单导入模板」生成标准表头"})
        return {"groups": groups, "issues": issues, "matched": [],
                "ignored": ignored, "mapping": mapping,
                "item_count": 0, "group_count": 0}

    has_line_amount = "amount" in mapping
    has_head_amount = "amount_total" in mapping
    by_key: Dict[str, Dict[str, Any]] = {}

    def warn(line_no: int, reason: str) -> None:
        issues.append({"row": line_no, "level": "warn", "fatal": False,
                       "reason": reason})

    def fatal(line_no: int, reason: str) -> None:
        issues.append({"row": line_no, "level": "error", "fatal": True,
                       "reason": reason})

    for offset, raw in enumerate(rows):
        line_no = offset + 2
        get = lambda key: raw.get(mapping.get(key))                # noqa: E731

        name = str(get("item_name") or "").strip()
        qty, qty_ok, qty_msg = _to_number(get("qty_ordered"))
        if not name:
            fatal(line_no, "物资名称为空")
            continue
        if not qty_ok or qty <= 0:
            fatal(line_no, "数量(实值)必须 > 0" +
                  (f"（{qty_msg}）" if qty_msg else "") +
                  "；询配码参考数量是虚值，不能带入订单")
            continue

        otype, type_msg = _order_type(get("order_type"))
        if not otype:
            fatal(line_no, type_msg)
            continue
        status, status_msg = _status(get("status"))
        if status_msg:
            warn(line_no, status_msg)

        price, price_ok, price_msg = _to_number(get("unit_price"))
        if not price_ok:
            warn(line_no, f"单价{price_msg}，已按 0 处理")

        # 明细总价：优先「总价」列；留空 = 自动 数量×单价；
        # 真实销售订单表无此列时用行「价税合计」
        line_amount: Any = None
        raw_line_amount = get("amount") if has_line_amount else None
        if has_line_amount and raw_line_amount not in (None, ""):
            amt, amt_ok, amt_msg = _to_number(raw_line_amount)
            if not amt_ok:
                warn(line_no, f"总价{amt_msg}，已按 数量×单价 计算")
            else:
                line_amount = amt
        raw_head_amount = get("amount_total") if has_head_amount else None
        head_amount, head_amt_ok, _ = _to_number(raw_head_amount)
        if raw_head_amount in (None, ""):
            head_amount = None
        elif not head_amt_ok:
            warn(line_no, f"价税合计填的是「{raw_head_amount}」，该行按明细汇总")
            head_amount = None
        if line_amount is None and has_head_amount and head_amount is not None:
            line_amount = head_amount

        has_qty_total = "item_qty_total" in mapping
        raw_qty_total = get("item_qty_total") if has_qty_total else None
        qty_total, qty_total_ok, _ = _to_number(raw_qty_total)
        if raw_qty_total in (None, "") or not qty_total_ok:
            qty_total = None

        head: Dict[str, Any] = {"order_type": otype, "status": status,
                              "_status_given": "status" in mapping}
        for key in ("project_no", "customer", "erp_order_no", "owner", "remark",
                    "source_code_no", "source_bid_no", "order_source"):
            head[key] = (_code(get(key)) if key in ("project_no", "erp_order_no")
                         else str(get(key) or "").strip())
        for key in ("required_date", "order_date"):
            value, date_msg = _date(get(key))
            head[key] = value
            if date_msg:
                warn(line_no, date_msg)
        for key in ("amount_total", "item_qty_total"):
            head[key] = ""          # 先置空，分组结束后按一致性决定是否保留声明值

        if otype == INTERNAL and not head["source_bid_no"]:
            fatal(line_no, "内部订单必须填「来源中标项目号」（可选列）")
            continue
        if otype == EXTERNAL and head["source_bid_no"]:
            fatal(line_no, "外部订单不得填「来源中标项目号」；"
                           "内部订单请填订单类型=内部订单")
            continue
        if otype == INTERNAL and head["source_code_no"]:
            fatal(line_no, "内部订单不得来源于询配码项目")
            continue

        production_name = str(get("production_name") or "").strip() or name
        item: Dict[str, Any] = {
            "item_code": _code(get("item_code")),
            "item_name": name,
            "production_name": production_name,
            "spec": str(get("spec") or "").strip(),
            "unit": str(get("unit") or "").strip() or "件",
            "qty_ordered": qty,
            "unit_price": price,
            "amount": line_amount,
            "param_desc": str(get("param_desc") or "").strip(),
            "remark": str(get("remark") or "").strip(),
            "source_code_item_no": _code(get("source_code_item_no")),
            "source_bid_line_no": _code(get("source_bid_line_no")),
        }
        if item["source_code_item_no"] and not head["source_code_no"]:
            warn(line_no, "填了来源询配码项号但未填来源询配码项目号，来源项号已忽略")
            item["source_code_item_no"] = ""
        if otype == INTERNAL and item["source_code_item_no"]:
            item["source_code_item_no"] = ""
        if otype == EXTERNAL and item["source_bid_line_no"]:
            item["source_bid_line_no"] = ""

        group_key = _code(get("group_key"))
        if group_key:
            key = f"G|{otype}|{group_key}"
        elif head["project_no"]:
            key = f"P|{otype}|{head['project_no']}"
        elif head["erp_order_no"]:
            key = f"E|{otype}|{head['erp_order_no']}"
        else:
            key = f"P|{otype}|__row_{line_no}"
        group = by_key.get(key)
        if group is None:
            group = {
                "key": group_key or head["project_no"] or head["erp_order_no"]
                       or f"（第 {line_no} 行）",
                "head": head, "items": [], "rows": [],
                "_amounts": [], "_qtys": [],
            }
            by_key[key] = group
            groups.append(group)
        else:
            for hk in STABLE_HEAD_KEYS:
                a, b = head.get(hk), group["head"].get(hk)
                if a and b and a != b:
                    warn(line_no, f"订单头「{hk}」与同组首行不一致，已按首行「{b}」")
                    break
        group["items"].append(item)
        group["rows"].append(line_no)
        group["_amounts"].append(
            head_amount if (has_head_amount and has_line_amount
                            and head_amount is not None) else None)
        group["_qtys"].append(qty_total if has_qty_total else None)

    # 头聚合列：组内各行一致且非空 → 视为订单级声明值；否则交给服务层自动汇总
    for g in groups:
        amounts = [v for v in g.pop("_amounts")]
        qtys = [v for v in g.pop("_qtys")]
        if amounts and all(v is not None for v in amounts) and len(set(amounts)) == 1:
            g["head"]["amount_total"] = amounts[0]
        if qtys and all(v is not None for v in qtys) and len(set(qtys)) == 1:
            g["head"]["item_qty_total"] = qtys[0]

    return {
        "groups": groups, "issues": issues,
        "matched": list(mapping.values()), "ignored": ignored,
        "mapping": mapping,
        "item_count": sum(len(g["items"]) for g in groups),
        "group_count": len(groups),
    }


def preview_rows(groups: Sequence[Dict[str, Any]], limit: int = 50) -> List[List[Any]]:
    """按模板 17 列展开分组结果，供「导入订单」预览；派生列显示最终生效值"""
    out: List[List[Any]] = []
    for g in groups:
        head, items = g["head"], g["items"]
        amt = (head["amount_total"] if head["amount_total"] != ""
               else round(sum(_effective_amount(it) for it in items), 2))
        # Q12：物资数量 = 明细项数（行数），与 Σ qty_ordered 无关
        qty = (head["item_qty_total"] if head["item_qty_total"] != ""
               else float(len(items)))
        for it in items:
            out.append([
                head["project_no"], head["customer"], head["order_type"],
                head["status"], amt, head["required_date"], qty,
                head["erp_order_no"],
                it["item_code"], it["item_name"], it["production_name"],
                it["spec"], it["unit"], it["qty_ordered"], it["unit_price"],
                _effective_amount(it), it["param_desc"],
            ])
            if len(out) >= limit:
                return out
    return out


def issue_summary(issues: Sequence[Dict[str, Any]], limit: int = 12) -> str:
    return _fdefs.issue_summary(issues, limit)


# ── v3.5.3 两段式建单：选中订单后单独导入「明细 9 字段」────────
#
# 为什么单独做一套解析，而不复用 parse_rows()：
#   parse_rows() 的语义是「每行 = 一张订单的一条明细」，缺少订单项目编号时
#   会给每一行生成 __row_N 分组键 → 一张 30 行的物资表被拆成 30 张空壳订单
#   （2026-11-19 实测复现），且要求首行必须是表头。选中订单后导明细，
#   目标订单已经确定，根本不需要分组；这里只解析 9 列明细、不建单头。

def item_headers() -> List[str]:
    """订单明细导入模板 / 预览表头（严格 9 列）"""
    return [f["label"] for f in ITEM_FIELDS]


def item_sample_rows() -> List[List[Any]]:
    """两行示例：数量是实值，总价留空自动 = 数量 × 单价"""
    base = [f["sample"] for f in ITEM_FIELDS]
    second = list(base)
    second[0] = "6001240603"
    second[1] = "U形波纹补偿器"
    second[2] = "U形波纹管补偿器 DN300"
    second[3] = "DN300 70B125-2002"
    second[5] = 20
    second[6] = 4980.0
    second[7] = 99600.0
    second[8] = "单式轴向型，3 波，法兰连接"
    return [base, second]


def _item_mapping(header_names: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """明细视角的表头映射：在 resolve_headers() 之上补两条真实表格兜底。

    · 真实销售订单表的「价税合计 / 总金额」其实是【行金额】，不是订单头合计；
    · 只有明细列的物资表里「物资数量」就是数量。
    仅在对应明细列没命中时才接管，避免覆盖标准 9 列模板。
    """
    mapping, ignored = resolve_headers(header_names)
    if "amount" not in mapping and "amount_total" in mapping:
        mapping["amount"] = mapping.pop("amount_total")
    if "qty_ordered" not in mapping and "item_qty_total" in mapping:
        mapping["qty_ordered"] = mapping.pop("item_qty_total")
    return mapping, ignored


def locate_item_header(matrix: Sequence[Sequence[Any]],
                       max_scan: int = 12) -> Dict[str, Any]:
    """在前 max_scan 行里找真正的表头行（Excel 常有标题 / 说明 / 合并单元格）。

    优先选同时含「物资名称」与「数量」别名的行；找不到时返回匹配分最高的一行，
    让 parse_item_rows() 给出「表头缺少必需列」的明确诊断，而不是静默全表失败。
    """
    best: Dict[str, Any] = {"header_index": 0, "headers": [], "mapping": {},
                           "ignored": [], "matched": False, "scanned": 0}
    rows = list(matrix or [])
    if not rows:
        return best
    best_score = -1
    for i, raw in enumerate(rows[:max_scan]):
        headers = [str(h).strip() if h is not None else "" for h in raw]
        mapping, ignored = _item_mapping(headers)
        has_name, has_qty = "item_name" in mapping, "qty_ordered" in mapping
        score = (4 if has_name else 0) + (4 if has_qty else 0) + len(mapping)
        if score > best_score:
            best_score = score
            best = {"header_index": i, "headers": headers, "mapping": mapping,
                    "ignored": ignored,
                    "matched": bool(has_name and has_qty), "scanned": i + 1}
        if has_name and has_qty:
            return best
    return best


def parse_item_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """CSV/Excel 行 → 订单明细 + 逐行报告（不分组、不建订单头）。

    只需「物资名称」「数量」两列；其余 7 列缺失一律按默认值处理。
    返回 {items, issues, matched, ignored, mapping, item_count, row_count}。
    """
    header_names: List[str] = (
        [str(k) for k in rows[0].keys() if str(k).strip()] if rows else [])
    mapping, ignored = _item_mapping(header_names)
    issues: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []

    missing = [k for k in ("item_name", "qty_ordered") if k not in mapping]
    if missing:
        labels = {"item_name": "物资名称", "qty_ordered": "数量"}
        issues.append({"row": 1, "level": "error", "fatal": True,
                       "reason": "表头缺少必需列：" +
                                 "、".join(labels[k] for k in missing) +
                                 "；请使用「下载明细导入模板」，或检查表头行是否正确"})
        return {"items": items, "issues": issues, "matched": [],
                "ignored": ignored, "mapping": mapping,
                "item_count": 0, "row_count": len(rows)}

    def warn(line_no: int, reason: str) -> None:
        issues.append({"row": line_no, "level": "warn", "fatal": False,
                       "reason": reason})

    for offset, raw in enumerate(rows):
        line_no = offset + 2
        get = lambda key: raw.get(mapping.get(key))            # noqa: E731
        if not any(v not in (None, "") for v in raw.values()):
            continue                                          # 全空行静默跳过
        name = str(get("item_name") or "").strip()
        if re.match(r"^(合\s*计|总\s*计|小\s*计|总\s*额|共\s*计)", name):
            warn(line_no, f"「{name}」是汇总行，已跳过")
            continue
        qty, qty_ok, qty_msg = _to_number(get("qty_ordered"))
        if not name:
            issues.append({"row": line_no, "level": "error", "fatal": True,
                           "reason": "物资名称为空"})
            continue
        if not qty_ok or qty <= 0:
            issues.append({"row": line_no, "level": "error", "fatal": True,
                           "reason": "数量(实值)必须 > 0" +
                                     (f"（{qty_msg}）" if qty_msg else "")})
            continue

        price, price_ok, price_msg = _to_number(get("unit_price"))
        if not price_ok:
            warn(line_no, f"单价{price_msg}，已按 0 处理")
        amount: Any = None
        raw_amount = get("amount") if "amount" in mapping else None
        if raw_amount not in (None, ""):
            amt, amt_ok, amt_msg = _to_number(raw_amount)
            if amt_ok:
                amount = amt
            else:
                warn(line_no, f"总价{amt_msg}，已按 数量×单价 计算")

        items.append({
            "item_code": _code(get("item_code")),
            "item_name": name,
            "production_name": str(get("production_name") or "").strip(),
            "spec": str(get("spec") or "").strip(),
            "unit": str(get("unit") or "").strip() or "件",
            "qty_ordered": qty,
            "unit_price": price if price_ok else 0.0,
            "amount": amount,
            "param_desc": str(get("param_desc") or "").strip(),
        })

    return {"items": items, "issues": issues,
            "matched": list(mapping.values()), "ignored": ignored,
            "mapping": mapping, "item_count": len(items),
            "row_count": len(rows)}


def item_preview_rows(items: Sequence[Dict[str, Any]],
                       limit: int = 50) -> List[List[Any]]:
    """按 9 列展开明细，总价显示最终生效值（留空 = 数量 × 单价）"""
    out: List[List[Any]] = []
    for it in items[:limit]:
        out.append([
            it.get("item_code") or "", it.get("item_name") or "",
            it.get("production_name") or "", it.get("spec") or "",
            it.get("unit") or "", it.get("qty_ordered"),
            it.get("unit_price") or 0,
            (it.get("amount") if it.get("amount") is not None
             else _effective_amount(it)),
            it.get("param_desc") or "",
        ])
    return out

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
询配码明细字段规范（v2.2 · 单一来源）

张哥 2026-11-06 给定的 10 个业务字段，顺序即业务表格顺序：

    询价物料名称 / 商品编码 / 物料编码 / 物料名称 / 单位 /
    厂家价格(元) / 物料具体情况 / 参考数量 / 备注 / 技术要求

本模块是【唯一定义处】，服务层写入、界面表单、结果表列、导出表头、
CSV/Excel 导入表头映射全部从这里取，避免各改一处导致列错位。

★ 口径不变：
  - qty_reference（参考数量）仍是【虚值】，只统计，不参与下单/排产/守恒；
  - factory_price（厂家价格）只是询价信息，不是订单单价，
    订单金额与数量仍以 order_items.qty_ordered / unit_price 为准；
  - item_name（物料名称）是主名称，订单明细与溯源快照沿用此列，
    因此设为唯一必填；只填了「询价物料名称」时自动兜底填充。
  - 旧列 spec / material / std_code / drawing_no 保留（兼容历史数据与旧模板），
    不在 10 列主表单中展示，但仍可通过「编辑选中记录」维护。
"""
import re
from typing import Any, Dict, List, Sequence, Tuple

# ── 10 个业务字段（顺序不可随意调整）────────────────────────

def _f(key: str, label: str, *, kind: str = "text", required: bool = False,
       default: Any = "", aliases: Sequence[str] = (), sample: Any = "") -> Dict[str, Any]:
    return {
        "key": key, "label": label, "kind": kind, "required": required,
        "default": default, "aliases": [label, *aliases], "sample": sample,
    }


CODE_ITEM_FIELDS: List[Dict[str, Any]] = [
    _f("inquiry_item_name", "询价物料名称",
       aliases=("询价名称", "客户物料名称", "询价品名", "询价物资名称"),
       sample="90°耐磨弯头（用户询价名）"),
    _f("goods_code", "商品编码",
       aliases=("商品编号", "商品码", "商品号", "商城编码"),
       sample="SP-250-90"),
    _f("material_code", "物料编码",
       aliases=("物料编号", "物料号", "物资编码", "物资编号", "ERP物料编码"),
       sample="WL-003215"),
    _f("item_name", "物料名称", required=True,
       aliases=("物资名称", "物料名", "名称", "品名", "标准物料名称"),
       sample="耐磨弯头"),
    _f("unit", "单位", default="件",
       aliases=("计量单位", "单位名称"),
       sample="件"),
    _f("factory_price", "厂家价格(元)", kind="float", default=0.0,
       aliases=("厂家价格", "厂家价格（元）", "厂家价", "厂家单价", "厂家报价",
                "出厂价", "出厂价格", "供方价格"),
       sample=1850.0),
    _f("item_detail", "物料具体情况", kind="textarea",
       aliases=("物料情况", "具体情况", "物料描述", "物料说明", "规格型号",
                "规格及型号", "物资具体情况"),
       sample="90° DN250 S=8，内壁堆焊，R=1.5D"),
    _f("qty_reference", "参考数量", kind="float", default=0.0,
       aliases=("参考数量(虚值)", "参考数量（虚值）", "数量(虚值)",
                "虚值数量", "预估数量", "预计数量", "数量"),
       sample=100),
    _f("remark", "备注", kind="textarea",
       aliases=("备注说明", "说明", "注释"),
       sample="虚值仅统计，不对应订单数量"),
    _f("tech_requirement", "技术要求", kind="textarea",
       aliases=("技术条件", "技术要求说明", "技术标准", "技术要求/标准"),
       sample="内壁堆焊耐磨层 ≥ 4 mm，硬度 HRC≥58"),
]

# 兼容历史数据 / 旧导入模板的附加列（仍可写、可编辑，但不在 10 列主表单里）
LEGACY_FIELDS: List[Dict[str, Any]] = [
    _f("spec", "规格", aliases=("规格型号", "型号")),
    _f("material", "材质", aliases=("材料", "材质牌号")),
    _f("std_code", "执行标准", aliases=("标准号", "标准")),
    _f("drawing_no", "图号", aliases=("图纸编号", "图号/版本")),
]

ALL_FIELDS = CODE_ITEM_FIELDS + LEGACY_FIELDS
CODE_ITEM_KEYS: List[str] = [f["key"] for f in CODE_ITEM_FIELDS]
NUMERIC_KEYS = {f["key"] for f in ALL_FIELDS if f["kind"] == "float"}
WRITABLE_KEYS = {f["key"] for f in ALL_FIELDS}

# 系统锚点列：不允许由导入/编辑覆盖
SYSTEM_KEYS = {"id", "code_project_id", "line_no", "item_code", "status",
               "created_at", "updated_at"}


def headers() -> List[str]:
    """导入模板 / 导出询配码文件的表头（严格 10 列，顺序与用户给定一致）"""
    return [f["label"] for f in CODE_ITEM_FIELDS]


def sample_row() -> List[Any]:
    return [f["sample"] for f in CODE_ITEM_FIELDS]


def form_fields() -> List[Dict[str, Any]]:
    """ui/dialogs.py::FormDialog 用的字段定义（两列表单）"""
    out = []
    for f in CODE_ITEM_FIELDS:
        spec: Dict[str, Any] = {
            "key": f["key"], "label": f["label"], "required": f["required"],
            "default": f["default"],
            "type": "float" if f["kind"] == "float" else f["kind"],
        }
        if f["kind"] == "float":
            spec.update(decimals=3 if f["key"] == "qty_reference" else 2,
                        minimum=0)
        out.append(spec)
    return out


def export_rows(items: Sequence[Dict[str, Any]]) -> List[List[Any]]:
    """按 10 列顺序取数，供导出询配码文件使用"""
    rows = []
    for item in items:
        row = []
        for f in CODE_ITEM_FIELDS:
            v = item.get(f["key"])
            row.append("" if v is None else v)
        rows.append(row)
    return rows


# ── 表头归一化与解析 ────────────────────────────────────────

def _norm(name: Any) -> str:
    """去空白/尾部分隔符、全角括号转半角、英文转小写，便于别名匹配"""
    s = str(name or "").strip()
    s = s.strip("；;，,、:：|\t\r\n ")
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _alias_index() -> Dict[str, str]:
    idx: Dict[str, str] = {}
    for f in ALL_FIELDS:
        for alias in f["aliases"]:
            idx.setdefault(_norm(alias), f["key"])
    return idx


ALIAS_INDEX = _alias_index()


def resolve_headers(header_names: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """
    返回 (字段key → 实际表头名, 未识别表头名列表)。
    同一字段出现多个别名时取第一个；未识别列不影响导入。
    """
    mapping: Dict[str, str] = {}
    ignored: List[str] = []
    for h in header_names:
        key = ALIAS_INDEX.get(_norm(h))
        if key and key not in mapping:
            mapping[key] = h
        elif not key and str(h or "").strip():
            ignored.append(str(h))
    return mapping, ignored


def _to_number(value: Any, key: str) -> Tuple[float, str]:
    if value in (None, ""):
        return 0.0, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    text = re.sub(r"(元|件|台|套|个)$", "", text).strip()
    if text == "":
        return 0.0, ""
    if text in ("面议", "待定", "-", "—", "/", "无"):
        return 0.0, f"{key} 填的是「{value}」，已按 0 处理"
    try:
        return float(text), ""
    except ValueError:
        # 非致命：置 0 并在导入报告里提示，不丢整行
        return 0.0, f"{key} 填的是「{value}」，已按 0 处理"


def parse_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    解析 CSV/Excel 读取出来的 list[dict] → 可写库的行 + 逐行报告。

    返回 {
      items:    [字段字典]（只含可写字段，可直接传给 add_item / add_items_bulk）
      issues:   [{row, level, reason, fatal}]  行号从 2 开始（首行是表头）
      matched:  已识别表头名列表
      ignored:  未识别表头名列表
      mapping:  字段 key → 实际表头名
    }
    致命行（两个名称都空）跳过；数值异常只警告并按 0 落库。
    """
    header_names: List[str] = []
    if rows:
        header_names = [str(k) for k in rows[0].keys() if str(k).strip()]
    mapping, ignored = resolve_headers(header_names)
    items: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []

    if "item_name" not in mapping and "inquiry_item_name" not in mapping:
        issues.append({"row": 1, "level": "error", "fatal": True,
                       "reason": "表头里找不到「物料名称」或「询价物料名称」列；"
                                 "请使用「下载导入模板」生成标准表头"})
        return {"items": items, "issues": issues, "matched": [],
                "ignored": ignored, "mapping": mapping}

    for offset, raw in enumerate(rows):
        line_no = offset + 2
        item: Dict[str, Any] = {}
        for key, header in mapping.items():
            item[key] = raw.get(header)
        for key in WRITABLE_KEYS:
            item.setdefault(key, "" if key not in NUMERIC_KEYS else 0.0)

        # 空值归一化
        for key in WRITABLE_KEYS:
            if item[key] is None:
                item[key] = 0.0 if key in NUMERIC_KEYS else ""
        for key in ("qty_reference", "factory_price"):
            val, warn = _to_number(item.get(key), key)
            item[key] = val
            if warn:
                issues.append({"row": line_no, "level": "warn",
                               "fatal": False, "reason": warn})

        name = str(item.get("item_name") or "").strip()
        inquiry = str(item.get("inquiry_item_name") or "").strip()
        if not name and inquiry:
            item["item_name"] = inquiry
            issues.append({"row": line_no, "level": "warn", "fatal": False,
                           "reason": "物料名称为空，已用询价物料名称填充"})
        elif not name:
            issues.append({"row": line_no, "level": "error", "fatal": True,
                           "reason": "物料名称与询价物料名称均为空"})
            continue

        item["item_name"] = str(item["item_name"]).strip()
        if not str(item.get("unit") or "").strip():
            item["unit"] = "件"
        items.append(item)

    return {"items": items, "issues": issues, "matched": list(mapping.values()),
            "ignored": ignored, "mapping": mapping}


def issue_summary(issues: Sequence[Dict[str, Any]], limit: int = 12) -> str:
    """给导入对话框用的可读报告"""
    if not issues:
        return "全部数据行校验通过"
    lines = []
    for i in issues[:limit]:
        tag = "跳过" if i.get("fatal") else "提示"
        lines.append(f"第 {i['row']} 行 [{tag}] {i['reason']}")
    if len(issues) > limit:
        lines.append(f"…… 另有 {len(issues) - limit} 条，详见完整报告")
    return "\n".join(lines)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Excel / CSV 数据通道（v2.2 P1）

- 导出：优先 .xlsx（openpyxl，带表头样式/冻结/列宽）；无 openpyxl 或指定 .csv 时退 CSV(utf-8-sig)
- 导入：.xlsx / .xls(提示转换) / .csv / .tsv，首行表头 → list[dict]
- 模板：询配码明细、投标明细、订单三张可直接填的导入模板

★ 询配码明细「参考数量」是虚值，导入时只写 qty_reference，不参与任何数量校验。
★ 2026-11-06：询配码明细 10 字段由 core/fields.py 定义；
  订单头 8 字段 + 明细 9 字段由 core/order_fields.py 定义（v2.3）。
  本模块负责 CSV/Excel 读写与表头解析，不再自己维护列名清单。
"""
import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from . import fields as fdefs
from . import order_fields as ofdefs

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:                                    # 零依赖降级
    HAS_OPENPYXL = False

SheetData = Tuple[Sequence[str], Sequence[Sequence[Any]]]

# 模板列名 → 服务层字段（供导入映射）
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "code_item": {
        "title": "询配码明细导入模板",
        "headers": fdefs.headers(),            # 10 列，顺序与用户给定一致
        "fields": fdefs.CODE_ITEM_KEYS,
        "sample": fdefs.sample_row(),
    },
    "bid_item": {
        "title": "投标明细导入模板",
        "headers": ["产品名称", "规格", "类别", "单位", "数量", "采购单价",
                    "报价比率", "备注"],
        "fields": ["product_name", "spec", "category", "unit", "qty",
                   "purchase_price", "quote_ratio", "remark"],
        "sample": ["耐磨弯头", "DN250", "管件", "件", 12, 1800.0, 0.85, ""],
    },
    # 2026-11-06 v2.3：订单头 8 字段 + 明细 9 字段，每行 = 一张订单的一条明细
    "order": {
        "title": "订单导入模板",
        "headers": ofdefs.headers(),       # 17 列，顺序见 core/order_fields.py
        "fields": ofdefs.ORDER_KEYS,
        # 3 行示例：同一「订单项目编号」两行明细合并为一单 + 另一编号一单
        "samples": ofdefs.sample_rows(),
    },
    # v3.5.3：先建订单头，再选中订单导入明细 —— 只要 9 列，不需要订单头列
    "order_item": {
        "title": "订单明细导入模板",
        "headers": ofdefs.item_headers(),
        "fields": ofdefs.ITEM_KEYS,
        "samples": ofdefs.item_sample_rows(),
    },
}


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return str(value)
    return value


def export_workbook(path: str, sheets: Dict[str, SheetData]) -> str:
    """
    导出工作簿。sheets = {sheet名: (headers, rows)}；返回实际写入路径。
    .xlsx 且 openpyxl 可用 → 多 sheet；否则 → CSV（多 sheet 时按 sheet 名拆文件）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() in (".xlsx", ".xlsm") and HAS_OPENPYXL:
        wb = Workbook()
        wb.remove(wb.active)
        head_fill = PatternFill("solid", fgColor="DCE6F1")
        for name, (headers, rows) in sheets.items():
            ws = wb.create_sheet(title=_safe_sheet_name(name, wb.sheetnames))
            ws.append(list(headers))
            for row in rows:
                ws.append([_cell(v) for v in row])
            for c in ws[1]:
                c.font = Font(bold=True, color="1F3864")
                c.fill = head_fill
                c.alignment = Alignment(horizontal="center", vertical="center")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for idx, header in enumerate(headers, start=1):
                width = max(10, min(38, len(str(header)) * 2 + 4))
                for row in rows[:200]:
                    if idx - 1 < len(row):
                        width = max(width, min(38, len(str(_cell(row[idx - 1]))) + 4))
                ws.column_dimensions[get_column_letter(idx)].width = width
        wb.save(str(p))
        return str(p)

    written = []
    for name, (headers, rows) in sheets.items():
        target = p if len(sheets) == 1 else p.with_name(
            f"{p.stem}_{_safe_file(name)}{p.suffix or '.csv'}")
        if target.suffix.lower() not in (".csv", ".tsv"):
            target = target.with_suffix(".csv")
        delim = "\t" if target.suffix.lower() == ".tsv" else ","
        with target.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=delim)
            w.writerow(list(headers))
            for row in rows:
                w.writerow([_cell(v) for v in row])
        written.append(str(target))
    return written[0] if len(written) == 1 else "; ".join(written)


def _read_delimited(p: Path) -> List[Tuple[Any, ...]]:
    """
    读 CSV / TSV：自动识别编码与分隔符。
    编码优先 UTF-8(BOM) → UTF-8 → GB18030 → GBK → Big5，覆盖 Excel 另存的中文 CSV；
    分隔符用 csv.Sniffer 嗅探 , \\t ; |，失败时按扩展名兜底。
    """
    raw = p.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    sample = text[:8192]
    default = "\t" if p.suffix.lower() == ".tsv" else ","
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        delim = default
    # 嗅探器可能被中文内容里的逗号误导；表头里出现制表符时优先用制表符
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if "\t" in first_line:
        delim = "\t"
    return [tuple(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]


def _read_matrix(path: str, sheet: str | int = 0) -> List[Tuple[Any, ...]]:
    """读文件为原始二维行（不假设首行是表头）。

    v3.1.1：先过 cloudfile.ensure_readable()，兼容 OneDrive「仅联机」占位文件。
    v3.5.3：抽成公共读取层，供「首行表头」与「自动定位表头」两种解析共用。
    """
    from . import cloudfile
    p = Path(cloudfile.ensure_readable(path))
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        if not HAS_OPENPYXL:
            raise RuntimeError("读取 .xlsx 需要 openpyxl；请 pip install openpyxl 或另存为 .csv")
        wb = load_workbook(str(p), data_only=True, read_only=True)
        ws = wb[sheet] if isinstance(sheet, str) else wb.worksheets[sheet]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    elif p.suffix.lower() == ".xls":
        raise RuntimeError("旧版 .xls 请先另存为 .xlsx 或 .csv")
    else:
        rows = _read_delimited(p)
    return [tuple(r) for r in rows]


def import_raw_rows(path: str, sheet: str | int = 0) -> List[Tuple[Any, ...]]:
    """原始二维行（含标题行）；给「表头不在首行」的导入向导用"""
    return _read_matrix(path, sheet)


def import_rows(path: str, sheet: str | int = 0) -> List[Dict[str, Any]]:
    """读首个工作表：首行表头，其余为数据；值统一为 str / 数字原值"""
    rows = _read_matrix(path, sheet)
    if not rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    result = []
    for raw in rows[1:]:
        if not any(v not in (None, "") for v in raw):
            continue
        result.append({h: (raw[i] if i < len(raw) else None)
                       for i, h in enumerate(headers) if h})
    return result


def map_template_rows(kind: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    模板表头 → 服务层字段名。

    code_item 走 core/fields.py 的 10 字段解析（支持别名、编码与错行报告），
    保持旧签名：只返回成功行；需要完整报告请用 parse_code_item_file()。
    bid_item 仍按固定模板表头映射。
    """
    if kind == "code_item":
        return fdefs.parse_rows(rows)["items"]
    if kind == "order":
        # 兼容旧签名：把分组结果摊平成「订单头 + 明细」的行字典；
        # 向导与批量建单请用 parse_order_file()，那里才有完整分组与错行报告。
        rep = ofdefs.parse_rows(rows)
        return [dict(g["head"], **it)
                for g in rep["groups"] for it in g["items"]]
    tpl = TEMPLATES[kind]
    headers, fields = tpl["headers"], tpl["fields"]
    numeric = {"qty_reference", "qty", "purchase_price", "quote_ratio", "unit_price"}
    mapped = []
    for row in rows:
        item = {}
        for h, key in zip(headers, fields):
            value = row.get(h)
            if value in (None, ""):
                if key in ("unit", "qty", "purchase_price", "quote_ratio"):
                    continue
                value = 0.0 if key in numeric else ""
            if key in numeric:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = 0.0
            item[key] = value
        if not str(item.get("item_name") or item.get("product_name") or "").strip():
            continue
        mapped.append(item)
    return mapped


def parse_code_item_file(path: str, sheet: str | int = 0) -> Dict[str, Any]:
    """
    询配码明细文件 → 完整导入报告（供导入向导预览 / 测试使用）：
      {path, sheet, items, issues, matched, ignored, mapping, summary, raw_count}
    """
    rows = import_rows(path, sheet)
    report = fdefs.parse_rows(rows)
    report.update({
        "path": path,
        "sheet": sheet,
        "raw_count": len(rows),
        "summary": fdefs.issue_summary(report["issues"]),
    })
    return report


def parse_order_file(path: str, sheet: str | int = 0) -> Dict[str, Any]:
    """
    订单文件 → 完整导入报告（供「导入订单」向导预览 / 测试使用）：
      {path, sheet, groups, issues, matched, ignored, mapping,
       summary, raw_count, item_count, group_count}
    """
    rows = import_rows(path, sheet)
    report = ofdefs.parse_rows(rows)
    report.update({
        "path": path,
        "sheet": sheet,
        "raw_count": len(rows),
        "summary": ofdefs.issue_summary(report["issues"]),
    })
    return report


def parse_order_item_file(path: str, sheet: str | int = 0) -> Dict[str, Any]:
    """v3.5.3 订单明细文件 → 完整导入报告（供「导入订单明细」向导 / 测试）。

    与 parse_order_file() 的区别：**不分组、不建订单头**。目标订单已由用户在
    主表选中，全部明细行都落到这一张订单，从根本上避免「一张 30 行物资表
    被拆成 30 张空壳订单」。表头可在前 12 行任意位置（自动定位），只要求
    「物资名称」与「数量」两列，其余 7 列缺失按默认值处理。
    """
    matrix = import_raw_rows(path, sheet)
    loc = ofdefs.locate_item_header(matrix)
    hi = int(loc["header_index"])
    headers = loc["headers"]
    rows: List[Dict[str, Any]] = []
    for raw in matrix[hi + 1:]:
        if not any(v not in (None, "") for v in raw):
            continue
        rows.append({h: (raw[i] if i < len(raw) else None)
                    for i, h in enumerate(headers) if h})
    report = ofdefs.parse_item_rows(rows)
    for issue in report["issues"]:
        issue["row"] = int(issue["row"]) + hi      # 行号换算成文件里的真实行号
    if hi:
        report["issues"].insert(0, {
            "row": hi + 1, "level": "info", "fatal": False,
            "reason": f"表头位于第 {hi + 1} 行，已跳过前 {hi} 行标题 / 说明"})
    report.update({
        "path": path, "sheet": sheet,
        "header_row": hi + 1,
        "header_found": bool(loc["matched"]),
        "skipped_preamble_rows": hi,
        "raw_count": len(rows),
        "summary": ofdefs.issue_summary(report["issues"]),
    })
    return report


def export_template(path: str, kind: str) -> str:
    if kind not in TEMPLATES:
        raise ValueError(f"未知模板：{kind}")
    tpl = TEMPLATES[kind]
    rows = tpl.get("samples") or [tpl["sample"]]
    return export_workbook(path, {tpl["title"]: (tpl["headers"], rows)})


def _safe_sheet_name(name: str, used: List[str]) -> str:
    clean = "".join(c for c in str(name) if c not in "[]:*?/\\")[:28] or "Sheet"
    base, i = clean, 1
    while clean in used:
        i += 1
        clean = f"{base[:25]}_{i}"
    return clean


def _safe_file(name: str) -> str:
    return "".join(c for c in str(name) if c not in r'\/:*?"<>|')[:24]


def backend_info() -> str:
    return ("openpyxl 可用：支持 .xlsx 多工作表导入导出"
            if HAS_OPENPYXL else "openpyxl 未安装：仅支持 CSV/TSV（Excel 可直接打开）")

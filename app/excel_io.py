#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Excel 导入 / 导出（openpyxl）
导出：动态列宽 + 合计行 + 样式
导入：按中文表头映射字段，安全校验后批量插入
"""
from typing import List, Dict, Any, Optional

import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.utils import get_column_letter

from . import calc


# 导出表头（顺序即列顺序）
EXPORT_HEADERS = [
    "产品名称", "规格型号", "产品分类", "对外报价(元)",
    "报价比率", "询价数量", "询价单位", "采购价格(元)",
    "投标项目", "是否中标", "报价人", "产品描述", "创建时间", "更新时间",
]

# 导入列：中文表头 -> 数据库字段（允许多别名）
IMPORT_HEADER_MAP = {
    "产品名称": "name",
    "name": "name",
    "规格型号": "spec",
    "spec": "spec",
    "产品分类": "category",
    "category": "category",
    "报价比率": "quote_ratio",
    "quote_ratio": "quote_ratio",
    "询价数量": "inquiry_qty",
    "inquiry_qty": "inquiry_qty",
    "询价单位": "inquiry_unit",
    "inquiry_unit": "inquiry_unit",
    "采购价格": "purchase_price",
    "采购价格(元)": "purchase_price",
    "purchase_price": "purchase_price",
    "投标项目": "project",
    "project": "project",
    "是否中标": "is_won",
    "is_won": "is_won",
    "报价人": "quoter",
    "quoter": "quoter",
    "产品描述": "description",
    "description": "description",
}

NUMERIC_FIELDS = {"quote_ratio", "inquiry_qty", "purchase_price"}


def _style_header(ws, n_cols: int):
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment


def _auto_width(ws, n_cols: int, min_w: float = 10.0, max_w: float = 40.0):
    for col in range(1, n_cols + 1):
        letter = get_column_letter(col)
        longest = 0
        for cell in ws[letter]:
            if cell.value is not None:
                # 中文按 ~1.8 倍宽度估算
                text = str(cell.value)
                width = sum(1.8 if ord(ch) > 127 else 1 for ch in text)
                longest = max(longest, width)
        ws.column_dimensions[letter].width = max(min_w, min(max_w, longest + 3))


def _product_row(product: Dict[str, Any]) -> List[Any]:
    purchase_price = calc.safe_float(product.get('purchase_price'))
    quote_ratio = calc.safe_float(product.get('quote_ratio'), 1.0)
    qp = purchase_price / quote_ratio if quote_ratio else 0.0
    return [
        product.get('name', ''),
        product.get('spec', ''),
        product.get('category', ''),
        round(qp, 2),
        quote_ratio,
        calc.safe_int(product.get('inquiry_qty')),
        product.get('inquiry_unit', '') or '',
        round(purchase_price, 2),
        product.get('project', ''),
        product.get('is_won', '') or '',
        product.get('quoter', ''),
        product.get('description', ''),
        product.get('created_at', ''),
        product.get('updated_at', ''),
    ]


def export_products(products: List[Dict[str, Any]], file_path: str,
                    sheet_title: str = "产品数据") -> None:
    """导出产品列表，含合计行"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title

    for col, header in enumerate(EXPORT_HEADERS, 1):
        ws.cell(row=1, column=col, value=header)
    _style_header(ws, len(EXPORT_HEADERS))

    for row, product in enumerate(products, 2):
        values = _product_row(product)
        for col, val in enumerate(values, 1):
            ws.cell(row=row, column=col, value=val)

    # 合计行
    if products:
        summary = calc.compute_summary(products)
        total_row = len(products) + 2
        ws.cell(row=total_row, column=1, value="合计")
        ws.cell(row=total_row, column=4, value=round(summary['total_quote'], 2))
        ws.cell(row=total_row, column=6, value=summary['count'])
        ws.cell(row=total_row, column=8, value=round(summary['total_purchase'], 2))
        for col in range(1, len(EXPORT_HEADERS) + 1):
            ws.cell(row=total_row, column=col).font = Font(bold=True)

    _auto_width(ws, len(EXPORT_HEADERS))
    wb.save(file_path)


def export_project_summary(groups: List[Dict[str, Any]], file_path: str) -> None:
    """导出投标项目汇总"""
    headers = ["投标项目", "产品数量", "采购总额(元)", "报价总额(元)", "毛利(元)", "毛利率(%)"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "投标项目汇总"

    for col, header in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=header)
    _style_header(ws, len(headers))

    for row, g in enumerate(groups, 2):
        ws.cell(row=row, column=1, value=g['project'])
        ws.cell(row=row, column=2, value=g['count'])
        ws.cell(row=row, column=3, value=round(g['total_purchase'], 2))
        ws.cell(row=row, column=4, value=round(g['total_quote'], 2))
        ws.cell(row=row, column=5, value=round(g['profit'], 2))
        ws.cell(row=row, column=6, value=round(g['profit_rate'], 2))

    if groups:
        total_row = len(groups) + 2
        tp = sum(g['total_purchase'] for g in groups)
        tq = sum(g['total_quote'] for g in groups)
        profit = tq - tp
        rate = (profit / tq * 100) if tq else 0.0
        ws.cell(row=total_row, column=1, value="总计")
        ws.cell(row=total_row, column=2, value=sum(g['count'] for g in groups))
        ws.cell(row=total_row, column=3, value=round(tp, 2))
        ws.cell(row=total_row, column=4, value=round(tq, 2))
        ws.cell(row=total_row, column=5, value=round(profit, 2))
        ws.cell(row=total_row, column=6, value=round(rate, 2))
        for col in range(1, len(headers) + 1):
            ws.cell(row=total_row, column=col).font = Font(bold=True)

    _auto_width(ws, len(headers))
    wb.save(file_path)


def import_products(db, file_path: str) -> Dict[str, Any]:
    """从 Excel 批量导入。返回 {inserted, skipped, errors:[...]}"""
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"inserted": 0, "skipped": 0, "errors": ["文件为空"]}

    header_cells = rows[0]
    # 建立 列索引 -> 字段名 映射
    col_to_field = {}
    for idx, h in enumerate(header_cells):
        if h is None:
            continue
        key = str(h).strip()
        if key in IMPORT_HEADER_MAP:
            col_to_field[idx] = IMPORT_HEADER_MAP[key]

    if not col_to_field:
        return {"inserted": 0, "skipped": 0, "errors": ["未找到可识别的表头（需含：产品名称/规格型号/采购价格 等）"]}

    inserted = 0
    skipped = 0
    errors = []

    for r_i, row in enumerate(rows[1:], start=2):
        record = {}
        for idx, field in col_to_field.items():
            value = row[idx] if idx < len(row) else None
            if value is None:
                value = ''
            record[field] = value

        name = str(record.get('name', '')).strip()
        # 跳过导出文件中的合计/小计/总计行，避免被当作产品导入
        if name in ("合计", "小计", "总计"):
            skipped += 1
            continue
        if not name:
            skipped += 1
            errors.append(f"第 {r_i} 行：缺少产品名称，已跳过")
            continue

        product_data = {
            'name': name,
            'spec': str(record.get('spec', '') or '').strip(),
            'category': str(record.get('category', '') or '').strip(),
            'quote_ratio': calc.safe_float(record.get('quote_ratio'), 0.85),
            'inquiry_qty': calc.safe_int(record.get('inquiry_qty'), 0),
            'purchase_price': calc.safe_float(record.get('purchase_price'), 0.0),
            'project': str(record.get('project', '') or '').strip(),
            'is_won': str(record.get('is_won', '') or '').strip(),
            'inquiry_unit': str(record.get('inquiry_unit', '') or '').strip(),
            'quoter': str(record.get('quoter', '') or '').strip(),
            'description': str(record.get('description', '') or '').strip(),
            'image_path': None,
            'drawing_path': None,
        }
        try:
            db.add_product(product_data)
            inserted += 1
        except Exception as e:  # noqa: BLE001
            skipped += 1
            errors.append(f"第 {r_i} 行：写入失败（{e}）")

    return {"inserted": inserted, "skipped": skipped, "errors": errors}

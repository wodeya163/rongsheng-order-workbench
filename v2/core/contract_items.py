#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采购合同明细表解析 + 物资自动分配建议（v3.5.1）。

为什么单独做这个模块：
  core/contract_amount.py 的 `_iter_lines()` 对 .doc 是「OLE 二进制扫描有结果
  就直接返回」，扫出来的只有抬头 / 总额 / 地址，Word 表格里的物资明细永远读不到；
  而且它的 COM 通道只把 `table.Range.Text` 拍平成文本行，丢掉了行列结构。
  自动分配必须拿到【结构化表格】（物资名称 / 数量 / 单价 / 总价），
  所以这里独立走 COM 的 Tables 逐单元格读取，不复用 _iter_lines。

硬口径（不破坏 v3.5.0 拍板的三条口径）：
  · 只产出【建议】，绝不写库；写库仍由 set_contract_items() 在用户点「保存分配」时完成。
  · 名称全称对不上时靠「类型关键词 + 规格数字 + 数量 + 剩余可采购量」打分，
    低于阈值的一律列 unmatched，不猜、不摊、不按 0 成本填。
  · 一份合同对同一订单明细只能有一行分配（UNIQUE 约束），
    多行命中同一明细时合并数量与金额、按加权平均反算单价。
  · 采购数量受「剩余可采购数量」硬约束，超出即截断并告警。
  · Σ 合同明细金额与合同台账金额对账，偏差只告警不阻断。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import money

WORD_EXTS = {".doc", ".docx", ".docm", ".wps", ".wpt", ".rtf"}
BOOK_EXTS = {".xlsx", ".xlsm", ".xls", ".et"}
TEXT_EXTS = {".csv", ".tsv", ".txt"}
SUPPORTED_EXTS = WORD_EXTS | BOOK_EXTS | TEXT_EXTS

# 表头别名（规范化后做子串匹配）。「单价」与「总价 / 金额」分开，避免串列。
HEADER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "name": ("物资名称", "产品名称", "品名", "货物名称", "商品名称", "物料名称", "名称"),
    "spec": ("规格型号", "规格", "型号", "材质规格"),
    "qty": ("采购数量", "订购数量", "数量", "订货数"),
    "unit_price": ("含税单价", "采购单价", "不含税单价", "单价"),
    "amount": ("价税合计", "总价", "总金额", "金额", "合计", "小计"),
    "unit": ("单位",),
    "code": ("商品编码", "物料编码", "物料号", "编码", "料号"),
}

# 物资类型核心词：长的在前，先命中先返回，避免「弯管 / 弯头」「管件」泛化误判。
TYPE_WORDS: Tuple[str, ...] = (
    "大小头", "异径管", "补偿器", "膨胀节", "波纹管", "补偿节",
    "弯头", "弯管", "三通", "四通", "法兰", "直管", "阀门",
    "管件", "接头", "衬板", "管座", "管托", "泵", "电机",
)

# 明确不能作为物资明细行的标志
TOTAL_MARKS = ("总计", "合计", "小计", "人民币", "大写", "价税合计", "金额合计")

MATCH_THRESHOLD = 40          # 低于此分不自动勾选
HIGH_CONFIDENCE = 60          # 达到此分标「高置信」


def _clean(value: Any) -> str:
    """Word 单元格尾部的 \\r\\x07 是单元格结束符，必须清掉。"""
    text = "" if value is None else str(value)
    text = text.replace("\r", "").replace("\x07", "").replace("\x01", "")
    return re.sub(r"\s+", " ", text).strip()


def _num(value: Any) -> float:
    text = _clean(value)
    text = re.sub(r"[,，\s元¥￥￥]", "", text)
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return 0.0
    try:
        return float(m.group())
    except ValueError:
        return 0.0


def _norm_header(cell: str) -> str:
    return re.sub(r"[\s（）()【】\[\]:：、/·元¥￥%％]", "", _clean(cell))


def _header_map(row: Sequence[str]) -> Dict[str, int]:
    found: Dict[str, int] = {}
    for i, cell in enumerate(row):
        c = _norm_header(cell)
        if not c:
            continue
        for key, aliases in HEADER_ALIASES.items():
            if key in found:
                continue
            if any(a in c for a in aliases):
                found[key] = i
                break
    return found


def _type_of(name: str) -> str:
    for w in TYPE_WORDS:
        if w in name:
            return w
    return ""


def _spec_numbers(text: str) -> set:
    """抓 DN254 / 内径254 / 单独的三四位数，作为规格弱线索。"""
    nums = {int(x) for x in re.findall(r"(?:DN|dn|内径|Φ|φ)\s*(\d{2,4})", text)}
    nums |= {int(x) for x in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", text)}
    return nums


@dataclass
class ContractLine:
    name: str = ""
    spec: str = ""
    unit: str = ""
    qty: float = 0.0
    unit_price: float = 0.0
    amount: float = 0.0
    code: str = ""
    row_no: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "spec": self.spec, "unit": self.unit,
            "qty": self.qty, "unit_price": self.unit_price,
            "amount": self.amount, "code": self.code, "row_no": self.row_no,
        }


@dataclass
class ParseResult:
    lines: List[ContractLine] = field(default_factory=list)
    source: str = ""
    warnings: List[str] = field(default_factory=list)
    table_index: int = 0

    @property
    def total(self) -> float:
        return float(money.sum2(x.amount for x in self.lines))

    @property
    def ok(self) -> bool:
        return bool(self.lines)


# ── 结构化表格读取 ──────────────────────────────────────────

def _tables_from_word_com(path: Path) -> List[List[List[str]]]:
    """Word/WPS COM：逐表逐单元格读，保留行列结构。"""
    import win32com.client as win32                        # 延迟导入

    app = None
    doc = None
    try:
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        try:
            app.DisplayAlerts = 0
        except Exception:
            pass
        doc = app.Documents.Open(str(path.resolve()), ReadOnly=True,
                                 AddToRecentFiles=False)
        out: List[List[List[str]]] = []
        for ti in range(1, doc.Tables.Count + 1):
            table = doc.Tables(ti)
            rows: List[List[str]] = []
            for ri in range(1, table.Rows.Count + 1):
                cells: List[str] = []
                for ci in range(1, table.Columns.Count + 1):
                    try:
                        cells.append(_clean(table.Cell(ri, ci).Range.Text))
                    except Exception:                  # 合并单元格取不到
                        cells.append("")
                rows.append(cells)
            out.append(rows)
        return out
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass


def _tables_from_docx(path: Path) -> List[List[List[str]]]:
    from docx import Document                            # 延迟导入

    doc = Document(str(path))
    return [[[_clean(c.text) for c in row.cells] for row in t.rows]
            for t in doc.tables]


def _tables_from_workbook(path: Path) -> List[List[List[str]]]:
    from openpyxl import load_workbook                     # 延迟导入

    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        return [[[_clean(c) for c in row] for row in ws.iter_rows(values_only=True)]
                for ws in wb.worksheets]
    finally:
        wb.close()


def _table_from_text(path: Path) -> List[List[List[str]]]:
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = [[_clean(c) for c in row] for row in csv.reader(fh, delimiter=delim)]
    return [rows]


def _read_tables(path: Path) -> Tuple[List[List[List[str]]], str]:
    ext = path.suffix.lower()
    if ext in (".docx", ".docm"):
        try:
            t = _tables_from_docx(path)
            if t:
                return t, "python-docx 表格"
        except Exception:
            pass
    if ext in (".xlsx", ".xlsm"):
        try:
            t = _tables_from_workbook(path)
            if t:
                return t, "openpyxl 工作表"
        except Exception:
            pass
    if ext in TEXT_EXTS:
        return _table_from_text(path), "CSV/TSV 文本"
    # .doc / .wps / .rtf / .xls：优先 Office COM（OLE 拿不到表格结构）
    try:
        t = _tables_from_word_com(path)
        if t:
            return t, "Office COM 表格"
    except Exception as exc:
        raise RuntimeError(
            f"无法读取 {path.name} 的表格：{type(exc).__name__}；"
            "旧版 .doc 需要本机安装 Word 或 WPS") from exc
    return [], "Office COM 表格"


def _parse_table(rows: List[List[str]]) -> Optional[Tuple[Dict[str, int], List[ContractLine]]]:
    """在一张表里找表头行，解析出物资明细。"""
    header_at = -1
    hmap: Dict[str, int] = {}
    for i, row in enumerate(rows[:6]):
        m = _header_map(row)
        # 必须有名称 + 数量 + （单价 或 总价），否则不是采购明细表
        if "name" in m and "qty" in m and ("unit_price" in m or "amount" in m):
            header_at, hmap = i, m
            break
    if header_at < 0:
        return None

    def cell(row: List[str], key: str) -> str:
        idx = hmap.get(key)
        return row[idx] if idx is not None and idx < len(row) else ""

    out: List[ContractLine] = []
    for row_no, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        joined = "".join(row)
        if not joined.strip():
            continue
        if any(mark in joined for mark in TOTAL_MARKS):
            continue
        name = cell(row, "name")
        qty = _num(cell(row, "qty"))
        # 无名称或非数值数量的行（如技术描述表）跳过
        if not name or qty <= 0:
            continue
        price = _num(cell(row, "unit_price")) if "unit_price" in hmap else 0.0
        amount = _num(cell(row, "amount")) if "amount" in hmap else 0.0
        if amount <= 0 and price > 0:
            amount = float(money.q2(money.mul(qty, price)))
        if price <= 0 and amount > 0:
            price = float(money.q2(amount / qty))
        out.append(ContractLine(
            name=name, spec=cell(row, "spec"), unit=cell(row, "unit"),
            qty=float(money.q4(qty)), unit_price=float(money.q4(price)),
            amount=float(money.q4(amount)), code=cell(row, "code"),
            row_no=row_no))
    return (hmap, out)


def read_contract_items(path: str | Path) -> ParseResult:
    """从合同文档读结构化采购明细行；失败给诊断，绝不抛到 UI 之外。"""
    p = Path(path)
    res = ParseResult()
    if not p.is_file():
        res.warnings.append(f"附件不存在：{p}")
        return res
    if p.suffix.lower() not in SUPPORTED_EXTS:
        res.warnings.append(f"暂不支持的文件类型：{p.suffix}")
        return res
    try:
        tables, source = _read_tables(p)
    except Exception as exc:
        res.source = "解析失败"
        res.warnings.append(str(exc))
        return res
    res.source = source
    for idx, rows in enumerate(tables, start=1):
        parsed = _parse_table(rows)
        if parsed and parsed[1]:
            res.table_index = idx
            res.lines = parsed[1]
            break
    if not res.lines:
        res.warnings.append(
            "文档里没找到含「物资名称 / 数量 / 单价（或总价）」的明细表；"
            "旧版 .doc 请确认本机已安装 Word 或 WPS。")
        return res
    no_price = [x for x in res.lines if x.unit_price <= 0]
    if no_price:
        res.warnings.append(
            f"{len(no_price)} 行没有可识别的单价或总价，这些行不会被自动勾选，需手工填写。")
    return res


# ── 匹配打分 ────────────────────────────────────────────────

def _score(line: ContractLine, cand: Dict[str, Any],
           candidate_count: int) -> Tuple[int, str]:
    """→ (分数, 命中理由)。名称全称对不上是常态，靠类型词 + 数量 + 规格。"""
    score = 0
    reasons: List[str] = []
    cname = str(cand.get("item_name") or "")
    a = _clean(line.name)
    if a and cname:
        if a == cname:
            score += 60
            reasons.append("名称完全一致")
        elif a in cname or cname in a:
            score += 35
            reasons.append("名称包含")
    ta, tc = _type_of(a), _type_of(cname)
    if ta and tc:
        if ta == tc:
            score += 40
            reasons.append(f"同为「{ta}」")
        else:
            score -= 60                       # 类型冲突，强排除
            reasons.append(f"类型冲突（{ta}≠{tc}）")
    sa = _spec_numbers(line.name + " " + line.spec)
    sc = _spec_numbers(cname + " " + str(cand.get("spec") or ""))
    if sa & sc:
        score += 15
        reasons.append("规格数字吻合")
    qo = float(cand.get("qty_ordered") or 0)
    if abs(line.qty - qo) < 1e-6:
        score += 12
        reasons.append("数量一致")
    if line.unit and str(cand.get("unit") or "").strip() == line.unit.strip():
        score += 4
        reasons.append("单位一致")
    remain = float(cand.get("remaining_qty") or 0)
    if line.qty <= remain + 1e-9:
        score += 6
    else:
        score -= 20
        reasons.append("超出剩余可采购量")
    if candidate_count == 1:
        score += 10
        reasons.append("该订单仅此一项物资")
    return score, "、".join(reasons) or "无线索"


def build_suggestions(parse: ParseResult,
                       candidates: Sequence[Dict[str, Any]],
                       contract_amount: float = 0.0,
                       doc_no: str = "") -> Dict[str, Any]:
    """把合同明细行匹配到订单明细，产出【只供预览】的分配建议，不写库。"""
    out: Dict[str, Any] = {
        "source": parse.source, "table_index": parse.table_index,
        "lines": [x.as_dict() for x in parse.lines],
        "suggestions": [], "unmatched": [], "specs": [],
        "total": parse.total, "contract_amount": float(contract_amount or 0),
        "gap": 0.0, "warnings": list(parse.warnings), "doc_no": doc_no,
        "ok": False,
    }
    gap = float(money.q2(money.to_dec(parse.total)
                          - money.to_dec(contract_amount or 0)))
    out["gap"] = gap
    if contract_amount and abs(gap) > 0.01:
        out["warnings"].append(
            f"合同明细合计 ¥{parse.total:,.2f} 与台账金额 "
            f"¥{float(contract_amount):,.2f} 相差 ¥{gap:,.2f}，请核对后再保存。")
    if not parse.lines or not candidates:
        if not candidates:
            out["warnings"].append("当前订单没有可分配的物资明细（或未关联订单）。")
        return out

    # 全局贪心：分数高的行优先挑候选，避免两行抢同一条订单明细
    pairs: List[Tuple[int, int, int, str]] = []
    for li, line in enumerate(parse.lines):
        for ci, cand in enumerate(candidates):
            s, why = _score(line, cand, len(candidates))
            pairs.append((s, li, ci, why))
    pairs.sort(key=lambda x: (-x[0], x[1]))

    assigned: Dict[int, Dict[str, Any]] = {}
    used_lines: set = set()
    for score, li, ci, why in pairs:
        if li in used_lines:
            continue
        cand = candidates[ci]
        iid = int(cand["id"])
        if score < MATCH_THRESHOLD:
            continue
        line = parse.lines[li]
        if line.unit_price <= 0:
            continue                      # 没单价，绝不填 0 冒充
        if iid in assigned:
            # 同一合同不能对同一明细建两行 → 合并数量 / 金额，反算加权单价
            s = assigned[iid]
            s["qty"] = float(money.q4(money.to_dec(s["qty"]) + money.to_dec(line.qty)))
            s["amount"] = float(money.q2(money.to_dec(s["amount"])
                                         + money.to_dec(line.amount)))
            s["unit_price"] = float(money.q4(
                money.to_dec(s["amount"]) / money.to_dec(s["qty"])))
            s["merged"] += 1
            s["reason"] += f"；合并第 {line.row_no} 行「{line.name}」"
            used_lines.add(li)
            continue
        assigned[iid] = {
            "order_item_id": iid,
            "item_name": str(cand.get("item_name") or ""),
            "spec": str(cand.get("spec") or ""),
            "qty": float(line.qty),
            "unit_price": float(line.unit_price),
            "amount": float(line.amount),
            "score": score,
            "confidence": "高" if score >= HIGH_CONFIDENCE else "需复核",
            "reason": f"「{line.name}」→ {why}",
            "source_row": line.row_no,
            "merged": 0,
        }
        used_lines.add(li)

    for li, line in enumerate(parse.lines):
        if li not in used_lines:
            out["unmatched"].append({
                "name": line.name, "qty": line.qty,
                "unit_price": line.unit_price, "amount": line.amount,
                "row_no": line.row_no,
                "reason": ("合同表未提供可识别的单价" if line.unit_price <= 0
                           else "未找到可信匹配的订单物资，请手工指定"),
            })

    # 数量守恒：超出剩余可采购量则截断
    rem = {int(c["id"]): float(c.get("remaining_qty") or 0) for c in candidates}
    for s in assigned.values():
        iid = s["order_item_id"]
        if s["qty"] > rem.get(iid, 0) + 1e-9:
            out["warnings"].append(
                f"「{s['item_name']}」合同数量 {s['qty']:g} 超出剩余可采购 "
                f"{rem.get(iid, 0):g}，已截断为 {rem.get(iid, 0):g}。")
            s["qty"] = float(rem.get(iid, 0))
            s["amount"] = float(money.q2(money.mul(s["qty"], s["unit_price"])))
        s["amount"] = float(money.q2(money.mul(s["qty"], s["unit_price"])))

    out["suggestions"] = sorted(assigned.values(), key=lambda x: x["order_item_id"])
    out["specs"] = [{
        "order_item_id": s["order_item_id"], "qty": s["qty"],
        "unit_price": s["unit_price"], "tax_rate": 0,
        "remark": f"自动匹配自合同明细第 {s['source_row']} 行",
    } for s in out["suggestions"]]
    out["ok"] = bool(out["specs"])
    if out["unmatched"]:
        out["warnings"].append(
            f"还有 {len(out['unmatched'])} 行未自动匹配，请在本页手工勾选并填写。")
    return out

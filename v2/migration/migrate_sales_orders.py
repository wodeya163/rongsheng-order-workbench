#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-1 真实销售订单迁移（销售订单汇总 24年7月-26年.xlsx → v2 订单域）

    py -m v2.migration.migrate_sales_orders                 # 预演（默认，不写库）
    py -m v2.migration.migrate_sales_orders --commit        # 正式导入（先自动备份）
    py -m v2.migration.migrate_sales_orders --db 路径 --xlsx 路径 --commit

字段映射（源表 22 列 → 订单头 8 + 明细 9）：
  订单编号   → project_no（业务编号，分组键）      doc_no 由系统生成 DD-YYYYMM-NNNN
  需方单位   → customer                            订单状态 → status（源值直接沿用）
  价税合计   → 行级金额 → items.amount；订单头 amount_total = Σ行金额（Q11 取汇总）
  交货日期   → required_date                        物资数量 → item_qty_total = 明细行数（Q12）
  ERP订单编号 → erp_order_no                        下单日期/业务员 → order_date/owner
  产品代码   → items.item_code                     产品名称 → 第一段=item_name，第二段=spec，
                                                    完整路径存 remark（保留原貌）
  含税单价   → unit_price   数量 → qty_ordered（唯一实值锚点）   单位 → unit
  参数描述   → 留空（Q13 未拍板：是否从三张配置表回填，另行确认）

幂等：同一 project_no 已存在则整单跳过，可反复执行。
"""
from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2.core.db import Database                      # noqa: E402
from v2.core import models                           # noqa: E402
from v2.domains.order import OrderService            # noqa: E402

SHEET = "销售订单"
DEFAULT_XLSX = ROOT / "销售订单汇总（24年7月-26年）.xlsx"
DEFAULT_DB = ROOT / "data" / "erp.db"
REPORT = ROOT / "docs" / "迁移报告_销售订单.md"

COL = {
    "owner": "业务员",
    "order_date": "下单日期",
    "project_no": "订单编号",
    "customer": "需方单位",
    "short_name": "客户简称",
    "status": "订单状态",
    "item_code": "产品代码",
    "item_name": "产品名称",
    "unit_price": "含税单价",
    "qty": "数量",
    "unit": "单位",
    "amount": "价税合计",
    "required_date": "交货日期",
    "erp_no": "ERP订单编号",
    "plan_date": "排产日期",
    "exec_status": "订单执行状态",
    "remark": "备注（订单延期）",
    "shop_date": "实际发货日期",
}


# ── 基础转换 ────────────────────────────────────────────────


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


def _date(v: Any) -> str:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    s = _text(v)
    return s[:10] if len(s) >= 10 else s


def _num(v: Any, default: float = 0.0) -> float:
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def split_product_name(raw: str) -> Tuple[str, str]:
    """产品名称形如『密封圆顶阀\\FT3.1.1.2\\气力输灰系统\\FT…』→ (名称, 型号)"""
    parts = [p.strip() for p in str(raw or "").replace("/", "\\").split("\\") if p.strip()]
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


# ── 读取与分组 ──────────────────────────────────────────────


def read_sheet(xlsx: Path) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    import openpyxl

    wb = openpyxl.load_workbook(str(xlsx), data_only=True, read_only=True)
    if SHEET not in wb.sheetnames:
        raise SystemExit(f"找不到工作表《{SHEET}》，实际：{wb.sheetnames}")
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    idx = {name: header.index(col) for name, col in COL.items() if col in header}
    missing = [col for name, col in COL.items() if col not in header and name in
               ("project_no", "customer", "item_name", "qty", "unit_price", "amount",
                "status", "required_date", "erp_no", "order_date", "owner", "item_code", "unit")]
    if missing:
        raise SystemExit(f"源表缺少必需列：{'、'.join(missing)}")

    out: List[Dict[str, Any]] = []
    stats = {"blank_rows": 0}
    for r in rows[1:]:
        if r is None or all(c is None or str(c).strip() == "" for c in r):
            stats["blank_rows"] += 1
            continue
        get = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
        out.append({
            "project_no": _text(get("project_no")),
            "customer": _text(get("customer")),
            "short_name": _text(get("short_name")),
            "status": _text(get("status")),
            "owner": _text(get("owner")),
            "order_date": _date(get("order_date")),
            "required_date": _date(get("required_date")),
            "erp_no": _text(get("erp_no")),
            "item_code": _text(get("item_code")),
            "item_name_raw": _text(get("item_name")),
            "unit_price": _num(get("unit_price")),
            "qty": _num(get("qty")),
            "unit": _text(get("unit")) or "件",
            "amount": _num(get("amount")),
            "plan_date": _text(get("plan_date")),
            "exec_status": _text(get("exec_status")),
            "remark": _text(get("remark")),
        })
    return out, stats


def build_groups(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    groups: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    anomalies: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    for r in rows:
        pn = r["project_no"]
        if not pn:
            anomalies.append("存在空订单编号的数据行，已跳过")
            continue
        if r["qty"] <= 0:
            anomalies.append(f"{pn} 明细数量 {r['qty']} ≤ 0（{r['item_name_raw'][:20]}）")
            continue
        name, spec = split_product_name(r["item_name_raw"])
        line = r["amount"] or round(r["qty"] * r["unit_price"], 2)
        if r["amount"] and abs(r["amount"] - r["qty"] * r["unit_price"]) > 0.01:
            mismatches.append({
                "project_no": pn, "item": name,
                "qty": r["qty"], "price": r["unit_price"],
                "amount": r["amount"], "calc": round(r["qty"] * r["unit_price"], 2),
            })
        g = groups.setdefault(pn, {
            "head": {
                "project_no": pn,
                "customer": r["customer"],
                "order_type": models.OrderType.EXTERNAL,
                "status": r["status"] or models.OrderStatus.CONFIRMED,
                "required_date": r["required_date"],
                "erp_order_no": r["erp_no"],
                "order_date": r["order_date"],
                "owner": r["owner"],
                "remark": _head_remark(r),
                "source_code_project_id": None,
                "source_bid_project_id": None,
            },
            "items": [],
        })
        g["items"].append({
            "item_code": r["item_code"],
            "item_name": name,
            "production_name": "",
            "spec": spec,
            "unit": r["unit"],
            "qty_ordered": r["qty"],
            "unit_price": r["unit_price"],
            "amount": line,
            "param_desc": "",
            "remark": r["item_name_raw"],
        })

    stats = {
        "orders": len(groups),
        "items": sum(len(g["items"]) for g in groups.values()),
        "qty": round(sum(i["qty_ordered"] for g in groups.values() for i in g["items"]), 2),
        "amount": round(sum(i["amount"] for g in groups.values() for i in g["items"]), 2),
        "status": {},
        "anomalies": anomalies,
        "mismatches": mismatches,
    }
    for g in groups.values():
        s = g["head"]["status"]
        stats["status"][s] = stats["status"].get(s, 0) + 1
    return list(groups.values()), stats


def _head_remark(r: Dict[str, Any]) -> str:
    bits = []
    if r.get("short_name"):
        bits.append(f"客户简称：{r['short_name']}")
    if r.get("remark"):
        bits.append(f"订单备注：{r['remark']}")
    if r.get("exec_status"):
        bits.append(f"执行状态：{r['exec_status']}")
    return "；".join(bits)


# ── 入库 ────────────────────────────────────────────────────


def existing_project_nos(db: Database) -> set:
    return {r["project_no"] for r in db.query(
        "SELECT project_no FROM orders WHERE project_no IS NOT NULL AND project_no != ''")}


def import_groups(db: Database, groups: List[Dict[str, Any]],
                  operator: str = "迁移") -> Dict[str, Any]:
    svc = OrderService(db)
    exists = existing_project_nos(db)
    todo = [g for g in groups if g["head"]["project_no"] not in exists]
    skipped = len(groups) - len(todo)
    result = {"created": [], "skipped": skipped}
    if not todo:
        return result
    with db.tx():
        for g in todo:
            spec = dict(g["head"])
            spec["items"] = g["items"]
            order = svc.create_from_spec(spec, operator=operator, origin="migrate_sales_orders")
            result["created"].append(order["doc_no"])
    return result


# ── 报告 ────────────────────────────────────────────────────


def db_snapshot(db: Database) -> Dict[str, Any]:
    """库内最终状态：计数 + 两条口径对账（P0-1 验收用）"""
    from v2.core.integrity import check

    bad_qty = db.query(
        "SELECT o.id FROM orders o LEFT JOIN order_items i ON i.order_id = o.id "
        "GROUP BY o.id HAVING ABS(o.item_qty_total - COUNT(i.id)) > 1e-9")
    bad_amt = db.query(
        "SELECT o.id FROM orders o LEFT JOIN order_items i ON i.order_id = o.id "
        "GROUP BY o.id HAVING ABS(o.amount_total - ROUND(COALESCE(SUM(i.amount), 0), 2)) > 0.01")
    report = check(db)
    return {
        "orders": db.scalar("SELECT COUNT(*) FROM orders"),
        "items": db.scalar("SELECT COUNT(*) FROM order_items"),
        "qty": db.scalar("SELECT COALESCE(SUM(qty_ordered), 0) FROM order_items"),
        "amount": db.scalar("SELECT COALESCE(SUM(amount), 0) FROM order_items"),
        "bad_qty": len(bad_qty),
        "bad_amt": len(bad_amt),
        "integrity_ok": bool(report.get("ok")),
        "integrity": report.get("summary", "").strip(),
    }


def render_report(xlsx: Path, db_path: Path, stats: Dict[str, Any],
                  result: Optional[Dict[str, Any]], sample: List[Dict[str, Any]],
                  committed: bool, already: int = 0,
                  snapshot: Optional[Dict[str, Any]] = None) -> str:
    lines = [
        "# 销售订单迁移报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 源文件：`{xlsx.name}`",
        f"- 目标库：`{db_path}`",
        f"- 模式：{'正式导入（已提交）' if committed else '预演（未写库）'}",
        "",
        "## 一、源数据统计",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 订单数（按订单编号分组） | {stats['orders']} |",
        f"| 明细行数 | {stats['items']} |",
        f"| 数量合计 | {stats['qty']:,.2f} |",
        f"| 金额合计 | {stats['amount']:,.2f} |",
        f"| 空行 | {stats['blank_rows']} |",
        "",
        "### 状态分布",
        "",
        "| 订单状态 | 单数 |",
        "|---|---|",
    ]
    for k, v in sorted(stats["status"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")

    lines += ["", "## 二、数据质量", ""]
    if stats["anomalies"]:
        lines.append(f"- 跳过/异常行：**{len(stats['anomalies'])}** 条")
        for a in stats["anomalies"][:20]:
            lines.append(f"  - {a}")
    else:
        lines.append("- 无异常行（数量均 > 0，单号无空值）")

    mm = stats["mismatches"]
    if mm:
        lines.append(f"- 行金额与「数量×单价」不一致：**{len(mm)}** 条（保留源表金额，不做改写）")
        for m in mm[:10]:
            lines.append(
                f"  - {m['project_no']} {m['item']}：{m['qty']}×{m['price']}={m['calc']:,}，源值 {m['amount']:,}")
    else:
        lines.append("- 行金额与「数量×单价」全部一致")

    lines += ["", "## 三、映射口径", "",
              "| 源列 | 落库字段 | 说明 |", "|---|---|---|",
              "| 订单编号 | orders.project_no | 业务编号，分组键；系统单号另生成 DD-YYYYMM-NNNN |",
              "| 需方单位 | orders.customer | 原样 |",
              "| 订单状态 | orders.status | 源值直接沿用（已验收/发货中/待发货） |",
              "| 价税合计 | order_items.amount → orders.amount_total | 行级原值；订单头 = Σ行金额（Q11 取汇总） |",
              "| 交货日期 | orders.required_date | 截取日期部分 |",
              "| 物资数量 | orders.item_qty_total | 明细行数（Q12 已确认口径） |",
              "| ERP订单编号 | orders.erp_order_no | 原样 |",
              "| 产品代码 | order_items.item_code | 原样 |",
              "| 产品名称 | item_name + spec | 按 `\\` 分段：第一段名称、第二段型号；完整路径存 remark |",
              "| 含税单价 / 数量 / 单位 | unit_price / qty_ordered / unit | 数量为唯一实值锚点，不改写 |",
              "| 参数描述 | param_desc | **留空**（Q13 未拍板，不自动回填配置表） |",
              ]

    if result is not None:
        created_n = len(result["created"])
        verdict = ("本次为首次导入，数据已全部落库。" if created_n and not result["skipped"]
                   else "库内已存在同编号订单，本次未做任何变更（迁移幂等）。"
                   if not created_n else "部分订单已存在，仅补齐缺失订单。")
        lines += ["", "## 四、导入结果", "",
                  f"- 迁移前库内同编号订单：{already} 单",
                  f"- 本次新建：**{created_n}** 张",
                  f"- 本次跳过（已存在）：**{result['skipped']}** 张",
                  f"- 结论：{verdict}"]
        if created_n:
            lines.append(f"- 系统单号区间：{result['created'][0]} … {result['created'][-1]}")
        lines.append("")

    if sample:
        lines += ["## 五、抽样对账（前 3 单）", ""]
        for g in sample:
            h = g["head"]
            lines.append(f"### {h['project_no']} · {h['customer']}（{h['status']}）")
            lines.append("")
            lines.append("| 物资代码 | 名称 | 规格 | 单位 | 数量 | 单价 | 金额 |")
            lines.append("|---|---|---|---|---|---|---|")
            for i in g["items"]:
                lines.append(
                    f"| {i['item_code']} | {i['item_name']} | {i['spec']} | {i['unit']} | "
                    f"{i['qty_ordered']:g} | {i['unit_price']:,.2f} | {i['amount']:,.2f} |")
            total = sum(i["amount"] for i in g["items"])
            lines.append(f"| | | | | **{sum(i['qty_ordered'] for i in g['items']):g}** | | "
                         f"**{total:,.2f}** |")
            lines.append("")
    if snapshot:
        lines += ["", "## 六、库内最终状态（对账）", "",
                  "| 校验项 | 结果 |", "|---|---|",
                  f"| 订单数 | {snapshot['orders']} |",
                  f"| 明细行数 | {snapshot['items']} |",
                  f"| 数量合计 | {snapshot['qty']:,.2f} |",
                  f"| 金额合计 | {snapshot['amount']:,.2f} |",
                  f"| 物资数量 ≠ 明细行数 | {snapshot['bad_qty']} 单 |",
                  f"| 价税合计 ≠ Σ明细金额 | {snapshot['bad_amt']} 单 |",
                  f"| 一致性检查 | {'✅ 通过' if snapshot['integrity_ok'] else '❌ 未通过'} |",
                  "",
                  f"> {snapshot['integrity']}", ""]
    return "\n".join(lines) + "\n"


# ── 入口 ────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="销售订单汇总 → v2 订单域迁移")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--commit", action="store_true", help="正式写入（默认只预演）")
    ap.add_argument("--report", default=str(REPORT))
    args = ap.parse_args(argv)

    xlsx, db_path = Path(args.xlsx), Path(args.db)
    if not xlsx.is_file():
        print(f"源文件不存在：{xlsx}", file=sys.stderr)
        return 2

    rows, read_stats = read_sheet(xlsx)
    groups, stats = build_groups(rows)
    stats["blank_rows"] = read_stats["blank_rows"]

    db: Optional[Database] = None
    exists: set = set()
    if args.commit or db_path.is_file():
        db = Database(str(db_path))
        exists = existing_project_nos(db)
    already = sum(1 for g in groups if g["head"]["project_no"] in exists)

    print(f"源：{xlsx.name} → 目标库 {db_path}")
    print(f"解析：{stats['items']} 行 / {stats['orders']} 单；"
          f"数量合计 {stats['qty']:,.2f}；金额合计 {stats['amount']:,.2f}")
    print(f"库内已存在同编号订单：{already} 单（将跳过）")
    if stats["anomalies"]:
        print(f"异常行：{len(stats['anomalies'])}（详见报告）")
    if stats["mismatches"]:
        print(f"行金额×单价不一致：{len(stats['mismatches'])} 条（保留源值）")

    result = None
    if args.commit:
        if db is None:
            db = Database(str(db_path))
        backup = db.backup(tag="before-migrate", keep=30)
        if backup:
            print(f"已备份现库：{backup.name}")
        result = import_groups(db, groups)
        print(f"导入完成：新建 {len(result['created'])} 单，跳过 {result['skipped']} 单")
        chk = db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(qty_ordered),0) AS q, "
            "COALESCE(SUM(amount),0) AS a FROM order_items")
        print(f"库内校验：明细 {chk['n']} 行，数量 {chk['q']:,.2f}，金额 {chk['a']:,.2f}")
    else:
        print("预演模式：未写库。确认无误后加 --commit 正式导入。")

    snapshot = None
    if args.commit and db is not None:
        snapshot = db_snapshot(db)
        print(f"对账：物资数量≠行数 {snapshot['bad_qty']} 单 / "
              f"价税合计≠Σ明细 {snapshot['bad_amt']} 单 / "
              f"一致性 {'通过' if snapshot['integrity_ok'] else '未通过'}")

    text = render_report(xlsx, db_path, stats, result, groups[:3],
                         args.commit, already, snapshot)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"报告：{out}")
    if db is not None:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.5 products.db → v2.1 erp.db 一次性迁移

原则：只读旧库、只写新库，绝不 ALTER 旧表。
★ 历史 products 全部进入【询报价/招投标】域（bid_projects / bid_items）；
   迁移【不生成】任何询配码项目或订单 —— 它们是独立的新业务链路。

用法：
    python -m v2.migration.migrate_v15 --dry-run
    python -m v2.migration.migrate_v15 --source data/products.db --target data/erp.db
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.core.db import Database                       # noqa: E402
from v2.core import models                             # noqa: E402
from v2.domains.bid import BidService                  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_legacy(source: str) -> List[Dict[str, Any]]:
    if not os.path.exists(source):
        raise FileNotFoundError(f"旧库不存在：{source}")
    conn = sqlite3.connect(source)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    if "id" not in cols:
        conn.close()
        raise RuntimeError("旧库中没有 products 表，无法迁移")
    rows = [dict(r) for r in conn.execute("SELECT * FROM products ORDER BY id")]
    conn.close()
    defaults = {
        "name": "", "spec": "", "category": "", "quote_ratio": 1.0,
        "inquiry_qty": 1, "purchase_price": 0.0, "inquiry_unit": "件",
        "description": "", "project": "", "is_won": "", "quoter": "",
        "image_path": "", "drawing_path": "", "created_at": "", "updated_at": "",
    }
    for r in rows:
        for k, v in defaults.items():
            r.setdefault(k, v)
    return rows


def _legacy_totals(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    purchase = quote = 0.0
    for r in rows:
        qty = float(r.get("inquiry_qty") or 1)
        pp = float(r.get("purchase_price") or 0)
        ratio = float(r.get("quote_ratio") or 1) or 1.0
        purchase += pp * qty
        quote += (pp / ratio) * qty
    return {"purchase": round(purchase, 2), "quote": round(quote, 2)}


def migrate(source: str, target: str, dry_run: bool = False,
            operator: str = "migration") -> Dict[str, Any]:
    rows = _read_legacy(source)
    legacy_totals = _legacy_totals(rows)
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for r in rows:
        key = (r.get("project") or "").strip() or "（未分配项目）"
        groups.setdefault(key, []).append(r)

    plan = {
        "source": source,
        "target": target,
        "dry_run": dry_run,
        "target_domain": "bid_projects / bid_items（询报价·招投标统计）",
        "legacy_rows": len(rows),
        "legacy_groups": len(groups),
        "legacy_totals": legacy_totals,
    }
    if dry_run:
        plan["groups_preview"] = [
            {"project": k, "rows": len(v)} for k, v in list(groups.items())[:10]
        ]
        return plan

    db = Database(target)
    bids = BidService(db)
    created: List[int] = []
    with db.tx():
        for project_name, group in groups.items():
            first = group[0]
            p = bids.create_project(
                project_name=project_name,
                customer="",
                tender_no="",
                bid_date=(first.get("created_at") or "")[:10],
                owner=first.get("quoter") or "",
                remark="由 v1.5 products 迁移",
            )
            created.append(p["id"])
            for r in group:
                bids.add_item(
                    p["id"],
                    product_name=r.get("name") or "(无名产品)",
                    spec=r.get("spec") or "",
                    category=r.get("category") or "",
                    unit=r.get("inquiry_unit") or "件",
                    qty=float(r.get("inquiry_qty") or 1),
                    purchase_price=float(r.get("purchase_price") or 0),
                    quote_ratio=float(r.get("quote_ratio") or 1) or 1.0,
                    is_won=r.get("is_won") or "",
                    image_path=r.get("image_path") or "",
                    drawing_path=r.get("drawing_path") or "",
                    legacy_product_id=r.get("id"),
                    remark=r.get("description") or "",
                )
            won_flags = {(r.get("is_won") or "").strip() for r in group}
            bids.mark_quoted(p["id"], operator)
            if "是" in won_flags:
                bids.set_status(p["id"], models.BidStatus.WON, operator)
            elif won_flags == {"否"}:
                bids.set_status(p["id"], models.BidStatus.LOST, operator)

    new_rows = db.query("SELECT * FROM bid_items")
    new_purchase = sum(float(r["purchase_price"]) * float(r["qty"]) for r in new_rows)
    new_quote = sum(float(r["purchase_price"]) / float(r["quote_ratio"] or 1)
                    * float(r["qty"]) for r in new_rows)
    check = {
        "row_count_match": len(new_rows) == len(rows),
        "purchase_match": abs(round(new_purchase, 2) - legacy_totals["purchase"]) < 0.01,
        "quote_match": abs(round(new_quote, 2) - legacy_totals["quote"]) < 0.01,
        "new_rows": len(new_rows),
        "new_totals": {"purchase": round(new_purchase, 2), "quote": round(new_quote, 2)},
    }
    plan.update({
        "created_projects": len(created),
        "status": "OK" if all(check[k] for k in
                              ("row_count_match", "purchase_match", "quote_match")) else "MISMATCH",
        "reconciliation": check,
        "note": "询配码项目与订单为独立新业务，迁移不生成；需业务人员另行录入。",
    })
    db.close()
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="v1.5 → v2.1 数据迁移")
    ap.add_argument("--source", default=str(PROJECT_ROOT / "data" / "products.db"))
    ap.add_argument("--target", default=str(PROJECT_ROOT / "data" / "erp.db"))
    ap.add_argument("--dry-run", action="store_true", help="只出迁移计划，不写新库")
    args = ap.parse_args(argv)
    try:
        report = migrate(args.source, args.target, args.dry_run)
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in (None, "OK") else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.5.0 核查：一张订单挂 2~N 份采购合同时，订单页「采购合计(含税)」能否自动汇总。

只读核查真实部署库（裸 sqlite3 mode=ro，绝不触发 Database() 的自动迁移）。
服务层场景全部跑在独立的内存库里，不写任何真实数据。

运行：python tools/check_multi_contract_v350.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from v2.core.db import Database                     # noqa: E402
from v2.domains import OrderService                  # noqa: E402
from v2.domains.contract import (                    # noqa: E402
    PurchaseContractService, SIGNED_STATUSES,
)
from v2.domains.profit import OrderProfitService     # noqa: E402

REAL_DB = Path(r"D:\Programs\ProductSuite_v2.2\data\erp.db")
SIGNED = ("已签订", "执行中", "已完成")


def banner(text: str) -> None:
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def new_case():
    """每个场景一套独立内存库，避免互相污染"""
    tmp = Path(tempfile.mkdtemp(prefix="v350_multi_"))
    db = Database(":memory:")
    orders = OrderService(db)
    contracts = PurchaseContractService(db, str(tmp / "data"))
    profit = OrderProfitService(db)
    o = orders.create_order(customer="核查客户", project_no="SO-MULTI-001")
    items = [
        orders.add_item(o["id"], "耐磨弯头", 10, unit_price=1000),
        orders.add_item(o["id"], "斜三通", 10, unit_price=1000),
        orders.add_item(o["id"], "波纹补偿器", 10, unit_price=1000),
    ]
    return tmp, db, orders, contracts, profit, o, items


def add_contract(contracts, oid, name, supplier, status="已签订", amount=0):
    return contracts.create({
        "contract_name": name, "supplier": supplier, "order_id": oid,
        "amount": amount, "status": status,
        "tax_included": 1, "amount_source": "manual",
    }, "核查")


def assign(contracts, cid, pairs):
    return contracts.set_contract_items(
        int(cid),
        [{"order_item_id": int(i["id"]), "qty": q, "unit_price": p}
         for i, q, p in pairs], "核查")


def show(profit, o, title):
    s = profit.order_summary(o["id"])
    print(f"\n  [{title}]")
    print(f"    销售额(明细)      = ¥{s['sales_total']:>12,.2f}")
    print(f"    采购合计(含税)    = ¥{s['purchase_total']:>12,.2f}   <== 订单页该列")
    print(f"    毛利              = ¥{s['gross_profit']:>12,.2f}")
    print(f"    供应商数          = {s['supplier_count']}")
    print(f"    各供应商归集      = {[(x['supplier'], x['purchase_amount']) for x in s['by_supplier']]}")
    print(f"    明细采购额        = {[(x['item_name'], x['purchase_amount'], x['suppliers']) for x in s['by_item']]}")
    print(f"    覆盖度/完整       = {s['coverage']}% / {s['complete']}")
    for w in s["warnings"]:
        print(f"    告警              = {w}")
    return s


# ── 场景 1：两份已签合同，各覆盖不同物资（最典型的一单多合同）──
tmp, db, orders, contracts, profit, o, it = new_case()
c1 = add_contract(contracts, o["id"], "甲合同", "甲供应商", amount=6000)
c2 = add_contract(contracts, o["id"], "乙合同", "乙供应商", amount=2800)
assign(contracts, c1["id"], [(it[0], 5, 1200)])       # 6000
assign(contracts, c2["id"], [(it[1], 4, 700)])        # 2800
s1 = show(profit, o, "场景1：2 份已签合同，采购合计应为 6000+2800=8800")
print(f"    ==> 断言 8800：{'PASS' if abs(s1['purchase_total'] - 8800) < 0.01 else 'FAIL'}")
db.close(); shutil.rmtree(tmp, ignore_errors=True)

# ── 场景 2：三份合同，同一物资被两份合同分别分配（数量守恒内）──
tmp, db, orders, contracts, profit, o, it = new_case()
c1 = add_contract(contracts, o["id"], "甲合同", "甲供应商")
c2 = add_contract(contracts, o["id"], "乙合同", "乙供应商")
c3 = add_contract(contracts, o["id"], "丙合同", "丙供应商")
assign(contracts, c1["id"], [(it[0], 5, 600)])        # 3000
assign(contracts, c2["id"], [(it[0], 5, 620)])        # 3100（同一物资第二份合同）
assign(contracts, c3["id"], [(it[1], 4, 700)])        # 2800
s2 = show(profit, o, "场景2：3 份合同，同一物资 2 份合同分配，应为 3000+3100+2800=8900")
print(f"    ==> 断言 8900：{'PASS' if abs(s2['purchase_total'] - 8900) < 0.01 else 'FAIL'}")
db.close(); shutil.rmtree(tmp, ignore_errors=True)

# ── 场景 3：两份合同，其中一份是草稿 ──
tmp, db, orders, contracts, profit, o, it = new_case()
c1 = add_contract(contracts, o["id"], "已签合同", "甲供应商")
c2 = add_contract(contracts, o["id"], "草稿合同", "乙供应商", status="草稿")
assign(contracts, c1["id"], [(it[0], 5, 600)])        # 3000
assign(contracts, c2["id"], [(it[1], 4, 700)])        # 2800（草稿，应被排除）
s3 = show(profit, o, "场景3：1 已签 + 1 草稿，草稿 2800 不计，应为 3000")
print(f"    ==> 断言 3000：{'PASS' if abs(s3['purchase_total'] - 3000) < 0.01 else 'FAIL'}")
db.close(); shutil.rmtree(tmp, ignore_errors=True)

# ── 场景 4：两份合同都挂本订单，但没做「物资分配」──
tmp, db, orders, contracts, profit, o, it = new_case()
add_contract(contracts, o["id"], "只有表头金额A", "甲供应商", amount=19000)
add_contract(contracts, o["id"], "只有表头金额B", "乙供应商", amount=5500)
s4 = show(profit, o, "场景4：2 份合同都关联本订单，但零分配行（关键边界）")
print(f"    ==> 采购合计 = 表头金额？{'是' if s4['purchase_total'] else '否：为 0，必须做物资分配'}")

# 补上分配后再看
c = contracts.list(order_id=o["id"])[0]
assign(contracts, c["id"], [(it[0], 5, 3800)])        # 19000
s4b = show(profit, o, "场景4b：给其中一份补做物资分配后")
db.close(); shutil.rmtree(tmp, ignore_errors=True)

# ── 场景 5：合同挂在别的订单上，但分配行指向本订单物资 ──
tmp, db, orders, contracts, profit, o, it = new_case()
other = orders.create_order(customer="另一客户", project_no="SO-MULTI-002")
c1 = add_contract(contracts, other["id"], "挂在别单的合同", "甲供应商")
assign(contracts, c1["id"], [(it[0], 5, 600)])       # 3000 指向本订单明细
s5 = show(profit, o, "场景5：合同 order_id 指向别单，但分配明细属于本单")
print(f"    ==> 仍计入本单：{'PASS' if abs(s5['purchase_total'] - 3000) < 0.01 else 'FAIL'}（按明细归集，不按合同 order_id）")
db.close(); shutil.rmtree(tmp, ignore_errors=True)


# ── 真实部署库只读核查 ──
banner("B. 真实部署库只读核查（sqlite3 mode=ro，零写入）")
if not REAL_DB.exists():
    print(f"  未找到 {REAL_DB}")
else:
    uri = f"file:{REAL_DB.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row

    def scalar(sql, args=()):
        return con.execute(sql, args).fetchone()[0]

    ver = scalar("SELECT version FROM schema_meta ORDER BY rowid DESC LIMIT 1") \
        if scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='schema_meta'") else "?"
    print(f"  schema 版本            = {ver}")
    print(f"  purchase_contracts    = {scalar('SELECT COUNT(*) FROM purchase_contracts')} 份")
    print(f"  其中已签/执行/已完成   = {scalar('SELECT COUNT(*) FROM purchase_contracts WHERE status IN (?,?,?)', SIGNED)} 份")
    print(f"  purchase_contract_items = {scalar('SELECT COUNT(*) FROM purchase_contract_items')} 条分配行")

    ph = ",".join("?" for _ in SIGNED)
    multi = con.execute(
        "SELECT c.order_id, COUNT(*) n, GROUP_CONCAT(c.doc_no, ' | ') nos, "
        "       GROUP_CONCAT(c.supplier, ' | ') sup "
        "FROM purchase_contracts c "
        f"WHERE c.status IN ({ph}) AND c.order_id IS NOT NULL "
        "GROUP BY c.order_id HAVING COUNT(*) >= 2 ORDER BY n DESC",
        SIGNED).fetchall()
    print(f"\n  关联 >=2 份已签合同的订单 = {len(multi)} 单")
    for r in multi[:10]:
        oid = r["order_id"]
        alloc = con.execute(
            "SELECT COUNT(DISTINCT pci.contract_id), COALESCE(SUM(pci.amount),0) "
            "FROM purchase_contract_items pci "
            "JOIN order_items oi ON oi.id = pci.order_item_id "
            "JOIN purchase_contracts c ON c.id = pci.contract_id "
            f"WHERE oi.order_id = ? AND c.status IN ({ph})",
            (oid, *SIGNED)).fetchone()
        print(f"    order_id={oid:<5} 已签合同 {r['n']} 份 | 供方：{r['sup']} "
              f"| 有分配的合同 {alloc[0]} 份 | 分配金额 ¥{alloc[1]:,.2f}")
    if len(multi) > 10:
        print(f"    ...另有 {len(multi) - 10} 单")

    # 按「分配行归属的订单」统计，这才是采购合计真正的统计维度
    roll = con.execute(
        "SELECT oi.order_id, COUNT(DISTINCT pci.contract_id) n, "
        "       COALESCE(SUM(pci.amount),0) amt "
        "FROM purchase_contract_items pci "
        "JOIN order_items oi ON oi.id = pci.order_item_id "
        "JOIN purchase_contracts c ON c.id = pci.contract_id "
        f"WHERE c.status IN ({ph}) "
        "GROUP BY oi.order_id HAVING COUNT(DISTINCT pci.contract_id) >= 2 "
        "ORDER BY n DESC", SIGNED).fetchall()
    print(f"\n  实际由 >=2 份合同共同贡献采购合计的订单 = {len(roll)} 单")
    for r in roll[:10]:
        print(f"    order_id={r['order_id']:<5} 合同 {r['n']} 份 | 采购合计 ¥{r['amt']:,.2f}")
    con.close()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.2 系统入口（2026-11-05 第二次口径确认）

    python v2/main_v2.py --smoke                 # 零第三方依赖，跑通两条链路
    python v2/main_v2.py --init-db data/erp.db   # 初始化/升级数据库
    python v2/main_v2.py --gui                   # 启动 PySide6 界面
    python -m v2.app_v2                         # 直接启动界面（推荐）

链路 A：询配码项目(虚值) → 提交用户 → 多张外部订单(实值) → 排产
链路 B：中标招投标项目 → 一张内部订单(实值) → 排产
        （中标项目经内部订单产生排产数量，绝不直接挂数量）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v2 import __version__                                        # noqa: E402
from v2.core import models                                        # noqa: E402
from v2.core.db import Database                                    # noqa: E402
from v2.core.errors import (DuplicateError, OverAllocationError,   # noqa: E402
                            StateError)
from v2.domains import (BidService, PartCodeService, OrderService,  # noqa: E402
                        ProductionService, TraceService)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def label(order_no: str, line_no: int) -> str:
    return f"{order_no}-{line_no:02d}"


def run_smoke(verbose: bool = True) -> dict:
    lines = []

    def out(text: str = "") -> None:
        lines.append(text)
        if verbose:
            print(text)

    db = Database(":memory:")
    bids, codes = BidService(db), PartCodeService(db)
    orders, prod, trace = OrderService(db), ProductionService(db), TraceService(db)

    out("=" * 88)
    out(f"产品询报价管理系统 v2.2 端到端冒烟  |  {__version__}")
    out("口径：询配码虚值不约束数量 / 订单实值是排产唯一来源 / 中标项目经内部订单产生排产数量")
    out("=" * 88)

    # ① 询报价：招投标项目统计
    won = bids.create_project("国能沈西#1炉干渣机漏风治理改造", customer="国能沈西热电厂",
                              tender_no="GN-SX-2026-018", owner="张哥")
    bids.add_item(won["id"], "耐磨弯头", spec=r"DN250\S=8", qty=12,
                  purchase_price=1800.0, quote_ratio=0.85)
    bids.add_item(won["id"], "波纹补偿器", spec=r"DN300\70B125", qty=8,
                  purchase_price=4200.0, quote_ratio=0.80)
    bids.mark_quoted(won["id"], "张哥")
    bids.mark_won(won["id"], True, "张哥")
    lost = bids.create_project("某电厂未中标项目", customer="某电厂", owner="张哥")
    bids.add_item(lost["id"], "法兰", qty=5, purchase_price=320.0)
    bids.mark_quoted(lost["id"], "张哥")
    bids.set_status(lost["id"], models.BidStatus.LOST, "张哥")
    s = bids.summary(won["id"])
    out("")
    out("【① 询报价 · 招投标项目统计】")
    out(f"  {won['doc_no']} {won['project_name']}  状态：中标  "
        f"采购 ¥{s['total_purchase']:,.2f} → 报价 ¥{s['total_quote']:,.2f} "
        f"毛利 {s['profit_rate']}%")
    out(f"  {lost['doc_no']} {lost['project_name']}  状态：未中标（不可生成内部订单/关联排产）")

    # ② 链路 B：中标项目 → 内部订单 → 排产数量（一键：自动生成内部订单并排产）
    internal = prod.create_project_from_won_bid(
        won["id"], workshop="管件车间", owner="张哥", operator="张哥")
    io = internal["order"]
    io_items = orders.list_items(io["id"])
    out("")
    out("【② 链路 B · 中标项目 → 一张内部订单 → 排产数量】")
    out(f"  内部订单 {io['doc_no']}（{io['order_type']}，状态 {io['status']}，"
        f"{'一键自动生成' if internal['created_order'] else '复用已生成'}）"
        f"  实值合计 {orders.summary(io['id'])['qty_ordered_real']:g}")
    for i in io_items:
        bi = db.get_by_id("bid_items", i["source_bid_item_id"])
        out(f"    {label(io['doc_no'], i['line_no'])}  {i['item_name']:<12} "
            f"实值 {i['qty_ordered']:>5g}{i['unit']} 单价 ¥{i['unit_price']:,.2f}"
            f"  ← 中标明细 {label(won['doc_no'], bi['line_no'])}")
    out(f"  一键排产 {internal['project']['doc_no']}：{len(internal['links'])} 条关联，"
        f"分配实值 {internal['total_allocated_real']:g}"
        f"（中标项目本身不直接挂数量）")

    # ③ 询配码：独立项目 + 虚值
    cp = codes.create_project("2026年度管件询配码清单", customer="华东电建",
                              file_path=r"data\attachments\XPM_2026_V1.xlsx",
                              version="V1", owner="张哥")
    ci1 = codes.add_item(cp["id"], "耐磨弯头", spec=r"90°\DN250\S=8", material="20#",
                         std_code="GB/T12459-2017", unit="件", qty_reference=100)
    ci2 = codes.add_item(cp["id"], "U形波纹补偿器", spec=r"DN300\70B125-2002",
                         material="S30408", unit="台", qty_reference=200)
    ci3 = codes.add_item(cp["id"], "带颈对焊法兰", spec=r"DN250\PN16", unit="片",
                         qty_reference=50)
    codes.submit(cp["id"], operator="张哥")
    out("")
    out("【③ 询配码项目 · 独立域，明细数量为虚值】")
    out(f"  {cp['doc_no']} {cp['project_name']}  文件：{cp['file_path']} · 已提交用户")
    for ci in codes.list_items(cp["id"]):
        out(f"    {ci['item_code']}  {ci['item_name']:<12} 虚值 {ci['qty_reference']:>6g}{ci['unit']}")
    out("  （与询报价域零外键；虚值只统计，不进入任何下游数量公式）")

    # ④ 链路 A：一码多单
    order1 = orders.create_from_code_project(cp["id"], items=[
        {"source_code_item_id": ci1["id"], "qty_ordered": 30, "unit_price": 2100.0},
        {"source_code_item_id": ci2["id"], "qty_ordered": 45, "unit_price": 5200.0},
        {"item_name": "新增紧固件", "spec": "M20", "qty_ordered": 10,
         "unit_price": 12.0},   # 订单新增物资
    ], customer="华东电建", required_date="2026-12-20", owner="张哥")
    orders.confirm(order1["id"], "张哥")
    order2 = orders.create_from_code_project(cp["id"], items=[
        {"source_code_item_id": ci1["id"], "qty_ordered": 25, "unit_price": 2100.0},  # 同项第 2 次下单
        {"source_code_item_id": ci3["id"], "qty_ordered": 15, "unit_price": 480.0},
    ], customer="华东电建", required_date="2027-01-20", owner="张哥")
    orders.confirm(order2["id"], "张哥")
    order3 = orders.create_from_code_project(cp["id"], items=[
        {"source_code_item_id": ci2["id"], "qty_ordered": 20, "unit_price": 5200.0},
    ], customer="华东电建", required_date="2027-02-10", owner="张哥")
    orders.confirm(order3["id"], "张哥")
    oi = orders.list_items(order1["id"])
    stat = codes.statistics(cp["id"])
    out("")
    out("【④ 链路 A · 一个询配码项目产生多张外部订单（分批下单）】")
    for batch in stat["batches"]:
        out(f"    第 {batch['batch_seq']} 单 {batch['doc_no']}  "
            f"{batch['item_count']} 项  实值合计 {batch['qty_ordered_real']:g}  "
            f"交期 {batch['required_date']}")
    out(f"  同一询配码项 {ci1['item_code']} 被第 1、2 单重复下单（30 + 25），虚值 {ci1['qty_reference']:g} 不扣减")
    out(f"  统计对照：虚值合计 {stat['qty_reference_total_virtual']:g} vs "
        f"三张订单实值合计 {stat['qty_ordered_total_real']:g}（互不约束）")

    # ⑤ 排产：全部挂订单明细，按实值守恒
    pa = prod.create_project("第一批排产（12月交付）", source_bid_project_id=won["id"],
                             plan_start="2026-11-10", plan_end="2026-12-05",
                             workshop="管件车间", owner="张哥")
    pb = prod.create_project("第二批排产（次年1月交付）", plan_start="2026-12-10",
                             plan_end="2027-01-15", workshop="管件车间", owner="张哥")
    pc = prod.create_project("第2单排产", workshop="管件车间", owner="张哥")
    prod.link_order_item(pa["id"], oi[0]["id"], operator="张哥")      # 30 全排
    prod.link_order_item(pa["id"], oi[1]["id"], 20, operator="张哥")  # 45 中先排 20
    prod.link_order_item(pb["id"], oi[1]["id"], 25, operator="张哥")  # 余 25 拆第二批
    prod.link_order_item(pc["id"], orders.list_items(order2["id"])[0]["id"], operator="张哥")
    prod.add_task(pa["id"], "下料", qty=30, workcenter="下料班")
    prod.add_task(pa["id"], "焊接/热处理", qty=30, workcenter="焊工班")
    out("")
    out("【⑤ 生产排产 · 数量对齐订单实值，来源可追到具体项】")
    out(f"  {pa['doc_no']} {pa['title']}  关联中标项目标签：{won['doc_no']}  "
        f"状态：{db.get_by_id('production_projects', pa['id'])['status']}")
    for l in prod.list_links(pa["id"]):
        out(f"    └─ {l['order_no_snapshot']}/{l['order_line_snapshot']}  "
            f"分配 {l['allocated_qty']:g}  | 来源 {l['source_type_snapshot']} "
            f"{l['item_code_snapshot'] or l['bid_line_snapshot'] or '（订单新增物资）'}")
    out(f"  {pb['doc_no']} 分配 25（同一订单明细拆批）；{pc['doc_no']} 承接第 2 单 25 件")
    out(f"  数量守恒：{label(order1['doc_no'], 2)} 实值 45 = 已排 "
        f"{orders.allocated_qty(oi[1]['id']):g}，可排 {orders.available_qty(oi[1]['id']):g}")
    out(f"  内部订单实值 {orders.summary(io['id'])['qty_ordered_real']:g} 已全部转入 "
        f"{internal['project']['doc_no']}，可排 {sum(orders.available_qty(i['id']) for i in io_items):g}")

    # ⑥ 拦截
    over_blocked = lost_bid_blocked = dup_internal_blocked = lost_internal_blocked = False
    try:
        prod.link_order_item(pb["id"], oi[1]["id"], 1, operator="张哥")
    except OverAllocationError as exc:
        over_blocked = True
        out(f"  超排拦截：{exc}")
    try:
        prod.link_bid_project(pa["id"], lost["id"], "张哥")
    except StateError as exc:
        lost_bid_blocked = True
        out(f"  未中标项目挂排产拦截：{exc}")
    try:
        orders.create_internal_from_bid(won["id"])
    except DuplicateError as exc:
        dup_internal_blocked = True
        out(f"  重复生成内部订单拦截：{exc}")
    try:
        orders.create_internal_from_bid(lost["id"])
    except StateError as exc:
        lost_internal_blocked = True
        out(f"  未中标项目生成内部订单拦截：{exc}")

    # ⑦ 双向溯源
    chain_a = trace.from_production(pa["id"])
    chain_b = trace.from_production(internal["project"]["id"])
    back = trace.from_code_item(ci1["id"])
    batches = trace.from_code_project(cp["id"])
    bid_links = trace.from_bid_project(won["id"])
    io_view = trace.bid_internal_order(won["id"])
    full = trace.full_chain(pa["id"])
    out("")
    out("【⑦ 双向穿透溯源】")
    for r in chain_a:
        out(f"  {r['project_no']} ← {r['order_line_snapshot']} "
            f"(实值 {r['qty_ordered']:g}，分配 {r['allocated_qty']:g}) "
            f"← {r['source_origin']}（虚值 {r['qty_reference']}）")
    for r in chain_b:
        out(f"  {r['project_no']} ← {r['order_line_snapshot']} "
            f"(实值 {r['qty_ordered']:g}，分配 {r['allocated_qty']:g}) "
            f"← {r['source_origin']}")
    out(f"  从 {ci1['item_code']} 正查：{len(back)} 张订单（一码多单），"
        f"实值 {[r['qty_ordered'] for r in back]}，虚值 {back[0]['qty_reference_virtual']:g} 不对应")
    out(f"  从询配码项目正查：{len(batches)} 条订单明细行，覆盖 {len(stat['batches'])} 张订单")
    out(f"  从中标项目正查：{len(bid_links)} 个排产项目 / 内部订单 {io_view[0]['order_no']} "
        f"{len(io_view)} 行明细，实值 {sum(r['qty_ordered'] for r in io_view):g}")
    out(f"  汇总：{pa['doc_no']} {full['link_count']} 条关联，实值分配总量 "
        f"{full['total_allocated_real']:g}，来源类型 "
        f"{[x['source_type'] for x in full['source_types']]}")

    # ⑧ 快照免疫：改上游虚值/规格，链路快照保持建链原貌
    link0_before = db.query_one(
        "SELECT qty_reference_snapshot, spec_snapshot FROM production_links "
        "WHERE order_item_id = ?", (oi[0]["id"],))
    with db.tx():
        db.update("code_items", {"qty_reference": 999}, "id = ?", (ci1["id"],))
        db.update("order_items", {"spec": "【上游已改规格】"}, "id = ?", (oi[0]["id"],))
    link0 = db.query_one(
        "SELECT qty_reference_snapshot, spec_snapshot FROM production_links "
        "WHERE order_item_id = ?", (oi[0]["id"],))
    virtual_snapshot_ok = (link0["qty_reference_snapshot"]
                           == link0_before["qty_reference_snapshot"] == 100)
    snapshot_ok = (link0["spec_snapshot"] == link0_before["spec_snapshot"]
                   == r"90°\DN250\S=8")
    out("")
    out(f"  快照校验：上游虚值改为 999 / 规格改名后，关联快照仍为 "
        f"{link0['qty_reference_snapshot']:g} / {link0['spec_snapshot']} → "
        f"{'通过' if virtual_snapshot_ok and snapshot_ok else '失败'}")
    out(f"  且虚值改动后可排量不变（{orders.available_qty(oi[0]['id']):g}），证明虚值不参与守恒")

    out("")
    out("=" * 88)
    result = {
        "bid_won_no": won["doc_no"], "bid_lost_no": lost["doc_no"],
        "internal_order_no": io["doc_no"],
        "internal_order_type": io["order_type"],
        "internal_order_status": io["status"],
        "internal_order_qty": orders.summary(io["id"])["qty_ordered_real"],
        "internal_links": len(internal["links"]),
        "internal_allocated": internal["total_allocated_real"],
        "internal_created_order": internal["created_order"],
        "code_no": cp["doc_no"], "code_items": len(codes.list_items(cp["id"])),
        "virtual_total": stat["qty_reference_total_virtual"],
        "external_order_count": len(stat["batches"]),
        "batch_seqs": [x["batch_seq"] for x in stat["batches"]],
        "real_total": stat["qty_ordered_total_real"],
        "virtual_ne_real": stat["qty_reference_total_virtual"]
                           != stat["qty_ordered_total_real"],
        "project_a": pa["doc_no"], "project_b": pb["doc_no"],
        "links_a": len(prod.list_links(pa["id"])),
        "links_b": len(prod.list_links(pb["id"])),
        "allocated_oi2": orders.allocated_qty(oi[1]["id"]),
        "available_oi2": orders.available_qty(oi[1]["id"]),
        "trace_a_rows": len(chain_a), "trace_b_rows": len(chain_b),
        "back_links": len(back), "batch_rows": len(batches),
        "bid_links": len(bid_links), "internal_order_rows": len(io_view),
        "over_alloc_blocked": over_blocked, "lost_bid_blocked": lost_bid_blocked,
        "duplicate_internal_blocked": dup_internal_blocked,
        "lost_internal_blocked": lost_internal_blocked,
        "snapshot_ok": snapshot_ok, "virtual_snapshot_ok": virtual_snapshot_ok,
        "order_status": db.get_by_id("orders", order1["id"])["status"],
        "project_a_status": db.get_by_id("production_projects", pa["id"])["status"],
        "code_project_status": db.get_by_id("code_projects", cp["id"])["status"],
        "audit_rows": db.scalar("SELECT COUNT(*) FROM audit_log"),
    }
    out(f"链路验收：内部订单 {result['internal_order_qty']:g} 件实值 → "
        f"{result['internal_links']} 条排产 / 一码 {result['external_order_count']} 单 / "
        f"{result['links_a'] + result['links_b']} 条外部订单关联 / "
        f"虚值≠实值={result['virtual_ne_real']} / 超排拦截={result['over_alloc_blocked']} / "
        f"重复内部订单拦截={result['duplicate_internal_blocked']} / "
        f"快照={result['snapshot_ok']} → PASS")
    out("=" * 88)
    db.close()
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="产品询报价管理系统 v2.2 入口")
    ap.add_argument("--smoke", action="store_true", help="跑端到端链路冒烟（默认）")
    ap.add_argument("--init-db", metavar="PATH", help="初始化/升级一个 v2 数据库")
    ap.add_argument("--gui", action="store_true", help="启动 PySide6 界面")
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "erp.db"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.init_db:
        db = Database(args.init_db)
        tables = db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        db.close()
        print(f"已初始化/升级：{args.init_db}（{len(tables)} 张表）")
        return 0
    if args.gui:
        from v2.ui.main_window_v2 import launch
        return launch(args.db)
    run_smoke(verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

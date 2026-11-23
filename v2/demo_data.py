#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键载入演示数据：覆盖 v2.2 两条链路，便于打开界面直接看清关联关系。"""
from typing import Any

from .domains import (BidService, PartCodeService, OrderService, ProductionService)


def seed(db: Any) -> str:
    bids, codes = BidService(db), PartCodeService(db)
    orders, prod = OrderService(db), ProductionService(db)

    # 链路 B：中标项目 → 一张内部订单 → 排产数量
    won = bids.create_project("演示·国能沈西#1炉干渣机漏风治理", customer="国能沈西热电厂",
                              tender_no="DEMO-2026-001", owner="张哥")
    bids.add_item(won["id"], "耐磨弯头", spec="DN250 S=8", qty=12,
                  purchase_price=1800.0, quote_ratio=0.85)
    bids.add_item(won["id"], "波纹补偿器", spec="DN300 70B125", qty=8,
                  purchase_price=4200.0, quote_ratio=0.80)
    bids.mark_quoted(won["id"], "演示")
    bids.mark_won(won["id"], True, "演示")
    internal = prod.create_project_from_won_bid(won["id"], workshop="管件车间",
                                                owner="张哥", operator="演示")

    # 未中标项目（不可排产）
    lost = bids.create_project("演示·某电厂未中标项目", customer="某电厂", owner="张哥")
    bids.add_item(lost["id"], "法兰", qty=5, purchase_price=320.0)
    bids.mark_quoted(lost["id"], "演示")
    bids.set_status(lost["id"], "未中标", "演示")

    # 链路 A：询配码项目(虚值) → 提交用户 → 两张外部订单(一码多单) → 排产
    cp = codes.create_project("演示·2026年度管件询配码清单", customer="华东电建",
                              file_path=r"data\attachments\DEMO_XPM_V1.xlsx",
                              version="V1", owner="张哥")
    ci1 = codes.add_item(
        cp["id"], "耐磨弯头", spec="90° DN250 S=8", material="20#",
        std_code="GB/T12459-2017", qty_reference=100,
        inquiry_item_name="90度内衬耐磨弯头", goods_code="SP-250-90",
        material_code="WL-003215", unit="件", factory_price=1850.0,
        item_detail="DN250 S=8，R=1.5D，内壁堆焊", remark="虚值仅统计",
        tech_requirement="耐磨层 ≥ 4mm，HRC≥58")
    ci2 = codes.add_item(
        cp["id"], "U形波纹补偿器", spec="DN300 70B125-2002",
        material="S30408", qty_reference=200,
        inquiry_item_name="DN300 U形补偿器", goods_code="SP-300-U",
        material_code="WL-007781", unit="台", factory_price=4980.0,
        item_detail="U形波纹管，70B125-2002，双波", remark="按批次供货",
        tech_requirement="疲劳寿命 ≥ 1000 次循环")
    ci3 = codes.add_item(
        cp["id"], "带颈对焊法兰", spec="DN250 PN16", qty_reference=50,
        inquiry_item_name="WN法兰 DN250", goods_code="SP-250-FL",
        material_code="WL-002044", unit="片", factory_price=420.0,
        item_detail="WN RF PN16，20# 锻件", remark="—",
        tech_requirement="按 GB/T 9115-2010 验收")
    codes.submit(cp["id"], operator="演示")

    order1 = orders.create_from_code_project(cp["id"], items=[
        {"source_code_item_id": ci1["id"], "qty_ordered": 30, "unit_price": 2100.0},
        {"source_code_item_id": ci2["id"], "qty_ordered": 45, "unit_price": 5200.0},
        {"item_name": "新增紧固件", "spec": "M20", "qty_ordered": 10, "unit_price": 12.0},
    ], customer="华东电建", required_date="2026-12-20", owner="演示")
    orders.confirm(order1["id"], "演示")
    order2 = orders.create_from_code_project(cp["id"], items=[
        {"source_code_item_id": ci1["id"], "qty_ordered": 25, "unit_price": 2100.0},
        {"source_code_item_id": ci3["id"], "qty_ordered": 15, "unit_price": 480.0},
    ], customer="华东电建", required_date="2027-01-20", owner="演示")
    orders.confirm(order2["id"], "演示")

    pa = prod.create_project("演示·第一批排产（12月交付）",
                             source_bid_project_id=won["id"],
                             plan_start="2026-11-10", plan_end="2026-12-05",
                             workshop="管件车间", owner="张哥")
    pb = prod.create_project("演示·第二批排产（次年1月交付）",
                             plan_start="2026-12-10", plan_end="2027-01-15",
                             workshop="管件车间", owner="张哥")
    oi = orders.list_items(order1["id"])
    prod.link_order_item(pa["id"], oi[0]["id"], operator="演示")
    prod.link_order_item(pa["id"], oi[1]["id"], 20, operator="演示")
    prod.link_order_item(pb["id"], oi[1]["id"], 25, operator="演示")
    prod.link_order_item(pb["id"], orders.list_items(order2["id"])[0]["id"], operator="演示")
    prod.add_task(pa["id"], "下料", qty=30, workcenter="下料班")
    prod.add_task(pa["id"], "焊接/热处理", qty=30, workcenter="焊工班")

    return (f"演示数据已写入：中标项目 {won['doc_no']} → 内部订单 "
            f"{internal['order']['doc_no']}（实值 {internal['total_allocated_real']:g}）；"
            f"询配码 {cp['doc_no']} → 2 张外部订单 → 2 个排产项目")

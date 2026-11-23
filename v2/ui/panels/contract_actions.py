#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采购合同动作（ContractActionsMixin · v3.4.0）

挂在【生产排产】操作模块，三个入口：
  · 采购合同管理 —— 打开管理窗口（生成 / 导入 / 编辑 / 状态 / 附件一屏完成）
  · 生成采购合同 —— 选中订单，一键建台账 + 生成 Word 合同 + 聚焦到该合同
  · 导入采购合同 —— 选中订单，一键导入已有合同文件（v3.4.5 导入即签订：
    编号取文件名中文前的字符、名称取文件名、状态自动「已签订」；
    v3.4.6 合同金额从【合同文档总金额】读取，不再取订单价税合计）

「生成」快捷入口只复用该订单【最新的草稿】台账，已签订/执行中/完成的合同一律
不碰，改为新建一份草稿（v3.5.0：一单多供应商各签各的合同，不再抢第一份）；
草稿金额留 0（v3.4.6 口径：合同金额只认合同文档，绝不取订单价税合计）。
供方、物资分配与商务条款留到合同管理窗口里补，绝不猜测供应商信息制造假数据。
「导入」不再预建草稿：由服务层按文件名编号归档，金额一律以合同文档为准；
识别不到总金额就留空并提示人工补录。
只有新建合同（表单）初始状态才是「草稿」。

独立成 Mixin 的原因：主窗口骨架有 <900 行的架构守卫，业务动作一律放动作层。
"""
from pathlib import Path
from typing import Any, Dict, Optional

from ...core import current_user as cur
from ...domains.contract import (CONTRACT_FILTER, amount_summary,
                                 auto_apply_summary)
from .. import theme


class ContractActionsMixin:

    # ── 当前选中订单（订单视图主表）────────────────────────

    def _current_order_id(self) -> Optional[int]:
        from .common import selected_id
        try:
            return selected_id(self.tbl_orders)
        except Exception:                                    # noqa: BLE001
            return None

    def _contract_for_order(self, order_id: int,
                            operator: str = "") -> Dict[str, Any]:
        """v3.5.0：一单多供应商时【不再抢第一份合同】。

        旧逻辑 `if existing: return existing[0]` 会把第二家供应商的合同
        复用/覆盖到最早那份台账上。现在只复用尚未签订的草稿（最新一份），
        已签订 / 执行中 / 已完成 / 作废的台账一律不碰，直接新建一份草稿。
        草稿金额不再带订单总额（v3.4.6 口径：合同金额只认合同文档）。
        """
        existing = self.contracts.list(order_id=int(order_id))
        drafts = [c for c in existing if c["status"] == "草稿"]
        if drafts:
            return drafts[0]                 # list() 按 id DESC，取最新草稿
        order = self.db.get_by_id("orders", int(order_id))
        if not order:
            raise ValueError(f"订单 {order_id} 不存在")
        signed = len(existing) - len(drafts)
        return self.contracts.create({
            "contract_name": f"{order['doc_no']} 采购合同"
                             + (f"（{signed + 1}）" if existing else ""),
            "contract_type": "物资采购",
            "order_id": int(order_id),
            "supplier": "",
            "buyer": order.get("customer") or "",
            "amount": 0,
            "amount_source": "manual",
            "sign_date": "",
            "delivery_date": order.get("required_date") or "",
            "status": "草稿",
            "remark": "由生产排产操作模块按订单自动登记；不同供方请分别新建合同并在"
                      "「合同物资分配」页指定该合同覆盖的物资；金额以合同文档为准。",
        }, operator or cur.name())

    # ── 三个动作 ──────────────────────────────────────────

    def _purchase_contracts(self, order_id: Optional[int] = None,
                            focus_contract_id: Optional[int] = None):
        """生产排产 → 采购合同管理（主入口）"""
        from ..contract_dialog import PurchaseContractDialog
        oid = order_id if order_id is not None else self._current_order_id()
        dlg = PurchaseContractDialog(self, self.contracts, order_id=oid,
                                     focus_contract_id=focus_contract_id)
        dlg.exec()
        return dlg.summary_text()

    def _generate_purchase_contract(self):
        """选中订单 → 按订单明细生成《采购合同》.docx 并登记台账"""
        oid = self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        c = self._contract_for_order(int(oid))
        res = self.contracts.generate(int(c["id"]), cur.name())
        note = "新生成" if res["created"] else "复用既有"
        self._purchase_contracts(order_id=int(oid), focus_contract_id=int(c["id"]))
        return (f"{c['doc_no']}：{note}《{Path(res['path']).name}》，"
                "已在采购合同管理中打开，可直接打开用 Word 编辑")

    def _import_purchase_contract(self):
        """选中订单 → 导入已有合同文件并登记台账（v3.4.5 导入即签订）"""
        oid = self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        paths = theme.pick_paths(self, "导入采购合同文件（可多选 · 导入即签订）",
                                 CONTRACT_FILTER)
        if not paths:
            return None
        res = self.contracts.import_contract_files(int(oid), paths, cur.name())
        focus = int(res["contracts"][0]["id"]) if res["contracts"] else None
        kinds = "、".join(f"{k} {n} 个" for k, n in res["kinds"].items())
        nos = "、".join(c["doc_no"] for c in res["contracts"]) or "—"
        money = amount_summary(res)
        applied = auto_apply_summary(res)
        missing = res.get("amount_missing") or []
        self._purchase_contracts(order_id=int(oid), focus_contract_id=focus)
        return (f"{nos}：已导入 {res['files']} 个文件（{kinds}）；"
                "编号 / 名称按文件名自动带出，状态已自动置为「已签订」；"
                + (f"合同金额取自文档：{money}。" if money else "")
                + (f"物资分配：{applied}。" if applied else "")
                + (f"其中 {len(missing)} 份未识别到总金额，请在台账手工补录。"
                   if missing else ""))

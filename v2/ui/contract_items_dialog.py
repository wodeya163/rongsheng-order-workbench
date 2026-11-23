#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采购合同物资分配（v3.5.0）

一张订单多项物资可分签不同供应商：在本窗口勾选「这份合同覆盖哪几项物资」，
填本次采购数量与含税单价，保存即写入 purchase_contract_items 桥表，
订单盈亏由 domains/profit.py 按 order_item_id 归集。

  · 可切换订单 → 一份合同也能覆盖多张订单的物资（合并采购）；
  · 数量守恒：本次数量 + 其他合同已占 ≤ 订单明细实值（服务层硬校验）；
  · 含税单价是采购价，生成 Word 合同模板时只输出本合同分配行 + 采购单价；
  · 合同台账金额仍以合同文档为准，「按明细汇总金额」是显式的一键对齐动作。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
)

from ..core import money
from ..core.errors import ValidationError
from . import theme
from .base_dialog import ThemedDialog

HEADERS = ["选", "行号", "物资名称", "规格", "订单数量", "他合同已占",
           "剩余可采购", "本次数量", "含税单价", "税率(%)", "含税金额", "备注"]


class ContractItemsDialog(ThemedDialog):
    """为一份采购合同指定其覆盖的订单物资明细"""

    def __init__(self, parent, svc, contract_id: int,
                 badge: str = theme.BADGE):
        self.svc = svc
        self.db = svc.db
        self.contract_id = int(contract_id)
        self._mine: Dict[int, Dict[str, Any]] = {}
        self._candidates: List[Dict[str, Any]] = []
        self._loading = False
        super().__init__(parent, "合同物资分配",
                         "勾选本合同覆盖的物资 · 填采购数量与含税单价　|　订单盈亏归集依据",
                         width=1180, height=720, badge=badge)
        v = self.body_layout
        v.addLayout(self._build_top())
        self.hint = theme.label("", "DialogHint", word_wrap=True)
        v.addWidget(self.hint)
        self.table = theme.style_table(QTableWidget())
        self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
        self.table.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.table, 1)
        v.addLayout(self._build_actions())
        self.add_buttons(ok_text="关闭", show_cancel=False)
        self._reload()

    # ── 顶部 ────────────────────────────────────────────────

    def _build_top(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        self.info = theme.label("", "KpiLabel", word_wrap=True)
        row.addWidget(self.info, 1)
        row.addWidget(QLabel("订单："))
        self.order_box = QComboBox()
        self.order_box.addItem("（不关联订单）", 0)
        for o in self.db.query(
                "SELECT id, doc_no, project_no, customer FROM orders "
                "ORDER BY id DESC"):
            caption = f"{o['project_no'] or o['doc_no']}（{o['customer'] or '—'}）"
            self.order_box.addItem(caption, int(o["id"]))
        c = self.svc.get(self.contract_id)
        oid = int(c.get("order_id") or 0) if c else 0
        idx = self.order_box.findData(oid)
        if idx >= 0:
            self.order_box.setCurrentIndex(idx)
        self.order_box.currentIndexChanged.connect(self._reload)
        row.addWidget(self.order_box, 1)
        return row

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        b = QPushButton("从合同文档自动匹配")
        b.setToolTip("读取已归档合同文档的物资明细表，自动勾选物资并填入数量与含税单价；"
                     "只预填到本页，核对后点「保存分配」才写库")
        b.clicked.connect(self._do_auto_match)
        row.addWidget(b)
        b = QPushButton("保存分配")
        b.setObjectName("Primary")
        b.setToolTip("全量替换本合同的物资分配行；数量超订单实值会被拦截")
        b.clicked.connect(self._do_save)
        row.addWidget(b)
        b = QPushButton("按明细汇总金额")
        b.setToolTip("把合同台账金额显式对齐到 Σ 分配明细（金额来源标记 items）")
        b.clicked.connect(self._do_sync_amount)
        row.addWidget(b)
        b = QPushButton("清空分配")
        b.clicked.connect(self._do_clear)
        row.addWidget(b)
        b = QPushButton("全选 / 全不选")
        b.clicked.connect(self._do_toggle_all)
        row.addWidget(b)
        row.addStretch(1)
        return row

    # ── 数据 ────────────────────────────────────────────────

    def _reload(self, *_args) -> None:
        c = self.svc.get(self.contract_id)
        if not c:
            self.hint.setText("合同不存在，请关闭窗口后刷新台账。")
            return
        oid = int(self.order_box.currentData() or 0)
        self._mine = {int(r["order_item_id"]): dict(r)
                      for r in self.svc.contract_items(self.contract_id)}
        rows = self.svc.candidate_items(self.contract_id, oid or None)
        self._candidates = rows
        self._loading = True
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            iid = int(r["id"])
            mine = self._mine.get(iid, {})
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                         | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if mine else Qt.Unchecked)
            self.table.setItem(i, 0, chk)
            for col, value in ((1, r["line_no"]), (2, r["item_name"]),
                               (3, r.get("spec") or ""),
                               (4, f"{float(r['qty_ordered']):g}"),
                               (5, f"{r['used_qty']:g}"),
                               (6, f"{r['remaining_qty']:g}")):
                item = QTableWidgetItem(str(value))
                item.setFlags(Qt.ItemIsEnabled)
                self.table.setItem(i, col, item)
            for col, value in ((7, mine.get("qty") or r["remaining_qty"] or 0),
                               (8, mine.get("unit_price") or 0),
                               (9, mine.get("tax_rate") or 0),
                               (11, mine.get("remark") or "")):
                item = QTableWidgetItem(f"{float(value):g}"
                                        if col != 11 else str(value))
                if col in (7, 8, 9, 11):
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable
                                  | Qt.ItemIsSelectable)
                self.table.setItem(i, col, item)
            amount = QTableWidgetItem(
                f"{float(money.mul(mine.get('qty') or 0, mine.get('unit_price') or 0)):,.2f}"
                if mine else "0.00")
            amount.setFlags(Qt.ItemIsEnabled)
            amount.setForeground(QColor(theme.BRAND_DARK))
            self.table.setItem(i, 10, amount)
            self.table.setRowHeight(i, 30)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for col in (7, 8, 9):
            self.table.setColumnWidth(col, max(90, self.table.columnWidth(col)))
        self.table.blockSignals(False)
        self._loading = False
        total = sum(float(x["amount"] or 0) for x in self._mine.values())
        self.info.setText(
            f"合同 {c['doc_no']}｜{c['contract_name']}｜供方 {c['supplier'] or '未填'}"
            f"｜台账金额 ¥{float(c['amount'] or 0):,.2f}"
            f"｜已分配 {len(self._mine)} 项 / ¥{total:,.2f}"
            f"｜{'含税' if int(c.get('tax_included') or 1) else '不含税'}")
        self._set_hint(c, total)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() not in (7, 8):
            return
        row = item.row()
        qty = self._num(row, 7)
        price = self._num(row, 8)
        self._loading = True
        self.table.blockSignals(True)
        amount = self.table.item(row, 10)
        if amount:
            amount.setText(f"{float(money.mul(qty, price)):,.2f}")
        self.table.blockSignals(False)
        self._loading = False

    def _num(self, row: int, col: int) -> float:
        item = self.table.item(row, col)
        if not item:
            return 0.0
        try:
            return float(str(item.text()).replace(",", "").strip() or 0)
        except ValueError:
            return 0.0

    def _checked(self, row: int) -> bool:
        item = self.table.item(row, 0)
        return bool(item and item.checkState() == Qt.Checked)

    def _set_hint(self, c: Dict[str, Any], total: float) -> None:
        gap = float(money.q2(money.to_dec(total) - money.to_dec(c["amount"] or 0)))
        note = ""
        if total and abs(gap) > 0.01:
            note = (f"　注意：已分配明细合计与合同台账金额相差 ¥{gap:,.2f}，"
                    "可点「按明细汇总金额」对齐（合同金额默认以文档为准）。")
        self.hint.setText(
            "勾选本合同覆盖的物资 → 填「本次数量 / 含税单价」→ 保存。"
            "剩余可采购数量已扣除其他合同占用；未关联已签合同的物资，"
            "订单利润率会标「不完整」。切换订单可让一份合同覆盖多张订单。" + note)

    # ── 动作 ────────────────────────────────────────────────

    def _specs(self) -> List[Dict[str, Any]]:
        specs: List[Dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            if not self._checked(row) or row >= len(self._candidates):
                continue
            specs.append({
                "order_item_id": int(self._candidates[row]["id"]),
                "qty": self._num(row, 7),
                "unit_price": self._num(row, 8),
                "tax_rate": self._num(row, 9),
                "remark": (self.table.item(row, 11).text()
                           if self.table.item(row, 11) else ""),
            })
        return specs

    def _do_auto_match(self) -> None:
        """v3.5.1：从合同文档明细表生成分配建议并【预填到本页】，绝不写库。

        名称全称对不上是常态（合同叫「双金属复合弯头」、订单叫「七段合金衬弯头」），
        服务层按类型词 / 规格数字 / 数量打分；低分与无单价的行不勾选，留给用户手工。
        """
        oid = int(self.order_box.currentData() or 0)
        try:
            res = self.svc.suggest_items_from_file(self.contract_id, oid or None)
        except Exception as exc:                             # noqa: BLE001
            theme.notify(self, "自动匹配失败", str(exc), kind="warn")
            return
        warns = [str(w) for w in (res.get("warnings") or [])]
        if not res.get("ok"):
            msg = "；".join(warns) or "合同文档里没有可识别的物资明细表。"
            self.hint.setText("自动匹配未生效：" + msg)
            theme.notify(self, "未找到可自动匹配的物资", msg, kind="warn")
            return

        by_iid = {int(s["order_item_id"]): s for s in res["suggestions"]}
        self._loading = True
        self.table.blockSignals(True)
        filled = 0
        review = 0
        for row in range(self.table.rowCount()):
            if row >= len(self._candidates):
                continue
            s = by_iid.get(int(self._candidates[row]["id"]))
            if not s:
                continue                    # 未匹配行保持用户原输入，不擅自清空
            chk = self.table.item(row, 0)
            if chk:
                chk.setCheckState(Qt.Checked)
            for col, val in ((7, s["qty"]), (8, s["unit_price"]), (9, 0)):
                item = self.table.item(row, col)
                if item:
                    item.setText(f"{float(val):g}")
            amt = self.table.item(row, 10)
            if amt:
                amt.setText(f"{float(money.mul(s['qty'], s['unit_price'])):,.2f}")
            remark = self.table.item(row, 11)
            if remark:
                mark = "" if s["confidence"] == "高" else "（需复核）"
                remark.setText(f"自动匹配自合同第 {s['source_row']} 行{mark}")
            filled += 1
            if s["confidence"] != "高":
                review += 1
        self.table.blockSignals(False)
        self._loading = False

        total = sum(float(money.mul(x["qty"], x["unit_price"]))
                    for x in self._specs())
        parts = [
            f"自动匹配：{res['file_name']}｜{res['source']} 第 {res['table_index']} 张表",
            f"合同明细 {len(res['lines'])} 行 / ¥{res['total']:,.2f}"
            f"（台账 ¥{res['contract_amount']:,.2f}，差额 ¥{res['gap']:,.2f}）",
            f"已预填 {filled} 项（需复核 {review}），未匹配 "
            f"{len(res['unmatched'])} 行；本页含税金额合计 ¥{total:,.2f}。",
            "核对后点「保存分配」才写库；一份合同跨多张订单时，切到对应订单再点一次。",
        ]
        if warns:
            parts.append("提示：" + "；".join(warns))
        self.hint.setText("　".join(parts))

        msg = [
            f"从「{res['file_name']}」读到 {len(res['lines'])} 行物资明细，"
            f"已预填 {filled} 项的数量与含税单价（需复核 {review} 项）。",
        ]
        if res["unmatched"]:
            names = "、".join(x["name"] for x in res["unmatched"][:5])
            if len(res["unmatched"]) > 5:
                names += f" 等 {len(res['unmatched'])} 行"
            msg.append(f"未自动匹配：{names}，请在表格中手工勾选。")
        msg.append("本页只是预填，点「保存分配」才写入数据库。")
        theme.notify(self, "已填入分配建议", "\n".join(msg))

    def _do_save(self) -> None:
        # 一份合同可覆盖多张订单：切到 B 订单保存时，必须把 A 订单上
        # 已存在的分配行原样带上，否则全量替换会把它们抹掉。
        current_oid = int(self.order_box.currentData() or 0)
        kept = [
            {"order_item_id": int(a["order_item_id"]),
             "qty": a["qty"], "unit_price": a["unit_price"],
             "tax_rate": a["tax_rate"], "remark": a.get("remark") or ""}
            for a in self.svc.contract_items(self.contract_id)
            if int(a.get("order_id") or 0) != current_oid
        ]
        try:
            res = self.svc.set_contract_items(
                self.contract_id, kept + self._specs(), "")
        except (ValidationError, ValueError) as exc:
            theme.notify(self, "无法保存", str(exc), kind="warn")
            return
        self._reload()
        theme.notify(self, "已保存",
                     f"本合同已分配 {res['count']} 项物资，"
                     f"含税金额合计 ¥{res['amount']:,.2f}。")

    def _do_sync_amount(self) -> None:
        res = self.svc.sync_amount_from_items(self.contract_id)
        theme.notify(self, "已对齐",
                     f"合同金额已按分配明细写为 ¥{float(res['amount']):,.2f}。")
        self._reload()

    def _do_clear(self) -> None:
        if not theme.confirm(self, "清空分配",
                             "确定清空本合同全部物资分配吗？",
                             "合同台账金额不会被改动，订单利润率将标为「不完整」。"):
            return
        self.svc.set_contract_items(self.contract_id, [], "")
        self._reload()

    def _do_toggle_all(self) -> None:
        target = not all(self._checked(r) for r in range(self.table.rowCount()))
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.Checked if target else Qt.Unchecked)

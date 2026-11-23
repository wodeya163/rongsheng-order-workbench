#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
经营分析窗口（v3.5.0）：订单采购价 / 销售价汇总、利润率、柱线组合图

一个窗口给出三件事：
  · 汇总：按订单 / 按供应商 / 按月份切换，销售额、采购额、毛利、利润率同源；
  · 图表：分组柱（销售额 / 采购额 / 毛利）+ 折线（利润率，次 Y 轴）；
  · 导出：图表 PNG、账面 Excel（订单汇总 / 物资明细 / 供方分析三张表）。

口径全部来自 v2/domains/profit.py，本窗口不做任何金额计算：
  销售额 = Σ 订单明细（与表头差额告警）、采购额 = 已签合同分配明细（统一含税）、
  未覆盖物资标「不完整」、含税口径混用时利润率「不出数」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
)

from ..core import excelio
from ..domains.profit import OrderProfitService
from . import theme
from .base_dialog import ThemedDialog
from .profit_chart import ProfitChart

SUMMARY_HEADERS = ["订单项目编号", "客户名称", "下单时间", "状态",
                   "销售额(含税)", "采购额(含税)", "毛利", "利润率(%)",
                   "覆盖度(%)", "完整性", "供方数"]
DIM_HEADERS = ["维度", "销售额(含税)", "采购额(含税)", "毛利", "利润率(%)"]


class ProfitDialog(ThemedDialog):
    """订单盈亏看板（图表 + 汇总表 + 导出）"""

    def __init__(self, parent, db, badge: str = theme.BADGE):
        self.db = db
        self.svc = OrderProfitService(db)
        self._summary: List[Dict[str, Any]] = []
        self._rows: List[Dict[str, Any]] = []
        super().__init__(parent, "经营分析 · 订单盈亏",
                         "销售 / 采购 / 毛利 / 利润率　|　柱线组合图与账面导出",
                         width=1240, height=880, badge=badge)
        v = self.body_layout
        v.addLayout(self._build_filters())
        self.hint = theme.label("", "DialogHint", word_wrap=True)
        v.addWidget(self.hint)
        self.chart = ProfitChart()
        v.addWidget(self.chart, 3)
        self.table = theme.style_table(QTableWidget())
        v.addWidget(self.table, 2)
        v.addLayout(self._build_actions())
        self.add_buttons(ok_text="关闭", show_cancel=False)
        self._reload()

    # ── 控件 ────────────────────────────────────────────────

    def _build_filters(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(QLabel("维度："))
        self.dim_box = QComboBox()
        for text, key in (("按订单", "order"), ("按供应商", "supplier"),
                          ("按月份", "month")):
            self.dim_box.addItem(text, key)
        self.dim_box.currentIndexChanged.connect(self._reload)
        row.addWidget(self.dim_box)
        row.addWidget(QLabel("订单状态："))
        self.status_box = QComboBox()
        self.status_box.addItem("全部", "")
        for r in self.db.query(
                "SELECT DISTINCT status FROM orders WHERE status IS NOT NULL "
                "ORDER BY status"):
            self.status_box.addItem(str(r["status"]), str(r["status"]))
        self.status_box.currentIndexChanged.connect(self._reload)
        row.addWidget(self.status_box)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("项目编号 / 客户名称，支持模糊筛选")
        self.keyword.returnPressed.connect(self._reload)
        row.addWidget(self.keyword, 1)
        self.limit_box = QComboBox()
        for text, n in (("最近 20 项", 20), ("最近 50 项", 50),
                        ("最近 100 项", 100), ("全部", 0)):
            self.limit_box.addItem(text, n)
        self.limit_box.currentIndexChanged.connect(self._reload)
        row.addWidget(self.limit_box)
        b = QPushButton("刷新")
        b.clicked.connect(self._reload)
        row.addWidget(b)
        return row

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        b = QPushButton("刷新数据")
        b.setObjectName("Primary")
        b.clicked.connect(self._reload)
        row.addWidget(b)
        b = QPushButton("导出图表 PNG")
        b.setToolTip("把当前柱线组合图保存为 PNG 图片")
        b.clicked.connect(self._do_export_png)
        row.addWidget(b)
        b = QPushButton("导出账面 Excel")
        b.setToolTip("导出订单盈亏汇总 / 物资盈亏明细 / 供方采购分析三张表")
        b.clicked.connect(self._do_export_excel)
        row.addWidget(b)
        b = QPushButton("合同物资分配")
        b.setToolTip("打开采购合同管理，为不同供应商指定该合同覆盖的物资")
        b.clicked.connect(self._do_contracts)
        row.addWidget(b)
        row.addStretch(1)
        return row

    # ── 数据 ────────────────────────────────────────────────

    def _reload(self, *_args) -> None:
        dim = self.dim_box.currentData() or "order"
        status = self.status_box.currentData() or ""
        kw = (self.keyword.text() or "").strip().lower()
        limit = int(self.limit_box.currentData() or 0)
        self._summary = self.svc.overview()
        if status:
            self._summary = [r for r in self._summary if r["status"] == status]
        if kw:
            self._summary = [r for r in self._summary
                             if kw in (r["project_no"] or "").lower()
                             or kw in (r["customer"] or "").lower()
                             or kw in (r["doc_no"] or "").lower()]

        if dim == "order":
            rows = [{
                "label": r["project_no"] or r["doc_no"],
                "sales_total": r["sales_total"],
                "purchase_total": r["purchase_total"],
                "gross_profit": r["gross_profit"],
                "profit_rate": r["profit_rate"],
            } for r in self._summary]
        else:
            rows = self.svc.breakdown(dim)
            if kw:
                rows = [r for r in rows if kw in r["label"].lower()]
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        self._rows = rows
        self.chart.set_rows(rows)
        self._fill_table(rows, dim)
        self._set_hint(rows, dim)

    def _fill_table(self, rows: List[Dict[str, Any]], dim: str) -> None:
        from .panels.common import fill_table

        if dim == "order":
            bylabel = {r["project_no"] or r["doc_no"]: r
                       for r in self._summary}
            data = [bylabel[r["label"]] for r in rows
                    if r["label"] in bylabel]
            headers = SUMMARY_HEADERS
            body = [(r["project_no"] or r["doc_no"], r["customer"],
                     r["order_date"], r["status"], r["sales_total"],
                     r["purchase_total"], r["gross_profit"],
                     (f"{r['profit_rate']:.2f}"
                      if r["profit_rate"] is not None else "不出数"),
                     r["coverage"],
                     "完整" if r["complete"] else "不完整",
                     r["supplier_count"]) for r in data]
        else:
            headers = DIM_HEADERS
            body = [(r["label"], r["sales_total"], r["purchase_total"],
                     r["gross_profit"],
                     (f"{r['profit_rate']:.2f}"
                      if r["profit_rate"] is not None else "不出数"))
                    for r in rows]
        fill_table(self.table, headers, body, stretch_col=0)

    def _set_hint(self, rows: List[Dict[str, Any]], dim: str) -> None:
        if not rows:
            self.hint.setText(
                "暂无可分析数据。口径：销售额取订单明细汇总（与表头差额告警），"
                "采购额只统计已签订合同的物资分配（统一含税）；"
                "请先在「采购合同管理 → 合同物资分配」为供应商指定物资。")
            return
        sales = sum(float(r["sales_total"] or 0) for r in rows)
        purchase = sum(float(r["purchase_total"] or 0) for r in rows)
        profit = sum(float(r["gross_profit"] or 0) for r in rows)
        incomplete = sum(1 for r in self._summary if not r["complete"])
        no_rate = sum(1 for r in self._summary if r["profit_rate"] is None)
        warn = ""
        if incomplete:
            warn += f"；{incomplete} 单物资未全部签订采购合同，利润率标「不完整」"
        if no_rate:
            warn += f"；{no_rate} 单含税口径不一致，利润率「不出数」"
        self.hint.setText(
            f"当前 {len(rows)} 项（{self.dim_box.currentText()}）："
            f"销售额 ¥{sales:,.2f} − 采购额 ¥{purchase:,.2f} "
            f"= 毛利 ¥{profit:,.2f}"
            + (f"，综合利润率 {profit / sales * 100:.2f}%" if sales else "")
            + warn + "。鼠标移到图上可看单项明细。")

    # ── 动作 ────────────────────────────────────────────────

    def _do_export_png(self) -> None:
        path = theme.pick_path(self, "导出柱线组合图", mode="save",
                               default_name="订单盈亏柱线图.png",
                               filters="PNG 图片 (*.png)")
        if not path:
            return
        try:
            self.chart.export_png(path)
            theme.notify(self, "导出完成", f"图表已保存：{path}")
        except OSError as exc:
            theme.notify(self, "导出失败", str(exc), kind="error")

    def _do_export_excel(self) -> None:
        path = theme.pick_path(self, "导出订单盈亏账面", mode="save",
                               default_name="订单盈亏账面.xlsx",
                               filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        try:
            excelio.export_workbook(path, self.svc.export_sheets(self._summary))
            theme.notify(self, "导出完成",
                         f"已导出 {len(self._summary)} 张订单的盈亏账面：{path}")
        except Exception as exc:  # noqa: BLE001
            theme.notify(self, "导出失败", str(exc), kind="error")

    def _do_contracts(self) -> None:
        from ..domains.contract import PurchaseContractService
        from .contract_dialog import PurchaseContractDialog

        svc = (getattr(self.parent(), "contracts", None)
               or PurchaseContractService(self.db))
        PurchaseContractDialog(self, svc).exec()
        self._reload()

    def summary_text(self) -> str:
        dim = self.dim_box.currentText()
        return f"经营分析（{dim}）：{len(self._rows)} 项，柱线组合图与账面已就绪"

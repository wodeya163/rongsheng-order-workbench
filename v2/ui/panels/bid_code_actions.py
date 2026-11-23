#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
① 询报价 / ② 询配码 动作（BidCodeActionsMixin）

每个方法对应左侧操作列表或菜单里的一项动作，统一经 self._run() 包异常。
本模块由 main_window_v2.py 拆分而来（P2-2），方法实现逐字迁移、未改行为；
跨模块协作一律走 self.xxx；禁止反向引用主窗口模块（会形成循环导入）。
"""
from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel, QComboBox,
    QHeaderView, QSplitter, QLineEdit, QFrame, QStatusBar,
    QTreeWidget, QTreeWidgetItem, QStackedWidget, QGroupBox, QScrollArea,
    QCheckBox, QSizePolicy, QAbstractItemView, QDialog, QMenu,
)  # noqa: F401
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QColor

from .. import dialogs, theme, query_panel
from ...core.db import Database, now
from ...core import models, excelio, applog
from ...core import query as qbuild
from ...core import current_user as cur
from ...core import fields as fdefs
from ...core import order_fields as ofdefs
from ...domains import (BidService, PartCodeService, OrderService,
                        ProductionService, TraceService, MaintenanceService,
                        OrderScheduleService)
from .common import (VIEW_NAMES, VIEW_KEYS, ACTION_GROUPS, TABLE_ENTITIES,
                     fill_table, make_readonly_table, selected_id, select_id,
                     _brief_json, _titled_table, _result_page)



class BidCodeActionsMixin:
    # ── ① 询报价动作 ─────────────────────────────────────

    def _new_bid(self):
        data = dialogs.ask(self, "新建招投标项目", dialogs.bid_project_fields())
        if data:
            p = self.bids.create_project(**data)
            return p["doc_no"]

    def _add_bid_item(self):
        pid = self._require(self.tbl_bids, "招投标项目")
        if pid is None:
            return
        data = dialogs.ask(self, "添加投标明细", dialogs.bid_item_fields())
        if data:
            self.bids.add_item(pid, **data)
            return "已添加 1 行"

    def _mark_quoted(self):
        pid = self._require(self.tbl_bids, "招投标项目")
        if pid is not None:
            self.bids.mark_quoted(pid, cur.name())

    def _mark_won(self):
        pid = self._require(self.tbl_bids, "招投标项目")
        if pid is not None:
            self.bids.mark_won(pid, True, cur.name())
            return "已中标（可生成内部订单）"

    def _mark_lost(self):
        pid = self._require(self.tbl_bids, "招投标项目")
        if pid is not None:
            self.bids.mark_won(pid, False, cur.name())
            return "已标记未中标（不可排产）"

    def _gen_internal(self):
        pid = self._require(self.tbl_bids, "中标项目")
        if pid is None:
            return
        order = self.orders.create_internal_from_bid(pid, owner=cur.name())
        return f"已生成 {order['doc_no']}，实值 {order['qty_ordered_real']:g}"

    def _won_to_production(self):
        rows = self.bids.won_projects()
        if not rows:
            theme.notify(self, "没有中标项目", "请先在①把项目标记为「中标」", kind="warn")
            return
        picked = dialogs.ask(self, "中标项目一键排产（自动生成内部订单）", [
            {"key": "bid_project_id", "label": "中标项目", "type": "combo",
             "options": dialogs.options_from(
                 rows, lambda r: f"{r['doc_no']}  {r['project_name']}"), "required": True},
            {"key": "title", "label": "生产项目名称（留空自动）"},
            {"key": "workshop", "label": "车间"},
            {"key": "plan_start", "label": "计划开工", "type": "date"},
            {"key": "plan_end", "label": "计划完工", "type": "date"},
        ])
        if not picked:
            return
        result = self.prod.create_project_from_won_bid(
            picked["bid_project_id"], title=picked["title"],
            workshop=picked["workshop"], plan_start=picked["plan_start"],
            plan_end=picked["plan_end"], owner=cur.name(), operator=cur.name())
        return (f"{result['project']['doc_no']} ← 内部订单 {result['order']['doc_no']}，"
                f"{len(result['links'])} 条关联，实值 {result['total_allocated_real']:g}"
                + ("（订单已存在，复用）" if not result["created_order"] else ""))

    def _import_bid_items(self):
        n = dialogs.import_items(self, self.db, "bid_item")
        return f"导入 {n} 行" if n else None

    def _export_bids(self):
        heads = self.db.query("SELECT * FROM bid_projects ORDER BY id")
        items = self.db.query("SELECT * FROM bid_items ORDER BY id")
        sheets = {
            "招投标项目": (list(heads[0].keys()) if heads else ["(空)"],
                          [list(r.values()) for r in heads]),
            "投标明细": (list(items[0].keys()) if items else ["(空)"],
                        [list(r.values()) for r in items]),
        }
        return dialogs.export_sheets(self, sheets, "招投标项目导出.xlsx")

    # ── ② 询配码动作 ─────────────────────────────────────

    def _new_code(self):
        data = dialogs.ask(self, "新建询配码项目", dialogs.code_project_fields())
        if data:
            p = self.codes.create_project(**data)
            return p["doc_no"]

    def _add_code_item(self):
        pid = self._require(self.tbl_codes, "询配码项目")
        if pid is None:
            return
        data = dialogs.ask(
            self, "添加询配码明细",
            dialogs.code_item_fields(),
            subtitle="10 个业务字段 · 仅「物料名称」必填；参考数量为虚值，厂家价格仅作询价信息")
        if data:
            self.codes.add_item(pid, **data)
            return "已添加 1 行（虚值仅统计，订单数量另行录入）"

    def _submit_code(self):
        pid = self._require(self.tbl_codes, "询配码项目")
        if pid is None:
            return
        data = dialogs.ask(self, "提交询配码文件给用户", [
            {"key": "file_path", "label": "文件路径/文件名", "required": True},
            {"key": "submit_date", "label": "提交日期", "type": "date"},
        ])
        if data:
            self.codes.submit(pid, file_path=data["file_path"],
                              submit_date=data["submit_date"], operator=cur.name())
            return "已提交，用户可开始分批下单"

    def _view_batches(self):
        pid = self._require(self.tbl_codes, "询配码项目")
        if pid is None:
            return
        rows = self.codes.order_batches(pid)
        headers = ["批次", "订单号", "状态", "下单日期", "交期", "客户名称",
                   "明细数", "订单实值合计", "已排量"]
        table = [(r["batch_seq"], r["doc_no"], r["status"], r["order_date"],
                  r["required_date"], r["customer"], r["item_count"],
                  r["qty_ordered_real"], r["qty_scheduled"]) for r in rows]
        dlg = dialogs.TableViewDialog(self, "一码多单 · 订单批次", headers, table)
        dlg.exec()

    def _import_code_items(self):
        """CSV / Excel 导入向导：默认带出主表当前点选的询配码项目"""
        dlg = dialogs.ImportCodeItemsDialog(
            self, self.db, selected_id(self.tbl_codes))
        if dlg.exec() != QDialog.Accepted or not dlg.result:
            return None
        warns = sum(1 for i in dlg.report["issues"] if not i.get("fatal"))
        return (f"项目 {dlg.result['doc_no']} 导入 {dlg.result['count']} 行"
                + (f"（{warns} 条提示，详见导入报告）" if warns else ""))

    def _download_code_template(self):
        path = theme.pick_path(
            self, "另存询配码明细导入模板", mode="save",
            default_name="询配码明细导入模板.xlsx",
            filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
        if not path:
            return None
        excelio.export_template(path, "code_item")
        return f"模板已保存：{path}（10 列：{' / '.join(fdefs.headers())}）"

    def _export_codes(self):
        pid = self._require(self.tbl_codes, "询配码项目")
        if pid is None:
            return
        cp = self.db.get_by_id("code_projects", pid)
        items = self.codes.list_items(pid)
        sheets = {
            "询配码项目": (["项目号", "项目名称", "客户名称", "状态", "版本", "提交文件", "提交日期"],
                         [(cp["doc_no"], cp["project_name"], cp["customer"], cp["status"],
                           cp["version"], cp["file_path"], cp["submit_date"])]),
            # ★ 导出给用户的询配码文件：严格按约定的 10 字段顺序
            "询配码明细": (fdefs.headers(), fdefs.export_rows(items)),
            "订单批次": (["批次", "订单号", "状态", "明细数", "订单实值合计", "已排量"],
                        [(b["batch_seq"], b["doc_no"], b["status"], b["item_count"],
                          b["qty_ordered_real"], b["qty_scheduled"])
                         for b in self.codes.order_batches(pid)]),
        }
        return dialogs.export_sheets(self, sheets, f"询配码_{cp['doc_no']}.xlsx")

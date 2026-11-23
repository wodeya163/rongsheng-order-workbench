#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
③ 订单 / ④ 订单排产与生产关联 动作（OrderActionsMixin）

订单动作与 v2.4 订单排产（DDPS）动作；排产守恒由服务层拦截，这里只做交互。
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



class OrderActionsMixin:
    # ── ③ 订单动作 ───────────────────────────────────────

    def _new_order(self):
        """v3.5.3 两段式：新建订单只建订单头，明细由两个独立动作补录"""
        dlg = dialogs.NewOrderDialog(self, self.db)
        if dlg.exec() != QDialog.Accepted or not dlg.result_spec():
            return None
        order = self.orders.create_from_spec(
            dlg.result_spec(), operator=cur.name(), origin="create_manual",
            allow_empty_items=True)
        s = self.orders.summary(order["id"])
        tail = (f"{s['item_count']} 项，实值合计 {s['qty_ordered_real']:g}，"
                f"价税合计 ¥{s['amount_total']:,.2f}" if s["item_count"]
                else "明细待录入：选中该单 →「录入订单明细」或「导入订单明细」")
        return (f"新建 {order['doc_no']}（{order['order_type']}），"
                f"订单项目编号「{order['project_no'] or '—'}」，"
                f"{tail}，状态「{order['status']}」")

    def _add_order(self):
        """添加订单：从图片 / 表格 / PDF / Word 读信息 → 17 列可编辑预览 → 建单"""
        dlg = dialogs.AddOrderDialog(self, self.db)
        if dlg.exec() != QDialog.Accepted or not dlg.result:
            return None
        fatal = sum(1 for i in (dlg.report or {}).get("issues", [])
                    if i.get("fatal"))
        return (f"添加 {dlg.result['order_count']} 张订单 / "
                f"{dlg.result['item_count']} 行明细，实值合计 "
                f"{dlg.result['qty_ordered_real']:g}，价税合计 "
                f"¥{dlg.result['amount_total']:,.2f}"
                + (f"（跳过 {fatal} 行）" if fatal else ""))

    def _import_orders(self):
        """导入订单：预览 + 逐行报告 + 单事务批量建单"""
        dlg = dialogs.ImportOrdersDialog(self, self.db)
        if dlg.exec() != QDialog.Accepted or not dlg.result:
            return None
        fatal = sum(1 for i in dlg.report["issues"] if i.get("fatal"))
        return (f"导入 {dlg.result['order_count']} 张订单 / "
                f"{dlg.result['item_count']} 行明细，实值合计 "
                f"{dlg.result['qty_ordered_real']:g}，价税合计 "
                f"¥{dlg.result['amount_total']:,.2f}"
                + (f"（跳过 {fatal} 行，详见导入报告）" if fatal else ""))

    # ── v3.5.3：选中订单 → 录入 / 导入订单明细 ──────────────

    def _add_order_items(self, order_id: int | None = None):
        """选中订单 → 9 字段表格批量录入明细（询配码项目的录入范式）"""
        oid = order_id if order_id is not None else self._require(
            self.tbl_orders, "订单")
        if oid is None:
            return None
        dlg = dialogs.AddOrderItemsDialog(self, self.db, oid)
        if dlg.exec() != QDialog.Accepted or not dlg.result:
            return None
        r = dlg.result
        select_id(self.tbl_orders, oid)
        return (f"订单 {r['order']['doc_no']} 已录入 {r['count']} 行明细，"
                f"累计 {r['item_count']} 行 / 实值 {r['qty_ordered_real']:g}，"
                f"价税合计 ¥{r['amount_total']:,.2f}")

    def _import_order_items(self, order_id: int | None = None):
        """选中订单 → CSV / Excel 导入明细；未选中时向导内也可指定目标订单"""
        oid = order_id if order_id is not None else selected_id(self.tbl_orders)
        dlg = dialogs.ImportOrderItemsDialog(self, self.db, oid)
        if dlg.exec() != QDialog.Accepted or not dlg.result:
            return None
        r = dlg.result
        fatal = sum(1 for i in (dlg.report or {}).get("issues", [])
                    if i.get("fatal"))
        select_id(self.tbl_orders, int(r["order"]["id"]))
        return (f"订单 {r['order']['doc_no']} 已导入 {r['count']} 行明细，"
                f"累计 {r['item_count']} 行 / 实值 {r['qty_ordered_real']:g}，"
                f"价税合计 ¥{r['amount_total']:,.2f}"
                + (f"（跳过 {fatal} 行，详见导入报告）" if fatal else ""))

    def _download_order_template(self):
        path = theme.pick_path(
            self, "另存订单导入模板", mode="save",
            default_name="订单导入模板.xlsx",
            filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
        if not path:
            return None
        excelio.export_template(path, "order")
        return (f"模板已保存：{path}（17 列 = 订单头 8 + 明细 9："
                f"{' / '.join(ofdefs.headers())}）")

    def _confirm_order(self):
        oid = self._require(self.tbl_orders, "订单")
        if oid is not None:
            self.orders.confirm(oid, cur.name())
            return "订单已确认，可排产"

    def _mark_order_shipped(self):
        """v3.3.0：选中订单 → 标记「已发货」→ 交期提醒立即停止（走状态机 + 审计）"""
        from ...core import duereminder

        oid = self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        o = duereminder.mark_shipped(self.db, oid, cur.name(self.db.data_dir))
        return (f"{o['doc_no']} 已标记「已发货」，"
                "本单不再进行交期提醒（可在审计留痕里查看）")

    # ── v2.9.2：订单编辑（更改 + 删除）────────────────────

    def _edit_order(self, order_id: int | None = None):
        """选中订单 → 专用编辑器：改头 / 改明细 / 增删行 / 删除整单，保存后自动刷新"""
        oid = order_id if order_id is not None else self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        dlg = dialogs.OrderEditDialog(self, self.db, oid)
        if dlg.exec() != QDialog.Accepted:
            return None

        if dlg.delete_requested:
            res = self.maint.delete("order", oid, operator=cur.name())
            n = sum(res["deleted_children"].values())
            return (f"已删除订单 {res['description']}"
                    + (f"，级联删除 {n} 行明细" if n else "")
                    + "；列表已刷新")

        head, updates, news, deleted = dlg.result_changes()
        res = self.orders.apply_edits(
            oid, head=head, item_updates=updates, new_items=news,
            deleted_item_ids=deleted, operator=cur.name())
        o = res["order"]
        parts = []
        if head:
            parts.append(f"头 {len(head)} 项")
        if updates:
            parts.append(f"改明细 {len(updates)} 行")
        if news:
            parts.append(f"新增 {len(news)} 行")
        if deleted:
            parts.append(f"删除 {len(deleted)} 行")
        if res.get("cleared"):
            parts.append("已恢复自动汇总")
        select_id(self.tbl_orders, oid)
        return (f"订单 {o['doc_no']} 已保存（{'、'.join(parts) or '无字段变化'}），"
                f"价税合计 {float(o['amount_total'] or 0):.2f} / "
                f"物资数量 {float(o['item_qty_total'] or 0):g} 项；列表已刷新")

    def _on_order_double_click(self, *_args):
        """双击订单行 = 直接进入编辑（最常用的路径）"""
        oid = selected_id(self.tbl_orders)
        if oid is None:
            return
        self._run("编辑订单", lambda: self._edit_order(oid))

    def _reset_totals(self, order_id: int | None = None):
        """P1-5：解除手工锁定，价税合计与物资数量按明细重算"""
        oid = order_id if order_id is not None else self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        o = self.orders.reset_totals(oid, operator=cur.name())
        return (f"已恢复自动汇总：价税合计 {o['amount_total']:.2f}、"
                f"物资数量 {o['item_qty_total']:g} 项")

    def _view_order_items(self):
        oid = self._require(self.tbl_orders, "订单")
        if oid is None:
            return
        rows = self.orders.list_items(oid)
        headers = ["行", "物资代码", "物资名称", "实际生产物资名称", "规格",
                   "单位", "数量", "单价", "总价", "参数描述",
                   "已排量", "可排量", "来源询配码项", "来源中标明细项"]
        table = []
        for i in rows:
            src_code = (self.db.get_by_id("code_items", i["source_code_item_id"]) or {}).get("item_code")
            src_bid = i["source_bid_item_id"]
            table.append((i["line_no"], i["item_code"] or "—", i["item_name"],
                          i["production_name"] or "—", i["spec"], i["unit"],
                          i["qty_ordered"], i["unit_price"], i["amount"],
                          i["param_desc"] or "—",
                          self.orders.allocated_qty(i["id"]),
                          self.orders.available_qty(i["id"]),
                          src_code or "—", src_bid or "—"))
        dialogs.TableViewDialog(self, f"订单明细 {oid}", headers, table).exec()

    def _view_order_item_trace(self):
        row = self.tbl_order_items.currentRow()
        if row < 0:
            return
        item = self.tbl_order_items.item(row, 0)
        iid = item.data(Qt.UserRole) if item else None
        if iid is None:
            return
        links = self.trace.from_order_item(iid)
        headers = ["生产项目", "项目名称", "状态", "分配量", "累计已排", "剩余可排",
                   "订单类型", "关联时间"]
        table = [(l["project_no"], l["project_title"], l["project_status"],
                  l["allocated_qty"], l["allocated_total"], l["available_qty"],
                  l["order_type_snapshot"], l["linked_at"]) for l in links]
        dialogs.TableViewDialog(self, "订单明细 → 被哪些生产项目领用",
                                headers, table).exec()

    def _export_orders(self):
        heads = self.db.query("SELECT * FROM orders ORDER BY id")
        items = self.db.query("SELECT * FROM order_items ORDER BY id")
        sheets = {
            "订单": (list(heads[0].keys()) if heads else ["(空)"],
                    [list(r.values()) for r in heads]),
            "订单明细": (list(items[0].keys()) if items else ["(空)"],
                        [list(r.values()) for r in items]),
        }
        return dialogs.export_sheets(self, sheets, "订单导出.xlsx")

    # ── ④ 订单排产（v2.4：取代「新建排产项目」）──────────

    def _orders_context_menu(self, pos):
        """主表右键：展开本单全部物资，任选一项建立/打开订单排产"""
        table = self.tbl_orders
        idx = table.indexAt(pos)
        if not idx.isValid():
            return
        table.selectRow(idx.row())
        oid = selected_id(table)
        if oid is None:
            return
        menu = self._build_orders_menu(oid, table.item(idx.row(), 0).text())
        menu.exec(table.viewport().mapToGlobal(pos))

    def _build_orders_menu(self, oid: int, caption: str = "") -> QMenu:
        """v3.5.3：构建订单主表右键菜单，与 exec() 解耦以便离屏回归直接检查动作"""
        menu = QMenu(self)
        head = QAction(f"订单 {caption or oid} · 订单排产", self)
        head.setEnabled(False)
        menu.addAction(head)
        menu.addSeparator()
        # v2.9.2：编辑入口放在最前，双击也能进
        edit = QAction("✎ 编辑订单（更改 / 删除）", self)
        edit.triggered.connect(
            lambda: self._run("编辑订单", lambda: self._edit_order(oid)))
        menu.addAction(edit)
        # v3.5.3：两段式建单的两个明细动作，与「新增订单」同级入口
        add_items = QAction("录入订单明细…（9 字段表格批量录）", self)
        add_items.triggered.connect(
            lambda: self._run("录入订单明细", lambda: self._add_order_items(oid)))
        menu.addAction(add_items)
        imp_items = QAction("导入订单明细…（CSV / Excel）", self)
        imp_items.triggered.connect(
            lambda: self._run("导入订单明细", lambda: self._import_order_items(oid)))
        menu.addAction(imp_items)
        menu.addSeparator()
        # v3.4.0：右键直接查看 / 编辑 / 更改该订单全部排产文件
        files = QAction("排产文件（查看 / 编辑 / 更改）", self)
        files.triggered.connect(
            lambda: self._run("排产文件", lambda: self._view_schedule_files(oid)))
        menu.addAction(files)
        menu.addSeparator()
        whole = QAction("★ 整单排产（先开订单排产窗口 · 全部物资共用审批单）", self)
        whole.triggered.connect(
            lambda: self._run("整单排产", lambda: self._whole_order_schedule(oid)))
        menu.addAction(whole)
        sub = menu.addMenu("选择物资建立订单排产（窗口内可改勾选合并/整单）")
        rows = self.orders.list_items(oid)
        if rows:
            done = {s["order_item_id"]: s["doc_no"]
                    for s in self.schedules.list(oid)}
            for r in rows:
                mark = "✓" if r["id"] in done else "○"
                caption = (f"{mark} 第{r['line_no']:02d}项 · {r['item_name']}"
                           f"（{r['qty_ordered']:g}{r['unit'] or ''}）")
                act = QAction(caption, self)
                act.triggered.connect(
                    lambda _checked=False, iid=r["id"]:
                    self._run("订单排产", lambda: self._open_order_schedule(iid)))
                sub.addAction(act)
        else:
            sub.addAction("（该订单暂无物资明细）").setEnabled(False)
        menu.addSeparator()
        act = QAction("查看选中订单明细", self)
        act.triggered.connect(lambda: self._view_order_items())
        menu.addAction(act)
        reset = QAction("恢复自动汇总（价税合计 / 物资数量）", self)
        reset.triggered.connect(lambda: self._run("恢复自动汇总",
                                                  lambda: self._reset_totals(oid)))
        menu.addAction(reset)
        return menu

    def _order_items_context_menu(self, pos):
        """明细表右键（支持多选）：一项=分项，多项=合并，一个窗口里统一操作"""
        table = self.tbl_order_items
        idx = table.indexAt(pos)
        if not idx.isValid():
            return
        if not table.selectionModel().isRowSelected(idx.row(), table.rootIndex()):
            table.selectRow(idx.row())
        ids = self._selected_item_ids(table)
        if not ids:
            return
        menu = QMenu(self)
        head = QAction(f"订单明细 · 已选 {len(ids)} 项", self)
        head.setEnabled(False)
        menu.addAction(head)
        menu.addSeparator()
        act = QAction("订单排产…（分项 / 合并 / 整单）", self)
        act.triggered.connect(
            lambda: self._run("订单排产",
                              lambda: self._open_order_schedule(ids[0], ids)))
        menu.addAction(act)
        if len(ids) > 1:
            merge = QAction(f"★ 合并排产（选中 {len(ids)} 项 · 共用一张审批单）", self)
            merge.triggered.connect(
                lambda: self._run("合并排产",
                                  lambda: self._merge_order_schedule(ids=ids)))
            menu.addAction(merge)
        menu.addSeparator()
        trace = QAction("查看被哪些生产项目领用", self)
        trace.triggered.connect(self._view_order_item_trace)
        menu.addAction(trace)
        menu.exec(table.viewport().mapToGlobal(pos))

    def _selected_item_ids(self, table=None) -> List[int]:
        """订单明细的选中行 id 集合（支持 Ctrl/Shift 多选，去重保序）"""
        table = table if table is not None else self.tbl_order_items
        rows: List[int] = []
        try:
            rows = sorted({i.row() for i in table.selectionModel().selectedRows()})
        except Exception:
            rows = []
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        out: List[int] = []
        for r in rows:
            cell = table.item(r, 0)
            value = cell.data(Qt.UserRole) if cell else None
            if value is not None and value not in out:
                out.append(value)
        return out

    def _order_schedule(self):
        """左侧操作列表 / 菜单入口：等价在明细行上右键（多选即合并）"""
        if self.current_view != "orders":
            self.switch_view("orders")
        ids = self._selected_item_ids()
        if not ids:
            theme.notify(self, "请选择物资",
                         "请先在下方「订单明细」中点选物资（Ctrl/Shift 可多选）；"
                         "也可直接在明细行上右键 → 订单排产。", kind="warn")
            return
        return self._open_order_schedule(ids[0], ids)

    def _open_order_schedule(self, order_item_id: int,
                             item_ids: Optional[List[int]] = None):
        """
        v3.0：所有排产入口都先开【订单排产】对话框，由勾选决定分项/合并/整单，
        不再一上来就弹生产排产审批文件编辑窗口。
        """
        item = self.db.get_by_id("order_items", order_item_id)
        if not item:
            theme.notify(self, "找不到物资", f"订单明细 {order_item_id} 不存在",
                         kind="warn")
            return None
        dlg = dialogs.OrderScheduleDialog(
            self, self.schedules, None, order_id=item["order_id"],
            item_ids=list(item_ids or [order_item_id]))
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.summary_text()

    def _merge_order_schedule(self, order_id: Optional[int] = None,
                              item_ids: Optional[List[int]] = None):
        """合并排产：明细多选（≥2 项）→ 订单排产窗口，一种审批单覆盖多种物资"""
        if self.current_view != "orders":
            self.switch_view("orders")
        ids = [int(i) for i in (item_ids or self._selected_item_ids())]
        if len(ids) < 2:
            theme.notify(self, "请多选物资",
                         "合并排产需在「订单明细」里用 Ctrl / Shift 选中 2 项以上物资。",
                         kind="warn")
            return None
        if order_id is None:
            item = self.db.get_by_id("order_items", ids[0])
            if not item:
                return None
            order_id = item["order_id"]
        dlg = dialogs.OrderScheduleDialog(
            self, self.schedules, None, order_id=order_id, item_ids=ids)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.summary_text()

    def _whole_order_schedule(self, order_id: Optional[int] = None):
        """
        整单排产（v3.0）：先启动【订单排产】对话框，默认勾选全部物资、标「整单」；
        在窗口里新建/导入审批、补图片图纸，保存时才落库，不再直接弹 Word。
        """
        oid = order_id if order_id is not None else self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        dlg = dialogs.OrderScheduleDialog(
            self, self.schedules, None, order_id=oid, scope="整单")
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.summary_text()

    # ── v3.4.0：订单级排产文件（查看 / 编辑 / 更改）─────────

    def _view_schedule_files(self, order_id: int | None = None):
        """选中订单 → 一屏查看该单全部排产文件，可直接打开编辑、改名、替换、增删"""
        oid = order_id if order_id is not None else self._require(self.tbl_orders, "订单")
        if oid is None:
            return None
        from ..schedule_files_dialog import ScheduleFilesDialog
        dlg = ScheduleFilesDialog(self, self.schedules, oid)
        dlg.exec()
        return dlg.summary_text()

    def _view_order_schedules(self):
        rows = self.db.query(
            """SELECT s.*, o.doc_no AS order_no, oi.line_no, oi.item_name
               FROM order_schedules s
               JOIN orders o ON o.id = s.order_id
               JOIN order_items oi ON oi.id = s.order_item_id
               ORDER BY s.id""")
        headers = ["订单排产单号", "订单号", "明细行", "物资名称", "排产数量",
                   "排产单状态", "图片", "图纸", "审批", "自动建项目", "备注", "建单时间"]
        counts = {}
        for f in self.db.query(
                "SELECT schedule_id, kind, COUNT(*) AS n FROM order_schedule_files "
                "GROUP BY schedule_id, kind"):
            counts[(f["schedule_id"], f["kind"])] = f["n"]
        table = [
            (r["doc_no"], r["order_no"], r["line_no"], r["item_name"],
             r["allocated_qty"], r["status"],
             counts.get((r["id"], "图片"), 0), counts.get((r["id"], "图纸"), 0),
             counts.get((r["id"], "审批"), 0),
             "是" if r["project_created"] else "复用",
             r["remark"] or "—", r["created_at"])
            for r in rows]
        dialogs.TableViewDialog(self, "订单排产单 · 清单", headers, table).exec()
        return f"共 {len(rows)} 张订单排产单"

    # ── ④ 生产排产动作（旧链路保留）──────────────────────

    def _link_item(self):
        pid = self._require(self.tbl_projects, "生产排产项目")
        if pid is None:
            return
        dlg = dialogs.LinkOrderItemDialog(self, self.db, self.orders)
        if dlg.exec() != dialogs.QDialog.Accepted:
            return
        spec = dlg.result_spec()
        self.prod.link_order_item(pid, operator=cur.name(), **spec)
        return f"已关联订单明细，分配 {spec['allocated_qty']:g}"

    def _unlink(self):
        lid = self._require(self.tbl_links, "关联行")
        if lid is not None:
            self.prod.unlink(lid, cur.name())
            return "已解除关联，数量回补可排量"

    def _advance_project(self):
        pid = self._require(self.tbl_projects, "生产排产项目")
        if pid is None:
            return
        current = self.db.get_by_id("production_projects", pid)["status"]
        allowed = models.TRANSITIONS["production_project"].get(current, [])
        if not allowed:
            return "当前已是终态，无可用状态"
        data = dialogs.ask(self, "推进生产项目状态", [
            {"key": "status", "label": f"当前状态：{current}", "type": "combo",
             "options": [(s, s) for s in allowed], "required": True},
        ])
        if data:
            self.prod.set_status(pid, data["status"], cur.name())
            return f"状态 → {data['status']}"

    def _export_production(self):
        heads = self.db.query("SELECT * FROM production_projects ORDER BY id")
        links = self.db.query("SELECT * FROM production_links ORDER BY id")
        tasks = self.db.query("SELECT * FROM production_tasks ORDER BY id")
        sheets = {
            "生产项目": (list(heads[0].keys()) if heads else ["(空)"],
                        [list(r.values()) for r in heads]),
            "排产关联": (list(links[0].keys()) if links else ["(空)"],
                        [list(r.values()) for r in links]),
            "工序任务": (list(tasks[0].keys()) if tasks else ["(空)"],
                        [list(r.values()) for r in tasks]),
        }
        return dialogs.export_sheets(self, sheets, "生产排产导出.xlsx")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视图构建 / 刷新 / 过滤 / 分页（ViewsMixin）

把「表格怎么建、数据怎么刷、条件怎么下推、翻页怎么算」集中在这里，
主窗口只负责把它们拼到布局里。
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
from ...domains.contract import SIGNED_STATUSES   # v3.4.1 签订后合同口径
from .common import (VIEW_NAMES, VIEW_KEYS, ACTION_GROUPS, TABLE_ENTITIES,
                     fill_table, make_readonly_table, selected_id, select_id,
                     _brief_json, _titled_table, _result_page)



class ViewsMixin:
    # ── 单据维护：作用对象（右侧当前点选的那张表） ────────

    def _register_target_tables(self):
        """主结果与明细下钻都能作为编辑/作废/删除的对象；鼠标/键盘点选即切换焦点"""
        for name, entity in TABLE_ENTITIES:
            table = getattr(self, name, None)
            if table is None:
                continue
            self._table_entity[id(table)] = entity
            table.itemClicked.connect(lambda _item, t=table: self._mark_focus(t))
            table.currentCellChanged.connect(
                lambda *_args, t=table: self._mark_focus(t))
            table.itemSelectionChanged.connect(self._update_target_label)

    def _mark_focus(self, table):
        if self._refreshing:
            return
        self._focus_table = table
        self._update_target_label()

    def _pick_target(self, silent: bool = False):
        """返回 (entity, entity_id, 中文描述)；没选中时提示并返回 None"""
        table = self._focus_table or self.result_tables.get(self.current_view)
        entity = self._table_entity.get(id(table)) if table is not None else None
        eid = selected_id(table) if table is not None and entity else None
        if not entity or eid is None:
            if not silent:
                theme.notify(self, "请先选择",
                             "请先在右侧结果区点选一个主单据或明细行；"
                             "当前点选的那张表就是维护动作的作用对象。", kind="warn")
            return None
        record = self.db.get_by_id(self.maint.spec(entity)["table"], eid)
        if not record:
            if not silent:
                theme.notify(self, "记录已不存在", "该行对应的数据已被删除，请刷新。",
                             kind="warn")
            return None
        return entity, eid, self.maint.describe(entity, record)

    def _update_target_label(self):
        label = getattr(self, "target_label", None)
        if label is None:
            return
        target = self._pick_target(silent=True)
        label.setText(
            f"操作对象：{target[2]}　|　可编辑 / 作废 / 删除，单号与来源受保护"
            if target else
            "操作对象：未选中（点选右侧主结果或明细表任意一行；溯源页为只读）")

    def _show_target_help(self):
        theme.notify(self, "单据维护 · 作用对象说明",
            "右侧上半是主结果、下半是明细下钻。用鼠标点选哪张表的一行，"
            "「编辑 / 作废 / 删除」就作用于哪一行，结果区标题下方会实时显示当前操作对象。\n\n"
            "· 编辑：只开放业务字段；单号、行号、来源外键、订单类型、状态均不可改。\n"
            "· 作废：仅四类主单据可用，状态置「作废」并保留数据；被下游引用时拦截。\n"
            "· 删除：仅在无下游引用且未进入执行态时允许；级联删除本级明细并逐行留痕。\n"
            "· 所有写操作记入审计留痕，可在「查看审计留痕」中按实体/操作查询。")

    # ── 视图切换 / 概览刷新 ──────────────────────────────

    def switch_view(self, key: str, refresh: bool = True):
        if key not in VIEW_KEYS:
            return
        self.current_view = key
        self._focus_table = None          # 切换视图后默认作用于该视图的主结果表
        self.result_stack.setCurrentIndex(VIEW_KEYS.index(key))
        self.query_panel.set_view(key)
        self.query_panel.clear()      # 每个视图独立查询上下文，避免条件串味
        self.filters = self.query_panel.values()
        act = self._view_actions.get(key)
        if act is not None:
            act.setChecked(True)
        self._update_view_meta()
        # v3.2.0：主操作界面快捷按钮随视图切换（只在这里刷，避免每次刷新数据都重建）
        if hasattr(self, "_refresh_action_bar"):
            self._refresh_action_bar()
        if refresh:
            self.refresh_all()
            self._ensure_selection()

    def _due_counts(self):
        """P2-6：全库「逾期 / 7 天内到期」订单数（与表格、筛选同一口径）"""
        try:
            result = []
            for state in ("overdue", "soon"):
                sql, params = qbuild.due_clause(state)
                result.append(int(self.db.scalar(
                    f"SELECT COUNT(*) FROM orders o WHERE {sql}", tuple(params), default=0)))
            return result[0], result[1]
        except Exception:                                  # noqa: BLE001
            return 0, 0

    def _update_view_meta(self):
        idx = VIEW_KEYS.index(self.current_view)
        self.view_badge.setText(VIEW_NAMES[idx])
        self.result_title.setText(query_panel.VIEW_BY_KEY[self.current_view]["result_title"])
        table = getattr(self, "result_tables", {}).get(self.current_view)
        n = table.rowCount() if table is not None else 0
        total = (self.page_state.get(self.current_view) or (0, n, n))[2]
        badge = f"{n} 条" if total <= n else f"{n} / {total} 条"
        if self.current_view == "orders":
            # P2-6：把「逾期 / 临期」直接摆在结果区标题旁，不用切筛选也能看见
            overdue, soon = self._due_counts()
            if overdue or soon:
                badge += f"　·　逾期 {overdue} / 临期 {soon}"
        self.count_badge.setText(badge)
        self._update_pager()
        if hasattr(self, "query_panel") and hasattr(self, "filters"):
            # 命中数用「过滤后的总数」，不是本页行数（P1-10 分页后两者不同）
            self.query_panel.set_hit(total, self._view_total(self.current_view))
        if hasattr(self, "btn_add_order"):
            # 「添加订单」只属于订单视图，避免在别的视图误点
            self.btn_add_order.setVisible(self.current_view == "orders")

    def _view_total(self, key: str) -> int:
        tables = {"bids": "bid_projects", "codes": "code_projects",
                  "orders": "orders", "production": "production_projects",
                  "trace": "production_links", "overview": "order_items"}
        return self.db.scalar(f"SELECT COUNT(*) FROM {tables[key]}")

    def _update_kpi(self):
        n = lambda t: self.db.scalar(f"SELECT COUNT(*) FROM {t}")
        ext = self.db.scalar("SELECT COUNT(*) FROM orders WHERE order_type='外部订单'")
        itl = self.db.scalar("SELECT COUNT(*) FROM orders WHERE order_type='内部订单'")
        links = n("production_links")
        self.kpi_nums["bid"].setText(str(n("bid_projects")))
        self.kpi_nums["code"].setText(str(n("code_projects")))
        self.kpi_nums["order"].setText(f"{ext} / {itl}")
        self.kpi_nums["item"].setText(str(n("order_items")))
        self.kpi_nums["project"].setText(str(n("production_projects")))
        self.kpi_nums["link"].setText(str(links))

    def _ensure_selection(self):
        pairs = [("tbl_bids", self._refresh_bid_items),
                 ("tbl_codes", self._refresh_code_items),
                 ("tbl_orders", self._refresh_order_items),
                 ("tbl_projects", self._refresh_links)]
        for name, fn in pairs:
            t = getattr(self, name)
            if t.currentRow() < 0 and t.rowCount() > 0:
                t.selectRow(0)
            fn()

    # ── 查询过滤（只作用于展示层，不改变服务层业务数据） ─

    # ── P1-10 分页 ────────────────────────────────────────

    def _paged_rows(self, view: str, sql: str, count_sql: str, params):
        """执行「COUNT + 分页查询」，并把 (offset, 本页行数, 总数) 记到 page_state"""
        total = int(self.db.scalar(count_sql, tuple(params), default=0))
        size = self.PAGE_SIZE
        offset = self.page_offsets.get(view, 0)
        if total and offset >= total:               # 数据被删/条件收紧后自动回退
            offset = ((total - 1) // size) * size
            self.page_offsets[view] = offset
        rows = self.db.query(sql + f" LIMIT {size} OFFSET {offset}", tuple(params))
        self.page_state[view] = (offset, len(rows), total)
        return rows, total

    def _page(self, delta: int):
        view = self.current_view
        state = self.page_state.get(view)
        if not state:
            return
        offset, _n, total = state
        target = offset + delta * self.PAGE_SIZE
        if target < 0 or target >= total:
            return
        self.page_offsets[view] = target
        self.refresh_all()
        self._ensure_selection()

    def _update_pager(self):
        view = self.current_view
        state = self.page_state.get(view)
        if not state:
            self.pager_label.setText("")
            self.btn_prev_page.setEnabled(False)
            self.btn_next_page.setEnabled(False)
            return
        offset, n, total = state
        if total > self.PAGE_SIZE:
            self.pager_label.setText(
                f"第 {offset + 1 if n else 0}–{offset + n} 条 / 共 {total} 条")
        else:
            self.pager_label.setText(f"共 {total} 条")
        self.btn_prev_page.setEnabled(offset > 0)
        self.btn_next_page.setEnabled(offset + n < total)

    def _filtered(self, rows, fields, customer_field="customer", status_field="status"):
        f = self.filters or {}
        kw = str(f.get("keyword") or "").strip().lower()
        status = str(f.get("status") or "")
        customer = str(f.get("customer") or "").strip().lower()
        project = str(f.get("project_no") or "").strip().lower()
        d1 = str(f.get("date_from") or "").strip()
        d2 = str(f.get("date_to") or "").strip()
        date_field = str(f.get("date_field") or "")
        won_only = bool(f.get("won_only"))
        has_capacity = bool(f.get("has_capacity"))
        order_type = str(f.get("order_type") or "")
        out = []
        for r in rows:
            if kw and not any(kw in str(r.get(k) or "").lower() for k in fields):
                continue
            if status and str(r.get(status_field) or "") != status:
                continue
            if customer and customer_field and \
                    customer not in str(r.get(customer_field) or "").lower():
                continue
            if project and project not in str(r.get("project_no") or "").lower():
                continue
            if (d1 or d2) and date_field:
                dv = str(r.get(date_field) or "")[:10]
                if d1 and (not dv or dv < d1):
                    continue
                if d2 and (not dv or dv > d2):
                    continue
            if won_only and str(r.get("status") or "") != "中标":
                continue
            if order_type and str(r.get("order_type") or "") != order_type:
                continue
            if has_capacity and float(r.get("qty_real") or 0) - \
                    float(r.get("qty_scheduled") or 0) <= 1e-9:
                continue
            out.append(r)
        return out

    def _filter_display(self, data, ids=None):
        """溯源/总览的拼接结果行：按关键字与订单类型做整行过滤"""
        f = self.filters or {}
        kw = str(f.get("keyword") or "").strip().lower()
        ot = str(f.get("order_type") or "")
        if not kw and not ot:
            return (data, ids) if ids is not None else data
        rows, keep_ids = [], []
        for i, row in enumerate(data):
            text = " | ".join("" if v is None else str(v) for v in row)
            if kw and kw not in text.lower():
                continue
            if ot and ot not in text:
                continue
            rows.append(row)
            if ids is not None and i < len(ids):
                keep_ids.append(ids[i])
        return (rows, keep_ids) if ids is not None else rows

    def _build_bids_tab(self):
        self.tbl_bids = make_readonly_table()
        self.tbl_bid_items = make_readonly_table()
        self.tbl_bids.itemSelectionChanged.connect(self._refresh_bid_items)
        self.result_stack.addWidget(_result_page(
            "招投标项目 · 主结果", self.tbl_bids,
            "投标明细 · 点选上方项目查看", self.tbl_bid_items))

    def _build_codes_tab(self):
        self.tbl_codes = make_readonly_table()
        self.tbl_code_items = make_readonly_table()
        self.tbl_codes.itemSelectionChanged.connect(self._refresh_code_items)
        self.result_stack.addWidget(_result_page(
            "询配码项目 · 主结果", self.tbl_codes,
            "询配码明细 · 10 字段 / 参考数量为虚值，仅统计", self.tbl_code_items))

    def _build_orders_tab(self):
        self.tbl_orders = make_readonly_table()
        self.tbl_order_items = make_readonly_table()
        # v3.0：订单明细支持多选 —— Ctrl/Shift 选中多项后右键「合并排产」
        self.tbl_order_items.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl_orders.itemSelectionChanged.connect(self._refresh_order_items)
        # v2.9.2：双击订单行 = 直接进入「编辑订单」（更改 / 删除）
        self.tbl_orders.doubleClicked.connect(self._on_order_double_click)
        # v3.4.1：单击「采购合同编号」列 = 直接进入该订单的采购合同
        self.tbl_orders.cellClicked.connect(self._on_order_contract_click)
        self.tbl_order_items.doubleClicked.connect(self._view_order_item_trace)
        # v2.4：主表右键列出本单物资、明细表右键直接对该项做订单排产
        for table, handler in ((self.tbl_orders, self._orders_context_menu),
                               (self.tbl_order_items, self._order_items_context_menu)):
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(handler)
        self.result_stack.addWidget(_result_page(
            "订单 · 主结果（双击行 = 编辑 / 右键更多动作）", self.tbl_orders,
            "订单明细 · 9 字段 / 右键某一项 → 订单排产（双击行反查领用）",
            self.tbl_order_items))

    def _build_production_tab(self):
        self.tbl_projects = make_readonly_table()
        self.tbl_links = make_readonly_table()
        self.tbl_projects.itemSelectionChanged.connect(self._refresh_links)
        self.result_stack.addWidget(_result_page(
            "生产排产项目 · 主结果", self.tbl_projects,
            "排产关联 · 挂的是订单明细，不是询配码明细", self.tbl_links))

    def _build_trace_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        h = QHBoxLayout()
        h.addWidget(QLabel("溯源方向："))
        self.trace_scope = QComboBox()
        self.trace_scope.addItem("按生产项目（排产 → 订单 → 询配码项/中标项）", "production")
        self.trace_scope.addItem("按询配码项目（一码多单 → 订单明细）", "code")
        self.trace_scope.addItem("按中标项目（内部订单 → 排产）", "bid")
        self.trace_scope.currentIndexChanged.connect(self._refresh_trace_entities)
        h.addWidget(self.trace_scope, 1)
        h.addWidget(QLabel("对象："))
        self.trace_entity = QComboBox()
        self.trace_entity.setMinimumWidth(300)
        self.trace_entity.currentIndexChanged.connect(self.refresh_trace)
        h.addWidget(self.trace_entity, 1)
        btn_export = QPushButton("导出当前溯源表")
        btn_export.clicked.connect(lambda: self._run("导出溯源", self._export_trace))
        h.addWidget(btn_export)
        v.addLayout(h)
        cap = QLabel("溯源明细 · 双击「按生产项目」视图的行反查领用")
        cap.setObjectName("KpiLabel")
        v.addWidget(cap)
        self.tbl_trace = make_readonly_table()
        self.tbl_trace.doubleClicked.connect(self._trace_row_detail)
        v.addWidget(self.tbl_trace, 1)
        self.trace_hint = QLabel("")
        self.trace_hint.setWordWrap(True)
        self.trace_hint.setObjectName("Hint")
        v.addWidget(self.trace_hint)
        self.result_stack.addWidget(page)

    def _build_overview_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.kpi_label = QLabel("")
        self.kpi_label.setWordWrap(True)
        self.kpi_label.setObjectName("Banner")
        v.addWidget(self.kpi_label)
        h = QHBoxLayout()
        btn_check = QPushButton("运行一致性检查")
        btn_check.setObjectName("Primary")
        btn_check.clicked.connect(lambda: self._run("一致性检查", self._consistency_check))
        btn_export = QPushButton("导出全系统 Excel")
        btn_export.clicked.connect(lambda: self._run("导出全系统", self._export_all))
        h.addWidget(btn_check)
        h.addWidget(btn_export)
        h.addStretch(1)
        v.addLayout(h)
        self.tbl_capacity = make_readonly_table()
        self.tbl_batch_overview = make_readonly_table()
        split = QSplitter(Qt.Vertical)
        split.addWidget(_titled_table(
            "订单明细可排量（实值守恒：Σ排产分配 ≤ 订单实值）", self.tbl_capacity))
        split.addWidget(_titled_table(
            "询配码项目 → 订单批次（一码多单）", self.tbl_batch_overview))
        split.setSizes([300, 260])
        v.addWidget(split, 1)
        self.result_stack.addWidget(page)

    # ── 刷新 ─────────────────────────────────────────────

    def refresh_all(self):
        """刷新期间屏蔽 focus 信号，避免程序化 selectRow 抢走用户点选的明细表"""
        self._refreshing = True
        try:
            return self._refresh_all_impl()
        finally:
            self._refreshing = False
            self._update_target_label()

    def _refresh_all_impl(self):
        keep = {name: selected_id(getattr(self, name)) for name in (
            "tbl_bids", "tbl_codes", "tbl_orders", "tbl_projects", "tbl_links",
            # 明细表也保持选中，否则「编辑选中明细」在刷新后会丢失作用对象
            "tbl_bid_items", "tbl_code_items", "tbl_order_items",
            "tbl_capacity")}
        self._refresh_bids()
        self._refresh_codes()
        self._refresh_orders()
        self._refresh_projects()
        self._refresh_overview()
        self._refresh_trace_entities()
        for name, sid in keep.items():
            select_id(getattr(self, name), sid)
        # 首次打开时默认选中首行，明细表直接有内容（fill_table 屏蔽了选择信号，需手动补刷）
        for name in ("tbl_bids", "tbl_codes", "tbl_orders", "tbl_projects"):
            table = getattr(self, name)
            if table.currentRow() < 0 and table.rowCount() > 0:
                table.selectRow(0)
        self._refresh_bid_items()
        self._refresh_code_items()
        self._refresh_order_items()
        self._refresh_links()
        # 明细表在最后一步才重填，选中状态需要二次恢复
        for name in ("tbl_bid_items", "tbl_code_items", "tbl_order_items",
                     "tbl_links", "tbl_capacity"):
            select_id(getattr(self, name), keep.get(name))
        counts = {t: self.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in (
            "bid_projects", "code_projects", "orders", "order_items",
            "production_projects", "production_links", "order_schedules")}
        self._update_kpi()
        self._update_view_meta()
        self.db_label.setText(f"数据库：{self.db.path}")
        self.status_label.setText(
            f"当前视图：{VIEW_NAMES[VIEW_KEYS.index(self.current_view)]}　|　"
            f"招投标 {counts['bid_projects']} / 询配码 {counts['code_projects']} / "
            f"订单 {counts['orders']}（明细 {counts['order_items']}）/ "
            f"生产项目 {counts['production_projects']} / 关联 {counts['production_links']} 条"
            f" / 订单排产 {counts['order_schedules']} 张"
            f"　|　{excelio.backend_info()}")

    def _refresh_bids(self):
        where, params = qbuild.build_where(
            self.filters, ["bp.doc_no", "bp.project_name", "bp.customer", "bp.tender_no"],
            status_column="bp.status", customer_column="bp.customer",
            date_column="bp.bid_date")
        base = "FROM bid_projects bp LEFT JOIN bid_items bi ON bi.bid_project_id = bp.id" + where
        agg = ("""SELECT bp.*, COUNT(bi.id) AS item_count,
                         ROUND(SUM(bi.purchase_price * bi.qty), 2) AS purchase_total,
                         ROUND(SUM(bi.purchase_price / bi.quote_ratio * bi.qty), 2) AS quote_total,
                         (SELECT o.doc_no FROM orders o
                          WHERE o.source_bid_project_id = bp.id
                            AND o.order_type='内部订单') AS internal_order
                  """ + base + " GROUP BY bp.id ORDER BY bp.id")
        count_sql = "SELECT COUNT(*) FROM (SELECT bp.id " + base + " GROUP BY bp.id)"
        rows, _total = self._paged_rows("bids", agg, count_sql, params)
        fill_table(self.tbl_bids,
                   ["项目号", "招投标项目", "客户名称", "招标编号", "状态", "明细数",
                    "采购总额", "报价总额", "内部订单"],
                   [(r["doc_no"], r["project_name"], r["customer"], r["tender_no"],
                     r["status"], r["item_count"], r["purchase_total"], r["quote_total"],
                     r["internal_order"] or "—") for r in rows],
                   ids=[r["id"] for r in rows], stretch_col=1)

    def _refresh_bid_items(self):
        pid = selected_id(self.tbl_bids)
        rows = (self.bids.list_items(pid) if pid else [])
        fill_table(self.tbl_bid_items,
                   ["行", "产品名称", "规格", "类别", "数量(实值)", "单位",
                    "采购单价", "报价比率", "对外单价", "中标", "备注"],
                   [(r["line_no"], r["product_name"], r["spec"], r["category"],
                     r["qty"], r["unit"], r["purchase_price"], r["quote_ratio"],
                     round(r["quote_price"], 2), r["is_won"] or "—", r["remark"])
                    for r in rows], ids=[r["id"] for r in rows], stretch_col=1)

    def _refresh_codes(self):
        where, params = qbuild.build_where(
            self.filters, ["cp.doc_no", "cp.project_name", "cp.customer", "cp.file_path"],
            status_column="cp.status", customer_column="cp.customer",
            date_column="cp.submit_date")
        base = "FROM code_projects cp LEFT JOIN code_items ci ON ci.code_project_id = cp.id" + where
        agg = ("""SELECT cp.*, COUNT(ci.id) AS item_count,
                         ROUND(SUM(ci.qty_reference), 2) AS virtual_total,
                         (SELECT COUNT(*) FROM orders o
                          WHERE o.source_code_project_id = cp.id
                            AND o.order_type='外部订单') AS order_count
                  """ + base + " GROUP BY cp.id ORDER BY cp.id")
        count_sql = "SELECT COUNT(*) FROM (SELECT cp.id " + base + " GROUP BY cp.id)"
        rows, _total = self._paged_rows("codes", agg, count_sql, params)
        fill_table(self.tbl_codes,
                   ["项目号", "询配码项目", "客户名称", "状态", "版本", "提交文件",
                    "明细数", "虚值合计(仅统计)", "订单张数(一码多单)"],
                   [(r["doc_no"], r["project_name"], r["customer"], r["status"],
                     r["version"], r["file_path"], r["item_count"],
                     r["virtual_total"], r["order_count"]) for r in rows],
                   ids=[r["id"] for r in rows], stretch_col=1)

    def _refresh_code_items(self):
        pid = selected_id(self.tbl_codes)
        rows = (self.codes.list_items(pid) if pid else [])
        # 明细列 = 系统项号 + 约定的 10 个业务字段 + 状态/下单次数
        body = fdefs.export_rows(rows)
        data = [
            [r["item_code"], *body[i], r["status"],
             self.db.scalar("SELECT COUNT(*) FROM order_items WHERE source_code_item_id = ?",
                            (r["id"],))]
            for i, r in enumerate(rows)
        ]
        fill_table(self.tbl_code_items,
                   ["系统项号", *fdefs.headers(), "状态", "被下单次数"],
                   data, ids=[r["id"] for r in rows], stretch_col=1)

    def _refresh_orders(self):
        # P1-10：条件全部下推 SQL（关键字多列 OR LIKE / 状态 / 客户 / 日期 / 类型 / 可排量）
        # P2-6：交期状态（逾期 / 7 天内到期）同样下推，避免只过滤当前页
        due_sql, due_params = qbuild.due_clause(str((self.filters or {}).get("due") or ""))
        where, params = qbuild.build_where(
            self.filters,
            ["o.doc_no", "o.project_no", "o.customer", "o.erp_order_no",
             "o.order_type", "o.order_source"],
            status_column="o.status", customer_column="o.customer",
            date_column="o.order_date", type_column="o.order_type",
            project_column="o.project_no",
            extra=[(due_sql, due_params)])
        having = qbuild.having_capacity() if (self.filters or {}).get("has_capacity") else ""
        base = ("FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id" + where)
        agg_core = ("""SELECT o.*, COUNT(oi.id) AS item_count,
                              COALESCE(SUM(oi.qty_ordered), 0) AS qty_real,
                              COALESCE((SELECT SUM(pl.allocated_qty)
                                        FROM production_links pl
                                        JOIN order_items oi2 ON oi2.id = pl.order_item_id
                                        WHERE oi2.order_id = o.id), 0) AS qty_scheduled,
                              COALESCE((SELECT GROUP_CONCAT(c.doc_no, '、')
                                        FROM purchase_contracts c
                                        WHERE c.order_id = o.id AND c.status IN
                                              ('已签订', '执行中', '已完成')), '') AS contract_nos
                       """ + base + " GROUP BY o.id" + having)
        # 计数必须包住带别名的子查询：HAVING 里引用了 qty_real / qty_scheduled
        count_sql = "SELECT COUNT(*) FROM (" + agg_core + ")"
        rows, _total = self._paged_rows("orders", agg_core + " ORDER BY o.id",
                                        count_sql, params)
        # v2.3：订单头全量展示，守恒指标并列；
        # v3.4.1：去掉「系统单号」列，新增可点击的「采购合同编号」列（汇总口径前，
        #         只统计签订后的合同：已签订/执行中/已完成，多个用「、」连接）
        # Q12：物资数量 = 物资项目数（存库值，可能是导入时显式声明的任意值）；
        #      「明细行数(实时)」是 COUNT(order_items)，两者不必相等；
        #      Σ 明细数量请读「实值合计」列。
        # v3.5.0：订单盈亏四列。口径唯一来源 domains/profit.py，视图不重算金额；
        # 追加在「汇总口径」之后，保证「采购合同编号」列索引与既有点击逻辑不变。
        svc = getattr(self, "profit", None)
        try:
            pmap = {int(p["order_id"]): p for p in svc.overview(
                [int(r["id"]) for r in rows])} if svc is not None else {}
        except Exception:                                   # noqa: BLE001
            pmap = {}

        def _profit_text(r, key: str):
            p = pmap.get(int(r["id"]))
            if not p:
                return "—"
            if key == "profit_rate":
                if p["profit_rate"] is None:
                    return "不出数"
                return (f"{p['profit_rate']:.2f}%"
                        + ("" if p["complete"] else " · 不完整"))
            if key == "coverage":
                return f"{p['coverage']:.0f}%"
            return round(float(p[key]), 2)

        fill_table(self.tbl_orders,
                   ["订单项目编号", "客户名称", "订单来源",
                    "订单类型", "订单状态",
                    "价税合计", "交货日期", "交期状态", "物资数量(项)", "ERP订单编号",
                    "明细行数(实时)", "实值合计", "已排量", "可排量",
                    "采购合同编号", "汇总口径",
                    "采购合计(含税)", "毛利(含税)", "利润率", "覆盖度"],
                   [(r["project_no"] or "—", r["customer"],
                     # v2.9：订单来源（渠道）；历史订单可能为空 → 显示 —
                     r["order_source"] or "—",
                     r["order_type"], r["status"],
                     round(float(r["amount_total"] or 0), 2),
                     r["required_date"],
                     # P2-6：交期预警（已验收/已完成等不再催交，口径见 core/query.py）
                     qbuild.DUE_TEXT[qbuild.due_state(r["required_date"], r["status"])],
                     r["item_qty_total"],
                     r["erp_order_no"] or "—",
                     r["item_count"], r["qty_real"], r["qty_scheduled"],
                     round(float(r["qty_real"]) - float(r["qty_scheduled"]), 3),
                     r["contract_nos"] or "—",
                     # P1-5：标识头字段是手工声明还是随明细自动汇总
                     "、".join([c for c, f in (
                         ("金额手工", r["amount_manual"]),
                         ("项数手工", r["item_qty_manual"])) if f]) or "自动",
                     _profit_text(r, "purchase_total"),
                     _profit_text(r, "gross_profit"),
                     _profit_text(r, "profit_rate"),
                     _profit_text(r, "coverage"))
                    for r in rows], ids=[r["id"] for r in rows], stretch_col=1)

    # v3.4.1：订单主表「采购合同编号」列的列索引（删系统单号后的新布局）
    ORDER_CONTRACT_COL = 14

    def _on_order_contract_click(self, row: int, col: int):
        """单击订单行的「采购合同编号」单元格 → 进入该订单的采购合同管理并定位"""
        if col != self.ORDER_CONTRACT_COL:
            return
        oid = selected_id(self.tbl_orders)
        if oid is None:
            return
        signed = [c for c in self.contracts.list(order_id=int(oid))
                  if c["status"] in SIGNED_STATUSES]
        if not signed:
            theme.notify(f"该订单暂无已签订的采购合同（草稿/作废不显示），"
                         "可在「生产排产 → 采购合同管理」中登记并签订")
            return
        from ..contract_dialog import PurchaseContractDialog
        dlg = PurchaseContractDialog(self, self.contracts, order_id=int(oid),
                                     focus_contract_id=int(signed[0]["id"]))
        dlg.exec()
        self.refresh_all()

    def _refresh_order_items(self):
        oid = selected_id(self.tbl_orders)
        rows = (self.orders.list_items(oid) if oid else [])
        # v2.3：明细 9 字段 + 守恒指标 + 溯源标记；v2.4 末列显示该项的订单排产单
        # v3.0：末列带上排产范围标记（分项 / 合并 / 整单）
        sched_by_item = {s["order_item_id"]: s for s in self.db.query(
            "SELECT order_item_id, doc_no, scope FROM order_schedules")}
        fill_table(self.tbl_order_items,
                   ["行", "物资代码", "物资名称", "实际生产物资名称", "规格", "单位",
                    "数量(实值)", "单价", "总价", "参数描述",
                    "已排量", "可排量", "来源询配码项", "来源中标明细项",
                    "订单排产单"],
                   [(r["line_no"], r["item_code"] or "—", r["item_name"],
                     r["production_name"] or "—", r["spec"], r["unit"],
                     r["qty_ordered"], r["unit_price"], r["amount"],
                     r["param_desc"] or "—",
                     self.orders.allocated_qty(r["id"]),
                     self.orders.available_qty(r["id"]),
                     (self.db.get_by_id("code_items", r["source_code_item_id"]) or {}).get("item_code", "—")
                     if r["source_code_item_id"] else "—",
                     r["source_bid_item_id"] or "—",
                     (f"{sched_by_item[r['id']]['doc_no']}"
                      f"〔{sched_by_item[r['id']].get('scope') or '分项'}〕"
                      if r["id"] in sched_by_item else "○ 未排产")) for r in rows],
                   ids=[r["id"] for r in rows], stretch_col=2)

    def _refresh_projects(self):
        rows = self.db.query(
            """SELECT pp.*, bp.doc_no AS bid_no,
                      COUNT(pl.id) AS link_count,
                      COALESCE(SUM(pl.allocated_qty), 0) AS allocated_total
               FROM production_projects pp
               LEFT JOIN bid_projects bp ON bp.id = pp.source_bid_project_id
               LEFT JOIN production_links pl ON pl.project_id = pp.id
               GROUP BY pp.id ORDER BY pp.id""")
        rows = self._filtered(rows, ["doc_no", "title", "workshop", "bid_no"],
                              customer_field=None)
        fill_table(self.tbl_projects,
                   ["生产项目号", "项目名称", "状态", "来源中标项目(标签)",
                    "计划开始", "计划结束", "车间", "关联明细数", "实值分配总量"],
                   [(r["doc_no"], r["title"], r["status"], r["bid_no"] or "—",
                     r["plan_start"], r["plan_end"], r["workshop"],
                     r["link_count"], r["allocated_total"]) for r in rows],
                   ids=[r["id"] for r in rows], stretch_col=1)

    def _refresh_links(self):
        pid = selected_id(self.tbl_projects)
        rows = (self.prod.list_links(pid) if pid else [])
        all_links = self.db.query("SELECT * FROM production_links ORDER BY id") if pid else []
        by_id = {l["order_item_id"]: l for l in all_links}
        fill_table(self.tbl_links,
                   ["订单号", "订单类型", "明细行", "物资名称", "分配量(实值)",
                    "订单实值", "来源类型", "来源项", "询配码项目", "虚值(仅对照)",
                    "中标项目"],
                   [(l["order_no_snapshot"], by_id[l["order_item_id"]].get("order_type_snapshot", "—"),
                     l["order_line_snapshot"], l["item_name_snapshot"], l["allocated_qty"],
                     l["qty_ordered"],
                     by_id[l["order_item_id"]].get("source_type_snapshot", "—"),
                     l.get("item_code_snapshot") or l.get("bid_line_snapshot") or "—",
                     l["code_no_snapshot"] or "—",
                     l["qty_reference_snapshot"] if l["qty_reference_snapshot"] is not None else "—",
                     l.get("bid_no_snapshot") or "—") for l in rows],
                   ids=[l["id"] for l in rows], stretch_col=3)

    def _refresh_overview(self):
        counts = {t: self.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in (
            "bid_projects", "code_projects", "orders", "order_items",
            "production_projects", "production_links", "audit_log")}
        won = self.db.scalar("SELECT COUNT(*) FROM bid_projects WHERE status='中标'")
        internal = self.db.scalar("SELECT COUNT(*) FROM orders WHERE order_type='内部订单'")
        external = self.db.scalar("SELECT COUNT(*) FROM orders WHERE order_type='外部订单'")
        self.kpi_label.setText(
            f"招投标项目 {counts['bid_projects']}（中标 {won}） · 询配码项目 {counts['code_projects']} · "
            f"外部订单 {external}（一码多单）/ 内部订单 {internal}（一中标一单） · "
            f"订单明细 {counts['order_items']} · 生产项目 {counts['production_projects']} · "
            f"排产关联 {counts['production_links']} · 审计留痕 {counts['audit_log']}")
        cap = self.db.query(
            """SELECT o.id AS order_id, o.doc_no AS order_no, o.order_type, o.batch_seq,
                      oi.id AS item_id, oi.line_no, oi.item_name, oi.qty_ordered,
                      COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                WHERE pl.order_item_id = oi.id), 0) AS scheduled
               FROM order_items oi JOIN orders o ON o.id = oi.order_id
               ORDER BY o.id, oi.line_no""")
        cap_headers = ["订单号", "类型", "批次", "行", "物资名称", "订单实值",
                       "已排量", "剩余可排", "排产进度"]
        cap_rows = [(r["order_no"], r["order_type"], r["batch_seq"], r["line_no"],
                     r["item_name"], r["qty_ordered"], r["scheduled"],
                     round(float(r["qty_ordered"]) - float(r["scheduled"]), 3),
                     models.order_item_status_by_qty(r["qty_ordered"], r["scheduled"]))
                    for r in cap]
        cap_ids = [r["item_id"] for r in cap]
        cap_rows, cap_ids = self._filter_display(cap_rows, cap_ids)
        fill_table(self.tbl_capacity, cap_headers, cap_rows, ids=cap_ids, stretch_col=4)
        batches = self.db.query(
            """SELECT cp.doc_no AS code_no, cp.project_name, cp.status AS code_status,
                      o.batch_seq, o.doc_no AS order_no, o.status AS order_status,
                      o.required_date, oi_count.n AS item_count,
                      COALESCE(SUM(oi.qty_ordered), 0) AS qty_real,
                      COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                JOIN order_items oi2 ON oi2.id = pl.order_item_id
                                WHERE oi2.order_id = o.id), 0) AS scheduled
               FROM code_projects cp
               JOIN orders o ON o.source_code_project_id = cp.id AND o.order_type='外部订单'
               LEFT JOIN order_items oi ON oi.order_id = o.id
               LEFT JOIN (SELECT order_id, COUNT(*) AS n FROM order_items GROUP BY order_id) oi_count
                 ON oi_count.order_id = o.id
               GROUP BY o.id ORDER BY cp.id, o.batch_seq""")
        batch_headers = ["询配码项目", "项目名称", "项目状态", "批次", "订单号",
                         "订单状态", "交期", "明细数", "订单实值", "已排量"]
        batch_rows = [(r["code_no"], r["project_name"], r["code_status"], r["batch_seq"],
                       r["order_no"], r["order_status"], r["required_date"],
                       r["item_count"] or 0, r["qty_real"], r["scheduled"])
                      for r in batches]
        batch_ids = [r["order_no"] for r in batches]
        batch_rows, batch_ids = self._filter_display(batch_rows, batch_ids)
        fill_table(self.tbl_batch_overview, batch_headers, batch_rows,
                   ids=batch_ids, stretch_col=1)

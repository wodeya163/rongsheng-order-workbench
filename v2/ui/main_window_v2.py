#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口（三栏工作台）· P2-2 拆分后的骨架

布局：顶部菜单栏 / 左侧操作列表 / 中间查询窗口 / 右侧查询结果（主表 + 明细下钻）

六个视图（顺序与 query_panel.VIEWS 一致）：
  ① 询报价·招投标  ② 询配码项目  ③ 订单管理  ④ 生产排产  ⑤ 溯源穿透  ⑥ 总览与守恒

模块分工（2026-11-07 由本文件拆出，行为未变）：
  · panels/common.py            视图元数据 / 动作清单 / 表格填充等无业务小工具
  · panels/views.py             视图构建、刷新、过滤、SQL 分页
  · panels/bid_code_actions.py  询报价与询配码动作
  · panels/order_actions.py     订单、订单排产、生产关联动作
  · panels/maint_actions.py     溯源、备份恢复、单据维护、审计
  本文件只保留：窗口骨架、菜单、三栏布局、动作包装与启动入口。

业务口径（界面上明确区分）：
  · 询配码明细「参考数量」= 虚值，只统计；订单「实值数量」= 排产唯一数量来源
  · 中标项目 → 一张内部订单 → 产生排产数量；一个询配码项目 → 多张外部订单
  · 排产项目可挂中标项目作来源标签，但标签不携带数量

PySide6 为可选依赖：未安装时模块仍可导入，launch() 给出明确提示。
"""
import sys
from pathlib import Path

from .panels.common import (VIEW_NAMES, VIEW_KEYS, ACTION_GROUPS,
                            TABLE_ENTITIES, DEFAULT_DB, PRIMARY_ACTIONS,
                            find_action)

try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QTableWidget, QTableWidgetItem,
        QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel, QComboBox,
        QHeaderView, QSplitter, QLineEdit, QFrame, QStatusBar,
        QTreeWidget, QTreeWidgetItem, QStackedWidget, QGroupBox, QScrollArea,
        QCheckBox, QSizePolicy, QAbstractItemView, QDialog, QMenu,
    )
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QColor, QCursor
    PYSIDE_AVAILABLE = True
    from . import dialogs, theme, query_panel, async_task
    from .panels.common import (fill_table, make_readonly_table, selected_id,
                                select_id, _brief_json, _titled_table,
                                _result_page, _StackTabs)
    from .panels.views import ViewsMixin
    from .panels.bid_code_actions import BidCodeActionsMixin
    from .panels.order_actions import OrderActionsMixin
    from .panels.maint_actions import MaintActionsMixin
    from .panels.due_actions import DueActionsMixin       # v3.3.0 交期提醒
    from .panels.contract_actions import ContractActionsMixin  # v3.4.0 采购合同
    from .panels.profit_actions import ProfitActionsMixin      # v3.5.0 订单盈亏
except ImportError:
    PYSIDE_AVAILABLE = False

from .. import __version__ as APP_VERSION, APP_TITLE
from ..core.db import Database
from ..core import models, excelio, applog          # noqa: F401
from ..core import query as qbuild                  # noqa: F401
from ..core import uistate                          # v3.2.0 导航折叠/隐藏偏好
from ..core import reminder_prefs                   # v3.3.0 交期提醒偏好
from ..core import current_user as cur              # noqa: F401
from ..core import fields as fdefs                  # noqa: F401
from ..core import order_fields as ofdefs           # noqa: F401
from ..core.presets import QueryPresetService
from ..domains import (BidService, PartCodeService, OrderService,
                       ProductionService, TraceService, MaintenanceService,
                       OrderScheduleService, PurchaseContractService,
                       OrderProfitService)

# 对外符号兼容：拆分前这些名字直接定义在本模块，测试与脚本按旧路径导入
__all__ = ["MainWindowV2", "VIEW_NAMES", "VIEW_KEYS", "ACTION_GROUPS",
           "TABLE_ENTITIES", "DEFAULT_DB", "fill_table", "selected_id",
           "select_id", "launch"]


if PYSIDE_AVAILABLE:

    class MainWindowV2(ViewsMixin, BidCodeActionsMixin, OrderActionsMixin,
                       MaintActionsMixin, DueActionsMixin, ContractActionsMixin,
                       ProfitActionsMixin, QMainWindow):
        # P1-10：单页行数。查询条件已下推 SQL，翻页只改 LIMIT/OFFSET
        PAGE_SIZE = 200

        def __init__(self, db: Database, parent=None):
            super().__init__(parent)
            self.db = db
            self.bids = BidService(db)
            self.codes = PartCodeService(db)
            self.orders = OrderService(db)
            self.prod = ProductionService(db)
            self.trace = TraceService(db)
            self.maint = MaintenanceService(db)
            self.schedules = OrderScheduleService(db)   # v2.4 订单排产（附件落在 data/）
            self.contracts = PurchaseContractService(db)  # v3.4.0 采购合同管理
            self.profit = OrderProfitService(db)          # v3.5.0 订单盈亏分析
            self.presets = QueryPresetService(db)       # P2-5 查询方案
            self.current_view = "bids"
            self.filters: dict = {}
            self._trace_ids: list = []
            self._trace_headers, self._trace_rows = [], []
            self._view_actions = {}
            self._focus_table = None
            self._table_entity = {}
            self._refreshing = False
            # P1-10：每个视图独立分页游标；page_state 记录 (offset, 本页行数, 总数)
            self.page_offsets = {k: 0 for k in VIEW_KEYS}
            self.page_state: dict = {}
            # 主题挂到 QApplication（不是本窗口），所有弹窗才会继承同一套 QSS
            theme.apply_theme(QApplication.instance())
            self.setWindowTitle(f"{APP_TITLE}  ·  询报价 / 询配码 / 订单 / 生产排产  v{APP_VERSION}")
            self.resize(1600, 940)
            self.setMinimumSize(1180, 720)
            self._build_ui()
            self.switch_view("bids")
            # v3.3.0：交期提醒（徽标刷新 + 启动弹窗 + 托盘 + 每小时复查）
            self._init_due_reminder()

        # ── 总布局：菜单栏 / 左操作列表 / 中查询窗口 / 右结果 ──

        def _build_ui(self):
            self._build_menus()
            # v3.2.0：导航折叠/隐藏偏好（data/ui_state.json，内存库不落盘）
            self._ui_state = uistate.load(self._ui_state_dir())
            # v3.3.0：交期提醒偏好（data/reminder.json，与数据库同目录）
            self._due_prefs = reminder_prefs.load(self._ui_state_dir())
            self._due_report = {"rows": [], "long_overdue": [],
                                 "summary": {}, "days": self._due_prefs["days"],
                                 "signature": ""}
            self._due_tray = None
            root = QWidget()
            root.setObjectName("Root")
            layout = QVBoxLayout(root)
            layout.setContentsMargins(14, 12, 14, 8)
            layout.setSpacing(10)
            layout.addWidget(self._brand_bar())

            split = QSplitter(Qt.Horizontal)
            split.setChildrenCollapsible(False)
            self.left_panel = self._build_actions_tree()
            self.mid_panel = self._build_query_column()
            self.result_panel = self._build_result_column()
            split.addWidget(self.left_panel)
            split.addWidget(self.mid_panel)
            split.addWidget(self.result_panel)
            split.setStretchFactor(0, 0)
            split.setStretchFactor(1, 0)
            split.setStretchFactor(2, 1)
            split.setSizes([248, 332, 900])
            # v3.2.0：导航整体隐藏后，左侧留一条 30px 恢复条，随时能点回来
            self.nav_restore = QPushButton(">\n操\n作\n导\n航")
            self.nav_restore.setObjectName("NavRestore")
            self.nav_restore.setFixedWidth(30)
            self.nav_restore.setToolTip("展开操作导航")
            self.nav_restore.clicked.connect(lambda: self.set_nav_visible(True))
            body = QHBoxLayout()
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(6)
            body.addWidget(self.nav_restore)
            body.addWidget(split, 1)
            layout.addLayout(body, 1)

            bar = QStatusBar()
            bar.setSizeGripEnabled(False)
            self.status_label = QLabel("")
            bar.addWidget(self.status_label, 1)
            # v3.3.0：状态栏常驻交期提醒徽标，点击随时看清单
            self.due_badge = QPushButton("交期提醒：—")
            self.due_badge.setObjectName("DueBadge")
            self.due_badge.setCursor(QCursor(Qt.PointingHandCursor))
            self.due_badge.clicked.connect(self._show_due_reminders)
            bar.addPermanentWidget(self.due_badge)
            self.db_label = QLabel(f"数据库：{self.db.path}")
            self.db_label.setObjectName("Hint")
            bar.addPermanentWidget(self.db_label)
            self.setStatusBar(bar)
            self.setCentralWidget(root)

            self.filters = self.query_panel.values()
            self.tabs = _StackTabs(self.result_stack)   # 兼容旧冒烟脚本
            self.set_nav_visible(bool(self._ui_state.get("nav_visible", True)),
                                 persist=False)

        def _brand_bar(self) -> QWidget:
            card = QFrame()
            card.setObjectName("Card")
            card.setFixedHeight(62)
            h = QHBoxLayout(card)
            h.setContentsMargins(18, 8, 18, 8)
            h.setSpacing(14)
            title = QLabel(APP_TITLE)
            title.setObjectName("PageTitle")
            sub = QLabel(f"询报价 · 询配码 · 订单 · 生产排产   |   三域一体 · 双链路订单枢纽   |   v{APP_VERSION}")
            sub.setObjectName("SubTitle")
            h.addWidget(title)
            h.addSpacing(10)          # v3.4.3：品牌名变长，与副标题留出呼吸空间
            h.addWidget(sub)
            h.addStretch(1)
            self.view_badge = QLabel(VIEW_NAMES[0])
            self.view_badge.setObjectName("ViewBadge")
            h.addWidget(self.view_badge)
            return card

        # ── 菜单栏 ───────────────────────────────────────────

        def _action(self, menu, text, fn, shortcut=None, view=None):
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(lambda: self._menu_run(text, fn, view))
            menu.addAction(act)
            return act

        def _menu_run(self, label, fn, view=None):
            if view and view != self.current_view:
                self.switch_view(view)
            if fn is None:
                return
            self._run(label, fn)

        def _build_menus(self):
            mb = self.menuBar()
            m_file = mb.addMenu("文件(&F)")
            self._action(m_file, "刷新全部", self.refresh_all, "F5")
            self._action(m_file, "载入演示数据", self._seed_demo)
            m_file.addSeparator()
            self._action(m_file, "导出全系统 Excel", self._export_all, "Ctrl+E")
            self._action(m_file, "下载导入模板", self._download_templates)
            m_file.addSeparator()
            act_exit = QAction("退出", self)
            act_exit.setShortcut(QKeySequence("Ctrl+Q"))
            act_exit.triggered.connect(self.close)
            m_file.addAction(act_exit)

            # v3.2.0：菜单措辞与操作导航单一口径（短名、正式、无星号/口语括号）
            m_biz = mb.addMenu("业务(&B)")
            sub_bid = m_biz.addMenu("询报价与招投标")
            for text, fn in [("新增招标项目", self._new_bid),
                             ("录入投标明细", self._add_bid_item),
                             ("登记报价", self._mark_quoted),
                             ("登记中标", self._mark_won),
                             ("登记落标", self._mark_lost),
                             ("生成内部订单", self._gen_internal),
                             ("中标转排产", self._won_to_production)]:
                self._action(sub_bid, text, fn, view="bids")
            sub_code = m_biz.addMenu("询配码项目")
            for text, fn in [("新增询配码项目", self._new_code),
                             ("录入询配码明细", self._add_code_item),
                             ("导入配码明细", self._import_code_items),
                             ("下载导入模板", self._download_code_template),
                             ("提交配码文件", self._submit_code),
                             ("查看订单批次", self._view_batches)]:
                self._action(sub_code, text, fn, view="codes")
            sub_order = m_biz.addMenu("订单管理")
            for text, fn in [("新增订单", self._new_order),
                             ("录入订单明细", self._add_order_items),
                             ("导入订单明细", self._import_order_items),
                             ("单据识别建单", self._add_order),
                             ("导入订单", self._import_orders),
                             ("下载订单模板", self._download_order_template),
                             ("确认订单", self._confirm_order),
                             ("订单排产", self._order_schedule),
                             ("排产文件", self._view_schedule_files),
                             ("查看订单明细", self._view_order_items)]:
                self._action(sub_order, text, fn, view="orders")
            sub_prod = m_biz.addMenu("生产排产")
            for text, fn, view in [
                    ("采购合同管理", self._purchase_contracts, "production"),
                    ("生成采购合同", self._generate_purchase_contract, "orders"),
                    ("导入采购合同", self._import_purchase_contract, "orders"),
                    ("订单排产", self._order_schedule, "orders"),
                    ("排产单清单", self._view_order_schedules, "production"),
                    ("中标转排产", self._won_to_production, "production"),
                    ("关联订单明细", self._link_item, "production"),
                    ("解除关联", self._unlink, "production"),
                    ("推进项目状态", self._advance_project, "production")]:
                self._action(sub_prod, text, fn, view=view)

            # v3.3.0：交期自动提醒入口
            m_due = mb.addMenu("提醒(&R)")
            self._action(m_due, "查看临期订单", self._show_due_reminders)
            self._action(m_due, "标记已发货", self._mark_order_shipped,
                         view="orders")
            m_due.addSeparator()
            self._action(m_due, "交期提醒设置…", self._reminder_settings)

            m_query = mb.addMenu("查询(&S)")
            group = QActionGroup(self)
            group.setExclusive(True)
            for key, name in zip(VIEW_KEYS, VIEW_NAMES):
                act = QAction(name, self)
                act.setCheckable(True)
                act.triggered.connect(lambda _, k=key: self.switch_view(k))
                group.addAction(act)
                m_query.addAction(act)
                self._view_actions[key] = act
            self._view_actions["bids"].setChecked(True)
            m_query.addSeparator()
            self._action(m_query, "运行查询", self.run_query, "Ctrl+Return")
            self._action(m_query, "重置条件", self.reset_query)
            self._action(m_query, "导出当前视图", self._export_current)

            m_data = mb.addMenu("数据(&D)")
            self._action(m_data, "导入投标明细", self._import_bid_items, view="bids")
            self._action(m_data, "导入配码明细", self._import_code_items, view="codes")
            self._action(m_data, "导入订单", self._import_orders, view="orders")
            self._action(m_data, "导入订单明细", self._import_order_items,
                         view="orders")
            self._action(m_data, "下载配码明细模板", self._download_code_template, view="codes")
            self._action(m_data, "下载订单模板", self._download_order_template, view="orders")
            m_data.addSeparator()
            self._action(m_data, "导出招投标数据", self._export_bids, view="bids")
            self._action(m_data, "导出配码数据", self._export_codes, view="codes")
            self._action(m_data, "导出订单数据", self._export_orders, view="orders")
            self._action(m_data, "导出排产数据", self._export_production, view="production")
            self._action(m_data, "导出溯源数据", self._export_trace, view="trace")
            m_data.addSeparator()
            self._action(m_data, "一致性检查", self._consistency_check, view="overview")

            # 单据维护：不指定 view，作用于右侧当前点选的主结果或明细表
            m_maint = mb.addMenu("维护(&M)")
            for text, fn in [("编辑记录", self._edit_selected),
                             ("作废单据", self._void_selected),
                             ("删除记录", self._delete_selected),
                             ("审计留痕", self._show_audit),
                             ("审计日志查询…", self._audit_query)]:
                self._action(m_maint, text, fn)
            m_maint.addSeparator()
            self._action(m_maint, "当前操作人…", self._set_operator)
            m_maint.addSeparator()
            self._action(m_maint, "备份数据库", self._backup_db)
            self._action(m_maint, "从备份恢复…", self._restore_db)
            self._action(m_maint, "附件体检", self._attachment_check)
            m_maint.addSeparator()
            self._action(m_maint, "打开数据目录", self._open_data_dir)
            self._action(m_maint, "打开日志目录", self._open_logs)
            m_maint.addSeparator()
            self._action(m_maint, "当前操作对象说明", self._show_target_help)

            m_view = mb.addMenu("视图(&V)")
            self.act_left = QAction("显示操作导航", self)
            self.act_left.setCheckable(True)
            self.act_left.setChecked(True)
            self.act_left.setShortcut(QKeySequence("Ctrl+Shift+N"))
            self.act_left.triggered.connect(self.set_nav_visible)
            self.act_query = QAction("显示查询窗口", self)
            self.act_query.setCheckable(True)
            self.act_query.setChecked(True)
            self.act_query.triggered.connect(
                lambda on: self.mid_panel.setVisible(on))
            m_view.addAction(self.act_left)
            m_view.addAction(self.act_query)
            m_view.addSeparator()
            act_expand = QAction("展开全部导航分组", self)
            act_expand.triggered.connect(lambda: self._expand_all(True))
            act_fold = QAction("折叠全部导航分组", self)
            act_fold.triggered.connect(lambda: self._expand_all(False))
            m_view.addAction(act_expand)
            m_view.addAction(act_fold)
            m_view.addSeparator()
            for key, name in zip(VIEW_KEYS, VIEW_NAMES):
                act = QAction(f"切换到 {name}", self)
                act.triggered.connect(lambda _, k=key: self.switch_view(k))
                m_view.addAction(act)

            m_help = mb.addMenu("帮助(&H)")
            self._action(m_help, "业务口径说明", self._show_policy)
            self._action(m_help, "关于本系统", self._show_about)
            m_help.addSeparator()
            self._action(m_help, "检查更新…", self._check_updates)
            self._action(m_help, "设置更新源…", self._set_update_source)

        def _show_policy(self):
            theme.notify(self, "业务口径（六条硬约束）",
                "1. 询配码域与询报价域完全独立，零外键互指。\n"
                "2. 询配码明细「参考数量」是虚值，仅统计，不参与任何数量校验。\n"
                "3. 订单明细 qty_ordered 是实值，是生产排产唯一的数量来源。\n"
                "4. 中标项目 → 一张内部订单 → 产生排产数量；项目本身不直接产生数量。\n"
                "5. 一个询配码项目可产生多张外部订单；排产项目可挂中标项目作来源标签（不携带数量）。\n"
                "6. 订单排产：右键明细物资进入排产对话框，一个入口可导入 Word/Excel/"
                "PDF/图片/图纸等各类文件；只要存在 ≥1 个 Word 或 Excel 审批文件，"
                "保存即默认该批物资已排产。全部物资排满后订单状态自动变更。\n\n"
                "数量守恒：Σ production_links.allocated_qty ≤ order_items.qty_ordered")

        def _show_about(self):
            theme.notify(self, "关于",
                f"{APP_TITLE} v{APP_VERSION}\n\n"
                "布局：菜单栏 / 左侧操作列表 / 中间查询窗口 / 右侧查询结果。\n"
                "六个视图由左侧分组或「查询」菜单切换；右侧上半为主结果、下半为明细下钻。\n"
                "询配码虚值与订单实值在界面、服务层、数据库三层分离。")

        # ── 检查更新（v3.1.0） ───────────────────────────────

        def _check_updates(self, silent_on_error: bool = False):
            """菜单入口：拉清单比对版本，有新版弹更新对话框"""
            from v2.core import updater
            from v2.ui.update_dialog import UpdateDialog

            # v3.5.4：旧版本地发布目录源自动切到 GitHub，旧源留作离线备用
            source = updater.migrate_source(self.db.data_dir)
            if not source:
                if silent_on_error:
                    return
                self._set_update_source(first_time=True)
                return

            def job():
                return updater.check(source, APP_VERSION,
                                     fallback=updater.load_fallback(self.db.data_dir))

            def done(res):
                if res["status"] == "update":
                    UpdateDialog(self, res["manifest"]).exec()
                elif res["status"] == "latest":
                    if not silent_on_error:
                        theme.notify(
                            self, "检查更新",
                            f"已是最新版本 v{APP_VERSION}"
                            f"（远端 v{res['manifest']['version']}）。")
                else:
                    if not silent_on_error:
                        theme.notify(self, "检查更新失败", res["message"],
                                     kind="warn")

            async_task.run_task(self, job, on_done=done, on_error=lambda m: (
                None if silent_on_error
                else theme.notify(self, "检查更新失败", m, kind="warn")))

        def _auto_check_updates(self):
            """启动 3 秒后自动检查：无源 / 出错 / 无更新一律静默"""
            try:
                self._check_updates(silent_on_error=True)
            except Exception:                       # noqa: BLE001
                pass

        def _set_update_source(self, first_time: bool = False):
            from v2.core import updater            # v3.5.4：原先缺这行，首次配置会 NameError
            from v2.ui.update_dialog import UpdateSourceDialog
            dlg = UpdateSourceDialog(self, self.db.data_dir)
            if dlg.exec() and first_time and updater.load_source(self.db.data_dir):
                self._check_updates()

        # v3.3.0：交期提醒动作全部在 panels/due_actions.py::DueActionsMixin，
        # 这里只保留状态栏徽标、菜单入口与启动挂载（保持主窗口骨架精简）。

        # ── 左侧操作列表（v3.2.0：逐项折叠 + 整体隐藏 + 状态持久化） ──

        def _ui_state_dir(self):
            """内存库 / 离屏冒烟不写盘，避免污染工程 data/"""
            return (None if getattr(self.db, "path", None) == ":memory:"
                    else self.db.data_dir)

        def _persist_ui_state(self):
            uistate.save(self._ui_state_dir(), self._ui_state)

        def set_nav_visible(self, on: bool, persist: bool = True):
            """隐藏/显示整个操作导航；隐藏时左侧保留 30px 恢复条"""
            on = bool(on)
            self.left_panel.setVisible(on)
            self.nav_restore.setVisible(not on)
            if hasattr(self, "act_left"):
                self.act_left.blockSignals(True)
                self.act_left.setChecked(on)
                self.act_left.blockSignals(False)
            if persist:
                self._ui_state["nav_visible"] = on
                self._persist_ui_state()

        def _set_group_expanded(self, group, expanded: bool,
                                persist: bool = True):
            """单个分组折叠/展开，标题前缀 ▼ / → 即折叠指示器
            （实截验证：微软雅黑含 ▼、→，但小三角 U+25BE/25B8 与 ▶ U+25B6
              均缺字渲染成 □ 方块，故折叠态统一用中文箭头 →）"""
            title = str(group.data(0, Qt.UserRole + 2) or "")
            group.setText(0, ("▼ " if expanded else "→ ") + title)
            group.setExpanded(bool(expanded))
            if persist and title:
                self._ui_state.setdefault("nav_expanded", {})[title] = bool(expanded)
                self._persist_ui_state()

        def _toggle_group(self, group):
            self._set_group_expanded(group, not group.isExpanded())

        def _expand_all(self, expanded: bool):
            for i in range(self.action_tree.topLevelItemCount()):
                self._set_group_expanded(self.action_tree.topLevelItem(i),
                                         expanded, persist=False)
            self._ui_state["nav_expanded"] = {
                str(g.data(0, Qt.UserRole + 2) or ""): bool(expanded)
                for g in (self.action_tree.topLevelItem(i)
                          for i in range(self.action_tree.topLevelItemCount()))}
            self._persist_ui_state()

        def _build_actions_tree(self) -> QWidget:
            panel = QFrame()
            panel.setObjectName("Card")
            v = QVBoxLayout(panel)
            v.setContentsMargins(12, 12, 12, 10)
            v.setSpacing(9)

            head_row = QHBoxLayout()
            head_row.setSpacing(6)
            head = QLabel("操作导航")
            head.setObjectName("SectionTitle")
            head_row.addWidget(head)
            head_row.addStretch(1)
            btn_expand = QPushButton("展开")
            btn_fold = QPushButton("折叠")
            btn_hide = QPushButton("隐藏 ←")
            for b, tip, fn in (
                    (btn_expand, "展开全部分组", lambda: self._expand_all(True)),
                    (btn_fold, "折叠全部分组", lambda: self._expand_all(False)),
                    (btn_hide, "隐藏整个操作导航（可从左侧细条或视图菜单恢复）",
                     lambda: self.set_nav_visible(False))):
                b.setObjectName("Quiet")
                b.setToolTip(tip)
                b.clicked.connect(fn)
                head_row.addWidget(b)
            v.addLayout(head_row)

            self.tree_search = QLineEdit()
            self.tree_search.setPlaceholderText("筛选操作…")
            self.tree_search.setClearButtonEnabled(True)
            self.tree_search.textChanged.connect(self._filter_tree)
            v.addWidget(self.tree_search)

            self.action_tree = QTreeWidget()
            self.action_tree.setHeaderHidden(True)
            # 折叠指示用标题自带的 ▼ / →，不启用 Qt 默认 branch 箭头，
            # 否则「点箭头折叠」与「点标题切视图」会在同一矩形里互相抢事件
            self.action_tree.setRootIsDecorated(False)
            self.action_tree.setIndentation(14)
            self.action_tree.itemClicked.connect(self._on_tree_clicked)
            saved = self._ui_state.get("nav_expanded") or {}
            for title, key, actions in ACTION_GROUPS:
                group = QTreeWidgetItem(self.action_tree, [title])
                group.setData(0, Qt.UserRole + 1, key)
                group.setData(0, Qt.UserRole + 2, title)
                f = group.font(0)
                f.setBold(True)
                group.setFont(0, f)
                group.setForeground(0, QColor(theme.BRAND_DARK))
                group.setBackground(0, QColor("#EEF4FF"))
                self._set_group_expanded(
                    group, bool(saved.get(title, True)), persist=False)
                for action in actions:
                    label, fn_name = action[0], action[1]
                    action_view = action[2] if len(action) > 2 else key
                    tip = action[3] if len(action) > 3 else ""
                    child = QTreeWidgetItem(group, [label])
                    child.setData(0, Qt.UserRole, (fn_name, action_view))
                    child.setToolTip(0, f"{title} · {label}\n{tip}" if tip
                                     else f"{title} · {label}")
            v.addWidget(self.action_tree, 1)
            count = sum(len(a) for _, _, a in ACTION_GROUPS)
            self.tree_hint = QLabel(
                f"共 {count} 项操作 · 点名称即执行\n"
                "点分组标题切视图 · 点标题左侧 ▼/→ 折叠 · 可整体隐藏")
            self.tree_hint.setObjectName("Hint")
            self.tree_hint.setWordWrap(True)
            v.addWidget(self.tree_hint)
            return panel

        def _filter_tree(self, text):
            kw = (text or "").strip().lower()
            for i in range(self.action_tree.topLevelItemCount()):
                group = self.action_tree.topLevelItem(i)
                title = str(group.data(0, Qt.UserRole + 2) or "")
                hit_group = kw in title.lower()
                shown = 0
                for j in range(group.childCount()):
                    child = group.child(j)
                    hit = (not kw) or hit_group or kw in child.text(0).lower()
                    child.setHidden(not hit)
                    shown += 1 if hit else 0
                group.setHidden(bool(kw) and not (hit_group or shown))
                # 搜索命中时自动展开，否则折叠的分组会看不到结果
                if kw and (hit_group or shown) and not group.isExpanded():
                    self._set_group_expanded(group, True, persist=False)

        def _on_tree_clicked(self, item, _col):
            key = item.data(0, Qt.UserRole + 1)
            payload = item.data(0, Qt.UserRole)
            if payload is None:                      # 分组标题行
                pos = self.action_tree.viewport().mapFromGlobal(QCursor.pos())
                rect = self.action_tree.visualItemRect(item)
                if pos.x() <= rect.left() + 22:       # 左侧折叠指示区
                    self._toggle_group(item)
                    return
                if key:
                    self.switch_view(key)
                return
            fn_name, view = payload
            if view and view != self.current_view:
                self.switch_view(view)
            fn = getattr(self, fn_name, None)
            if fn is not None:
                self._run(item.text(0), fn)

        # ── 中间查询窗口 ─────────────────────────────────────

        def _build_query_column(self) -> QWidget:
            panel = QWidget()
            v = QVBoxLayout(panel)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(10)
            self.query_panel = query_panel.QueryPanel(
                self.db, on_query=self.run_query, on_reset=self.reset_query,
                presets=self.presets,
                on_save_preset=self._save_query_preset,
                on_apply_preset=self._apply_query_preset,
                on_delete_preset=self._delete_query_preset)
            v.addWidget(self.query_panel, 1)

            kpi = QGroupBox("全库概览")
            grid = QGridLayout(kpi)
            grid.setContentsMargins(12, 16, 12, 12)
            grid.setSpacing(8)
            self.kpi_nums = {}
            specs = [
                ("bid", "招投标项目"), ("code", "询配码项目"),
                ("order", "订单（外 / 内）"), ("item", "订单明细"),
                ("project", "生产项目"), ("link", "排产关联"),
            ]
            for i, (key, caption) in enumerate(specs):
                card, num = theme.kpi_card("0", caption)
                self.kpi_nums[key] = num
                grid.addWidget(card, i // 2, i % 2)
            v.addWidget(kpi)
            return panel

        def run_query(self):
            self.filters = self.query_panel.values()
            self.page_offsets = {k: 0 for k in VIEW_KEYS}   # 条件变了回到第一页
            self.refresh_all()
            self.status_label.setText(f"查询完成 · {self.query_panel.summary()}")

        def reset_query(self):
            self.query_panel.clear()
            self.filters = self.query_panel.values()
            self.page_offsets = {k: 0 for k in VIEW_KEYS}
            self.refresh_all()
            self.status_label.setText("查询条件已重置 · 显示全部")

        # ── P2-5 查询方案 ────────────────────────────────────

        def _save_query_preset(self):
            """把当前条件存成方案；同名时询问是否覆盖"""
            filters = self.query_panel.values()
            if self.query_panel.summary() == "未设置条件 · 显示全部":
                theme.notify(self, "无需保存", "当前没有设置任何条件，方案会和「全部」重复。",
                             kind="info")
                return
            data = dialogs.ask(self, "保存查询方案", [
                {"key": "name", "label": "方案名称", "type": "text", "required": True,
                 "placeholder": "例如：上月待发货 / 我的逾期单"},
            ], subtitle="方案只保存筛选条件，不保存查询结果；同名再存即为覆盖。")
            if not data:
                return
            name = str(data.get("name") or "").strip()
            exist = self.presets.find_by_name(name)
            try:
                saved = self.presets.save(name, self.current_view, filters,
                                          overwrite=bool(exist), operator=cur.name(self.db.data_dir))
            except Exception as exc:                      # noqa: BLE001
                theme.notify(self, "保存方案失败", str(exc), kind="error")
                return
            self.query_panel.reload_presets()
            idx = self.query_panel.preset_box.findData(saved["id"])
            if idx >= 0:
                self.query_panel.preset_box.setCurrentIndex(idx)
            theme.notify(self, "已保存查询方案",
                         f"「{saved['name']}」已保存，下次在左侧「方案」下拉里直接选。")

        def _apply_query_preset(self, preset_id: int):
            try:
                preset = self.presets.get(int(preset_id))
            except Exception as exc:                      # noqa: BLE001
                theme.notify(self, "套用方案失败", str(exc), kind="error")
                return
            view = preset.get("view_key") or self.current_view
            if view != self.current_view:
                self.switch_view(view, refresh=False)
            self.query_panel.apply_filters(preset["filters"])
            self.filters = self.query_panel.values()
            self.page_offsets = {k: 0 for k in VIEW_KEYS}
            self.refresh_all()
            self.status_label.setText(f"已套用查询方案：{preset['name']}")

        def _delete_query_preset(self, preset_id):
            if not preset_id:
                theme.notify(self, "请先选择方案", "下拉里还没有选中任何方案。", kind="info")
                return
            try:
                preset = self.presets.get(int(preset_id))
            except Exception:                             # noqa: BLE001
                return
            if not theme.confirm(self, "删除查询方案",
                                 f"确定删除「{preset['name']}」？只删条件，不影响业务数据。"):
                return
            self.presets.delete(int(preset_id))
            self.query_panel.reload_presets()
            self.status_label.setText(f"已删除查询方案：{preset['name']}")

        def _export_current(self):
            mapping = {
                "bids": self._export_bids, "codes": self._export_codes,
                "orders": self._export_orders, "production": self._export_production,
                "trace": self._export_trace, "overview": self._export_all,
            }
            return mapping[self.current_view]()

        # ── 右侧查询结果 ─────────────────────────────────────

        def _build_result_column(self) -> QWidget:
            panel = QFrame()
            panel.setObjectName("Card")
            v = QVBoxLayout(panel)
            v.setContentsMargins(14, 12, 14, 12)
            v.setSpacing(10)
            head = QHBoxLayout()
            head.setSpacing(10)
            self.result_title = QLabel(VIEW_NAMES[0])
            self.result_title.setObjectName("SectionTitle")
            self.count_badge = QLabel("0 条")
            self.count_badge.setObjectName("CountBadge")
            head.addWidget(self.result_title)
            head.addWidget(self.count_badge)
            head.addStretch(1)
            # P1-10：SQL 分页控件（条件已下推到数据库，这里只翻页）
            self.pager_label = QLabel("")
            self.pager_label.setObjectName("Hint")
            self.btn_prev_page = QPushButton("‹ 上一页")
            self.btn_next_page = QPushButton("下一页 ›")
            self.btn_prev_page.setEnabled(False)
            self.btn_next_page.setEnabled(False)
            self.btn_prev_page.clicked.connect(lambda: self._page(-1))
            self.btn_next_page.clicked.connect(lambda: self._page(1))
            head.addWidget(self.pager_label)
            head.addWidget(self.btn_prev_page)
            head.addWidget(self.btn_next_page)
            btn_export = QPushButton("导出当前视图")
            btn_export.clicked.connect(lambda: self._run("导出当前视图", self._export_current))
            self.btn_add_order = QPushButton("添加订单")
            self.btn_add_order.setToolTip(
                "从图片 / 表格 / PDF / Word 读取订单信息并建单；仅订单视图显示")
            self.btn_add_order.clicked.connect(
                lambda: self._run("添加订单", self._add_order))
            btn_refresh = QPushButton("刷新")
            btn_refresh.setObjectName("Primary")
            btn_refresh.clicked.connect(self.refresh_all)
            head.addWidget(btn_export)
            head.addWidget(self.btn_add_order)
            head.addWidget(btn_refresh)
            v.addLayout(head)

            # v3.2.0：重要操作直接摆在主操作界面，不必每次去左侧导航里找
            self.action_bar_host = QWidget()
            self.action_bar_host.setObjectName("ActionBar")
            self.action_bar = QHBoxLayout(self.action_bar_host)
            self.action_bar.setContentsMargins(0, 0, 0, 0)
            self.action_bar.setSpacing(7)
            v.addWidget(self.action_bar_host)

            self.target_label = QLabel(
                "操作对象：未选中（点选右侧主结果或明细表任意一行）")
            self.target_label.setObjectName("Hint")
            self.target_label.setWordWrap(True)
            v.addWidget(self.target_label)

            self.result_stack = QStackedWidget()
            self._build_bids_tab()
            self._build_codes_tab()
            self._build_orders_tab()
            self._build_production_tab()
            self._build_trace_tab()
            self._build_overview_tab()
            v.addWidget(self.result_stack, 1)

            self.result_tables = {
                "bids": self.tbl_bids, "codes": self.tbl_codes,
                "orders": self.tbl_orders, "production": self.tbl_projects,
                "trace": self.tbl_trace, "overview": self.tbl_capacity,
            }
            self._register_target_tables()
            self._update_target_label()
            return panel

        # ── 主操作界面快捷栏（v3.2.0） ───────────────────────

        def _refresh_action_bar(self):
            """按当前视图重建高频按钮 + 「更多操作」完整动作菜单"""
            if not hasattr(self, "action_bar"):
                return
            while self.action_bar.count():
                item = self.action_bar.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            for i, (fn, override) in enumerate(
                    PRIMARY_ACTIONS.get(self.current_view, [])):
                entry = find_action(fn, self.current_view)
                if not entry:
                    continue
                label, _, default_view, tip = entry
                view = default_view if override is None else (override or None)
                b = QPushButton(label)
                b.setToolTip(tip or label)
                if i == 0:
                    b.setObjectName("Primary")
                b.clicked.connect(
                    lambda _=False, l=label, f=fn, v=view:
                    self._run_action(l, f, v))
                self.action_bar.addWidget(b)

            btn_more = QPushButton("更多操作 ▼")
            btn_more.setObjectName("MoreActions")
            menu = QMenu(btn_more)
            for title, key, acts in ACTION_GROUPS:
                if key not in (self.current_view, None):
                    continue
                for act in acts:
                    label, fn = act[0], act[1]
                    view = act[2] if len(act) > 2 else key
                    tip = act[3] if len(act) > 3 else ""
                    a = menu.addAction(label)
                    a.setToolTip(f"{title} · {tip}" if tip else label)
                    a.triggered.connect(
                        lambda _=False, l=label, f=fn, v=view:
                        self._run_action(l, f, v))
            btn_more.setMenu(menu)
            self.action_bar.addWidget(btn_more)
            self.action_bar.addStretch(1)

        def _run_action(self, label, fn_name, view=None):
            """导航 / 菜单 / 快捷栏共用的动作执行口（先按需切视图）"""
            if view and view != self.current_view:
                self.switch_view(view)
            fn = getattr(self, fn_name, None)
            if fn is not None:
                self._run(label, fn)

        # ── 统一动作包装 ─────────────────────────────────────

        def _run(self, label, fn):
            try:
                result = fn()
            except Exception as exc:
                applog.log_exception(label, exc)      # P0-4：失败必有堆栈落盘
                theme.notify(self, f"{label} · 失败",
                             f"{exc}\n\n详细堆栈已写入日志，可从「维护 → 打开日志目录」查看。",
                             kind="error")
                self.status_label.setText(f"{label} 失败：{exc}")
                return None
            self.refresh_all()
            # v3.3.0：任何业务动作（尤其是编辑状态 / 标记发货）后即时重算提醒
            try:
                self._refresh_due_badge()
            except Exception:                                   # noqa: BLE001
                pass
            self.status_label.setText(f"{label} · 完成" + (f"：{result}" if isinstance(result, str) else ""))
            return result

        def _require(self, table, what: str):
            entity_id = selected_id(table)
            if entity_id is None:
                theme.notify(self, "请先选择", f"请先在表格中点选一个{what}", kind="warn")
            return entity_id


else:

    class MainWindowV2:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise RuntimeError("未安装 PySide6，无法创建界面。请先 pip install PySide6")


def launch(db_path: str = None) -> int:
    if not PYSIDE_AVAILABLE:
        print("未检测到 PySide6，界面不可用。核心链路仍可用：python v2/main_v2.py --smoke",
              file=sys.stderr)
        return 1
    db = Database(db_path or str(DEFAULT_DB))
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindowV2(db)
    window.show()
    # v3.1.0：启动 3 秒后后台自动检查更新（无源/出错/无更新均静默）
    try:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, window._auto_check_updates)
    except Exception:
        pass
    return app.exec()

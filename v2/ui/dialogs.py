#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.2 业务对话框（P1 可操作界面，主题统一版）

包含：通用表单、新建项目/明细、新建订单、导入订单、排产关联、只读表格、Excel 导入导出。
所有对话框只负责收集输入，业务动作由主窗口调用服务层执行并统一处理异常。

外观：全部继承 ThemedDialog，与主窗口共用 theme.QSS；消息框/文件框一律走
theme.notify / theme.confirm / theme.pick_path，不再直接调用 Qt 静态函数，
以保证 Qt 自绘弹窗也能吃到同一套配色。
"""
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from PySide6.QtWidgets import (
        QDialog, QFormLayout, QGridLayout, QHBoxLayout, QVBoxLayout,
        QLineEdit, QTextEdit, QDoubleSpinBox, QSpinBox, QComboBox, QPushButton,
        QCheckBox, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
        QScrollArea, QWidget, QFrame, QAbstractItemView, QGroupBox,
    )
    from PySide6.QtCore import Qt
    PYSIDE_AVAILABLE = True
except ImportError:
    PYSIDE_AVAILABLE = False

from ..core import excelio
from ..core import current_user as cur
from ..core import docread
from ..core import fields as fdefs
from ..core import models
from ..core import order_fields as ofdefs
# v3.1.2：排产附件的类别/过滤器常量已收敛到 domains.schedule + schedule_dialog，
# 本模块不再重复导入（原先是 v2.9.3 四按钮时代的历史遗留，实际未使用）。
from . import theme
from . import async_task

if PYSIDE_AVAILABLE:

    # v3.0：弹窗外壳与订单排产对话框已拆到独立模块，这里保持同名再导出，
    # 外部（主窗口 / 测试 / 脚本）继续用 dialogs.ThemedDialog / dialogs.OrderScheduleDialog。
    from .base_dialog import ThemedDialog          # noqa: E402,F401
    from .datefield import DatePicker             # noqa: E402  v3.2.0 统一日历选择
    from .customerfield import CustomerComboBox   # noqa: E402  v3.4.2 客户名称检索
    from .partyfield import PartyComboBox         # noqa: E402  v3.4.5 合同需方/供方检索

    def _brief(text: Any, limit: int = 88) -> str:
        """审计 JSON 摘要：单行 + 截断，避免撑爆表格列宽"""
        if not text:
            return "—"
        s = " ".join(str(text).split())
        return s if len(s) <= limit else s[:limit] + "…"

    # ── 通用表单 ────────────────────────────────────────────

    class FormDialog(ThemedDialog):
        """
        fields: [{key,label,type,default,options,required,decimals,minimum,maximum}]
        type: text | float | int | combo | textarea | date | path | customer | party
        customer（v3.4.2）= 客户名称智能输入：可编辑下拉 + 历史客户候选 +
                           关键字包含匹配；parent 带 db 时自动从订单/项目收集。
        party（v3.4.5）  = 合同往来单位智能输入：party_column 取 buyer / supplier，
                           从历史采购合同（需方另补订单客户）现查候选，关键字包含匹配。
        columns=2 时用两列网格（询配码明细 10 字段 / 编辑白名单 14 字段用），
        字段多时自动加滚动区，避免弹窗被撑出屏幕。
        """
        def __init__(self, parent, title: str, fields: List[Dict[str, Any]],
                     subtitle: str = "", columns: int = 1,
                     width: int = 580, height: int = 460):
            super().__init__(parent, title,
                             subtitle or "按表单填写业务数据，带 * 为必填",
                             width=width if columns == 1 else max(width, 900),
                             height=height if columns == 1 else max(height, 560))
            self.fields = fields
            self.widgets: Dict[str, Any] = {}
            form = QGridLayout() if columns > 1 else QFormLayout()
            if columns > 1:
                form.setHorizontalSpacing(14)
                form.setVerticalSpacing(9)
                form.setColumnStretch(1, 1)
                form.setColumnStretch(3, 1)
            for idx, f in enumerate(fields):
                key, typ = f["key"], f.get("type", "text")
                default = f.get("default", "")
                if typ == "combo":
                    w = QComboBox()
                    for label, value in f.get("options", []):
                        w.addItem(str(label), value)
                    # v2.9：可编辑下拉 = 预设项 + 自由输入（订单来源这类会新增取值的字段）
                    if f.get("editable"):
                        w.setEditable(True)
                        w.setInsertPolicy(QComboBox.NoInsert)
                    if default not in (None, "") and f.get("options"):
                        idx_data = w.findData(default)
                        if idx_data >= 0:
                            w.setCurrentIndex(idx_data)
                elif typ == "float":
                    w = QDoubleSpinBox()
                    w.setDecimals(f.get("decimals", 3))
                    w.setRange(f.get("minimum", 0.0), f.get("maximum", 1e9))
                    w.setValue(float(default or 0))
                elif typ == "int":
                    w = QSpinBox()
                    w.setRange(int(f.get("minimum", 0)), int(f.get("maximum", 999999)))
                    w.setValue(int(default or 0))
                elif typ == "textarea":
                    w = QTextEdit()
                    w.setPlainText(str(default or ""))
                    w.setMinimumHeight(70)
                elif typ == "path":
                    # v3.0.1：路径字段 = 输入框 + 「浏览…」按钮，
                    # 点击走 theme.pick_path 选文件，不再要求手工敲路径
                    holder = QWidget()
                    hh = QHBoxLayout(holder)
                    hh.setContentsMargins(0, 0, 0, 0)
                    hh.setSpacing(6)
                    edit = QLineEdit(str(default or ""))
                    btn = QPushButton("浏览…")
                    btn.setObjectName("Secondary")
                    def _browse():
                        picked = theme.pick_path(
                            holder, f.get("dialog_title", "选择文件"),
                            "open", filters=f.get("filters", ""))
                        if picked:
                            edit.setText(picked)
                    btn.clicked.connect(_browse)
                    hh.addWidget(edit, 1)
                    hh.addWidget(btn)
                    w = holder
                    self.widgets[key] = (edit, "text")
                elif typ == "date":
                    # v3.2.0：所有日期字段改为只读 + 弹出日历，禁止手工输入
                    w = DatePicker(value=default)
                    w.setPlaceholderText("点击选择日期（可留空）")
                elif typ == "customer":
                    # v3.4.2：客户名称 = 历史候选 + 关键字检索 + 可自由新输
                    # 候选库从调用者（主窗口，带 self.db）取，FormDialog 自身不持 db
                    w = CustomerComboBox(parent=self,
                                         db=getattr(parent, "db", None),
                                         value=default)
                elif typ == "party":
                    # v3.4.5：需方 / 供方 = 历史合同往来单位 + 关键字检索 + 自由新输
                    w = PartyComboBox(parent=self,
                                      db=getattr(parent, "db", None),
                                      column=f.get("party_column") or "supplier",
                                      value=default)
                else:
                    w = QLineEdit(str(default or ""))
                w.setMinimumWidth(220 if columns > 1 else 260)
                if typ != "path":      # path 已在上面登记为 (QLineEdit, "text")
                    self.widgets[key] = (w, typ)
                caption = f"{f['label']}{' *' if f.get('required') else ''}"
                if columns > 1:
                    row, col = divmod(idx, columns)
                    lab = QLabel(caption)
                    lab.setObjectName("FormLabel")
                    lab.setWordWrap(True)
                    form.addWidget(lab, row, col * 2)
                    form.addWidget(w, row, col * 2 + 1)
                else:
                    form.addRow(caption, w)
            if len(fields) >= 12 and columns > 1:
                holder = QWidget()
                holder.setObjectName("FormScrollContent")
                holder.setLayout(form)
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setWidget(holder)
                self.body_layout.addWidget(scroll, 1)
            else:
                self.body_layout.addLayout(form)
            self.hint = theme.label("带 * 为必填", "DialogHint")
            self.body_layout.addWidget(self.hint)
            self.add_buttons(on_ok=self._validate)

        def _validate(self):
            for f in self.fields:
                if f.get("required") and not str(self.value(f["key"]) or "").strip():
                    self.hint.setText(f"请填写：{f['label']}")
                    self.hint.setStyleSheet(
                        f"color: {theme.DANGER}; font-weight: 600;")
                    return
            self.accept()

        def value(self, key: str) -> Any:
            w, typ = self.widgets[key]
            if typ == "combo":
                data = w.currentData()
                if w.isEditable():
                    text = w.currentText().strip()
                    # 手输内容没有对应条目（currentData 为 None）→ 直接落原文
                    if text and data is None:
                        return text
                return data
            if typ in ("float", "int"):
                return w.value()
            if typ == "textarea":
                return w.toPlainText().strip()
            return w.text().strip()

        def values(self) -> Dict[str, Any]:
            return {f["key"]: self.value(f["key"]) for f in self.fields}

    def ask(parent, title: str, fields: List[Dict[str, Any]],
            subtitle: str = "", **layout) -> Optional[Dict[str, Any]]:
        # 字段多时自动两列，避免长表单被压成竖排或超出屏幕
        layout.setdefault("columns", 2 if len(fields) >= 8 else 1)
        dlg = FormDialog(parent, title, fields, subtitle, **layout)
        return dlg.values() if dlg.exec() == QDialog.Accepted else None

    def options_from(rows: Sequence[Dict[str, Any]], label) -> List[tuple]:
        return [(label(r), r["id"]) for r in rows]

    # ── 只读表格（含导出） ──────────────────────────────────

    class TableViewDialog(ThemedDialog):
        def __init__(self, parent, title: str, headers: Sequence[str],
                     rows: Sequence[Sequence[Any]], default_export: str = "导出结果"):
            super().__init__(parent, title, "只读明细 · 可导出 Excel / CSV",
                             width=900, height=480)
            self.headers, self.rows = list(headers), [list(r) for r in rows]
            self.default_export = default_export
            self.table = theme.style_table(QTableWidget())
            self.table.setColumnCount(len(self.headers))
            self.table.setHorizontalHeaderLabels(self.headers)
            self.table.setRowCount(len(self.rows))
            for i, row in enumerate(self.rows):
                for j, value in enumerate(row):
                    self.table.setItem(i, j, QTableWidgetItem(
                        "" if value is None else str(value)))
            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(
                min(1, len(self.headers) - 1), QHeaderView.Stretch)
            self.body_layout.addWidget(self.table, 1)
            self.body_layout.addWidget(theme.label(f"共 {len(self.rows)} 行", "DialogCount"))
            btn_export = QPushButton("导出 Excel/CSV")
            btn_export.clicked.connect(self._export)
            self.footer_layout.insertWidget(0, btn_export)
            self.add_buttons(ok_text="关闭", show_cancel=False)

        def _export(self):
            path = theme.pick_path(
                self, "导出", mode="save", default_name=f"{self.default_export}.xlsx",
                filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
            if path:
                excelio.export_workbook(path, {self.windowTitle(): (self.headers, self.rows)})
                theme.notify(self, "导出完成", f"已导出：{path}")

    class AuditQueryDialog(ThemedDialog):
        """审计日志查询（P1-9）：条件下推 SQL + 分页，避免一次性把全表读进内存"""

        HEADERS = ["时间", "单据类型", "操作", "对象 ID", "操作人", "变更前", "变更后"]

        def __init__(self, parent, db, maint):
            super().__init__(parent, "审计日志查询",
                             "按实体 / 操作 / 操作人 / 关键字 / 日期筛选 · 分页浏览 · 可导出",
                             width=1080, height=640)
            self.db, self.maint = db, maint
            self.offset, self.limit, self.total = 0, 200, 0

            box = QGridLayout()
            box.setHorizontalSpacing(10)
            box.setVerticalSpacing(6)
            self.cb_entity = QComboBox()
            self.cb_entity.addItem("全部单据", "")
            for e in maint.audit_entities():
                self.cb_entity.addItem(f"{e['label']}（{e['count']}）", e["key"])
            self.cb_action = QComboBox()
            self.cb_action.addItem("全部操作", "")
            for a in maint.audit_actions():
                self.cb_action.addItem(f"{a['label']}（{a['count']}）", a["key"])
            self.ed_operator = QLineEdit()
            self.ed_operator.setPlaceholderText("操作人，如 Administrator")
            self.ed_keyword = QLineEdit()
            self.ed_keyword.setPlaceholderText("内容关键字（变更前/后 JSON）")
            self.ed_from = DatePicker()
            self.ed_from.setPlaceholderText("起始日期（可留空）")
            self.ed_to = DatePicker()
            self.ed_to.setPlaceholderText("截止日期（可留空）")

            for col, (cap, w) in enumerate((
                    ("单据类型", self.cb_entity), ("操作", self.cb_action),
                    ("操作人", self.ed_operator), ("关键字", self.ed_keyword))):
                box.addWidget(theme.label(cap, "Hint"), 0, col * 2)
                box.addWidget(w, 0, col * 2 + 1)
            box.addWidget(theme.label("时间范围", "Hint"), 1, 0)
            box.addWidget(self.ed_from, 1, 1)
            box.addWidget(theme.label("→", "Hint"), 1, 2)
            box.addWidget(self.ed_to, 1, 3)
            btn = QPushButton("查询")
            btn.clicked.connect(lambda: self.search(reset=True))
            box.addWidget(btn, 1, 7)
            for i in range(4):
                box.setColumnStretch(i * 2 + 1, 1)
            self.body_layout.addLayout(box)

            self.table = theme.style_table(QTableWidget())
            self.table.setColumnCount(len(self.HEADERS))
            self.table.setHorizontalHeaderLabels(self.HEADERS)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.body_layout.addWidget(self.table, 1)

            bar = QHBoxLayout()
            self.lbl = theme.label("", "Hint")
            self.btn_prev = QPushButton("上一页")
            self.btn_next = QPushButton("下一页")
            self.btn_prev.clicked.connect(lambda: self._page(-1))
            self.btn_next.clicked.connect(lambda: self._page(1))
            btn_export = QPushButton("导出查询结果")
            btn_export.clicked.connect(self._export)
            bar.addWidget(self.lbl)
            bar.addStretch(1)
            bar.addWidget(self.btn_prev)
            bar.addWidget(self.btn_next)
            bar.addWidget(btn_export)
            self.body_layout.addLayout(bar)
            self.add_buttons(ok_text="关闭", show_cancel=False)
            for w in (self.cb_entity, self.cb_action):
                w.currentIndexChanged.connect(lambda _=0: self.search(reset=True))
            self.search(reset=True)

        # ── 数据 ─────────────────────────────────────────
        def _query_args(self) -> Dict[str, Any]:
            return {
                "entity": self.cb_entity.currentData() or "",
                "action": self.cb_action.currentData() or "",
                "operator": self.ed_operator.text().strip(),
                "keyword": self.ed_keyword.text().strip(),
                "date_from": self.ed_from.text().strip(),
                "date_to": self.ed_to.text().strip(),
            }

        def search(self, reset: bool = False):
            if reset:
                self.offset = 0
            res = self.maint.audit_page(offset=self.offset, limit=self.limit,
                                        **self._query_args())
            self.total = res["total"]
            self.offset = res["offset"]
            rows = res["rows"]
            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                cells = (r["at"], r["entity_label"], r["action_label"],
                         r["entity_id"] or "—", r["operator"] or "—",
                         _brief(r["before_json"]), _brief(r["after_json"]))
                for j, v in enumerate(cells):
                    self.table.setItem(i, j, QTableWidgetItem(str(v)))
            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            start = self.offset + 1 if rows else 0
            end = self.offset + len(rows)
            self.lbl.setText(f"共 {self.total} 条 · 当前第 {start}–{end} 条")
            self.btn_prev.setEnabled(self.offset > 0)
            self.btn_next.setEnabled(self.offset + len(rows) < self.total)

        def _page(self, delta: int):
            target = self.offset + delta * self.limit
            if target < 0:
                return
            self.offset = target
            self.search()

        def _export(self):
            res = self.maint.audit_page(offset=0, limit=100000, **self._query_args())
            if not res["rows"]:
                theme.notify(self, "没有数据", "当前筛选条件没有命中任何审计记录。", kind="warn")
                return
            data = [(r["at"], r["entity_label"], r["action_label"], r["entity_id"] or "",
                     r["operator"] or "", r["before_json"] or "", r["after_json"] or "")
                    for r in res["rows"]]
            path = theme.pick_path(self, "导出审计日志", mode="save",
                                   default_name="审计日志.xlsx",
                                   filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
            if path:
                excelio.export_workbook(path, {"审计日志": (self.HEADERS, data)})
                theme.notify(self, "导出完成", f"已导出 {len(data)} 条：{path}")

    def export_sheets(parent, sheets: Dict[str, Any], default_name: str = "导出") -> Optional[str]:
        path = theme.pick_path(
            parent, "导出", mode="save", default_name=default_name,
            filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
        if not path:
            return None
        return excelio.export_workbook(path, sheets)

    # ── 表单字段定义 ────────────────────────────────────────

    def source_combo(placeholder: str = "（未填 · 可下拉选择或直接输入）") -> QComboBox:
        """v2.9 订单来源：预设四值 + 自由输入。取值唯一来源 ofdefs.ORDER_SOURCES"""
        box = QComboBox()
        box.setEditable(True)
        box.setInsertPolicy(QComboBox.NoInsert)
        box.addItem(placeholder, "")
        for src in ofdefs.ORDER_SOURCES:
            box.addItem(src, src)
        box.setMinimumWidth(210)
        box.setToolTip("预设：" + " / ".join(ofdefs.ORDER_SOURCES) +
                       "\n新渠道可直接输入任意文本，按输入值原样落库。")
        return box

    def source_text(box: QComboBox) -> str:
        """可编辑下拉取值：条目未被改写时用其 data，手输内容用原文。

        注意 Qt 的 setEditText() 不会把 currentIndex 置 -1，所以不能只看 index：
        必须比对「当前文本」与「当前条目文本」，不一致才认定是手输。
        占位项 data = '' → 返回空串，避免把提示文字当成来源存库。
        """
        idx = box.currentIndex()
        text = box.currentText().strip()
        if idx >= 0 and (not text or text == box.itemText(idx).strip()):
            data = box.itemData(idx)
            return "" if data is None else str(data).strip()
        return text

    def bid_project_fields() -> List[Dict[str, Any]]:
        return [
            {"key": "project_name", "label": "招投标项目名称", "required": True},
            {"key": "customer", "label": "客户名称", "type": "customer"},
            {"key": "tender_no", "label": "招标编号"},
            {"key": "bid_date", "label": "投标日期", "type": "date",
             "default": date.today().isoformat()},
            {"key": "owner", "label": "负责人", "default": "张哥"},
            {"key": "remark", "label": "备注", "type": "textarea"},
        ]

    def bid_item_fields() -> List[Dict[str, Any]]:
        return [
            {"key": "product_name", "label": "产品名称", "required": True},
            {"key": "spec", "label": "规格"},
            {"key": "category", "label": "类别"},
            {"key": "unit", "label": "单位", "default": "件"},
            {"key": "qty", "label": "数量(实值)", "type": "float", "default": 1,
             "decimals": 3, "minimum": 0.001},
            {"key": "purchase_price", "label": "采购单价 ¥", "type": "float", "default": 0},
            {"key": "quote_ratio", "label": "报价比率", "type": "float", "default": 0.85,
             "decimals": 4, "minimum": 0.0001},
            {"key": "remark", "label": "备注", "type": "textarea"},
        ]

    def code_project_fields() -> List[Dict[str, Any]]:
        return [
            {"key": "project_name", "label": "询配码项目名称", "required": True},
            {"key": "customer", "label": "客户名称", "type": "customer"},
            {"key": "file_path", "label": "询配码文件路径", "type": "path",
             "dialog_title": "选择询配码文件",
             "filters": "Excel / Word / PDF (*.xlsx *.xls *.xlsm *.csv "
                        "*.docx *.doc *.pdf);;所有文件 (*.*)"},
            {"key": "version", "label": "版本", "default": "V1"},
            {"key": "owner", "label": "负责人", "default": "张哥"},
            {"key": "remark", "label": "备注", "type": "textarea"},
        ]

    def code_item_fields() -> List[Dict[str, Any]]:
        """10 个业务字段严格取自 core/fields.py，顺序与导入模板/导出文件一致"""
        return fdefs.form_fields()

    # v2.4：原 production_project_fields()（新建排产项目表单）已随「新建排产项目」
    # 操作一并移除；生产项目改由 OrderScheduleService.ensure() / 中标一键排产自动创建。

    # ── 新建订单（2026-11-06 v2.3：订单头 8 字段 + 明细 9 字段）──

    class NewOrderDialog(ThemedDialog):
        """
        v3.5.3 两段式建单（对齐询配码项目范式）：本窗口**只建订单头**，
        明细改由订单管理里选中该单后的「录入订单明细 / 导入订单明细」补录。

        为不打断老习惯，下方保留了可勾选的「同时录入订单明细」折叠区（默认关闭）；
        勾选后仍可像 v2.3 那样建单时一次录入明细。

        订单头：订单项目编号 / 客户名称 / 订单类型 / 订单状态 /
                价税合计 / 交货日期 / 物资数量（= 物资项目数）/ ERP订单编号
                + 订单来源（v2.9：预设四值 + 可手输）
        明细：  物资代码 / 物资名称 / 实际生产物资名称 / 规格 / 单位 /
                数量 / 单价 / 总价 / 参数描述

        ★ 「数量」必须显式 > 0，是排产唯一数量锚点，绝不取自询配码虚值。
        ★ 总价/价税合计留空自动汇总；「物资数量」= 物资项目数（明细行数，
          Q12 2026-11-07 确认），留空自动取明细行数，填了则原样保留；
          「订单状态」直接指定，确认后才可排产。
        ★ 下方「系统溯源（可选）」是链路锚点，不属于 8 个业务字段。
        """
        def __init__(self, parent, db):
            super().__init__(
                parent, "新建订单",
                "先建订单头；创建后在订单管理里选中该单，用「录入订单明细 / 导入订单明细」补明细",
                width=1180, height=730)
            self.db = db
            self._spec: Optional[Dict[str, Any]] = None
            self.columns = ofdefs.form_item_columns()
            v = self.body_layout

            title_head = QLabel("订单头 · 8 字段 + 订单来源")
            title_head.setObjectName("SectionTitle")
            v.addWidget(title_head)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(8)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)

            self.edit_project_no = QLineEdit()
            self.edit_project_no.setPlaceholderText("如 CG20261106000001（导入时作合并键）")
            # v3.4.2：客户名称 = 从订单/项目台账汇总历史客户 + 关键字检索 + 可手输新客户
            self.edit_customer = CustomerComboBox(parent=self, db=db)
            # v2.9：订单来源 = 可编辑下拉（四个预设 + 自由输入）
            self.combo_source = source_combo()
            self.combo_type = QComboBox()
            self.combo_type.addItem("外部订单（用户下单 / 手工订单）", "外部订单")
            self.combo_type.addItem("内部订单（中标项目生成）", "内部订单")
            self.combo_type.currentIndexChanged.connect(self._on_type)
            self.combo_status = QComboBox()
            for s in models.OrderStatus.CREATABLE:
                self.combo_status.addItem(s, s)
            idx = self.combo_status.findData(models.OrderStatus.CONFIRMED)
            if idx >= 0:
                self.combo_status.setCurrentIndex(idx)
            self.edit_amount_total = QLineEdit()
            self.edit_amount_total.setPlaceholderText("留空 = Σ 明细总价")
            self.edit_delivery = DatePicker()
            self.edit_delivery.setPlaceholderText("点击选择交货日期（可留空）")
            self.edit_qty_total = QLineEdit()
            self.edit_qty_total.setPlaceholderText("留空 = 明细项数")
            self.edit_qty_total.setToolTip(
                "Q12 口径：物资数量 = 物资项目数（明细行数），不是各明细数量之和；"
                "明细数量合计请看订单结果表的「实值合计」列。")
            self.edit_erp = QLineEdit()
            self.edit_erp.setPlaceholderText("ERP 系统订单编号")

            head_widgets = [
                ("订单项目编号", self.edit_project_no),
                ("客户名称", self.edit_customer),
                ("订单来源", self.combo_source),
                ("订单类型 *", self.combo_type),
                ("订单状态", self.combo_status),
                ("价税合计", self.edit_amount_total),
                ("交货日期", self.edit_delivery),
                ("物资数量", self.edit_qty_total),
                ("ERP订单编号", self.edit_erp),
            ]
            for i, (cap, w) in enumerate(head_widgets):
                row, col = divmod(i, 2)
                grid.addWidget(QLabel(cap), row, col * 2)
                grid.addWidget(w, row, col * 2 + 1)
            v.addLayout(grid)

            title_trace = QLabel("系统溯源与兼容字段（可选 · 非业务字段）")
            title_trace.setObjectName("SectionTitle")
            v.addWidget(title_trace)
            trace = QGridLayout()
            trace.setHorizontalSpacing(12)
            trace.setVerticalSpacing(6)
            trace.setColumnStretch(1, 1)
            trace.setColumnStretch(3, 1)

            self.combo_code = QComboBox()
            self.combo_code.addItem("（无 · 手工订单，不关联询配码）", None)
            for r in db.query(
                    "SELECT * FROM code_projects WHERE status IN "
                    "('已提交','部分下单','已下单','关闭') ORDER BY id"):
                self.combo_code.addItem(
                    f"{r['doc_no']}  {r['project_name']}  [{r['status']}]", r["id"])
            self.combo_bid = QComboBox()
            for r in db.query("SELECT * FROM bid_projects WHERE status = '中标' ORDER BY id"):
                if db.scalar(
                        "SELECT COUNT(*) FROM orders WHERE source_bid_project_id = ? "
                        "AND order_type = '内部订单'", (r["id"],), default=0):
                    continue                 # 一个中标项目只能有一张内部订单
                self.combo_bid.addItem(f"{r['doc_no']}  {r['project_name']}", r["id"])
            if not self.combo_bid.count():
                self.combo_bid.addItem("（暂无可生成内部订单的中标项目）", None)
            self.edit_date = DatePicker(value=date.today().isoformat())
            self.edit_owner = QLineEdit("张哥")
            self.edit_remark = QLineEdit()

            self.label_code = QLabel("来源询配码项目")
            self.label_bid = QLabel("来源中标项目 *")
            trace.addWidget(self.label_code, 0, 0)
            trace.addWidget(self.combo_code, 0, 1)
            trace.addWidget(self.label_bid, 0, 2)
            trace.addWidget(self.combo_bid, 0, 3)
            trace.addWidget(QLabel("下单日期"), 1, 0)
            trace.addWidget(self.edit_date, 1, 1)
            trace.addWidget(QLabel("负责人"), 1, 2)
            trace.addWidget(self.edit_owner, 1, 3)
            trace.addWidget(QLabel("订单备注"), 2, 0)
            trace.addWidget(self.edit_remark, 2, 1, 1, 3)
            v.addLayout(trace)

            self.hint = theme.label(
                "明细 9 字段：数量是实值（>0），总价留空自动 = 数量×单价；"
                "订单头价税合计留空 = Σ明细总价；物资数量 = 物资项目数，"
                "留空自动 = 明细行数（Q12 口径，非数量合计）。",
                "DialogHint", word_wrap=True)
            v.addWidget(self.hint)
            v.addWidget(theme.label(
                "【两段式】本窗口先创建订单头 → 回到订单管理选中该单 → 点"
                "「录入订单明细」逐条填，或用「导入订单明细」从 CSV / Excel 一次导入。"
                " 明细不再必填，也不会因为文件格式不对导致建单失败。",
                "DialogHint", word_wrap=True))

            # v3.5.3：明细区默认收起，老习惯仍可勾选后一次录入
            self.items_box = QGroupBox("同时录入订单明细（可选 · 默认收起）")
            self.items_box.setCheckable(True)
            self.items_box.setChecked(False)
            self.items_box.setToolTip(
                "不勾选 = 只建订单头；创建后在订单管理里用两个独立动作补明细")
            outer = QVBoxLayout(self.items_box)
            outer.setContentsMargins(10, 10, 10, 10)
            self.items_content = QWidget()
            iv = QVBoxLayout(self.items_content)
            iv.setContentsMargins(0, 0, 0, 0)
            iv.setSpacing(5)

            title_items = QLabel("订单明细 · 9 字段（双击单元格直接改）")
            title_items.setObjectName("SectionTitle")
            iv.addWidget(title_items)
            self.table = theme.style_table(QTableWidget())
            self.table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                QAbstractItemView.AnyKeyPressed)
            self.table.setColumnCount(len(self.columns))
            self.table.setHorizontalHeaderLabels([c["label"] for c in self.columns])
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            for i, c in enumerate(self.columns):
                if i != 1:
                    self.table.setColumnWidth(i, c["width"])
            iv.addWidget(self.table, 1)
            for _ in range(3):
                self._append_row()

            btn_row = QHBoxLayout()
            btn_add = QPushButton("＋ 添加明细行")
            btn_add.clicked.connect(self._append_row)
            btn_del = QPushButton("删除选中行")
            btn_del.clicked.connect(self._remove_row)
            btn_row.addWidget(btn_add)
            btn_row.addWidget(btn_del)
            btn_row.addStretch(1)
            iv.addLayout(btn_row)
            outer.addWidget(self.items_content)
            self.items_box.toggled.connect(self.items_content.setVisible)
            self.items_content.setVisible(False)
            v.addWidget(self.items_box, 1)

            self.add_buttons(ok_text="创建订单", on_ok=self._validate)
            self._on_type()

        def _append_row(self, _=None):
            r = self.table.rowCount()
            self.table.insertRow(r)
            # 全空行：只有用户填过内容的行才会被收集，避免预填值误判为有效明细
            for c in range(len(self.columns)):
                self.table.setItem(r, c, QTableWidgetItem(""))

        def _remove_row(self):
            r = self.table.currentRow()
            if r >= 0:
                self.table.removeRow(r)

        def _on_type(self, *_):
            internal = self.combo_type.currentData() == "内部订单"
            self.label_code.setVisible(not internal)
            self.combo_code.setVisible(not internal)
            self.label_bid.setVisible(internal)
            self.combo_bid.setVisible(internal)
            self.hint.setText(
                "内部订单由中标项目生成，一个中标项目仅一张；物资代码可与中标明细匹配。"
                "单位留空=件，单价/总价留空自动计算；物资数量=物资项数（Q12）。" if internal else
                "外部订单可关联询配码项目（一码多单），也可完全手工新建；"
                "物资代码可自动匹配来源询配码项。单位留空=件，单价/总价留空自动计算；"
                "物资数量=物资项数，留空自动=明细行数（Q12）。")

        def _text(self, row: int, col: int) -> str:
            item = self.table.item(row, col)
            return item.text().strip() if item else ""

        @staticmethod
        def _num(text: str) -> float:
            try:
                return float(text.replace(",", "").replace("¥", "") or 0)
            except ValueError:
                return 0.0

        def _source_value(self) -> str:
            """订单来源取值（预设命中取 data，手输取原文）"""
            return source_text(self.combo_source)

        def _collect_items(self):
            """9 列表格 → 明细字典；列序严格取自 ofdefs.form_item_columns()"""
            items, errors = [], []
            for r in range(self.table.rowCount()):
                cells = [self._text(r, c) for c in range(len(self.columns))]
                if not any(cells):          # 全空行跳过（初始 3 行是空白待录行）
                    continue
                qty, price = self._num(cells[5]), self._num(cells[6])
                if not cells[1]:
                    errors.append(f"第 {r + 1} 行：物资名称不能为空")
                if qty <= 0:
                    errors.append(f"第 {r + 1} 行：数量(实值)必须 > 0")
                items.append({
                    "item_code": cells[0],
                    "item_name": cells[1],
                    "production_name": cells[2],
                    "spec": cells[3],
                    "unit": cells[4] or "件",
                    "qty_ordered": qty,
                    "unit_price": price,
                    "amount": self._num(cells[7]) if cells[7] else None,
                    "param_desc": cells[8],
                })
            return items, errors

        def _validate(self):
            # v3.5.3：只有展开了「同时录入明细」才校验明细；默认只建订单头，
            # 明细由订单管理的两个独立动作补录，绝不因 0 明细拦住建单。
            if self.items_box.isChecked():
                items, errors = self._collect_items()
                if errors:
                    theme.notify(self, "明细填写有误", "\n".join(errors[:8]) +
                                 ("\n……" if len(errors) > 8 else ""), kind="warn")
                    return
                if not items:
                    theme.notify(self, "缺少明细", "已展开明细区，请至少录入一行"
                                 "（或取消勾选，创建后再用「录入订单明细」补录）",
                                 kind="warn")
                    return
            if self.combo_type.currentData() == "内部订单" and \
                    self.combo_bid.currentData() is None:
                theme.notify(self, "缺少中标项目", "内部订单必须选择来源中标项目；"
                             "也可改用「① 询报价 · 生成内部订单」", kind="warn")
                return
            self._spec = self.result_spec()
            self.accept()

        def result_spec(self) -> Optional[Dict[str, Any]]:
            if self._spec is not None:
                return self._spec
            internal = self.combo_type.currentData() == "内部订单"
            # 明细区收起时一律返回空明细：创建订单头，后续由独立动作补录
            items = (self._collect_items()[0] if self.items_box.isChecked()
                     else [])
            return {
                # 订单头 8 字段
                "project_no": self.edit_project_no.text().strip(),
                "customer": self.edit_customer.text().strip(),
                "order_source": self._source_value(),
                "order_type": "内部订单" if internal else "外部订单",
                "status": self.combo_status.currentData(),
                "amount_total": self.edit_amount_total.text().strip(),
                "required_date": self.edit_delivery.text().strip(),
                "item_qty_total": self.edit_qty_total.text().strip(),
                "erp_order_no": self.edit_erp.text().strip(),
                # 系统溯源 / 兼容字段
                "order_date": self.edit_date.text().strip(),
                "owner": self.edit_owner.text().strip(),
                "remark": self.edit_remark.text().strip(),
                "source_code_project_id": None if internal
                                          else self.combo_code.currentData(),
                "source_bid_project_id": self.combo_bid.currentData() if internal
                                         else None,
                "items": items,
            }

    # ── 订单编辑（v2.9.2：更改 + 删除） ──────────────────────

    class OrderEditDialog(ThemedDialog):
        """
        选中订单的专用编辑器：订单头可改、明细可直接在表格里改、可增删明细行。

        · 「保存修改」= 一次性提交变更集 → 服务层落库 + 写审计 → 列表自动刷新；
        · 「删除订单」= 同窗口完成整单删除（有排产关联时先拦住并告知谁在用）；
        · 订单类型 / 状态 / 系统单号是链路锚点，只读展示，改状态请走「确认 / 状态流转」。
        """
        NUM_KEYS = ("qty_ordered", "unit_price", "amount")

        def __init__(self, parent, db, order_id: int):
            o = db.get_by_id("orders", order_id)
            if not o:
                raise ValueError(f"订单 {order_id} 不存在")
            super().__init__(
                parent, f"编辑订单 · {o['doc_no']}",
                "改完点「保存修改」即落库并自动刷新列表；最后一列显示已排量，"
                "数量不能改到低于已排量",
                width=1260, height=770)
            self.db = db
            self.order_id = int(order_id)
            self.order = dict(o)
            self.orig = {int(r["id"]): dict(r) for r in db.query(
                "SELECT * FROM order_items WHERE order_id = ? ORDER BY line_no",
                (order_id,))}
            self.columns = ofdefs.form_item_columns()
            self.delete_requested = False
            self.head_changes: Dict[str, Any] = {}
            self.item_updates: List[Any] = []
            self.new_items: List[Dict[str, Any]] = []
            self.deleted_item_ids: List[int] = []
            v = self.body_layout

            # ── 只读身份行 ──
            ident = QLabel(
                f"系统单号 {o['doc_no']}　|　订单类型 {o['order_type']}　|　"
                f"当前状态 {o['status']}　|　明细 {len(self.orig)} 行")
            ident.setObjectName("Hint")
            v.addWidget(ident)

            title_head = QLabel("订单头 · 可改字段")
            title_head.setObjectName("SectionTitle")
            v.addWidget(title_head)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(8)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)

            self.edit_project_no = QLineEdit(str(o.get("project_no") or ""))
            # v3.4.2：客户名称同样支持历史候选与关键字检索
            self.edit_customer = CustomerComboBox(
                parent=self, db=self.db, value=str(o.get("customer") or ""))
            self.combo_source = source_combo()
            _src = str(o.get("order_source") or "")
            _idx = self.combo_source.findData(_src)
            if _idx >= 0:
                self.combo_source.setCurrentIndex(_idx)
            elif _src:
                self.combo_source.setCurrentText(_src)
            self.edit_amount = QLineEdit(
                "" if o.get("amount_total") is None
                else f"{float(o['amount_total']):.2f}")
            self.edit_amount.setPlaceholderText("留空 = 自动 Σ明细总价")
            self.edit_delivery = DatePicker(value=str(o.get("required_date") or ""))
            self.edit_delivery.setPlaceholderText("点击选择交货日期（可留空）")
            self.edit_qty = QLineEdit(
                "" if o.get("item_qty_total") is None
                else f"{float(o['item_qty_total']):g}")
            self.edit_qty.setPlaceholderText("留空 = 明细行数")
            self.edit_erp = QLineEdit(str(o.get("erp_order_no") or ""))
            self.edit_date = DatePicker(value=str(o.get("order_date") or ""))
            self.edit_owner = QLineEdit(str(o.get("owner") or ""))
            self.edit_remark = QLineEdit(str(o.get("remark") or ""))

            head_widgets = [
                ("订单项目编号", self.edit_project_no),
                ("客户名称", self.edit_customer),
                ("订单来源", self.combo_source),
                ("价税合计", self.edit_amount),
                ("交货日期", self.edit_delivery),
                ("物资数量(项)", self.edit_qty),
                ("ERP订单编号", self.edit_erp),
                ("下单日期", self.edit_date),
                ("负责人", self.edit_owner),
                ("备注", self.edit_remark),
            ]
            for i, (cap, w) in enumerate(head_widgets):
                row, col = divmod(i, 2)
                grid.addWidget(QLabel(cap), row, col * 2)
                grid.addWidget(w, row, col * 2 + 1)
            v.addLayout(grid)

            title_items = QLabel("订单明细 · 双击单元格直接改（数量 / 单价 / 总价 留空自动算）")
            title_items.setObjectName("SectionTitle")
            v.addWidget(title_items)
            self.table = theme.style_table(QTableWidget())
            self.table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                QAbstractItemView.AnyKeyPressed)
            headers = ["行"] + [c["label"] for c in self.columns] + ["已排量", "可排量"]
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
            widths = [44] + [c["width"] for c in self.columns] + [70, 70]
            for i, w in enumerate(widths):
                if i != 3:
                    self.table.setColumnWidth(i, w)
            v.addWidget(self.table, 1)
            self._load_rows()

            btn_add = QPushButton("＋ 添加明细行")
            btn_add.clicked.connect(lambda: self._append_row())
            btn_del = QPushButton("删除选中行")
            btn_del.clicked.connect(self._remove_row)
            btn_kill = QPushButton("删除订单")
            btn_kill.setToolTip("整单删除（含全部明细）；已有排产关联时会被拦下")
            btn_kill.setStyleSheet(f"color:{theme.DANGER};font-weight:600;")
            btn_kill.clicked.connect(self._ask_delete)

            self.add_buttons(ok_text="保存修改", on_ok=self._collect)
            self.footer_layout.insertWidget(0, btn_add)
            self.footer_layout.insertWidget(1, btn_del)
            self.footer_layout.insertWidget(2, btn_kill)

        # ── 明细表 ───────────────────────────────────────
        def _load_rows(self):
            for item in self.orig.values():
                self._append_row(item)

        def _append_row(self, item: Optional[Dict[str, Any]] = None):
            r = self.table.rowCount()
            self.table.insertRow(r)
            iid = int(item["id"]) if item else None
            no_item = QTableWidgetItem(str(item["line_no"]) if item else "新")
            no_item.setData(Qt.UserRole, iid)
            no_item.setFlags(no_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, no_item)
            for c, col in enumerate(self.columns, start=1):
                key = col["key"]
                value = item.get(key) if item else ""
                if key in self.NUM_KEYS and value not in (None, ""):
                    text = f"{float(value):.4f}" if key == "unit_price" else f"{float(value):g}"
                else:
                    text = "" if value is None else str(value)
                self.table.setItem(r, c, QTableWidgetItem(text))
            # 已排量 / 可排量：只读参考列
            allocated = self._allocated(iid) if iid else 0.0
            qty = float(item["qty_ordered"]) if item else 0.0
            for c, text in ((len(self.columns) + 1, f"{allocated:g}"),
                            (len(self.columns) + 2, f"{qty - allocated:g}")):
                ro = QTableWidgetItem(text)
                ro.setFlags(ro.flags() & ~Qt.ItemIsEditable)
                ro.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, ro)

        def _allocated(self, item_id: int) -> float:
            return float(self.db.scalar(
                "SELECT COALESCE(SUM(allocated_qty), 0) FROM production_links "
                "WHERE order_item_id = ?", (item_id,), default=0.0))

        def _remove_row(self):
            r = self.table.currentRow()
            if r < 0:
                return
            self.table.removeRow(r)

        @staticmethod
        def _cell(table, r: int, c: int) -> str:
            item = table.item(r, c)
            return item.text().strip() if item else ""

        def _row_id(self, r: int) -> Optional[int]:
            item = self.table.item(r, 0)
            value = item.data(Qt.UserRole) if item else None
            return int(value) if value else None

        # ── 变更集 ───────────────────────────────────────
        def _diff_head(self) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            for key, widget, original in (
                    ("project_no", self.edit_project_no, self.order.get("project_no")),
                    ("customer", self.edit_customer, self.order.get("customer")),
                    ("required_date", self.edit_delivery, self.order.get("required_date")),
                    ("erp_order_no", self.edit_erp, self.order.get("erp_order_no")),
                    ("order_date", self.edit_date, self.order.get("order_date")),
                    ("owner", self.edit_owner, self.order.get("owner")),
                    ("remark", self.edit_remark, self.order.get("remark"))):
                new = widget.text().strip()
                if new != str(original or "").strip():
                    out[key] = new
            new_source = source_text(self.combo_source)
            if new_source != str(self.order.get("order_source") or ""):
                out["order_source"] = new_source
            for key, widget in (("amount_total", self.edit_amount),
                                ("item_qty_total", self.edit_qty)):
                new = widget.text().strip()
                old = self.order.get(key)
                if new == "":
                    if old not in (None, ""):        # 清空 = 解除手工锁 → 自动汇总
                        out[key] = ""
                    continue
                try:
                    if abs(float(new) - float(old or 0)) > 1e-9:
                        out[key] = new
                except ValueError:
                    out[key] = new                   # 交给服务层报「必须是数字」
            return out

        def _diff_items(self):
            updates, news, errors = [], [], []
            seen = set()
            for r in range(self.table.rowCount()):
                cells = [self._cell(self.table, r, c)
                         for c in range(1, len(self.columns) + 1)]
                if not any(cells):
                    continue                          # 整行空 = 忽略
                iid = self._row_id(r)
                name = cells[1]
                try:
                    qty = float(cells[5] or 0)
                except ValueError:
                    qty = 0.0
                try:
                    price = float(cells[6] or 0)
                except ValueError:
                    errors.append(f"第 {r + 1} 行：单价必须是数字")
                    price = 0.0
                if not name:
                    errors.append(f"第 {r + 1} 行：物资名称不能为空")
                if qty <= 0:
                    errors.append(f"第 {r + 1} 行：数量必须 > 0")
                data = {"item_code": cells[0], "item_name": name,
                        "production_name": cells[2], "spec": cells[3],
                        "unit": cells[4] or "件", "qty_ordered": qty,
                        "unit_price": price,
                        "amount": cells[7] or None, "param_desc": cells[8]}
                if iid is None:
                    news.append(data)
                    continue
                seen.add(iid)
                old = self.orig.get(iid) or {}
                changes = {}
                for key, value in data.items():
                    if key in self.NUM_KEYS:
                        old_v = old.get(key)
                        if value in (None, "") or old_v in (None, ""):
                            if (value in (None, "")) != (old_v in (None, "")):
                                changes[key] = value
                            continue
                        if abs(float(value) - float(old_v)) > 1e-9:
                            changes[key] = value
                    elif str(value or "") != str(old.get(key) or ""):
                        changes[key] = value
                if changes:
                    updates.append((iid, changes))
            deleted = [iid for iid in self.orig if iid not in seen]
            return updates, news, deleted, errors

        def _collect(self):
            head = self._diff_head()
            updates, news, deleted, errors = self._diff_items()
            if errors:
                theme.notify(self, "明细填写有误",
                             "\n".join(errors[:8]) + ("\n……" if len(errors) > 8 else ""),
                             kind="warn")
                return
            if not (head or updates or news or deleted):
                theme.notify(self, "没有改动",
                             "未检测到任何修改；如需退出请点「取消」。", kind="info")
                return
            self.head_changes = head
            self.item_updates = updates
            self.new_items = news
            self.deleted_item_ids = deleted
            self.accept()

        def result_changes(self):
            return (self.head_changes, self.item_updates,
                    self.new_items, self.deleted_item_ids)

        # ── 删除整单 ─────────────────────────────────────
        def _ask_delete(self):
            from ..domains.maintenance import MaintenanceService
            maint = MaintenanceService(self.db)
            refs = maint.references("order", self.order_id)
            if refs:
                detail = "\n".join(
                    f"· {r['label']} {r['count']} 条：{'、'.join(r['samples'])}"
                    for r in refs)
                theme.notify(self, "不能删除 · 已被下游引用",
                             "该订单已有排产关联，删除会造成链路断裂。\n"
                             "请先在 ④ 生产排产 里解除关联，再回来删除。",
                             kind="warn", detail=detail)
                return
            n = int(self.db.scalar(
                "SELECT COUNT(*) FROM order_items WHERE order_id = ?",
                (self.order_id,), default=0))
            if not theme.confirm(
                    self, "确认删除订单",
                    f"确定删除订单 {self.order['doc_no']} 吗？此操作不可撤销。",
                    detail=f"将一并删除 {n} 行订单明细；\n"
                           f"删除前后的完整快照会写入审计留痕，可追溯。"):
                return
            self.delete_requested = True
            self.accept()

    # ── 导入订单（CSV / Excel） ─────────────────────────────

    # ── v3.5.3 两段式建单：选中订单后录入 / 导入明细 ──────────

    class AddOrderItemsDialog(ThemedDialog):
        """选中订单 → 9 字段可编辑表格，一次录入多行后统一保存。

        与「新建订单」彻底解耦：目标订单已存在，录入失败不影响订单头。
        """
        def __init__(self, parent, db, order_id: int):
            o = db.get_by_id("orders", order_id)
            if not o:
                raise ValueError(f"订单 {order_id} 不存在")
            super().__init__(
                parent, f"录入订单明细 · {o['doc_no']}",
                "9 字段表格可一次录多行；数量(实值)必须 > 0，总价留空自动 = 数量×单价",
                width=1180, height=620)
            self.db = db
            self.order_id = int(order_id)
            self.result: Optional[Dict[str, Any]] = None
            self.columns = ofdefs.form_item_columns()
            v = self.body_layout
            existing = int(db.scalar(
                "SELECT COUNT(*) FROM order_items WHERE order_id = ?",
                (order_id,), default=0))
            v.addWidget(theme.label(
                f"目标订单 {o['doc_no']}　|　客户名称 {o['customer'] or '—'}　|　"
                f"已有明细 {existing} 行　|　本次追加的行按现有行号续编",
                "DialogHint", word_wrap=True))
            v.addWidget(theme.label(
                "只需填「物资名称」与「数量」两列即可保存；其余列留空按默认值"
                "（单位=件、单价=0、总价自动算）。全空行自动忽略。",
                "DialogHint", word_wrap=True))

            self.table = theme.style_table(QTableWidget())
            self.table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                QAbstractItemView.AnyKeyPressed)
            self.table.setColumnCount(len(self.columns))
            self.table.setHorizontalHeaderLabels([c["label"] for c in self.columns])
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            for i, c in enumerate(self.columns):
                if i != 1:
                    self.table.setColumnWidth(i, c["width"])
            v.addWidget(self.table, 1)
            for _ in range(3):
                self._append_row()

            btn_add = QPushButton("＋ 添加明细行")
            btn_add.clicked.connect(self._append_row)
            btn_del = QPushButton("删除选中行")
            btn_del.clicked.connect(self._remove_row)
            self.footer_layout.insertWidget(0, btn_add)
            self.footer_layout.insertWidget(1, btn_del)
            self.add_buttons(ok_text="保存明细", on_ok=self._do_save)

        def _append_row(self, _=None):
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c in range(len(self.columns)):
                self.table.setItem(r, c, QTableWidgetItem(""))

        def _remove_row(self):
            r = self.table.currentRow()
            if r >= 0:
                self.table.removeRow(r)

        def _text(self, row: int, col: int) -> str:
            item = self.table.item(row, col)
            return item.text().strip() if item else ""

        @staticmethod
        def _num(text: str) -> float:
            try:
                return float(str(text).replace(",", "").replace("¥", "") or 0)
            except ValueError:
                return 0.0

        def _specs(self):
            specs, errors = [], []
            for r in range(self.table.rowCount()):
                cells = [self._text(r, c) for c in range(len(self.columns))]
                if not any(cells):
                    continue
                qty, price = self._num(cells[5]), self._num(cells[6])
                if not cells[1]:
                    errors.append(f"第 {r + 1} 行：物资名称不能为空")
                if qty <= 0:
                    errors.append(f"第 {r + 1} 行：数量(实值)必须 > 0")
                specs.append({
                    "item_code": cells[0], "item_name": cells[1],
                    "production_name": cells[2], "spec": cells[3],
                    "unit": cells[4] or "件", "qty_ordered": qty,
                    "unit_price": price,
                    "amount": self._num(cells[7]) if cells[7] else None,
                    "param_desc": cells[8],
                })
            return specs, errors

        def _do_save(self):
            specs, errors = self._specs()
            if errors:
                theme.notify(self, "明细填写有误", "\n".join(errors[:8]) +
                             ("\n……" if len(errors) > 8 else ""), kind="warn")
                return
            if not specs:
                theme.notify(self, "没有明细", "请至少录入一行（物资名称 + 数量），"
                            "或改用「导入订单明细」从 CSV / Excel 一次导入", kind="warn")
                return
            from ..domains import OrderService
            try:
                self.result = OrderService(self.db).add_items_bulk(
                    self.order_id, specs, operator=cur.name(),
                    origin="add_order_items")
            except Exception as exc:
                theme.notify(self, "保存失败", str(exc), kind="error")
                return
            self.accept()

    class ImportOrderItemsDialog(ThemedDialog):
        """选中订单 → CSV / Excel 明细表 → 9 列预览 → 逐行报告 → 单事务导入。

        ★ 只解析明细 9 列、不分组、不建订单头；表头不在首行也能自动定位。
        ★ 旧的「导入订单」是 17 列整单模板，缺订单头列时会把一行拆成一张订单；
          本向导从设计上根除该问题（2026-11-19 实测复现）。
        """
        def __init__(self, parent, db, order_id=None):
            super().__init__(
                parent, "导入订单明细 · CSV / Excel",
                "目标订单已选好；只需「物资名称 + 数量」两列，其余 7 列可缺；"
                "表头前有标题行也能自动识别",
                width=1120, height=620)
            self.db = db
            self.report: Optional[Dict[str, Any]] = None
            self.result: Optional[Dict[str, Any]] = None

            top = QHBoxLayout()
            top.addWidget(QLabel("目标订单："))
            self.combo = QComboBox()
            for r in db.query("SELECT * FROM orders ORDER BY id DESC"):
                caption = (f"{r['doc_no']}  {r['project_no'] or '（无编号）'}  "
                           f"{r['customer'] or ''}")
                self.combo.addItem(caption, r["id"])
            if order_id is not None:
                idx = self.combo.findData(order_id)
                if idx >= 0:
                    self.combo.setCurrentIndex(idx)
            top.addWidget(self.combo, 1)
            btn_file = QPushButton("选择文件…")
            btn_file.setObjectName("Primary")
            btn_file.clicked.connect(self._choose_file)
            top.addWidget(btn_file)
            btn_tpl = QPushButton("下载明细导入模板")
            btn_tpl.clicked.connect(self._download_template)
            top.addWidget(btn_tpl)
            self.body_layout.addLayout(top)

            self.path_label = theme.label("尚未选择文件", "DialogHint")
            self.body_layout.addWidget(self.path_label)
            self.summary = theme.label(
                "有效行 0 / 跳过 0 / 提示 0 · 表头行 1", "DialogCount")
            self.body_layout.addWidget(self.summary)
            self.table = theme.style_table(QTableWidget())
            self.table.setObjectName("ImportPreview")
            self._set_preview([], [])
            self.body_layout.addWidget(self.table, 1)
            self.issues = QTextEdit()
            self.issues.setReadOnly(True)
            self.issues.setPlainText("导入报告：选择文件后显示逐行校验结果。")
            self.issues.setMaximumHeight(96)
            self.body_layout.addWidget(self.issues)

            self._box = self.add_buttons(ok_text="导入 0 行", cancel_text="取消",
                                         on_ok=self._do_import)
            self._ok_btn = next((b for b in self._box.buttons()
                                 if b.objectName() == "Primary"), None)
            if self._ok_btn:
                self._ok_btn.setEnabled(False)
            if not self.combo.count():
                self.path_label.setText("请先「新增订单」创建订单头，再导入明细")

        def _set_preview(self, headers, rows):
            self.table.setColumnCount(len(headers) or 1)
            self.table.setHorizontalHeaderLabels(headers or ["（等待选择文件）"])
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    self.table.setItem(i, j, QTableWidgetItem(
                        "" if value is None else str(value)))
            if headers:
                self.table.resizeColumnsToContents()
                for c in range(self.table.columnCount()):
                    if self.table.columnWidth(c) > 168:
                        self.table.setColumnWidth(c, 168)
                self.table.horizontalHeader().setSectionResizeMode(
                    min(1, len(headers) - 1), QHeaderView.Stretch)

        def _choose_file(self):
            path = theme.pick_path(
                self, "选择订单明细文件", mode="open",
                filters="Excel / CSV（*.xlsx *.csv *.tsv）;;Excel 工作簿 (*.xlsx);;"
                        "CSV / TSV (*.csv *.tsv);;所有文件 (*.*)")
            if path:
                self._load(path)

        def _download_template(self):
            path = theme.pick_path(
                self, "另存订单明细导入模板", mode="save",
                default_name="订单明细导入模板.xlsx",
                filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
            if path:
                excelio.export_template(path, "order_item")
                theme.notify(self, "模板已生成",
                             f"已保存：{path}\n\n9 列均为订单明细字段；"
                             "只填「物资名称」「数量」也能导入，其余留空按默认值。")

        def _load(self, path: str):
            try:
                rep = excelio.parse_order_item_file(path)
            except Exception as exc:
                theme.notify(self, "读取失败", str(exc), kind="error")
                return
            self.report = rep
            self.path_label.setText(f"文件：{path}")
            fatal = sum(1 for i in rep["issues"] if i.get("fatal"))
            warns = len(rep["issues"]) - fatal
            self.summary.setText(
                f"有效行 {rep['item_count']} / 跳过 {fatal} / 提示 {warns} · "
                f"表头行 {rep['header_row']}；未识别列 {len(rep['ignored'])}")
            self._set_preview(ofdefs.item_headers(),
                              ofdefs.item_preview_rows(rep["items"][:50]))
            lines = [f"第 {i['row']} 行 [{'跳过' if i.get('fatal') else '提示'}] "
                     f"{i['reason']}" for i in rep["issues"]]
            if rep["ignored"]:
                lines.append("未识别列（已忽略）：" + "、".join(rep["ignored"]))
            if rep["item_count"] > 50:
                lines.append(f"预览仅显示前 50 行，实际将导入 {rep['item_count']} 行")
            self.issues.setPlainText("\n".join(lines) if lines
                                     else "全部数据行校验通过")
            if self._ok_btn:
                self._ok_btn.setText(f"导入 {rep['item_count']} 行明细")
                self._ok_btn.setEnabled(bool(rep["items"]) and self.combo.count() > 0)

        def _do_import(self):
            if not self.report or not self.report["items"]:
                theme.notify(self, "无法导入", "请先选择含有效明细行的文件", kind="warn")
                return
            oid = self.combo.currentData()
            if oid is None:
                theme.notify(self, "无法导入", "请先创建并选择目标订单", kind="warn")
                return
            from ..domains import OrderService
            try:
                self.result = OrderService(self.db).add_items_bulk(
                    int(oid), self.report["items"], operator=cur.name(),
                    origin="import_order_items")
            except Exception as exc:
                theme.notify(self, "导入失败",
                             f"{exc}\n\n本次导入未产生任何明细（整批已回滚）",
                             kind="error")
                return
            fatal = sum(1 for i in self.report["issues"] if i.get("fatal"))
            theme.notify(
                self, "导入完成",
                f"订单 {self.result['order']['doc_no']} 已导入 "
                f"{self.result['count']} 行明细，累计 {self.result['item_count']} 行，"
                f"价税合计 ¥{self.result['amount_total']:,.2f}；跳过 {fatal} 行。\n"
                "明细全部落到这一张订单，不会拆成多张空壳订单。")
            self.accept()

    class ImportOrdersDialog(ThemedDialog):
        """
        选 .xlsx/.csv/.tsv → 自动识别编码与表头别名 → 按「订单项目编号」合并
        → 17 列预览 + 逐行报告 → 单事务批量建单（失败整体回滚）。

        v2.3：订单头 8 字段 + 明细 9 字段；价税合计/总价留空自动汇总，
        物资数量按 Q12 = 物资项目数（留空自动 = 明细行数）；
        订单状态由文件「订单状态」列决定（待发货/发货中/已验收 直接支持）。
        """
        def __init__(self, parent, db):
            super().__init__(
                parent, "导入订单 · CSV / Excel",
                "每行 = 一张订单的一条明细；同一「订单项目编号」合并为一张订单",
                width=1240, height=680)
            self.db = db
            self.report: Optional[Dict[str, Any]] = None
            self.result: Optional[Dict[str, Any]] = None

            top = QHBoxLayout()
            self.btn_file = QPushButton("选择文件…")
            self.btn_file.setObjectName("Primary")
            self.btn_file.clicked.connect(self._choose_file)
            top.addWidget(self.btn_file)
            btn_tpl = QPushButton("下载订单导入模板")
            btn_tpl.clicked.connect(self._download_template)
            top.addWidget(btn_tpl)
            self.status_hint = theme.label(
                "订单状态取自文件「订单状态」列；价税合计/总价留空自动汇总，"
                "物资数量留空自动 = 明细项数（Q12）",
                "DialogHint")
            top.addWidget(self.status_hint)
            top.addStretch(1)
            self.body_layout.addLayout(top)

            self.path_label = theme.label("尚未选择文件", "DialogHint")
            self.body_layout.addWidget(self.path_label)
            self.summary = theme.label(
                "识别列 0 / 订单 0 张 / 明细 0 行 / 跳过 0 / 提示 0", "DialogCount")
            self.body_layout.addWidget(self.summary)

            self.table = theme.style_table(QTableWidget())
            self.table.setObjectName("ImportPreview")
            self._set_preview([], [])
            self.body_layout.addWidget(self.table, 1)

            self.issues = QTextEdit()
            self.issues.setReadOnly(True)
            self.issues.setPlainText("导入报告：选择文件后显示逐行校验结果。")
            self.issues.setMaximumHeight(96)
            self.body_layout.addWidget(self.issues)

            self._box = self.add_buttons(ok_text="导入 0 张订单",
                                         cancel_text="取消", on_ok=self._do_import)
            self._ok_btn = next((b for b in self._box.buttons()
                                 if b.objectName() == "Primary"), None)
            if self._ok_btn:
                self._ok_btn.setEnabled(False)

        def _set_preview(self, headers, rows):
            self.table.setColumnCount(len(headers) or 1)
            self.table.setHorizontalHeaderLabels(headers or ["（等待选择文件）"])
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    self.table.setItem(i, j, QTableWidgetItem(
                        "" if value is None else str(value)))
            if headers:
                self.table.resizeColumnsToContents()
                for c in range(self.table.columnCount()):
                    # 物资名称/实际生产物资名称/参数描述较长，单独放宽
                    cap = 210 if c in (9, 10, 16) else 150
                    if self.table.columnWidth(c) > cap:
                        self.table.setColumnWidth(c, cap)
                self.table.horizontalHeader().setSectionResizeMode(
                    min(1, len(headers) - 1), QHeaderView.Stretch)

        def _choose_file(self):
            path = theme.pick_path(
                self, "选择订单文件", mode="open",
                filters="Excel / CSV（*.xlsx *.csv *.tsv）;;Excel 工作簿 (*.xlsx);;"
                        "CSV / TSV (*.csv *.tsv);;所有文件 (*.*)")
            if path:
                self._load(path)

        def _download_template(self):
            path = theme.pick_path(
                self, "另存订单导入模板", mode="save",
                default_name="订单导入模板.xlsx",
                filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
            if path:
                excelio.export_template(path, "order")
                theme.notify(self, "模板已生成",
                             f"已保存：{path}\n\n17 列模板 = 订单头 8 字段 + 明细 9 字段，"
                             "含 3 行示例：同一「订单项目编号」两行明细合并为一单，"
                             "另一个项目编号单独成单。\n"
                             "价税合计 / 总价留空自动汇总；物资数量 = 物资项目数，"
                             "留空自动 = 明细行数（Q12 口径）。\n"
                             "订单状态可填 草稿/已确认/待发货/发货中/已验收 等。")

        def _load(self, path: str):
            try:
                rep = excelio.parse_order_file(path)
            except Exception as exc:
                theme.notify(self, "读取失败", str(exc), kind="error")
                return
            self.report = rep
            self.path_label.setText(f"文件：{path}")
            fatal = sum(1 for i in rep["issues"] if i.get("fatal"))
            warns = len(rep["issues"]) - fatal
            self.summary.setText(
                f"识别列 {len(rep['matched'])} / 订单 {rep['group_count']} 张 / "
                f"明细 {rep['item_count']} 行 / 跳过 {fatal} / 提示 {warns}；"
                f"未识别列 {len(rep['ignored'])}")
            self._set_preview(ofdefs.headers(),
                              ofdefs.preview_rows(rep["groups"], limit=50))
            lines = [f"第 {i['row']} 行 [{'跳过' if i.get('fatal') else '提示'}] {i['reason']}"
                     for i in rep["issues"]]
            if rep["ignored"]:
                lines.append("未识别列（已忽略）：" + "、".join(rep["ignored"]))
            if rep["item_count"] > 50:
                lines.append(f"预览仅显示前 50 行，实际将导入 {rep['item_count']} 行明细")
            self.issues.setPlainText("\n".join(lines) if lines else "全部数据行校验通过")
            if self._ok_btn:
                self._ok_btn.setText(
                    f"导入 {rep['group_count']} 张订单 / {rep['item_count']} 行明细")
                self._ok_btn.setEnabled(bool(rep["groups"]))

        def _do_import(self):
            if not self.report or not self.report["groups"]:
                theme.notify(self, "无法导入", "请先选择含有效数据行的文件", kind="warn")
                return
            from ..domains import OrderService
            try:
                self.result = OrderService(self.db).import_orders(
                    self.report["groups"], operator=cur.name())
            except Exception as exc:
                theme.notify(self, "导入失败", f"{exc}\n\n本次导入未产生任何订单"
                             "（整批已回滚）", kind="error")
                return
            fatal = sum(1 for i in self.report["issues"] if i.get("fatal"))
            theme.notify(
                self, "导入完成",
                f"已创建 {self.result['order_count']} 张订单、"
                f"{self.result['item_count']} 行明细，订单实值合计 "
                f"{self.result['qty_ordered_real']:g}，价税合计 "
                f"¥{self.result['amount_total']:,.2f}。\n"
                f"跳过 {fatal} 行。单号：{'、'.join(self.result['doc_nos'][:8])}"
                f"{'……' if len(self.result['doc_nos']) > 8 else ''}\n"
                "数量为订单实值，与询配码参考数量(虚值)无关。")
            self.accept()

    # ── 添加订单（图片 / 表格 / PDF / Word）──────────────────

    class AddOrderDialog(ThemedDialog):
        """
        从图片、表格、PDF、Word 里读订单信息 → 17 列可编辑预览 → 建单。

        * 解析统一走 `core/docread.read_source()`，结果摊成 17 列标准标签；
          建单仍交给 `OrderService.import_orders()`，与「导入订单」同一套口径。
        * 预览表可直接双击改（OCR 把「件」认成「+」、单价串列都能就地修正）。
        * 一份单据通常就是一张订单：若所有行都没有项目编号/ERP 编号，
          自动按整份文件合并成一张（订单分组 = 文件名）。
        """
        def __init__(self, parent, db):
            super().__init__(
                parent, "添加订单 · 图片 / 表格 / PDF / Word",
                "选文件 → 自动识别订单头与明细 → 可逐格修改 → 确认后建单",
                width=1320, height=760)
            self.db = db
            self.source: Optional[Dict[str, Any]] = None
            self.rows: List[Dict[str, Any]] = []
            self.report: Optional[Dict[str, Any]] = None
            self.result: Optional[Dict[str, Any]] = None
            self._group_key = "手动添加"
            self._filling = False

            top = QHBoxLayout()
            self.btn_file = QPushButton("选择文件…")
            self.btn_file.setObjectName("Primary")
            self.btn_file.clicked.connect(self._choose_file)
            top.addWidget(self.btn_file)
            top.addWidget(theme.label(
                "支持 图片(png/jpg/bmp/webp) · 表格(xlsx/xls/csv) · PDF · Word(docx/doc)；"
                "扫描件与照片走 OCR。", "DialogHint"))
            top.addStretch(1)
            self.body_layout.addLayout(top)

            # v2.9：本批订单默认来源（文件里若自带「订单来源」列则以文件为准）
            src_row = QHBoxLayout()
            src_row.addWidget(QLabel("订单来源"))
            self.combo_source = source_combo("（未填 · 本批订单默认来源）")
            src_row.addWidget(self.combo_source)
            src_row.addWidget(theme.label(
                "预设 " + " / ".join(ofdefs.ORDER_SOURCES) +
                "；可直接输入新渠道。仅填空缺行，不覆盖文件里已有的来源。",
                "DialogHint"))
            src_row.addStretch(1)
            self.body_layout.addLayout(src_row)
            # 改来源后重算预览（下拉选项立即生效；手输在回车/失焦时生效）
            self.combo_source.currentIndexChanged.connect(
                lambda *_: self._refresh_report())
            self.combo_source.lineEdit().editingFinished.connect(
                lambda: self._refresh_report())

            self.path_label = theme.label("尚未选择文件", "DialogHint")
            self.body_layout.addWidget(self.path_label)
            self.summary = theme.label("识别 0 行 / 订单 0 张 / 明细 0 行", "DialogCount")
            self.body_layout.addWidget(self.summary)

            self.table = theme.style_table(QTableWidget())
            self.table.setObjectName("AddOrderPreview")
            self.table.setColumnCount(len(ofdefs.headers()))
            self.table.setHorizontalHeaderLabels(ofdefs.headers())
            self.table.setEditTriggers(
                QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                | QAbstractItemView.SelectedClicked)
            self.table.itemChanged.connect(self._on_cell_changed)
            self.body_layout.addWidget(self.table, 1)

            self.issues = QTextEdit()
            self.issues.setReadOnly(True)
            self.issues.setPlainText(
                "选择文件后显示识别过程与逐行校验结果；预览表可双击单元格直接修改。")
            self.issues.setMaximumHeight(104)
            self.body_layout.addWidget(self.issues)

            self._box = self.add_buttons(ok_text="添加 0 张订单",
                                         cancel_text="取消", on_ok=self._do_add)
            self._ok_btn = next((b for b in self._box.buttons()
                                 if b.objectName() == "Primary"), None)
            if self._ok_btn:
                self._ok_btn.setEnabled(False)

        # ── 文件选择与读取 ──────────────────────────────────

        def _choose_file(self):
            path = theme.pick_path(self, "选择订单来源文件", mode="open",
                                   filters=docread.FILE_FILTER)
            if path:
                self._load(path)

        def _load(self, path: str):
            """P1-7：异步读取。PDF 抽表 + OCR 在子线程跑，界面不再「未响应」"""
            if self._ok_btn:
                self._ok_btn.setEnabled(False)
            self.btn_file.setEnabled(False)
            self.path_label.setText(f"正在识别：{Path(path).name} …（PDF/扫描件需要几秒）")
            self.issues.setPlainText("识别中… 界面保持可响应，完成后自动填入预览表。")
            async_task.run_task(
                self, lambda: docread.read_source(path),
                on_done=lambda source: self._apply_source(path, source),
                on_error=self._load_failed)

        def _load_blocking(self, path: str):
            """同步读取：供测试 / 脚本调用，行为与异步版完全一致"""
            try:
                self._apply_source(path, docread.read_source(path))
            except Exception as exc:
                self._load_failed(str(exc))

        def _load_failed(self, message: str):
            self.btn_file.setEnabled(True)
            self.path_label.setText(f"读取失败：{message}")
            self.issues.setPlainText(f"读取失败：{message}")
            theme.notify(self, "读取失败", message, kind="error")

        def _apply_source(self, path: str, source: Dict[str, Any]):
            self.btn_file.setEnabled(True)
            self.source = source
            self.rows = [dict(r) for r in source["rows"]]
            self._group_key = Path(path).stem or "手动添加"
            self.path_label.setText(
                f"文件:{path}　·　{source['kind_label']}　·　"
                f"识别 {len(self.rows)} 行 / 命中 {source['score']} 个订单字段")
            self._fill_table()
            self._refresh_report()

        def _fill_table(self):
            self._filling = True
            self.table.setRowCount(len(self.rows))
            for i, row in enumerate(self.rows):
                for j, label in enumerate(ofdefs.headers()):
                    value = row.get(label)
                    self.table.setItem(i, j, QTableWidgetItem(
                        "" if value is None else str(value)))
            self._filling = False
            self.table.resizeColumnsToContents()
            for c in range(self.table.columnCount()):
                cap = 200 if c in (1, 9, 10, 16) else 130
                if self.table.columnWidth(c) > cap:
                    self.table.setColumnWidth(c, cap)
            self.table.horizontalHeader().setSectionResizeMode(
                9, QHeaderView.Stretch)

        def _on_cell_changed(self, item):
            if self._filling:
                return
            row_index, col = item.row(), item.column()
            labels = ofdefs.headers()
            if 0 <= row_index < len(self.rows) and 0 <= col < len(labels):
                self.rows[row_index][labels[col]] = item.text().strip()
                self._refresh_report()

        # ── 解析与报告 ──────────────────────────────────────

        def _rows_for_parse(self) -> List[Dict[str, Any]]:
            rows = [dict(r) for r in self.rows]
            # v2.9：本批默认来源只补「没填」的行；文件自带来源优先
            source = source_text(self.combo_source)
            if source:
                for row in rows:
                    if not str(row.get("订单来源") or "").strip():
                        row["订单来源"] = source
            docread.ensure_grouping(rows, self._group_key)
            return rows

        def _refresh_report(self):
            if not self.rows:
                return
            self.report = ofdefs.parse_rows(self._rows_for_parse())
            rep = self.report
            fatal = sum(1 for i in rep["issues"] if i.get("fatal"))
            warns = len(rep["issues"]) - fatal
            self.summary.setText(
                f"识别 {len(self.rows)} 行 / 订单 {rep['group_count']} 张 / "
                f"明细 {rep['item_count']} 行 / 跳过 {fatal} / 提示 {warns}")

            lines: List[str] = []
            if self.source and self.source.get("notes"):
                lines.extend(f"· {n}" for n in self.source["notes"])
            if self.source and self.source.get("ignored_columns"):
                lines.append("未识别列（已忽略）：" +
                             "、".join(self.source["ignored_columns"]))
            lines.extend(f"第 {i['row']} 行 [{'跳过' if i.get('fatal') else '提示'}] {i['reason']}"
                         for i in rep["issues"])
            if not rep["groups"]:
                lines.append("未解析出订单：请检查「物资名称」「数量」两列是否已填。")
            elif fatal:
                lines.append("跳过行不影响其余行建单；可在预览表内直接补齐后重试。")
            self.issues.setPlainText("\n".join(lines) if lines else "全部数据行校验通过")

            if self._ok_btn:
                self._ok_btn.setText(
                    f"添加 {rep['group_count']} 张订单 / {rep['item_count']} 行明细")
                self._ok_btn.setEnabled(bool(rep["groups"]))

        # ── 建单 ────────────────────────────────────────────

        def _do_add(self):
            if not self.report or not self.report["groups"]:
                theme.notify(self, "无法添加",
                             "请先选择文件，并确保「物资名称」「数量」有有效值",
                             kind="warn")
                return
            from ..domains import OrderService
            try:
                self.result = OrderService(self.db).import_orders(
                    self.report["groups"], operator="添加订单")
            except Exception as exc:
                theme.notify(self, "添加失败",
                             f"{exc}\n\n本次添加未产生任何订单（整批已回滚）",
                             kind="error")
                return
            fatal = sum(1 for i in self.report["issues"] if i.get("fatal"))
            theme.notify(
                self, "添加完成",
                f"已创建 {self.result['order_count']} 张订单、"
                f"{self.result['item_count']} 行明细，订单实值合计 "
                f"{self.result['qty_ordered_real']:g}，价税合计 "
                f"¥{self.result['amount_total']:,.2f}。\n"
                f"跳过 {fatal} 行。单号：{'、'.join(self.result['doc_nos'][:8])}"
                f"{'……' if len(self.result['doc_nos']) > 8 else ''}\n"
                "来源文件路径未写入数据库；如需留档请另存到订单排产附件。")
            self.accept()

    # ── 排产关联订单明细 ────────────────────────────────────

    class LinkOrderItemDialog(ThemedDialog):
        """选订单 → 看每条明细的实值/已排/可排 → 指定分配量"""
        def __init__(self, parent, db, orders):
            super().__init__(parent, "关联订单明细到生产排产项目",
                             "数量守恒：Σ分配量 ≤ 订单实值（排产唯一数量锚点）",
                             width=880, height=460)
            self.db, self.orders = db, orders
            v = self.body_layout
            h = QHBoxLayout()
            h.addWidget(theme.label("订单：", "DialogCount"))
            self.combo = QComboBox()
            self.combo.setMinimumWidth(420)
            self.combo.currentIndexChanged.connect(self._load_items)
            h.addWidget(self.combo, 1)
            v.addLayout(h)
            self.table = theme.style_table(QTableWidget())
            self.table.setColumnCount(7)
            self.table.setHorizontalHeaderLabels(
                ["明细行", "物资名称", "规格", "订单实值", "已排量", "可排量", "来源"])
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.table.currentCellChanged.connect(self._on_select)
            v.addWidget(self.table, 1)
            h2 = QHBoxLayout()
            h2.addWidget(theme.label("本次分配量：", "DialogCount"))
            self.spin = QDoubleSpinBox()
            self.spin.setRange(0.001, 1e9)
            self.spin.setDecimals(3)
            h2.addWidget(self.spin)
            self.tip = theme.label("", "DialogHint")
            h2.addWidget(self.tip, 1)
            v.addLayout(h2)
            self.add_buttons(ok_text="确认关联", on_ok=self._validate)
            rows = self.db.query("SELECT * FROM orders ORDER BY id")
            for o in rows:
                tag = "内部" if o["order_type"] == "内部订单" else f"第{o['batch_seq']}单"
                self.combo.addItem(
                    f"{o['doc_no']}  [{o['order_type']}/{tag}]  {o['status']}  {o['customer'] or ''}",
                    o["id"])
            if not rows:
                self.combo.addItem("（暂无订单）", None)
            self._load_items()

        def _load_items(self):
            oid = self.combo.currentData()
            self._items = []
            if oid is not None:
                self._items = self.db.query(
                    """SELECT oi.*, ci.item_code, bi.line_no AS bid_line_no,
                              COALESCE((SELECT SUM(pl.allocated_qty) FROM production_links pl
                                        WHERE pl.order_item_id = oi.id), 0) AS scheduled
                       FROM order_items oi
                       LEFT JOIN code_items ci ON ci.id = oi.source_code_item_id
                       LEFT JOIN bid_items bi ON bi.id = oi.source_bid_item_id
                       WHERE oi.order_id = ? ORDER BY oi.line_no""", (oid,))
            self.table.setRowCount(len(self._items))
            for i, r in enumerate(self._items):
                origin = (r["item_code"] or
                          (f"中标明细-{r['bid_line_no']:02d}" if r["bid_line_no"] else "订单新增"))
                values = (f"{r['line_no']:02d}", r["item_name"], r["spec"] or "",
                          r["qty_ordered"], r["scheduled"],
                          round(float(r["qty_ordered"]) - float(r["scheduled"]), 3), origin)
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QTableWidgetItem(str(value)))
            self.table.resizeColumnsToContents()
            if self._items:
                self.table.selectRow(0)

        def _on_select(self, row, *_):
            if 0 <= row < len(self._items):
                avail = round(float(self._items[row]["qty_ordered"])
                              - float(self._items[row]["scheduled"]), 3)
                self.spin.setMaximum(max(0.001, avail))
                self.spin.setValue(max(0.001, avail))
                self.tip.setText(f"该明细可排 {avail:g}（订单实值 {self._items[row]['qty_ordered']:g}）")

        def _validate(self):
            row = self.table.currentRow()
            if not (0 <= row < len(self._items)):
                theme.notify(self, "请选择明细", "请先点选一行订单明细", kind="warn")
                return
            avail = round(float(self._items[row]["qty_ordered"])
                          - float(self._items[row]["scheduled"]), 3)
            if self.spin.value() > avail + 1e-9:
                theme.notify(self, "超出可排量", f"最多可排 {avail:g}", kind="warn")
                return
            self.accept()

        def result_spec(self) -> Dict[str, Any]:
            row = self.table.currentRow()
            return {"order_item_id": self._items[row]["id"],
                    "allocated_qty": float(self.spin.value())}

    # ── 订单排产（v3.0：移到 ui/schedule_dialog.py，分项/合并/整单三范围）──
    # 这里只做再导出，保持 dialogs.OrderScheduleDialog 的老调用不变。
    from .schedule_dialog import OrderScheduleDialog        # noqa: E402,F401

    # ── 询配码明细导入向导（CSV / Excel）─────────────────────

    class ImportCodeItemsDialog(ThemedDialog):
        """
        选项目 → 选 .xlsx/.csv/.tsv → 自动识别编码与表头别名 → 预览 → 单事务导入。
        10 个业务字段严格按 core/fields.py；坏行给逐行报告，不静默吞掉。
        """
        def __init__(self, parent, db, project_id=None):
            super().__init__(
                parent, "导入询配码明细 · CSV / Excel",
                "支持 .xlsx / .csv / .tsv；自动识别编码、分隔符与表头别名",
                width=1060, height=620)
            self.db = db
            self.report: Optional[Dict[str, Any]] = None
            self.result: Optional[Dict[str, Any]] = None

            top = QHBoxLayout()
            top.addWidget(QLabel("目标项目："))
            self.combo = QComboBox()
            projects = db.query("SELECT * FROM code_projects ORDER BY id")
            for r in projects:
                self.combo.addItem(f"{r['doc_no']}  {r['project_name']}  [{r['status']}]",
                                   r["id"])
            if project_id is not None:
                idx = self.combo.findData(project_id)
                if idx >= 0:
                    self.combo.setCurrentIndex(idx)
            top.addWidget(self.combo, 1)
            btn_file = QPushButton("选择文件…")
            btn_file.setObjectName("Primary")
            btn_file.clicked.connect(self._choose_file)
            top.addWidget(btn_file)
            btn_tpl = QPushButton("下载导入模板")
            btn_tpl.clicked.connect(self._download_template)
            top.addWidget(btn_tpl)
            self.body_layout.addLayout(top)

            self.path_label = theme.label("尚未选择文件", "DialogHint")
            self.body_layout.addWidget(self.path_label)
            self.summary = theme.label("识别列 0 / 有效行 0 / 跳过 0 / 提示 0", "DialogCount")
            self.body_layout.addWidget(self.summary)

            self.table = theme.style_table(QTableWidget())
            self.table.setObjectName("ImportPreview")
            self._set_preview([], [])
            self.body_layout.addWidget(self.table, 1)

            self.issues = QTextEdit()
            self.issues.setReadOnly(True)
            self.issues.setPlainText("导入报告：选择文件后显示逐行校验结果。")
            self.issues.setMaximumHeight(96)
            self.body_layout.addWidget(self.issues)

            self._box = self.add_buttons(ok_text="导入 0 行", cancel_text="取消",
                                         on_ok=self._do_import)
            self._ok_btn = next((b for b in self._box.buttons()
                                 if b.objectName() == "Primary"), None)
            if self._ok_btn:
                self._ok_btn.setEnabled(False)
            if not projects:
                self.path_label.setText("请先创建询配码项目，再导入明细")

        def _set_preview(self, headers, rows):
            self.table.setColumnCount(len(headers) or 1)
            self.table.setHorizontalHeaderLabels(headers or ["（等待选择文件）"])
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    self.table.setItem(i, j, QTableWidgetItem(
                        "" if value is None else str(value)))
            if headers:
                self.table.resizeColumnsToContents()
                for c in range(self.table.columnCount()):
                    if self.table.columnWidth(c) > 168:
                        self.table.setColumnWidth(c, 168)
                self.table.horizontalHeader().setSectionResizeMode(
                    min(3, len(headers) - 1), QHeaderView.Stretch)

        def _choose_file(self):
            path = theme.pick_path(
                self, "选择询配码明细文件", mode="open",
                filters="Excel / CSV（*.xlsx *.csv *.tsv）;;Excel 工作簿 (*.xlsx);;"
                        "CSV / TSV (*.csv *.tsv);;所有文件 (*.*)")
            if path:
                self._load(path)

        def _download_template(self):
            path = theme.pick_path(
                self, "另存导入模板", mode="save",
                default_name="询配码明细导入模板.xlsx",
                filters="Excel 工作簿 (*.xlsx);;CSV (*.csv)")
            if path:
                excelio.export_template(path, "code_item")
                theme.notify(self, "模板已生成", f"已保存：{path}\n\n"
                             "表头 10 列即为本次约定的字段，按列填写后可直接导入。")

        def _load(self, path: str):
            try:
                rep = excelio.parse_code_item_file(path)
            except Exception as exc:
                theme.notify(self, "读取失败", str(exc), kind="error")
                return
            self.report = rep
            self.path_label.setText(f"文件：{path}")
            fatal = sum(1 for i in rep["issues"] if i.get("fatal"))
            warns = len(rep["issues"]) - fatal
            self.summary.setText(
                f"识别列 {len(rep['matched'])} / 有效行 {len(rep['items'])} / "
                f"跳过 {fatal} / 提示 {warns}；未识别列 {len(rep['ignored'])}")
            self._set_preview(fdefs.headers(), fdefs.export_rows(rep["items"][:50]))
            lines = [f"第 {i['row']} 行 [{'跳过' if i.get('fatal') else '提示'}] {i['reason']}"
                     for i in rep["issues"]]
            if rep["ignored"]:
                lines.append("未识别列（已忽略）：" + "、".join(rep["ignored"]))
            if len(rep["items"]) > 50:
                lines.append(f"预览仅显示前 50 行，实际将导入 {len(rep['items'])} 行")
            self.issues.setPlainText("\n".join(lines) if lines else "全部数据行校验通过")
            if self._ok_btn:
                self._ok_btn.setText(f"导入 {len(rep['items'])} 行")
                self._ok_btn.setEnabled(bool(rep["items"]) and self.combo.count() > 0)

        def _do_import(self):
            if not self.report or not self.report["items"]:
                theme.notify(self, "无法导入", "请先选择含有效数据行的文件", kind="warn")
                return
            pid = self.combo.currentData()
            if pid is None:
                theme.notify(self, "无法导入", "请先创建并选择目标询配码项目", kind="warn")
                return
            from ..domains import PartCodeService
            try:
                self.result = PartCodeService(self.db).add_items_bulk(
                    pid, self.report["items"], operator=cur.name())
            except Exception as exc:
                theme.notify(self, "导入失败", str(exc), kind="error")
                return
            warn = sum(1 for i in self.report["issues"] if not i.get("fatal"))
            skipped = len(self.report["issues"]) - warn
            theme.notify(
                self, "导入完成",
                f"项目 {self.result['doc_no']} 已导入 {self.result['count']} 行；"
                f"跳过 {skipped} 行、提示 {warn} 条。\n"
                "参考数量为虚值，不参与下单与排产；厂家价格仅作询价信息。")
            self.accept()

    # ── 导入明细（兼容旧入口）───────────────────────────────

    def import_items(parent, db, kind: str) -> int:
        """
        兼容旧入口：询配码明细转新的导入向导（预览 + 错行报告 + 单事务）；
        其余类型（投标明细）沿用模板列映射。返回成功行数。
        """
        if kind == "code_item":
            dlg = ImportCodeItemsDialog(parent, db)
            if dlg.exec() == QDialog.Accepted and dlg.result:
                return dlg.result["count"]
            return 0
        rows = db.query("SELECT * FROM bid_projects ORDER BY id")
        label = lambda r: f"{r['doc_no']}  {r['project_name']}  [{r['status']}]"
        title = "导入投标明细"
        if not rows:
            theme.notify(parent, title, "请先创建对应的项目头", kind="warn")
            return 0
        picked = ask(parent, title, [{
            "key": "project_id", "label": "目标项目", "type": "combo",
            "options": options_from(rows, label), "required": True,
        }])
        if not picked:
            return 0
        path = theme.pick_path(
            parent, "选择文件", mode="open",
            filters="Excel/CSV (*.xlsx *.csv *.tsv);;Excel (*.xlsx);;CSV (*.csv *.tsv)")
        if not path:
            return 0
        data = excelio.map_template_rows(kind, excelio.import_rows(path))
        if not data:
            theme.notify(parent, title, "未读到有效数据行（请检查模板列名与首行）",
                         kind="warn")
            return 0
        from ..domains import BidService
        service = BidService(db)
        for item in data:
            service.add_item(picked["project_id"], **item)
        theme.notify(parent, title, f"成功导入 {len(data)} 行明细")
        return len(data)

else:

    class ThemedDialog:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise RuntimeError("未安装 PySide6，对话框不可用")

    class FormDialog(ThemedDialog):  # type: ignore[no-redef]
        pass

    ImportCodeItemsDialog = ThemedDialog   # type: ignore[misc,assignment]
    NewOrderDialog = ThemedDialog         # type: ignore[misc,assignment]
    AddOrderItemsDialog = ThemedDialog    # type: ignore[misc,assignment]
    ImportOrderItemsDialog = ThemedDialog # type: ignore[misc,assignment]
    ImportOrdersDialog = ThemedDialog     # type: ignore[misc,assignment]

    def ask(*args, **kwargs):
        raise RuntimeError("未安装 PySide6，对话框不可用")

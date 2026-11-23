#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采购合同管理窗口（v3.4.0 · 第 14 轮需求；v3.4.5 导入即签订；v3.4.6 金额取自文档）

生产排产操作模块 →「采购合同管理」的主窗口。一个窗口做完三件事：
  · 生成：按选中订单的物资明细生成《采购合同》.docx 并登记为「合同正文」；
  · 导入：从资源管理器一次多选任意格式合同文件，自动按文件名截取编号、
          以文件名作合同名称、状态流转为「已签订」，并自动分类归档；
          v3.4.6 起同时从合同文档读取总金额写入台账，不再取订单总额；
  · 管理：台账字段编辑、状态流转、删除、附件查看/编辑/更改、台账导出、
          「重读金额」按已归档合同文件重算金额（用于纠正历史台账）。

文件物理落在 data/采购合同/<合同号>/，双击附件即用本机 Word/Excel/PDF
阅读器打开编辑，改完保存即可（内容改动不改动登记关系）。
v3.4.5：新建 / 编辑表单的「需方」「供方」改为历史候选智能输入
（PartyComboBox：输入关键字即弹出包含匹配的历史名称，支持自由新输）。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core import current_user as cur
from ..core.errors import NotFoundError
from ..domains.contract import (
    CONTRACT_FILTER, CONTRACT_TYPES, KINDS, STATUSES, STATUS_DRAFT,
    amount_summary, auto_apply_summary,
)
from . import theme
from .base_dialog import ThemedDialog

CONTRACT_HEADERS = ["合同编号", "合同名称", "类型", "供方", "关联订单",
                    "金额(¥)", "签订日期", "交货日期", "状态", "附件数"]
FILE_HEADERS = ["文件名", "类别", "大小(KB)", "在盘", "登记时间"]


class PurchaseContractDialog(ThemedDialog):
    """采购合同台账 + 附件管理"""

    def __init__(self, parent, svc, order_id: Optional[int] = None,
                 focus_contract_id: Optional[int] = None,
                 badge: str = theme.BADGE):
        self.svc = svc
        self.db = svc.db
        self.order_id = int(order_id) if order_id else None
        self.focus_id = int(focus_contract_id) if focus_contract_id else None
        self.changes: List[str] = []
        super().__init__(parent, "采购合同管理",
                         "生成 · 导入 · 编辑 · 状态 · 附件　|　生产排产操作模块",
                         width=1180, height=820, badge=badge)
        v = self.body_layout

        v.addLayout(self._build_filter_row())
        self.hint = theme.label("", "DialogHint", word_wrap=True)
        v.addWidget(self.hint)

        self.contract_table = theme.style_table(QTableWidget())
        self.contract_table.setColumnCount(len(CONTRACT_HEADERS))
        self.contract_table.setHorizontalHeaderLabels(CONTRACT_HEADERS)
        self.contract_table.currentCellChanged.connect(self._on_contract_changed)
        self.contract_table.doubleClicked.connect(self._do_edit)
        self.file_table = theme.style_table(QTableWidget())
        self.file_table.setColumnCount(len(FILE_HEADERS))
        self.file_table.setHorizontalHeaderLabels(FILE_HEADERS)
        self.file_table.doubleClicked.connect(self._do_open)

        box1 = QWidget()
        l1 = QVBoxLayout(box1)
        l1.setContentsMargins(0, 0, 0, 0)
        l1.setSpacing(5)
        l1.addWidget(theme.label("采购合同台账 · 双击行编辑合同信息", "KpiLabel"))
        l1.addWidget(self.contract_table, 1)
        box2 = QWidget()
        l2 = QVBoxLayout(box2)
        l2.setContentsMargins(0, 0, 0, 0)
        l2.setSpacing(5)
        l2.addWidget(theme.label("合同文件 · 双击用本机程序打开编辑，改完直接保存", "KpiLabel"))
        l2.addWidget(self.file_table, 1)
        v.addWidget(box1, 3)
        v.addWidget(box2, 2)

        v.addLayout(self._build_contract_actions())
        v.addLayout(self._build_file_actions())
        self.add_buttons(ok_text="关闭", show_cancel=False)
        self._reload_contracts()

    # ── 筛选 / 动作区 ──────────────────────────────────────

    def _build_filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(QLabel("订单："))
        self.order_box = QComboBox()
        self.order_box.addItem("全部订单", 0)
        # v3.4.1：下拉显示「订单项目编号（客户名称）」，项目编号为空回退系统单号
        for o in self.db.query("SELECT id, doc_no, project_no, customer FROM orders ORDER BY id DESC"):
            caption = (f"{o['project_no'] or o['doc_no']}"
                       f"（{o.get('customer') or '—'}）")
            self.order_box.addItem(caption, o["id"])
        if self.order_id:
            idx = self.order_box.findData(self.order_id)
            if idx >= 0:
                self.order_box.setCurrentIndex(idx)
        self.order_box.currentIndexChanged.connect(self._reload_contracts)
        row.addWidget(self.order_box, 1)
        row.addWidget(QLabel("状态："))
        self.status_box = QComboBox()
        self.status_box.addItem("全部状态", "")
        for s in STATUSES:
            self.status_box.addItem(s, s)
        self.status_box.currentIndexChanged.connect(self._reload_contracts)
        row.addWidget(self.status_box)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("合同编号 / 名称 / 供方 / 订单号，支持模糊查询")
        self.keyword.returnPressed.connect(self._reload_contracts)
        row.addWidget(self.keyword, 1)
        b = QPushButton("筛选")
        b.clicked.connect(self._reload_contracts)
        row.addWidget(b)
        b = QPushButton("重置")
        b.clicked.connect(self._reset_filter)
        row.addWidget(b)
        return row

    def _build_contract_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        b = QPushButton("新建合同")
        b.setObjectName("Primary")
        b.setToolTip("登记一份新的采购合同台账（可关联订单，也可独立登记）")
        b.clicked.connect(self._do_new)
        row.addWidget(b)
        for text, slot in (("编辑合同", self._do_edit),
                           ("更改状态", self._do_status),
                           ("物资分配", self._do_items),
                           ("删除合同", self._do_delete),
                           ("导出台账", self._do_export)):
            x = QPushButton(text)
            if text == "物资分配":
                x.setToolTip("指定本合同覆盖的订单物资、采购数量与含税单价；"
                             "一张订单的多项物资可分签不同供应商")
            x.clicked.connect(slot)
            row.addWidget(x)
        row.addStretch(1)
        return row

    def _build_file_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        b = QPushButton("生成合同")
        b.setObjectName("Primary")
        b.setToolTip("按关联订单的物资明细生成 Word 采购合同模板")
        b.clicked.connect(self._do_generate)
        row.addWidget(b)
        b = QPushButton("导入合同")
        b.setToolTip("导入即签订：自动取文件名中文前的字符作合同编号、文件名作合同名称，"
                     "状态流转为「已签订」；相同编号的文件归入同一份合同；"
                     "合同金额从文档总金额 / 中文大写金额读取，不再取订单总额；"
                     "右侧「导入即自动分配」开启且全部校验通过时，物资分配一并写入")
        b.clicked.connect(self._do_import)
        row.addWidget(b)
        self.auto_apply_box = QCheckBox("导入即自动分配")
        self.auto_apply_box.setChecked(
            bool(self.svc.prefs().get("auto_apply_items", True)))
        self.auto_apply_box.setToolTip(
            "导入合同后自动把物资分配写入台账，省掉「物资分配 → 保存分配」这一步。\n"
            "仅在【合同明细表全部行高置信匹配 + 零未匹配 + 金额对账零差额 + "
            "数量守恒 + 无手填单价 / 无跨订单分配】同时成立时生效；\n"
            "任一条件不满足只在下方提示原因、绝不写库。\n"
            "开关对所有导入入口（含生产排产快捷导入）同时生效，随升级保留。")
        self.auto_apply_box.toggled.connect(self._on_auto_apply_toggled)
        row.addWidget(self.auto_apply_box)
        b = QPushButton("重读金额")
        b.setToolTip("按该合同已归档的全部文件重新读取总金额并写回台账，"
                     "用于纠正历史按订单总额带出的金额；手工填写的金额不会被覆盖")
        b.clicked.connect(self._do_refresh_amount)
        row.addWidget(b)
        for text, slot in (("打开", self._do_open),
                           ("重命名", self._do_rename),
                           ("替换内容", self._do_replace),
                           ("更改类别", self._do_kind),
                           ("移除", self._do_remove),
                           ("打开目录", self._open_folder)):
            x = QPushButton(text)
            x.clicked.connect(slot)
            row.addWidget(x)
        row.addStretch(1)
        return row

    # ── 数据渲染 ──────────────────────────────────────────

    def _filters(self):
        oid = self.order_box.currentData() or None
        return (oid, self.status_box.currentData() or "",
                self.keyword.text().strip())

    def _reset_filter(self):
        self.order_box.setCurrentIndex(0)
        self.status_box.setCurrentIndex(0)
        self.keyword.clear()
        self._reload_contracts()

    def _reload_contracts(self, *_args):
        oid, status, kw = self._filters()
        rows = self.svc.list(order_id=oid, keyword=kw, status=status)
        self.contract_table.blockSignals(True)
        self.contract_table.clearContents()
        self.contract_table.setRowCount(len(rows))
        focus_row = -1
        for r, c in enumerate(rows):
            values = (c["doc_no"], c["contract_name"], c["contract_type"],
                      c.get("supplier") or "—", c.get("order_no") or "—",
                      f"{float(c.get('amount') or 0):,.2f}",
                      c.get("sign_date") or "—", c.get("delivery_date") or "—",
                      c["status"], c["file_count"])
            if self.focus_id and int(c["id"]) == self.focus_id:
                focus_row = r
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if col == 0:
                    cell.setData(Qt.UserRole, c["id"])
                self.contract_table.setItem(r, col, cell)
        self.contract_table.resizeColumnsToContents()
        self.contract_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.contract_table.blockSignals(False)
        if focus_row >= 0:
            self.contract_table.selectRow(focus_row)
            self.focus_id = None
        elif rows:
            self.contract_table.selectRow(0)
        self._reload_files()
        total = float(sum(x.get("amount") or 0 for x in rows))
        self.hint.setText(
            f"当前筛出 {len(rows)} 份合同，金额合计 ¥{total:,.2f}。"
            "生成 / 导入的文件统一归档在 data/采购合同/<合同号>/；"
            "导入会自动按文件名带出编号与名称、置为「已签订」，"
            "合同金额取合同文档总金额（非订单总额）；"
            "编辑合同正文请用「打开」，改完在 Word 里保存即可。")

    def _reload_files(self):
        cid = self.selected_contract_id()
        rows = self.svc.files(cid) if cid else []
        self.file_table.blockSignals(True)
        self.file_table.clearContents()
        self.file_table.setRowCount(len(rows))
        for r, f in enumerate(rows):
            values = (f["file_name"], f["kind"],
                      f"{int(f.get('file_size') or 0) / 1024:.1f}",
                      "在盘" if f["exists"] else "缺失",
                      str(f.get("created_at") or "")[:19])
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if col == 0:
                    cell.setData(Qt.UserRole, f["id"])
                if col == 3 and not f["exists"]:
                    cell.setForeground(QColor(theme.DANGER))
                self.file_table.setItem(r, col, cell)
        self.file_table.resizeColumnsToContents()
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.blockSignals(False)

    def _on_contract_changed(self, *_args):
        self._reload_files()

    def selected_contract_id(self) -> Optional[int]:
        row = self.contract_table.currentRow()
        cell = self.contract_table.item(row, 0) if row >= 0 else None
        value = cell.data(Qt.UserRole) if cell else None
        return int(value) if value is not None else None

    def selected_file_id(self) -> Optional[int]:
        row = self.file_table.currentRow()
        cell = self.file_table.item(row, 0) if row >= 0 else None
        value = cell.data(Qt.UserRole) if cell else None
        return int(value) if value is not None else None

    def _need_contract(self) -> Optional[Dict[str, Any]]:
        cid = self.selected_contract_id()
        c = self.svc.get(cid) if cid else None
        if not c:
            theme.notify(self, "请先选择", "请点选上方台账里的一份采购合同", kind="warn")
        return c

    def _need_file(self) -> Optional[int]:
        fid = self.selected_file_id()
        if fid is None:
            theme.notify(self, "请先选择", "请点选下方合同文件表里的一行", kind="warn")
        return fid

    # ── 台账表单 ──────────────────────────────────────────

    def _form(self, c: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        from . import dialogs
        c = c or {}
        d = self._filters()
        default_order = c.get("order_id") or (d[0] or 0)
        orders = [("（不关联订单）", 0)] + [
            (f"{o['project_no'] or o['doc_no']}（{o.get('customer') or '—'}）", o["id"])
            for o in self.db.query(
                "SELECT id, doc_no, project_no, customer FROM orders ORDER BY id DESC")]
        fields = [
            {"key": "contract_name", "label": "合同名称", "type": "text",
             "default": c.get("contract_name") or "采购合同", "required": True},
            {"key": "contract_type", "label": "合同类型", "type": "combo",
             "options": [(x, x) for x in CONTRACT_TYPES],
             "default": c.get("contract_type") or CONTRACT_TYPES[0]},
            {"key": "order_id", "label": "关联订单", "type": "combo",
             "options": orders, "default": default_order},
            {"key": "supplier", "label": "供方", "type": "party",
             "party_column": "supplier", "default": c.get("supplier") or ""},
            {"key": "buyer", "label": "需方", "type": "party",
             "party_column": "buyer", "default": c.get("buyer") or ""},
            {"key": "contact", "label": "供方联系人", "type": "text",
             "default": c.get("contact") or ""},
            {"key": "amount", "label": "合同金额", "type": "float",
             "default": float(c.get("amount") or 0), "decimals": 2,
             "maximum": 1e12},
            {"key": "currency", "label": "币种", "type": "text",
             "default": c.get("currency") or "CNY"},
            {"key": "tax_included", "label": "含税口径", "type": "combo",
             "options": [("含税（统一口径）", 1), ("不含税", 0)],
             "default": (1 if c.get("tax_included") is None
                         else int(c["tax_included"]))},
            {"key": "sign_date", "label": "签订日期", "type": "date",
             "default": c.get("sign_date") or ""},
            {"key": "delivery_date", "label": "交货日期", "type": "date",
             "default": c.get("delivery_date") or ""},
            {"key": "status", "label": "合同状态", "type": "combo",
             "options": [(x, x) for x in STATUSES],
             "default": c.get("status") or STATUS_DRAFT},
            {"key": "payment_terms", "label": "付款方式", "type": "text",
             "default": c.get("payment_terms") or ""},
            {"key": "remark", "label": "备注", "type": "textarea",
             "default": c.get("remark") or ""},
        ]
        return dialogs.ask(
            self, "编辑采购合同" if c else "新建采购合同", fields,
            subtitle="带 * 为必填；关联订单用于生成合同物资清单，可留空独立登记。")

    def _do_new(self):
        data = self._form()
        if not data:
            return
        spec = dict(data)
        spec["order_id"] = spec.get("order_id") or None
        # v3.4.5 硬口径：新建合同最初状态固定「草稿」，只有导入才自动签订
        spec["status"] = spec.get("status") or STATUS_DRAFT
        created = self._apply(lambda: self.svc.create(spec, cur.name()), "新建合同")
        if created:
            self.focus_id = int(created["id"])
            self._reload_contracts()

    def _do_edit(self, *_args):
        c = self._need_contract()
        if not c:
            return
        data = self._form(c)
        if not data:
            return
        data["order_id"] = data.get("order_id") or None
        self._apply(lambda: self.svc.update(int(c["id"]), data, cur.name()),
                    f"编辑合同 {c['doc_no']}")

    def _do_status(self):
        c = self._need_contract()
        if not c:
            return
        from . import dialogs
        data = dialogs.ask(self, "更改合同状态", [
            {"key": "status", "label": "合同状态", "type": "combo",
             "options": [(x, x) for x in STATUSES], "default": c["status"]},
        ], subtitle=f"{c['doc_no']} · {c['contract_name']}（当前：{c['status']}）")
        if not data:
            return
        self._apply(lambda: self.svc.set_status(int(c["id"]),
                                                str(data["status"]), cur.name()),
                    f"合同 {c['doc_no']} 状态 → {data['status']}")

    def _do_delete(self):
        c = self._need_contract()
        if not c:
            return
        if not theme.confirm(self, "删除采购合同",
                             f"确认删除合同「{c['doc_no']} {c['contract_name']}」？",
                             "台账记录、附件登记与 data/采购合同/ 下该合同目录内的"
                             "物理文件将一并清理；订单与排产数量不受影响。"):
            return
        self._apply(lambda: self.svc.delete(int(c["id"]), cur.name()),
                    f"删除合同 {c['doc_no']}")

    def _do_export(self):
        from . import dialogs
        self._apply(lambda: dialogs.export_sheets(
            self, self.svc.export_sheets(), "采购合同台账.xlsx"), "导出台账")

    # ── 生成 / 导入 / 附件 ─────────────────────────────────

    def _do_items(self):
        """v3.5.0：指定本合同覆盖的订单物资（一单多供应商的归集依据）"""
        c = self._need_contract()
        if not c:
            return
        from .contract_items_dialog import ContractItemsDialog

        ContractItemsDialog(self, self.svc, int(c["id"])).exec()
        self._reload_contracts()

    def _do_generate(self):
        c = self._need_contract()
        if not c:
            return
        ctx = self.svc.context(int(c["id"]))
        if ctx.get("items_source") != "contract_items" and not theme.confirm(
                self, "尚未分配合同物资",
                "本合同还没指定覆盖哪些物资，将按【整单全部明细 + 销售单价】生成模板。",
                "多供应商订单建议先点「物资分配」指定该供方覆盖的物资与含税采购价；"
                "否则每份合同都会列出全部物资，且单价是销售价而非采购价。"):
            return
        res = self._apply(lambda: self.svc.generate(int(c["id"]), cur.name()),
                          f"生成合同 {c['doc_no']}")
        if res:
            theme.notify(
                self, "合同已生成" if res["created"] else "合同已存在",
                (f"已生成 {Path(res['path']).name}；点「打开」即可用本机 Word 编辑，"
                 "改完保存不需要重新导入。") if res["created"] else
                (f"{Path(res['path']).name} 已在台账中，直接复用；"
                 "删除该附件后可重新生成。"))

    def _on_auto_apply_toggled(self, checked: bool) -> None:
        """v3.5.2：开关落 data/contract_prefs.json，两个导入入口共用"""
        self.svc.set_auto_apply(bool(checked))
        self.hint.setText(
            "「导入即自动分配」已" + ("开启" if checked else "关闭")
            + "。开启时仍只在全部匹配高置信、零未匹配、金额零差额、数量守恒"
              "且无手填单价时自动写入；否则只提示原因，可在「物资分配」页手工处理。")

    def _do_import(self):
        c = self._need_contract()
        if not c:
            return
        paths = theme.pick_paths(self, "导入采购合同文件（可多选 · 导入即签订）",
                                 CONTRACT_FILTER)
        if not paths:
            return
        res = self._apply(
            lambda: self.svc.import_contract_files(
                c.get("order_id"), paths, cur.name(), contract_id=int(c["id"])),
            f"导入 {len(paths)} 个合同文件")
        if not res or not res.get("contracts"):
            return
        self.focus_id = int(res["contracts"][0]["id"])
        self._reload_contracts()
        summary = "、".join(
            f"{x['doc_no']}（{x['contract_name']} · {x['status']}）"
            for x in res["contracts"])
        money = amount_summary(res)
        applied = auto_apply_summary(res)
        self.hint.setText(
            f"已导入 {res['files']} 个文件 → {summary}。"
            "编号取文件名中文前的字符、名称取文件名，状态自动置为「已签订」；"
            "相同编号的文件归入同一份合同。"
            + (f" 合同金额：{money}。" if money else "")
            + (f" 物资分配：{applied}。" if applied else ""))

    def _do_refresh_amount(self):
        """v3.4.6：按已归档文件重算合同金额，纠正历史订单总额"""
        c = self._need_contract()
        if not c:
            return
        res = self._apply(
            lambda: self.svc.refresh_amount_from_files(
                int(c["id"]), cur.name()),
            f"重读合同金额 {c['doc_no']}")
        if not res:
            return
        self.focus_id = int(c["id"])
        self._reload_contracts()
        self.hint.setText(
            f"{c['doc_no']}：{res.get('reason') or '金额未变化'}"
            f"（¥{float(res.get('before') or 0):,.2f} → "
            f"¥{float(res.get('amount') or 0):,.2f}）；"
            f"共核对 {res.get('files', 0)} 份已归档文件"
            + (f"，来源《{res.get('source')}》" if res.get("source") else "")
            + "。手工填写过的金额不会被覆盖。")

    def _do_open(self, *_args):
        fid = self._need_file()
        if fid is None:
            return
        ok, msg = self.svc.open_file(fid)
        if not ok:
            theme.notify(self, "无法打开", msg, kind="warn")

    def _do_rename(self):
        fid = self._need_file()
        if fid is None:
            return
        f = self.db.get_by_id("purchase_contract_files", fid)
        from . import dialogs
        data = dialogs.ask(self, "重命名合同文件", [
            {"key": "name", "label": "文件名", "type": "text",
             "default": f["file_name"], "required": True},
        ], subtitle="扩展名不可更改，保证登记关系与本机程序关联不变。")
        if not data:
            return
        self._apply(lambda: self.svc.rename_file(fid, str(data["name"]).strip(),
                                                 cur.name()),
                    f"重命名 {f['file_name']}")

    def _do_replace(self):
        fid = self._need_file()
        if fid is None:
            return
        path = theme.pick_path(self, "选择用于替换的合同文件", "open",
                               filters=CONTRACT_FILTER)
        if not path:
            return
        self._apply(lambda: self.svc.replace_file(fid, path, cur.name()),
                    f"替换内容 ← {Path(path).name}")

    def _do_kind(self):
        fid = self._need_file()
        if fid is None:
            return
        f = self.db.get_by_id("purchase_contract_files", fid)
        from . import dialogs
        data = dialogs.ask(self, "更改合同文件类别", [
            {"key": "kind", "label": "类别", "type": "combo",
             "options": [(x, x) for x in KINDS], "default": f["kind"]},
        ], subtitle="类别用于检索与统计，不改变物理文件。")
        if not data:
            return
        self._apply(lambda: self.svc.set_file_kind(fid, str(data["kind"]), cur.name()),
                    f"类别 → {data['kind']}")

    def _do_remove(self):
        fid = self._need_file()
        if fid is None:
            return
        f = self.db.get_by_id("purchase_contract_files", fid)
        if not theme.confirm(self, "移除合同文件",
                             f"确认移除「{f['file_name']}」？",
                             "只移除本合同的这条附件登记并清理物理文件，"
                             "合同台账与其它合同不受影响。"):
            return
        self._apply(lambda: self.svc.remove_file(fid, cur.name()),
                    f"移除 {f['file_name']}")

    def _open_folder(self):
        c = self._need_contract()
        if not c:
            return
        folder = self.svc.contract_dir(c["doc_no"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ── 统一执行 / 结果 ───────────────────────────────────

    def _apply(self, fn, label: str):
        try:
            result = fn()
        except Exception as exc:                                # noqa: BLE001
            theme.notify(self, f"{label} · 失败", str(exc), kind="error")
            return None
        self.changes.append(label)
        self.focus_id = self.focus_id or self.selected_contract_id()
        self._reload_contracts()
        self._reload_files()
        return result

    def summary_text(self) -> str:
        n = len(self.svc.list())
        return (f"采购合同管理：台账共 {n} 份合同；"
                + ("；".join(self.changes) if self.changes else "仅查看，未做更改"))

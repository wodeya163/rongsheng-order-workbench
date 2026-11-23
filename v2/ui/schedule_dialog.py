#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单排产对话框（v3.0 · 分项 / 合并 / 整单三种范围统一入口；v3.1.2 动作收敛）

用户口径（2026-11-07 第 6 轮）：
  · 整单排产也要先启动【订单排产】对话框，不再直接弹生产排产审批文件编辑窗口；
  · 对话框里对排产单做分项 / 合并 / 整单标记；
  · 订单明细可以多选多种物料做合并排产，合并同样在这个窗口里操作。

用户口径（2026-11-08 第 11 轮 · v3.1.2）：
  · 删除「导入图片」「导入图纸」两个按钮，只留「导入生产排产审批」一个入口，
    但它可以一次导入任意类型文件（按扩展名自动分类）；
  · 只要存在 1 个 Word/Excel 格式的审批文件，保存即默认本批物资已排产；
  · 「新建生产排产审批」只生成并登记 .docx 模板，不再自动启动本机 Word。

因此本窗口是所有排产动作的唯一入口：
  勾选 1 项        → 分项排产（一张排产单 + 一份分项审批单）
  勾选 2..n-1 项   → 合并排产（多张排产单共用一张合并审批单）
  勾选全部物资     → 整单排产（多张排产单共用一张整单审批单）

数量口径不变：Σ production_links.allocated_qty ≤ order_items.qty_ordered，
排产单上的 allocated_qty 只是快照；本窗口只调「单物资的本次排产数量」，
多物资时用「保存时按可排量排满」勾选项显式声明，不做隐式补量。
"""
from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDoubleSpinBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from ..core import current_user as cur
from ..core.errors import NotFoundError
from ..domains.schedule import (
    IMPORT_FILTER, SCOPE_ITEM, SCOPE_MERGE, SCOPE_WHOLE, scope_label,
)
from . import theme
from .base_dialog import ThemedDialog

# 物料清单列：勾选框 + 业务列 + 排产标记
ITEM_HEADERS = ["", "行", "物资名称", "规格", "单位", "订单数量",
                "已排量", "可排量", "订单排产单", "排产标记"]

_SCOPE_COLOR = {
    SCOPE_ITEM: theme.OK,
    SCOPE_MERGE: theme.WARN,
    SCOPE_WHOLE: theme.BRAND,
}


class _CheckDelegate(QStyledItemDelegate):
    """
    勾选列指示器自绘（v3.0.1）。

    用户反馈「选择框选择后无法清晰看到」：表格内 QTableWidgetItem 的勾选框
    走 QStyle 原生绘制，QSS 的 QCheckBox::indicator 管不到它，Windows 深色
    主题下指示器发灰、勾选后看不出状态。这里整列换成自绘——
    未勾选：白底 + 灰蓝描边圆角方块；已勾选：品牌蓝实底 + 白色对勾。
    数据模型不变（仍是 CheckStateRole），_checked_item_ids / 测试全兼容。
    """

    SIZE = 19

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # PySide6 坑：CheckStateRole 数据是 int（2/0），不是 Qt.CheckState 枚举，
        # 直接 == Qt.Checked 会恒 False → 勾选行全部画成未选中
        state = index.data(Qt.CheckStateRole)
        checked = (state is not None
                   and int(state) == int(Qt.CheckState.Checked.value))
        painter.setRenderHint(QPainter.Antialiasing, True)
        side = float(self.SIZE)
        cell = QRectF(option.rect)
        box = QRectF(cell.center().x() - side / 2,
                     cell.center().y() - side / 2, side, side)
        if checked:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.BRAND))
            painter.drawRoundedRect(box, 4.5, 4.5)
            pen = QPen(QColor("#FFFFFF"), 2.3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(box.left() + side * 0.24, box.top() + side * 0.53)
            path.lineTo(box.left() + side * 0.42, box.top() + side * 0.72)
            path.lineTo(box.left() + side * 0.78, box.top() + side * 0.28)
            painter.drawPath(path)
        else:
            painter.setBrush(QColor("#FFFFFF"))
            painter.setPen(QPen(QColor("#94A3B8"), 1.6))
            painter.drawRoundedRect(box, 4.5, 4.5)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 30))
        return size


class OrderScheduleDialog(ThemedDialog):
    """订单排产（v3.0）：先在窗口里定范围（分项 / 合并 / 整单），再出审批文档。"""

    def __init__(self, parent, svc, schedule_id: Optional[int] = None,
                 badge: str = theme.BADGE, order_id: Optional[int] = None,
                 item_ids: Optional[Sequence[int]] = None,
                 scope: Optional[str] = None):
        self.svc = svc
        self.db = svc.db
        self.schedule_id = int(schedule_id) if schedule_id else None
        sched = self.svc.get(self.schedule_id) if self.schedule_id else None
        if schedule_id and not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")

        if sched:
            self.order_id = int(sched["order_id"])
            self.item = self.db.get_by_id("order_items", sched["order_item_id"])
        else:
            if order_id is None:
                raise NotFoundError("打开订单排产需要订单 ID 或订单排产单 ID")
            self.order_id = int(order_id)
            self.item = None
        self.order = self.db.get_by_id("orders", self.order_id)
        if not self.order:
            raise NotFoundError(f"订单 {self.order_id} 不存在")

        self.items = svc.orders.list_items(self.order_id)
        if not self.items:
            raise NotFoundError(f"订单 {self.order['doc_no']} 没有物资明细")
        self._touched = False

        preset = list(item_ids or [])
        if scope == SCOPE_WHOLE or (not preset and not self.item):
            preset = [it["id"] for it in self.items]
        elif not preset and self.item:
            preset = [self.item["id"]]
        self._preset = {int(i) for i in preset}

        sub = f"{self.order['doc_no']}　·　{self.order.get('customer') or '—'}"
        if sched:
            sub = f"{sched['doc_no']}　·　{self.order['doc_no']} 第 {self.item['line_no']:02d} 项"
        super().__init__(parent, "订单排产", sub,
                         width=1140, height=840, badge=badge)
        v = self.body_layout

        v.addWidget(self._build_head())
        v.addLayout(self._build_scope_row())
        # v3.1.2：附件区比物资表更需要垂直空间（提示文字变长、附件行数多），
        # stretch 由 3:1 调成 2:3，避免附件表被压到只剩 2 行。
        v.addWidget(self._build_items_block(), 2)
        v.addLayout(self._build_qty_row())
        v.addLayout(self._build_action_row())
        v.addWidget(self._build_files_block(), 3)
        v.addLayout(self._build_file_actions())
        v.addWidget(theme.label("备注（本范围排产单共用）", "DialogHint"))
        self.remark = QTextEdit()
        self.remark.setPlainText(sched.get("remark") if sched else "")
        self.remark.setMaximumHeight(46)
        self.remark.textChanged.connect(self._mark_touched)
        v.addWidget(self.remark)
        v.addWidget(self._build_footer_hint(sched))

        self._reload_items()
        self._reload_files()
        self._sync_scope()
        self.add_buttons(ok_text="保存并关闭", show_cancel=False, on_ok=self._finish)

    # ── 顶部：订单身份卡 ──────────────────────────────────

    def _build_head(self) -> QFrame:
        info = QFrame()
        info.setObjectName("Card")
        grid = QGridLayout(info)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        pairs = [
            ("订单系统单号", self.order["doc_no"],
             "订单状态", self.order.get("status") or "—"),
            ("订单项目编号", self.order.get("project_no") or "—",
             "客户名称", self.order.get("customer") or "—"),
            ("订单类型", self.order.get("order_type") or "—",
             "交货日期", self.order.get("required_date") or "—"),
        ]
        for r, (k1, v1, k2, v2) in enumerate(pairs):
            grid.addWidget(theme.label(k1, "DialogHint"), r, 0)
            grid.addWidget(theme.label(str(v1), "DialogCount"), r, 1)
            grid.addWidget(theme.label(k2, "DialogHint"), r, 2)
            grid.addWidget(theme.label(str(v2), "DialogCount"), r, 3)
        return info

    # ── 排产范围徽标行 ────────────────────────────────────

    def _build_scope_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(theme.label("本批排产范围：", "DialogHint"))
        self.scope_value = theme.label("—", "DialogCount")
        row.addWidget(self.scope_value)
        row.addWidget(theme.label(
            "勾选 1 项 = 分项　|　勾选多项 = 合并（共用一张审批单）　|　勾选全部 = 整单",
            "DialogHint"), 1)
        self.btn_all = QPushButton("全选（整单）")
        self.btn_none = QPushButton("清空")
        self.btn_all.clicked.connect(lambda: self._set_checked(all_on=True))
        self.btn_none.clicked.connect(lambda: self._set_checked(all_on=False))
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        return row

    # ── 物料清单（勾选框决定范围）─────────────────────────

    def _build_items_block(self) -> QFrame:
        box = QFrame()
        box.setObjectName("Card")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(theme.label("订单物资 · 勾选决定排产范围", "DialogCount"))
        head.addStretch(1)
        self.item_hint = theme.label("", "DialogHint")
        head.addWidget(self.item_hint)
        v.addLayout(head)
        self.item_table = theme.style_table(QTableWidget())
        self.item_table.setObjectName("ResultTable")
        self.item_table.setColumnCount(len(ITEM_HEADERS))
        self.item_table.setHorizontalHeaderLabels(ITEM_HEADERS)
        self.item_table.setMinimumHeight(150)
        self.item_table.setMaximumHeight(360)
        self.item_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        # v3.0.1：勾选列自绘指示器 + 固定列宽 + 加高行距，勾选状态一眼可辨
        self.item_table.setItemDelegateForColumn(0, _CheckDelegate(self.item_table))
        self.item_table.setColumnWidth(0, 48)
        self.item_table.verticalHeader().setDefaultSectionSize(32)
        self.item_table.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.item_table, 1)
        return box

    # ── 单物资数量 ────────────────────────────────────────

    def _build_qty_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.qty_label = theme.label("本次排产数量（实值）：", "DialogCount")
        row.addWidget(self.qty_label)
        self.spin_qty = QDoubleSpinBox()
        self.spin_qty.setDecimals(3)
        self.spin_qty.setRange(0.001, max(0.001, 1e9))
        self.spin_qty.setFixedWidth(132)
        self.spin_qty.valueChanged.connect(self._mark_touched)
        row.addWidget(self.spin_qty)
        self.qty_hint = theme.label("", "DialogHint")
        row.addWidget(self.qty_hint, 1)
        self.chk_fill = QCheckBox("额外：保存时按可排量排满本范围物资")
        # v3.1.2：默认不勾。否则多物资时无条件补满会绕过「必须有 Word/Excel
        # 审批文件才默认已排产」的新口径，出现「没审批文件也自动已排产」。
        self.chk_fill.setChecked(False)
        self.chk_fill.setToolTip(
            "默认不勾：保存只按随单附件判定（存在 Word/Excel 审批文件即默认已排产）。"
            "仅在确实需要强制补量时才勾选，勾选后保存会把本批每项物资补满可排量。")
        self.chk_fill.stateChanged.connect(self._mark_touched)
        row.addWidget(self.chk_fill)
        return row

    # ── 审批动作 ──────────────────────────────────────────

    def _build_action_row(self) -> QHBoxLayout:
        """v3.1.2：动作收敛为两个按钮——新建审批 / 导入审批（任意类型）"""
        acts = QHBoxLayout()
        self.btn_approval = QPushButton("新建生产排产审批")
        self.btn_approval.setObjectName("Primary")
        self.btn_approval.setToolTip(
            "生成《生产排产审批单》.docx 模板并登记为审批附件；"
            "本版本不再自动启动本机 Word，可在下方附件列表双击打开编辑。")
        self.btn_approval.clicked.connect(self._do_approval)
        self.btn_import_approval = QPushButton("导入生产排产审批")
        self.btn_import_approval.setToolTip(
            "从资源管理器一次多选任意类型文件（Word / Excel / PDF / 图片 / 图纸 / 其它），"
            "按扩展名自动归类；存在 Word 或 Excel 审批文件后保存即默认本批物资已排产")
        self.btn_import_approval.clicked.connect(self._do_import)
        for b in (self.btn_approval, self.btn_import_approval):
            acts.addWidget(b)
        acts.addStretch(1)
        return acts

    # ── 随单附件 ──────────────────────────────────────────

    def _build_files_block(self) -> QFrame:
        box = QFrame()
        box.setObjectName("Card")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(theme.label("随单附件 · 审批 / 图片 / 图纸 / 其他", "DialogCount"))
        head.addStretch(1)
        v.addLayout(head)
        # 提示单独占一整行：v3.1.2 文案变长，放在表头右侧会被挤成竖排窄条
        self.kind_hint = theme.label("", "DialogHint", word_wrap=True)
        self.kind_hint.setMinimumHeight(38)
        v.addWidget(self.kind_hint)
        self.table = theme.style_table(QTableWidget())
        self.table.setObjectName("ResultTable")
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["类别", "文件名", "大小(KB)", "导入时间"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        # 4 行附件 × 30px + 表头 ≈ 150px，给足 168 免得最后一行被裁
        self.table.setMinimumHeight(168)
        self.table.setMaximumHeight(230)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.doubleClicked.connect(self._open_file)
        v.addWidget(self.table, 1)
        return box

    def _build_file_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.btn_open = QPushButton("打开选中附件")
        self.btn_open.clicked.connect(self._open_file)
        self.btn_remove = QPushButton("移除选中附件")
        self.btn_remove.clicked.connect(self._remove_file)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_remove)
        row.addStretch(1)
        row.addWidget(theme.label(
            "附件存于 data/订单排产附件/<订单号>/<排产单号>/", "DialogHint"))
        return row

    def _build_footer_hint(self, sched: Optional[Dict[str, Any]]) -> QLabel:
        s = self.svc.summary(self.order_id)
        extra = ""
        if sched and sched.get("batch_no"):
            extra = f"；本单属批次 {sched['batch_no']}（{sched.get('scope')}排产）"
        self.hint = theme.label(
            f"该订单共 {s['item_count']} 项物资，已建 {s['scheduled_count']} 张订单排产单"
            f"{extra}；全部物资都有订单排产单且实值排满时，"
            "订单状态自动变更为「已排产」。",
            "DialogHint", word_wrap=True)
        return self.hint

    # ── 物料清单渲染 / 勾选 ───────────────────────────────

    def _reload_items(self):
        scheds = self.svc.item_map(self.order_id)
        rows = self.items
        self.item_table.blockSignals(True)
        self.item_table.clearContents()
        self.item_table.setRowCount(len(rows))
        for r, it in enumerate(rows):
            s = scheds.get(it["id"])
            first = QTableWidgetItem("")
            first.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            first.setCheckState(Qt.Checked if it["id"] in self._preset
                                else Qt.Unchecked)
            first.setData(Qt.UserRole, it["id"])
            self.item_table.setItem(r, 0, first)
            values = (
                it["line_no"], it["item_name"], it.get("spec") or "—",
                it.get("unit") or "—", f"{float(it['qty_ordered']):g}",
                f"{self.svc.orders.allocated_qty(it['id']):g}",
                f"{self.svc.orders.available_qty(it['id']):g}",
                s["doc_no"] if s else "—",
                (s.get("scope") or SCOPE_ITEM) if s else "未排产",
            )
            for c, value in enumerate(values, start=1):
                cell = QTableWidgetItem(str(value))
                if c in (1, 5, 6, 7):
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 9:
                    color = _SCOPE_COLOR.get(str(value), theme.MUTED)
                    cell.setForeground(QColor(color))
                    f = cell.font()
                    f.setBold(True)
                    cell.setFont(f)
                self.item_table.setItem(r, c, cell)
        self.item_table.resizeColumnsToContents()
        self.item_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.item_table.blockSignals(False)
        built = len(scheds)
        self.item_hint.setText(
            f"已建 {built} / {len(rows)} 张排产单；勾选多项即为合并排产")
        # 自适应高度：全部物资行都要一眼可见（v3.0.1 反馈：勾选了看不全）
        # 行高实测 ≈ 36px（32 + item 上下 padding），表头 ≈ 34px
        self.item_table.setMinimumHeight(
            min(400, 38 + 37 * max(len(rows), 1)))
        self.item_table.setMaximumHeight(440)

    def _set_checked(self, all_on: bool):
        self.item_table.blockSignals(True)
        state = Qt.Checked if all_on else Qt.Unchecked
        for r in range(self.item_table.rowCount()):
            cell = self.item_table.item(r, 0)
            if cell:
                cell.setCheckState(state)
        self.item_table.blockSignals(False)
        self._mark_touched()
        self._sync_scope()

    def _on_item_changed(self, _cell):
        self._mark_touched()
        self._sync_scope()

    def _checked_item_ids(self) -> List[int]:
        out = []
        for r in range(self.item_table.rowCount()):
            cell = self.item_table.item(r, 0)
            if cell and cell.checkState() == Qt.Checked:
                out.append(int(cell.data(Qt.UserRole)))
        return out

    def _checked_schedules(self) -> List[Dict[str, Any]]:
        scheds = self.svc.item_map(self.order_id)
        return [scheds[i] for i in self._checked_item_ids() if i in scheds]

    def _scope(self) -> str:
        picked = self._checked_item_ids()
        return scope_label(len(picked), len(self.items))

    def _mark_touched(self, *_args):
        self._touched = True

    # ── 范围联动 ──────────────────────────────────────────

    def _sync_scope(self):
        picked = self._checked_item_ids()
        scope = scope_label(len(picked), len(self.items))
        total = len(self.items)
        if not picked:
            self.scope_value.setText("未选择物资（请先勾选）")
        else:
            text = f"{scope}排产 · {len(picked)} / {total} 项物资"
            if scope == SCOPE_MERGE:
                text += "　（共用一张合并审批单）"
            elif scope == SCOPE_WHOLE:
                text += "　（全部物资共用一张整单审批单）"
            self.scope_value.setText(text)
        color = _SCOPE_COLOR.get(scope, theme.MUTED) if picked else theme.WARN
        self.scope_value.setStyleSheet(f"color: {color}; font-weight: 700;")

        if scope == SCOPE_MERGE:
            self.btn_approval.setText(
                f"新建生产排产审批（合并 {len(picked)} 项 · 共用一张审批单）")
        elif scope == SCOPE_WHOLE:
            self.btn_approval.setText(
                "新建生产排产审批（整单 · 全部物资共用一张审批单）")
        else:
            self.btn_approval.setText("新建生产排产审批")

        for b in (self.btn_approval, self.btn_import_approval):
            b.setEnabled(bool(picked))

        single = len(picked) == 1
        self.qty_label.setVisible(single)
        self.spin_qty.setVisible(single)
        self.qty_hint.setVisible(single)
        self.chk_fill.setVisible(not single)
        if single:
            iid = picked[0]
            item = self.db.get_by_id("order_items", iid)
            s = self.svc.get_for_item(iid)
            avail = self.svc.orders.available_qty(iid)
            self.spin_qty.setRange(0.001, max(0.001, float(item["qty_ordered"])))
            allocated = float(s["allocated_qty"]) if s and s.get("allocated_qty") else 0.0
            self.spin_qty.blockSignals(True)
            self.spin_qty.setValue(allocated or max(0.001, avail))
            self.spin_qty.blockSignals(False)
            self.qty_hint.setText(
                f"可排量 {avail:g}　|　数量守恒：Σ排产分配量 ≤ 订单实值")
        self._reload_files()

    # ── 附件 ──────────────────────────────────────────────

    def _reload_files(self):
        sids = [s["id"] for s in self._checked_schedules()]
        rows = self.svc.scope_files(sids)
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for r, f in enumerate(rows):
            name = f["file_name"] + (f"（{f['shared']} 张排产单共用）"
                                     if f.get("shared", 1) > 1 else "")
            values = (f["kind"], name,
                      f"{int(f.get('file_size') or 0) / 1024:.1f}",
                      str(f.get("created_at") or "")[:19])
            for c, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if c == 0:
                    cell.setData(Qt.UserRole, f["id"])
                self.table.setItem(r, c, cell)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.blockSignals(False)
        self._update_kind_hint()

    def _update_kind_hint(self):
        """v3.1.2：存在 Word/Excel 审批文件 → 保存即默认已排产"""
        sids = [s["id"] for s in self._checked_schedules()]
        rows = self.svc.scope_files(sids)
        kinds = sorted({f["kind"] for f in rows})
        docs = self.svc.scope_approval_docs(sids)
        if not sids:
            self.kind_hint.setText("尚未建立排产单，勾选物资后点审批/导入即可建立。")
        elif docs:
            self.kind_hint.setText(
                f"随单附件已含 {len(kinds)} 类（{' / '.join(kinds)}），"
                f"且存在 Word/Excel 审批文件（{docs[0]}）——"
                "保存后本批物资默认【已排产】，排产数量自动补满可排量。")
        else:
            self.kind_hint.setText(
                f"随单附件 {len(kinds)} 类（{' / '.join(kinds) if kinds else '无'}）；"
                "尚未检测到 Word/Excel 审批文件。新建或导入一份 "
                ".doc/.docx/.wps/.xls/.xlsx 审批文档后保存，即默认本批物资已排产。")

    def _selected_file_id(self):
        row = self.table.currentRow()
        cell = self.table.item(row, 0) if row >= 0 else None
        return cell.data(Qt.UserRole) if cell else None

    # ── 动作 ──────────────────────────────────────────────

    def _ensure_scope(self) -> Optional[Dict[str, Any]]:
        picked = self._checked_item_ids()
        if not picked:
            theme.notify(self, "请选择物资",
                         "请先在「订单物资」里勾选至少一项再操作。", kind="warn")
            return None
        try:
            res = self.svc.ensure_scope(self.order_id, picked, cur.name())
        except Exception as exc:
            theme.notify(self, "排产失败", str(exc), kind="error")
            return None
        self._preset = set(picked)
        self._reload_items()
        return res

    def _do_approval(self):
        """v3.1.2：只生成/复用审批文档并登记，不再启动本机 Word"""
        res = self._ensure_scope()
        if not res:
            return
        sids = [s["id"] for s in res["schedules"]]
        try:
            path, created = self.svc.build_scope_approval(
                self.order_id, sids, res["scope"], cur.name())
        except Exception as exc:
            theme.notify(self, "生成失败", str(exc), kind="error")
            return
        self._touched = True
        self._reload_files()
        if created:
            if res["scope"] == SCOPE_ITEM:
                head = "已生成《生产排产审批单》并登记为审批附件"
            else:
                head = (f"已生成【{res['scope']}】审批文档"
                        f"（{len(sids)} 项物资共用一张）")
            tail = ("本版本不再自动启动本机 Word；可在下方「随单附件」双击，"
                    "或点「打开选中附件」编辑。")
        else:
            head = "该范围已有审批文档，未重复生成"
            tail = ("可在下方「随单附件」双击打开，或用「导入生产排产审批」"
                    "补充新文件。")
        theme.notify(self, "新建生产排产审批", f"{head}；{tail}",
                     kind="info", detail=path)

    def _do_import(self, *_args):
        """v3.1.2：一个入口导入任意类型文件，服务层按扩展名自动分类"""
        paths = theme.pick_paths(
            self, "导入生产排产审批（可多选 · 任意格式）", IMPORT_FILTER)
        if not paths:
            return
        res = self._ensure_scope()
        if not res:
            return
        sids = [s["id"] for s in res["schedules"]]
        try:
            result = self.svc.import_files(sids, paths, cur.name())
        except Exception as exc:
            theme.notify(self, "导入失败", str(exc), kind="error")
            return
        self._touched = True
        self._reload_files()
        kinds = "、".join(f"{k} {n} 个" for k, n in result["kinds"].items())
        scope_note = ("" if len(sids) == 1
                      else f"，登记到本批 {len(sids)} 张排产单（共用）")
        docs = self.svc.scope_approval_docs(sids)
        state = ("已存在 Word/Excel 审批文件，保存后本批物资默认【已排产】"
                 if docs else
                 "尚无 Word/Excel 审批文件，保存不会默认已排产")
        theme.notify(self, "导入完成",
                     f"已导入 {result['files']} 个文件（{kinds}）{scope_note}；{state}")

    def _open_file(self, *_):
        fid = self._selected_file_id()
        if fid is None:
            theme.notify(self, "请先选择", "请点选一行附件", kind="warn")
            return
        ok, msg = self.svc.open_file(fid)
        if not ok:
            theme.notify(self, "无法打开", msg, kind="warn")

    def _remove_file(self):
        fid = self._selected_file_id()
        if fid is None:
            theme.notify(self, "请先选择", "请点选一行附件", kind="warn")
            return
        if not theme.confirm(self, "移除附件", "确认移除这条附件登记？",
                             "合并/整单排产时该文件在多张排产单下共用，将一并移除；"
                             "物理文件也会被清理，不影响排产单与数量守恒。"):
            return
        n = self.svc.remove_file(fid, cur.name())
        self._touched = True
        self._reload_files()
        if n and n > 1:
            theme.notify(self, "已移除", f"共清理 {n} 条共用登记")

    # ── 保存 ──────────────────────────────────────────────

    def _save(self) -> bool:
        picked = self._checked_item_ids()
        if not picked:
            theme.notify(self, "请选择物资",
                         "请先在「订单物资」里勾选至少一项再保存。", kind="warn")
            return False
        try:
            res = self.svc.ensure_scope(self.order_id, picked, cur.name())
            sids = [s["id"] for s in res["schedules"]]
            remark = self.remark.toPlainText().strip()
            for sid in sids:
                self.svc.set_remark(sid, remark, cur.name())
                # v3.1.2：存在 Word/Excel 审批文件 → 保存即默认该物资
                # 已排产（自动补满可排量）；图片/图纸/PDF 不再触发
                self.svc.auto_fill_if_ready(sid, cur.name())
            if len(sids) > 1 and self.chk_fill.isChecked():
                self.svc.fill_scope(sids, cur.name())
            if len(sids) == 1:
                qty = float(self.spin_qty.value())
                base = float(self.svc.get(sids[0]).get("allocated_qty") or 0)
                if abs(qty - base) > 1e-9:
                    self.svc.set_allocated_qty(sids[0], qty, cur.name())
            self.svc.recheck_order(self.order_id, cur.name())
        except Exception as exc:
            theme.notify(self, "保存失败", str(exc), kind="error")
            return False
        return True

    def _finish(self):
        if self._save():
            self.accept()

    def reject(self):
        # 点右上角 × 时：只有真的动过才落库，避免「打开看一眼」就建出一堆排产单
        if not self._touched or self._save():
            super().reject()

    # ── 供调用方拼消息 ────────────────────────────────────

    def summary_text(self) -> str:
        picked = self._checked_item_ids()
        scope = scope_label(len(picked), len(self.items))
        scheds = self._checked_schedules()
        nums = "、".join(s["doc_no"] for s in scheds) or "未建单"
        return (f"{self.order['doc_no']} {scope}排产：{len(picked)} 项物资，"
                f"排产单 {nums}")

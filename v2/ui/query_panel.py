#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中间「查询窗口」组件 + 六视图元数据。

职责：按当前视图动态生成查询条件、收集 filters、给出命中数。
过滤的实际执行在主窗口的 _filtered() 与各 _refresh_*() 中，本组件不碰业务数据。
VIEWS 为纯数据，零依赖环境也可读取。
v2.9.1：界面收敛 —— 去掉顶部说明与底部口径长文；订单视图仅
关键字(模糊) + 订单项目编号 + 客户名称 + 下单时间 + 订单类型 + 订单状态 + 交货期。
"""
from typing import Callable, Dict, List, Optional

# ── 六视图元数据（顺序 = 右侧结果区 QStackedWidget 页序） ───────
VIEWS: List[Dict[str, object]] = [
    {
        "key": "bids", "no": "①", "name": "询报价 · 招投标",
        "result_title": "招投标项目", "detail_title": "投标明细",
        "desc": "统计招投标项目与报价数据；只有【中标】项目可生成内部订单并进入排产。",
        "placeholder": "项目号 / 项目名称 / 客户名称 / 招标编号",
        "date_label": "投标日期", "date_field": "bid_date",
        "status_sql": "SELECT DISTINCT status FROM bid_projects ORDER BY status",
        "fields": ["keyword", "status", "customer", "date", "won_only"],
        "policy": "招投标明细数量为实值，是内部订单的数量来源；对外报价 = 采购单价 ÷ 报价比率。",
    },
    {
        "key": "codes", "no": "②", "name": "询配码项目",
        "result_title": "询配码项目", "detail_title": "询配码明细",
        "desc": "独立域，与招投标零关联。询配码文件直接提交用户，用户按需分批下单。",
        "placeholder": "项目号 / 项目名称 / 客户名称 / 文件名",
        "date_label": "提交日期", "date_field": "submit_date",
        "status_sql": "SELECT DISTINCT status FROM code_projects ORDER BY status",
        "fields": ["keyword", "status", "customer", "date"],
        "policy": "★ 明细「参考数量」为虚值，仅统计展示；不参与下单、不参与排产、不设上限。",
    },
    {
        "key": "orders", "no": "③", "name": "订单管理",
        "result_title": "订单", "detail_title": "订单明细",
        "desc": "订单枢纽：订单头 8 字段 + 明细 9 字段；外部订单（一码多单）/ 内部订单（一中标一单）共用守恒规则。",
        "placeholder": "系统单号 / 订单项目编号 / 客户名称 / ERP订单编号",
        "date_label": "下单日期", "date_field": "order_date",
        "status_sql": "SELECT DISTINCT status FROM orders ORDER BY status",
        # v2.9.1：订单查询窗口按用户口径收敛 —— 仅六字段 + 关键字模糊；
        # labels 覆盖默认行标签（下单时间/订单状态/交货期…）
        "fields": ["keyword", "project_no", "customer", "date", "order_type",
                   "status", "due"],
        "labels": {"keyword": "关键字", "project_no": "订单项目编号",
                   "customer": "客户名称", "date": "下单时间",
                   "order_type": "订单类型", "status": "订单状态",
                   "due": "交货期"},
        "policy": "★ 明细「数量」= qty_ordered，是【唯一实值锚点】：Σ排产分配量 ≤ 订单实值；价税合计/物资数量/总价为金额与展示字段，不参与守恒。Q12：「物资数量」= 物资项目数（明细行数），Σ明细数量看「实值合计」列。右键明细某项 → 订单排产对话框（一个入口导入各类随单附件，存在 Word/Excel 审批文件保存即默认已排产）；全部物资排满时订单自动变「已排产」。",
    },
    {
        "key": "production", "no": "④", "name": "生产排产",
        "result_title": "生产排产项目", "detail_title": "排产关联（订单明细）",
        "desc": "排产项目关联的是【订单明细】，可挂中标项目作来源标签（标签不携带数量）。",
        "placeholder": "生产项目号 / 项目名称 / 车间",
        "date_label": "计划开工", "date_field": "plan_start",
        "status_sql": "SELECT DISTINCT status FROM production_projects ORDER BY status",
        "fields": ["keyword", "status", "date"],
        "policy": "关联时可指定分配量；超排由服务层硬拦截，解链后数量自动回补可排量。",
    },
    {
        "key": "trace", "no": "⑤", "name": "溯源穿透",
        "result_title": "溯源明细", "detail_title": "双击行反查领用",
        "desc": "正查 / 反查：排产 → 订单明细 → 询配码项目的哪一项，或中标项目的哪一项。",
        "placeholder": "订单号 / 物资名称 / 项目号",
        "date_label": "交期", "date_field": "required_date",
        "status_sql": None,
        "fields": ["keyword", "date", "order_type"],
        "policy": "溯源链只回答「来自哪里」，不携带数量；虚值列仅作对照，绝不参与计算。",
    },
    {
        "key": "overview", "no": "⑥", "name": "总览与数量守恒",
        "result_title": "订单明细可排量", "detail_title": "一码多单 · 订单批次",
        "desc": "全局 KPI、实值守恒一览、询配码项目 → 订单批次，以及一致性检查。",
        "placeholder": "订单号 / 物资名称 / 询配码项目号",
        "date_label": "交期", "date_field": "required_date",
        "status_sql": None,
        "fields": ["keyword", "date", "order_type"],
        "policy": "一致性检查覆盖：超排、来源混挂、孤立关联、一中标多内部订单四类异常。",
    },
]

VIEW_INDEX: Dict[str, int] = {v["key"]: i for i, v in enumerate(VIEWS)}
VIEW_BY_KEY: Dict[str, Dict[str, object]] = {v["key"]: v for v in VIEWS}

try:
    from PySide6.QtWidgets import (
        QFrame, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QComboBox,
        QCheckBox, QPushButton, QWidget, QLabel, QSizePolicy,
    )
    from PySide6.QtCore import Qt
    from .datefield import DatePicker          # v3.2.0：日期一律弹日历选择
    PYSIDE_AVAILABLE = True
except ImportError:
    PYSIDE_AVAILABLE = False

if PYSIDE_AVAILABLE:

    class QueryPanel(QFrame):
        """中间查询窗口：条件输入 + 查询/重置 + 命中数 + 口径提示"""

        def __init__(self, db, on_query: Callable, on_reset: Callable, parent=None,
                     presets=None, on_save_preset: Optional[Callable] = None,
                     on_apply_preset: Optional[Callable] = None,
                     on_delete_preset: Optional[Callable] = None):
            super().__init__(parent)
            self.db = db
            self._on_query = on_query
            self._on_reset = on_reset
            # P2-5：查询方案（可选注入，缺省时整行隐藏，不影响既有用法）
            self._presets = presets
            self._on_save_preset = on_save_preset
            self._on_apply_preset = on_apply_preset
            self._on_delete_preset = on_delete_preset
            self.setObjectName("Card")
            self.rows: Dict[str, QWidget] = {}
            self.widgets: Dict[str, QWidget] = {}
            self.view = VIEWS[0]
            self._build()

        # ── 构建 ─────────────────────────────────────────
        def _build(self):
            root = QVBoxLayout(self)
            root.setContentsMargins(16, 14, 16, 14)
            root.setSpacing(10)

            self.title = QLabel("查询条件")
            self.title.setObjectName("SectionTitle")
            root.addWidget(self.title)

            # P2-5：查询方案（保存常用条件，一键套用）
            self.preset_row = QWidget()
            ph = QHBoxLayout(self.preset_row)
            ph.setContentsMargins(0, 0, 0, 0)
            ph.setSpacing(6)
            plab = QLabel("方案")
            plab.setObjectName("Hint")
            plab.setFixedWidth(88)
            plab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.preset_box = QComboBox()
            self.preset_box.setToolTip("选择已保存的查询方案，自动套用其条件")
            self.preset_box.activated.connect(self._on_preset_picked)
            btn_save = QPushButton("存为方案…")
            btn_save.setToolTip("把左侧当前条件保存为方案，下次一键套用")
            btn_save.clicked.connect(lambda: self._on_save_preset and self._on_save_preset())
            btn_del = QPushButton("删除")
            btn_del.setToolTip("删除当前选中的查询方案（不影响业务数据）")
            btn_del.clicked.connect(
                lambda: self._on_delete_preset and self._on_delete_preset(self.current_preset_id()))
            ph.addWidget(plab)
            ph.addWidget(self.preset_box, 1)
            ph.addWidget(btn_save)
            ph.addWidget(btn_del)
            root.addWidget(self.preset_row)
            self.preset_row.setVisible(bool(self._presets))
            self.reload_presets()

            self.form_host = QWidget()
            form = QVBoxLayout(self.form_host)
            form.setContentsMargins(0, 2, 0, 2)
            form.setSpacing(8)

            self._add_row(form, "keyword", "关键字", self._line(""))
            self._add_row(form, "project_no", "项目编号",
                          self._line("订单项目编号（模糊）"))
            self._add_row(form, "customer", "客户名称", self._line("客户名称（模糊）"))
            self._add_row(form, "date", "日期范围", self._date_row())
            self._add_row(form, "order_type", "订单类型", self._combo())
            self._add_row(form, "status", "状态", self._combo())
            self._add_row(form, "due", "交期", self._combo())
            self._add_row(form, "options", "附加条件", self._options_row())
            root.addWidget(self.form_host)

            actions = QHBoxLayout()
            btn_query = QPushButton("查 询")
            btn_query.setObjectName("Primary")
            btn_query.clicked.connect(self._on_query)
            btn_reset = QPushButton("重 置")
            btn_reset.clicked.connect(self._reset)
            actions.addWidget(btn_query)
            actions.addWidget(btn_reset)
            actions.addStretch(1)
            root.addLayout(actions)

            self.hit = QLabel("")
            self.hit.setObjectName("Hint")
            root.addWidget(self.hit)
            root.addStretch(1)

            self.set_view("bids")

        def _line(self, placeholder: str) -> "QLineEdit":
            w = QLineEdit()
            w.setPlaceholderText(placeholder)
            w.setClearButtonEnabled(True)
            return w

        def _combo(self) -> "QComboBox":
            w = QComboBox()
            w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            return w

        def _date_row(self) -> QWidget:
            # v3.2.0：起/止各一行，避免两个「只读框 + 选择 + 清空」挤在半行里
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(5)
            d1 = DatePicker()
            d2 = DatePicker()
            d1.setPlaceholderText("起始日期（可留空）")
            d2.setPlaceholderText("截止日期（可留空）")
            for cap, picker in (("起", d1), ("止", d2)):
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(6)
                lab = QLabel(cap)
                lab.setObjectName("Hint")
                lab.setFixedWidth(22)
                row.addWidget(lab)
                row.addWidget(picker, 1)
                v.addLayout(row)
            self.widgets["date_from"] = d1
            self.widgets["date_to"] = d2
            return box

        def _options_row(self) -> QWidget:
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(12)
            won = QCheckBox("仅中标项目")
            cap = QCheckBox("仅剩余可排量 > 0")
            h.addWidget(won)
            h.addWidget(cap)
            h.addStretch(1)
            self.widgets["won_only"] = won
            self.widgets["has_capacity"] = cap
            return box

        def _add_row(self, form, key, caption, widget):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(9)
            lab = QLabel(caption)
            lab.setObjectName("Hint")
            lab.setFixedWidth(88)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            h.addWidget(lab)
            h.addWidget(widget, 1)
            form.addWidget(row)
            self.rows[key] = row
            if key not in ("options", "date"):
                self.widgets[key] = widget

        # ── 视图切换 ─────────────────────────────────────
        def set_view(self, key: str):
            self.view = VIEW_BY_KEY.get(key, VIEWS[0])
            v = self.view
            self.title.setText(f"{v['no']} {v['name']} · 查询")
            self.widgets["keyword"].setPlaceholderText("输入关键字…")
            self.widgets["keyword"].setToolTip(f"可检索：{v['placeholder']}")
            self.rows["date"].setToolTip(
                f"按{v['date_label']}过滤：点击日历选择起止日期，均可选留空")
            fields = set(v["fields"])  # type: ignore[arg-type]
            visible = {
                "keyword": True,
                "project_no": "project_no" in fields,
                "status": "status" in fields,
                "customer": "customer" in fields,
                "date": "date" in fields,
                "order_type": "order_type" in fields,
                "due": "due" in fields,
                "options": bool(fields & {"won_only", "has_capacity"}),
            }
            for name, row in self.rows.items():
                row.setVisible(visible[name])
            # v2.9.1：行标签按视图覆盖（订单视图 → 下单时间/订单状态/交货期…）
            labels = {"keyword": "关键字", "project_no": "项目编号",
                      "status": "状态", "customer": "客户名称", "date": "日期范围",
                      "order_type": "订单类型", "due": "交期"}
            labels.update(v.get("labels") or {})  # type: ignore[arg-type]
            for name, text in labels.items():
                row = self.rows.get(name)
                if row is not None:
                    row.findChild(QLabel).setText(text)
            self.widgets["won_only"].setVisible("won_only" in fields)
            self.widgets["has_capacity"].setVisible("has_capacity" in fields)
            status_box = self.widgets["status"]
            if "status" in fields:
                self._load_status(str(v["status_sql"]))
            else:
                status_box.clear()
                status_box.addItem("（全部）", "")
            type_box = self.widgets["order_type"]
            type_box.clear()
            type_box.addItem("（全部）", "")
            type_box.addItem("外部订单", "外部订单")
            type_box.addItem("内部订单", "内部订单")
            due_box = self.widgets["due"]
            due_box.clear()
            for key, label in (("", "（全部）"), ("overdue", "已逾期"),
                               ("soon", "7 天内到期"), ("normal", "未到期")):
                due_box.addItem(label, key)
            self.reload_presets()

        def _load_status(self, sql: Optional[str]):
            box = self.widgets["status"]
            box.blockSignals(True)
            box.clear()
            box.addItem("（全部）", "")
            if sql:
                for r in self.db.query(sql):
                    s = str(r["status"] or "")
                    if s:
                        box.addItem(s, s)
            box.blockSignals(False)

        # ── 查询方案（P2-5）──────────────────────────────
        def current_preset_id(self):
            return self.preset_box.currentData()

        def _on_preset_picked(self, index: int):
            pid = self.preset_box.itemData(index)
            if pid and self._on_apply_preset:
                self._on_apply_preset(pid)

        def reload_presets(self):
            """重建方案下拉（切视图、保存、删除后都要刷）"""
            if not self._presets or not hasattr(self, "preset_box"):
                return
            keep = self.preset_box.currentData()
            self.preset_box.blockSignals(True)
            self.preset_box.clear()
            self.preset_box.addItem("（不套用方案）", None)
            try:
                rows = self._presets.list(str(self.view["key"]))
            except Exception:
                rows = []
            for p in rows:
                self.preset_box.addItem(p["name"], p["id"])
            idx = self.preset_box.findData(keep)
            self.preset_box.setCurrentIndex(idx if idx >= 0 else 0)
            self.preset_box.blockSignals(False)

        def apply_filters(self, filters: Dict[str, object]):
            """把方案里的条件填回各控件（只动控件，不触发查询）"""
            self.clear()
            f = filters or {}
            self.widgets["keyword"].setText(str(f.get("keyword") or ""))
            self.widgets["project_no"].setText(str(f.get("project_no") or ""))
            self.widgets["customer"].setText(str(f.get("customer") or ""))
            self.widgets["date_from"].setText(str(f.get("date_from") or ""))
            self.widgets["date_to"].setText(str(f.get("date_to") or ""))
            for key in ("status", "order_type", "due"):
                box = self.widgets.get(key)
                if box is None:
                    continue
                idx = box.findData(str(f.get(key) or ""))
                box.setCurrentIndex(idx if idx >= 0 else 0)
            self.widgets["won_only"].setChecked(bool(f.get("won_only")))
            self.widgets["has_capacity"].setChecked(bool(f.get("has_capacity")))

        # ── 值 / 摘要 ────────────────────────────────────
        def clear(self):
            """清空全部条件控件（切换视图 / 重置时调用）"""
            for key in ("keyword", "project_no", "customer", "date_from", "date_to"):
                self.widgets[key].clear()
            for key in ("status", "order_type", "due"):
                box = self.widgets[key]
                box.blockSignals(True)
                box.setCurrentIndex(0)
                box.blockSignals(False)
            for key in ("won_only", "has_capacity"):
                self.widgets[key].setChecked(False)

        def _reset(self):
            # 由主窗口统一执行 clear + 刷新，保证菜单「重置条件」与按钮行为一致
            self._on_reset()

        def values(self) -> Dict[str, object]:
            # v2.9.1：按「当前视图字段集」判定是否取值，不再依赖 isVisible()
            # （离屏/未显示窗口 isVisible() 恒 False，会让条件悄悄丢失）
            fields = set(self.view["fields"])  # type: ignore[arg-type]

            def text(key: str, enabled: bool = True) -> str:
                if not enabled:
                    return ""
                return self.widgets[key].text().strip()

            date_vis = "date" in fields
            return {
                "keyword": text("keyword"),
                "project_no": text("project_no", "project_no" in fields),
                "status": (self.widgets["status"].currentData()
                           if "status" in fields else "") or "",
                "customer": text("customer", "customer" in fields),
                "date_from": text("date_from", date_vis),
                "date_to": text("date_to", date_vis),
                "order_type": (self.widgets["order_type"].currentData()
                               if "order_type" in fields else "") or "",
                "due": (self.widgets["due"].currentData() if "due" in fields else "") or "",
                "won_only": ("won_only" in fields and self.widgets["won_only"].isChecked()),
                "has_capacity": ("has_capacity" in fields and self.widgets["has_capacity"].isChecked()),
                "date_field": str(self.view["date_field"]) if date_vis else "",
            }

        def summary(self) -> str:
            f = self.values()
            parts = []
            if f["keyword"]:
                parts.append(f"关键字「{f['keyword']}」")
            if f.get("project_no"):
                parts.append(f"项目编号「{f['project_no']}」")
            if f["status"]:
                parts.append(f"状态「{f['status']}」")
            if f["customer"]:
                parts.append(f"客户「{f['customer']}」")
            if f["date_from"] or f["date_to"]:
                parts.append(f"日期 {f['date_from'] or '不限'} ~ {f['date_to'] or '不限'}")
            if f["order_type"]:
                parts.append(f"类型「{f['order_type']}」")
            if f["won_only"]:
                parts.append("仅中标")
            if f["has_capacity"]:
                parts.append("仅剩余可排量")
            if f.get("due"):
                parts.append({"overdue": "已逾期", "soon": "7 天内到期",
                              "normal": "未到期"}.get(str(f["due"]), str(f["due"])))
            return " · ".join(parts) if parts else "未设置条件 · 显示全部"

        def set_hit(self, count: int, total: int):
            self.hit.setText(f"命中 {count} 条 / 全库 {total} 条")

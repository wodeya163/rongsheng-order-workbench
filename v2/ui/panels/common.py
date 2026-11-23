#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口共享常量与小工具（P2-2 拆分产物）

· 视图元数据、左侧动作清单、表格→实体映射：纯数据，零依赖环境可导入
· fill_table 等：只被 ui/panels/* 与主窗口使用，不承载业务逻辑
由 main_window_v2.py 原样迁出，符号名保持不变。
"""
import sys
from pathlib import Path

try:                                   # 纯数据部分不依赖 Qt，零依赖环境也能导入
    from PySide6.QtWidgets import (
        QWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QHBoxLayout,
        QLabel, QSplitter, QHeaderView,
    )
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    _QT_OK = True
    from .. import theme
except ImportError:                    # pragma: no cover - 无 Qt 环境
    _QT_OK = False

# 纯数据：零依赖环境下导入本模块也安全
VIEW_NAMES = ["① 询报价·招投标", "② 询配码项目", "③ 订单管理",
              "④ 生产排产", "⑤ 溯源穿透", "⑥ 总览与数量守恒"]
VIEW_KEYS = ["bids", "codes", "orders", "production", "trace", "overview"]

# 左侧操作列表（v3.2.0 名称正式化）：
#   (分组标题, 视图 key, [(显示名, 方法名, 视图覆盖, 说明), ...])
#   · 显示名：短、规范、无编号/星号/口语括号；完整口径放第 4 元素 tooltip
#   · 视图覆盖：执行前要切换到的视图；None = 不切视图（单据维护组）
#   · 方法名一律不变，保证导航 / 菜单 / 主界面快捷按钮三处共用同一份数据源
ACTION_GROUPS = [
    ("询报价与招投标", "bids", [
        ("新增招标项目", "_new_bid", "bids", "登记新的招投标项目（项目号、名称、客户、投标日期等）"),
        ("录入投标明细", "_add_bid_item", "bids", "为选中的招标项目逐条录入投标物资明细"),
        ("登记报价", "_mark_quoted", "bids", "将选中项目状态登记为「已报价」"),
        ("登记中标", "_mark_won", "bids", "将选中项目状态登记为「中标」"),
        ("登记落标", "_mark_lost", "bids", "将选中项目状态登记为「未中标」"),
        ("生成内部订单", "_gen_internal", "bids", "按中标项目生成唯一一张内部订单"),
        ("中标转排产", "_won_to_production", "bids", "中标项目生成内部订单并进入生产排产"),
        ("导入投标明细", "_import_bid_items", "bids", "从 CSV / Excel 批量导入投标明细"),
        ("导出招投标数据", "_export_bids", "bids", "导出当前招投标项目与投标明细"),
    ]),
    ("询配码项目", "codes", [
        ("新增询配码项目", "_new_code", "codes", "登记新的询配码项目（独立域，与招投标零关联）"),
        ("录入询配码明细", "_add_code_item", "codes", "逐条录入询配码明细；参考数量为虚值，仅统计"),
        ("提交配码文件", "_submit_code", "codes", "登记提交日期与文件，将询配码文件提交用户"),
        ("查看订单批次", "_view_batches", "codes", "查看一个询配码项目对应的多张外部订单"),
        ("导入配码明细", "_import_code_items", "codes", "从 CSV / Excel 批量导入询配码明细"),
        ("下载导入模板", "_download_code_template", "codes", "下载询配码明细导入模板"),
        ("导出配码数据", "_export_codes", "codes", "导出询配码项目与明细"),
    ]),
    ("订单管理", "orders", [
        ("新增订单", "_new_order", "orders", "先创建订单头（明细不再必填），随后选中该单补录明细"),
        # v3.5.3：对齐询配码项目范式，建单与明细录入 / 导入彻底分离
        ("录入订单明细", "_add_order_items", "orders",
         "选中订单后用 9 字段表格逐条或批量录入物资明细，一次可保存多行"),
        ("导入订单明细", "_import_order_items", "orders",
         "选中订单后从 CSV / Excel 导入 9 列明细；只需物资名称 + 数量，表头可不在首行"),
        ("编辑订单", "_edit_order", "orders", "修改选中订单头与明细或删除整单，保存即自动落库"),
        ("单据识别建单", "_add_order", "orders", "从图片 / 表格 / PDF / Word 识别订单信息并建单"),
        ("导入订单", "_import_orders", "orders", "从 CSV / Excel 批量导入订单"),
        ("下载订单模板", "_download_order_template", "orders", "下载订单导入模板"),
        ("确认订单", "_confirm_order", "orders", "将选中订单状态置为「已确认」"),
        ("重算订单合计", "_reset_totals", "orders", "清除手工合计锁定，按明细重新汇总"),
        ("订单排产", "_order_schedule", "orders", "打开订单排产对话框，按勾选物资分项排产"),
        ("合并排产", "_merge_order_schedule", "orders", "明细多选 ≥2 项合并排产，共用一张审批文档"),
        ("整单排产", "_whole_order_schedule", "orders", "整单全部物资统一排产，共用一张审批文档"),
        # v3.4.0：右键菜单同源入口，一屏查看 / 编辑 / 更改该单全部排产文件
        ("排产文件", "_view_schedule_files", "orders",
         "查看选中订单的全部排产文件，可打开编辑、重命名、替换、增删附件"),
        ("查看订单明细", "_view_order_items", "orders", "查看选中订单的物资明细"),
        ("导出订单数据", "_export_orders", "orders", "导出订单主表与明细"),
        # v3.3.0 交期自动提醒：重要操作同时进导航 / 菜单 / 更多操作
        ("临期订单", "_show_due_reminders", "orders",
         "查看距交货期 10 天内及逾期未发货的订单，可一键标记已发货"),
        ("标记已发货", "_mark_order_shipped", "orders",
         "把选中订单状态改为「已发货」，立即停止该单交期提醒"),
        ("交期提醒设置", "_reminder_settings", "orders",
         "设置提前天数、逾期上限、启动弹窗与托盘气泡"),
        # v3.5.0：一单多供应商的采购价 / 销售价汇总、利润率与柱线组合图
        ("经营分析", "_profit_analysis", "orders",
         "按订单 / 供应商 / 月份查看销售额、采购额、毛利与利润率柱线组合图，可导出 PNG 与 Excel"),
    ]),
    # 第三元素 = 该动作执行前要切换到的视图（默认用分组的 view）
    ("生产排产", "production", [
        # v3.4.0 采购合同管理：一个窗口完成生成 / 导入 / 编辑 / 状态 / 附件
        ("采购合同管理", "_purchase_contracts", "production",
         "打开采购合同管理窗口，完成合同生成、导入、编辑与台账管理"),
        ("生成采购合同", "_generate_purchase_contract", "orders",
         "按选中订单生成一份 Word 采购合同模板并登记入台账"),
        ("导入采购合同", "_import_purchase_contract", "orders",
         "从资源管理器导入已有采购合同文件并登记入台账"),
        ("订单排产", "_order_schedule", "orders", "切到订单视图，打开订单排产对话框"),
        ("排产单清单", "_view_order_schedules", "production", "查看全部订单排产单及随单附件"),
        ("中标转排产", "_won_to_production", "production", "中标项目生成内部订单并进入生产排产"),
        ("关联订单明细", "_link_item", "production", "把订单明细按可排量关联到生产项目"),
        ("解除关联", "_unlink", "production", "解除选中排产关联，数量自动回补可排量"),
        ("推进项目状态", "_advance_project", "production", "按状态机推进生产项目状态"),
        ("导出排产数据", "_export_production", "production", "导出生产项目与排产关联"),
    ]),
    ("溯源与总览", "trace", [
        ("一致性检查", "_consistency_check", "trace", "检查超排、来源混挂、孤立关联等四类异常"),
        ("导出溯源数据", "_export_trace", "trace", "导出当前溯源明细表"),
        ("导出全系统", "_export_all", "trace", "导出全库业务表为 Excel"),
        ("下载导入模板", "_download_templates", "trace", "一次下载三类导入模板"),
    ]),
    # 单据维护：view=None 表示不切换视图，作用于「右侧当前点选的那张表」
    ("单据维护", None, [
        ("编辑记录", "_edit_selected", None, "编辑右侧当前点选的主结果或明细行"),
        ("作废单据", "_void_selected", None, "作废选中单据，状态置「作废」并保留数据"),
        ("删除记录", "_delete_selected", None, "删除选中记录（受下游引用约束，逐行留痕）"),
        ("审计留痕", "_show_audit", None, "查看选中记录的审计留痕"),
    ]),
]

# 主操作界面（右侧结果区）快捷栏：每个视图的高频动作，按方法名复用 ACTION_GROUPS
# 的显示名与说明，避免三处文案各写一份。二元组第二项：
#   None = 沿用 ACTION_GROUPS 的视图覆盖；"" = 强制不切视图；字符串 = 强制切到该视图
PRIMARY_ACTIONS = {
    "bids": [("_new_bid", None), ("_add_bid_item", None),
             ("_mark_won", None), ("_gen_internal", None)],
    "codes": [("_new_code", None), ("_add_code_item", None),
              ("_submit_code", None), ("_import_code_items", None)],
    # 「单据识别建单」已在结果区头部有独立按钮，快捷栏不重复摆同一入口
    "orders": [("_new_order", None), ("_add_order_items", None),
               ("_import_order_items", None), ("_import_orders", None),
               ("_order_schedule", None), ("_view_schedule_files", None),
               ("_edit_order", None), ("_whole_order_schedule", None),
               ("_profit_analysis", None)],
    "production": [("_purchase_contracts", None),
                   ("_order_schedule", "orders"),
                   ("_view_order_schedules", None), ("_link_item", None),
                   ("_advance_project", None)],
    "trace": [("_consistency_check", ""), ("_export_trace", None)],
    "overview": [("_consistency_check", ""), ("_export_all", None)],
}


def find_action(fn_name: str, prefer_view: str = "") -> tuple:
    """按方法名从 ACTION_GROUPS 找 (显示名, 方法名, 视图, 说明)；找不到返回空元组"""
    fallback = ()
    for title, key, acts in ACTION_GROUPS:
        for act in acts:
            if act[1] != fn_name:
                continue
            entry = (act[0], act[1], act[2] if len(act) > 2 else key,
                     act[3] if len(act) > 3 else "")
            if key == prefer_view:
                return entry
            if not fallback:
                fallback = entry
    return fallback

# 右侧表格 → 维护域实体。主结果与明细下钻都可作为编辑/作废/删除的作用对象。
# 键用 id(table) 而不是 Qt 对象本身，避免 PySide6 包装对象的哈希差异。
TABLE_ENTITIES = [
    ("tbl_bids", "bid_project"), ("tbl_bid_items", "bid_item"),
    ("tbl_codes", "code_project"), ("tbl_code_items", "code_item"),
    ("tbl_orders", "order"), ("tbl_order_items", "order_item"),
    ("tbl_projects", "production_project"), ("tbl_links", "production_link"),
    ("tbl_capacity", "order_item"),
]

_GOOD_STATUS = {"中标", "已提交", "已确认", "已完成", "已下单", "部分下单", "已排产", "生产中"}
_WARN_STATUS = {"未中标", "作废", "草稿", "待排", "待下单", "已关闭"}

# 打包后（sys.frozen）数据落在 exe 同目录 data/，便于整体拷贝与备份
DEFAULT_DB = ((Path(sys.executable).parent / "data" / "erp.db")
              if getattr(sys, "frozen", False)
              else (Path(__file__).resolve().parents[2] / "data" / "erp.db"))


if _QT_OK:

    def fill_table(table, headers, rows, ids=None, stretch_col: int = -1) -> None:
        """填充只读表格；ids 存入第 0 列的 UserRole，供动作取当前实体 ID"""
        table.blockSignals(True)
        table.clear()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                text = "" if value is None else str(value)
                item = QTableWidgetItem("—" if text == "" else text)
                if j == 0 and ids is not None and i < len(ids):
                    item.setData(Qt.UserRole, ids[i])
                # 主单号列加粗；状态列按语义着色；数值列右对齐
                if j == 0:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setForeground(QColor(theme.BRAND_DARK))
                elif isinstance(value, str) and value == "已逾期":
                    # P2-6：逾期红色加粗、临期橙色，与业务状态色区分开
                    item.setForeground(QColor(theme.DANGER))
                    f = item.font(); f.setBold(True); item.setFont(f)
                elif isinstance(value, str) and value == "临期":
                    item.setForeground(QColor(theme.WARN))
                elif isinstance(value, str) and value in _GOOD_STATUS:
                    item.setForeground(QColor(theme.OK))
                    f = item.font(); f.setBold(True); item.setFont(f)
                elif isinstance(value, str) and value in _WARN_STATUS:
                    item.setForeground(QColor(theme.WARN))
                elif isinstance(value, (int, float)):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(i, j, item)
        table.resizeColumnsToContents()
        for c in range(table.columnCount()):
            if table.columnWidth(c) > 320:
                table.setColumnWidth(c, 320)
        if stretch_col >= 0 and table.columnCount() > stretch_col:
            table.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
        table.blockSignals(False)


    def make_readonly_table() -> "QTableWidget":
        return theme.style_table(QTableWidget())


    def selected_id(table) -> object | None:
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return item.data(Qt.UserRole) if item else None


    def select_id(table, entity_id) -> None:
        if entity_id is None:
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == entity_id:
                table.selectRow(row)
                return


    def _brief_json(text, limit: int = 88) -> str:
        """审计快照在表格里只显示摘要，避免超长 JSON 撑爆列宽"""
        if not text:
            return "—"
        s = str(text).replace("\n", " ")
        return s if len(s) <= limit else s[:limit] + "…"


    def _titled_table(caption: str, table) -> "QWidget":
        """结果区里的一块「小标题 + 表格」，上下两块组成主表/明细下钻"""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        lab = QLabel(caption)
        lab.setObjectName("KpiLabel")
        v.addWidget(lab)
        v.addWidget(table, 1)
        return box


    def _result_page(main_caption: str, main_table, detail_caption: str, detail_table) -> "QWidget":
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Vertical)
        split.addWidget(_titled_table(main_caption, main_table))
        split.addWidget(_titled_table(detail_caption, detail_table))
        split.setSizes([360, 250])
        v.addWidget(split, 1)
        return page


    class _StackTabs:
        """兼容旧离屏冒烟脚本的 view 适配器（count/tabText/currentIndex）"""

        def __init__(self, stack):
            self._stack = stack

        def count(self):
            return self._stack.count()

        def tabText(self, index):
            return VIEW_NAMES[index] if 0 <= index < len(VIEW_NAMES) else ""

        def currentIndex(self):
            return self._stack.currentIndex()

        def setCurrentIndex(self, index):
            self._stack.setCurrentIndex(index)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口
"""
import os
from datetime import datetime
from typing import Dict, Any, List

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QStatusBar, QToolBar, QMenu, QFileDialog, QMessageBox, QComboBox,
    QLineEdit,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
# PySide6 (Qt6) 将 QAction 从 QtWidgets 迁移到 QtGui；兼容旧版回退
try:
    from PySide6.QtGui import QAction
except ImportError:
    from PySide6.QtWidgets import QAction

from .db import DatabaseManager
from . import calc
from .paths import get_data_dir, set_data_dir, reset_data_dir, is_custom_data_dir
from .excel_io import export_products, import_products
from .dialogs import ProductDialog, ProductDetailDialog, ProjectSummaryDialog


class MainWindow(QMainWindow):
    """主窗口"""

    def __init__(self):
        super().__init__()
        self.db = DatabaseManager()
        self.init_ui()
        self.load_products()

    def init_ui(self):
        self.setWindowTitle("产品询报价管理系统 v1.5.0")
        self.setGeometry(100, 100, 1200, 720)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.create_toolbar()

        search_group = QGroupBox("搜索条件")
        search_layout = QGridLayout()

        search_layout.addWidget(QLabel("产品名称："), 0, 0)
        self.search_name = QLineEdit()
        self.search_name.setPlaceholderText("输入产品名称")
        search_layout.addWidget(self.search_name, 0, 1)

        search_layout.addWidget(QLabel("产品分类："), 0, 2)
        self.search_category = QLineEdit()
        self.search_category.setPlaceholderText("输入分类")
        search_layout.addWidget(self.search_category, 0, 3)

        search_layout.addWidget(QLabel("规格型号："), 1, 0)
        self.search_spec = QLineEdit()
        self.search_spec.setPlaceholderText("输入规格")
        search_layout.addWidget(self.search_spec, 1, 1)

        search_layout.addWidget(QLabel("投标项目："), 1, 2)
        self.search_project = QLineEdit()
        self.search_project.setPlaceholderText("输入项目")
        search_layout.addWidget(self.search_project, 1, 3)

        search_layout.addWidget(QLabel("报价人："), 2, 0)
        self.search_quoter = QLineEdit()
        self.search_quoter.setPlaceholderText("输入报价人")
        search_layout.addWidget(self.search_quoter, 2, 1)

        search_layout.addWidget(QLabel("是否中标："), 2, 2)
        self.search_is_won = QComboBox()
        self.search_is_won.addItems(["全部", "是", "否"])
        search_layout.addWidget(self.search_is_won, 2, 3)

        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self.load_products)
        search_layout.addWidget(self.search_btn, 0, 4, 3, 1)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self.clear_search)
        search_layout.addWidget(self.clear_btn, 0, 5, 3, 1)

        search_group.setLayout(search_layout)
        main_layout.addWidget(search_group)

        self.table = QTableWidget()
        self.table.setColumnCount(13)
        self.table.setHorizontalHeaderLabels([
            "产品名称", "规格型号", "分类", "对外报价",
            "报价比率", "询价数量", "询价单位", "采购价格",
            "投标项目", "是否中标", "报价人", "产品描述", "操作"
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        main_layout.addWidget(self.table)

        summary_group = QGroupBox("价格汇总")
        summary_layout = QHBoxLayout()

        self.summary_count = QLabel("产品总数：0")
        self.summary_count.setStyleSheet("font-weight: bold; color: #333;")
        summary_layout.addWidget(self.summary_count)

        self.summary_purchase = QLabel("采购总额：0.00 元")
        self.summary_purchase.setStyleSheet("font-weight: bold; color: #d35400;")
        summary_layout.addWidget(self.summary_purchase)

        self.summary_quote = QLabel("报价总额：0.00 元")
        self.summary_quote.setStyleSheet("font-weight: bold; color: #27ae60;")
        summary_layout.addWidget(self.summary_quote)

        self.summary_profit = QLabel("毛利：0.00 元")
        self.summary_profit.setStyleSheet("font-weight: bold; color: #2980b9;")
        summary_layout.addWidget(self.summary_profit)

        self.summary_profit_rate = QLabel("毛利率：0.00%")
        self.summary_profit_rate.setStyleSheet("font-weight: bold; color: #8e44ad;")
        summary_layout.addWidget(self.summary_profit_rate)

        summary_layout.addStretch()
        summary_group.setLayout(summary_layout)
        main_layout.addWidget(summary_group)

        self.statusBar().showMessage("就绪")

    def create_toolbar(self):
        toolbar = self.addToolBar("操作")
        toolbar.setIconSize(QSize(24, 24))

        add_action = QAction("➕ 新增产品", self)
        add_action.triggered.connect(self.add_product)
        toolbar.addAction(add_action)

        edit_action = QAction("✏️ 编辑产品", self)
        edit_action.triggered.connect(self.edit_product)
        toolbar.addAction(edit_action)

        delete_action = QAction("🗑️ 删除产品", self)
        delete_action.triggered.connect(self.delete_product)
        toolbar.addAction(delete_action)

        toolbar.addSeparator()

        import_action = QAction("📥 导入Excel", self)
        import_action.triggered.connect(self.import_excel)
        toolbar.addAction(import_action)

        project_action = QAction("📊 投标项目汇总", self)
        project_action.triggered.connect(self.show_project_summary)
        toolbar.addAction(project_action)

        export_action = QAction("📤 导出Excel", self)
        export_action.triggered.connect(self.export_excel)
        toolbar.addAction(export_action)

        refresh_action = QAction("🔄 刷新", self)
        refresh_action.triggered.connect(self.load_products)
        toolbar.addAction(refresh_action)

        toolbar.addSeparator()

        data_dir_action = QAction("📁 数据目录设置", self)
        data_dir_action.triggered.connect(self.change_data_dir)
        toolbar.addAction(data_dir_action)

    def _collect_filters(self) -> Dict[str, str]:
        filters = {}
        if self.search_name.text():
            filters['name'] = self.search_name.text()
        if self.search_category.text():
            filters['category'] = self.search_category.text()
        if self.search_spec.text():
            filters['spec'] = self.search_spec.text()
        if self.search_project.text():
            filters['project'] = self.search_project.text()
        if self.search_quoter.text():
            filters['quoter'] = self.search_quoter.text()
        won = self.search_is_won.currentText()
        if won and won != "全部":
            filters['is_won'] = won
        return filters

    def load_products(self):
        filters = self._collect_filters()
        products = self.db.search_products(filters)

        self.table.setRowCount(len(products))
        for row, product in enumerate(products):
            pp = calc.safe_float(product.get('purchase_price'))
            qr = calc.safe_float(product.get('quote_ratio'), 1.0)
            qp = pp / qr if qr else 0.0

            self.table.setItem(row, 0, QTableWidgetItem(product.get('name', '')))
            self.table.setItem(row, 1, QTableWidgetItem(product.get('spec', '')))
            self.table.setItem(row, 2, QTableWidgetItem(product.get('category', '')))
            self.table.setItem(row, 3, QTableWidgetItem(f"{qp:.2f}"))
            self.table.setItem(row, 4, QTableWidgetItem(f"{qr:.2f}"))
            self.table.setItem(row, 5, QTableWidgetItem(str(calc.safe_int(product.get('inquiry_qty')))))
            self.table.setItem(row, 6, QTableWidgetItem(product.get('inquiry_unit', '') or ''))
            self.table.setItem(row, 7, QTableWidgetItem(f"{pp:.2f}"))
            self.table.setItem(row, 8, QTableWidgetItem(product.get('project', '')))
            self.table.setItem(row, 9, QTableWidgetItem(product.get('is_won', '') or ''))
            self.table.setItem(row, 10, QTableWidgetItem(product.get('quoter', '')))
            self.table.setItem(row, 11, QTableWidgetItem(product.get('description', '')))
            self.table.item(row, 0).setData(Qt.UserRole, product['id'])

            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(0, 0, 0, 0)
            view_btn = QPushButton("查看")
            view_btn.clicked.connect(lambda checked, pid=product['id']: self.view_product(pid))
            btn_layout.addWidget(view_btn)
            edit_btn = QPushButton("编辑")
            edit_btn.clicked.connect(lambda checked, pid=product['id']: self.edit_product(pid))
            btn_layout.addWidget(edit_btn)
            self.table.setCellWidget(row, 12, btn_widget)

        summary = self.db.get_summary(filters)
        self.summary_count.setText(f"产品总数：{summary['count']}")
        self.summary_purchase.setText(f"采购总额：{summary['total_purchase']:.2f} 元")
        self.summary_quote.setText(f"报价总额：{summary['total_quote']:.2f} 元")
        self.summary_profit.setText(f"毛利：{summary['profit']:.2f} 元")
        self.summary_profit_rate.setText(f"毛利率：{summary['profit_rate']:.2f}%")

        self.statusBar().showMessage(f"已加载 {len(products)} 条产品记录  |  数据目录：{get_data_dir()}")

    def clear_search(self):
        self.search_name.clear()
        self.search_category.clear()
        self.search_spec.clear()
        self.search_project.clear()
        self.search_quoter.clear()
        self.search_is_won.setCurrentText("全部")
        self.load_products()

    def add_product(self):
        dialog = ProductDialog(self)
        if dialog.exec():
            self.load_products()
            self.statusBar().showMessage("产品添加成功")

    def edit_product(self, product_id: int = None):
        if not product_id:
            selected = self.table.selectedItems()
            if not selected:
                QMessageBox.warning(self, "警告", "请先选择要编辑的产品")
                return
            row = selected[0].row()
            product_id = self.table.item(row, 0).data(Qt.UserRole)

        dialog = ProductDialog(self, product_id)
        if dialog.exec():
            self.load_products()
            self.statusBar().showMessage("产品更新成功")

    def view_product(self, product_id: int):
        ProductDetailDialog(self, product_id).exec()

    def delete_product(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "警告", "请先选择要删除的产品")
            return

        row = selected[0].row()
        product_id = self.table.item(row, 0).data(Qt.UserRole)
        product_name = self.table.item(row, 0).text()

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除产品 '{product_name}' 吗？\n此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.db.delete_product(product_id)
            self.load_products()
            self.statusBar().showMessage(f"产品 '{product_name}' 已删除")

    def import_excel(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "导入Excel", "", "Excel文件 (*.xlsx *.xls)"
        )
        if not file_path:
            return
        try:
            result = import_products(self.db, file_path)
            msg = f"导入完成：成功 {result['inserted']} 条，跳过 {result['skipped']} 条。"
            if result['errors']:
                msg += "\n\n前几条问题：\n" + "\n".join(result['errors'][:5])
            QMessageBox.information(self, "导入结果", msg)
            self.load_products()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", f"导入过程中发生错误：\n{str(e)}")

    def show_project_summary(self):
        ProjectSummaryDialog(self).exec()

    def export_excel(self):
        filters = self._collect_filters()
        products = self.db.search_products(filters)
        if not products:
            QMessageBox.information(self, "提示", "没有数据可导出")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出Excel",
            f"产品数据_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            "Excel文件 (*.xlsx)"
        )
        if not file_path:
            return

        try:
            export_products(products, file_path)
            QMessageBox.information(self, "成功", f"数据已导出到：\n{file_path}")
            self.statusBar().showMessage(f"Excel导出完成：{len(products)} 条记录")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"导出过程中发生错误：\n{str(e)}")

    # ── 数据目录设置 ──────────────────────────────────────────

    def change_data_dir(self):
        """打开数据目录设置对话框"""
        current_dir = get_data_dir()
        custom = is_custom_data_dir()

        info_text = (
            f"当前数据目录：\n{current_dir}\n\n"
            f"模式：{'自定义目录' if custom else '默认目录'}\n\n"
            "点击「选择目录」可更换数据保存位置，已有数据将自动迁移到新目录。\n"
            "点击「恢复默认」可恢复到程序默认位置。"
        )

        msg = QMessageBox(self)
        msg.setWindowTitle("数据目录设置")
        msg.setText(info_text)
        msg.setIcon(QMessageBox.Icon.Information)

        select_btn = msg.addButton("选择目录", QMessageBox.ButtonRole.AcceptRole)
        reset_btn = msg.addButton("恢复默认", QMessageBox.ButtonRole.ResetRole)
        cancel_btn = msg.addButton("取消", QMessageBox.ButtonRole.RejectRole)

        msg.exec()

        if msg.clickedButton() is select_btn:
            self._select_new_data_dir(current_dir)
        elif msg.clickedButton() is reset_btn:
            self._reset_data_dir()

    def _select_new_data_dir(self, current_dir: str):
        """让用户选择新数据目录并执行切换"""
        new_dir = QFileDialog.getExistingDirectory(
            self, "选择数据保存目录", current_dir
        )
        if not new_dir:
            return

        # 检查目标目录是否已有数据库
        new_db = os.path.join(new_dir, 'products.db')
        if os.path.exists(new_db):
            reply = QMessageBox.question(
                self, "目录已有数据",
                f"所选目录中已存在数据库文件（products.db）。\n\n"
                f"选择「是」：直接使用该目录中的现有数据\n"
                f"选择「否」：取消操作",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            # 使用已有数据 — 仅切换目录，不迁移
            result = set_data_dir(new_dir, migrate=False)
        else:
            result = set_data_dir(new_dir, migrate=True)

        if result['success']:
            # 重新初始化数据库连接
            self.db = DatabaseManager()
            self.load_products()
            QMessageBox.information(
                self, "切换成功",
                f"{result['message']}\n\n"
                f"旧目录：{result.get('old_dir', '')}\n"
                f"新目录：{result['new_dir']}"
            )

    def _reset_data_dir(self):
        """恢复默认数据目录"""
        result = reset_data_dir()
        if result['success']:
            self.db = DatabaseManager()
            self.load_products()
            QMessageBox.information(
                self, "已恢复默认",
                f"{result['message']}\n\n"
                f"旧目录：{result.get('old_dir', '')}\n"
                f"新目录：{result['new_dir']}"
            )

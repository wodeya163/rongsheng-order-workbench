#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对话框：新增/编辑、详情、投标项目汇总
"""
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QFileDialog, QMessageBox, QHeaderView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from .db import DatabaseManager
from .paths import attachments_dir
from . import calc
from .excel_io import export_project_summary


QUOTE_RATIO_MIN = 0.60
QUOTE_RATIO_MAX = 0.85
QUOTE_RATIO_STEP = 0.05
QUOTE_RATIO_DEFAULT = 0.85

# 常用询价单位（下拉可选项，同时允许手动输入自定义单位）
COMMON_INQUIRY_UNITS = [
    "个", "件", "台", "套", "只", "根", "块", "片", "条",
    "米", "厘米", "千克", "克", "吨", "升", "毫升",
    "箱", "批", "组", "捆", "卷", "把", "袋", "瓶", "罐", "车",
]

# 是否中标选项
WON_CHOICES = ["", "是", "否"]


class ProductDialog(QDialog):
    """产品新增/编辑对话框（含输入校验，防崩溃）"""

    def __init__(self, parent=None, product_id: int = None):
        super().__init__(parent)
        self.product_id = product_id
        self.image_path: Optional[str] = None
        self.drawing_path: Optional[str] = None
        self.attachments_dir = attachments_dir()
        self.init_ui()
        if product_id:
            self.load_product()

    def init_ui(self):
        self.setWindowTitle("编辑产品" if self.product_id else "新增产品")
        self.setMinimumWidth(520)

        layout = QVBoxLayout()
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("必填")
        form.addRow("产品名称：", self.name_edit)

        self.spec_edit = QLineEdit()
        form.addRow("规格型号：", self.spec_edit)

        self.category_edit = QLineEdit()
        form.addRow("产品分类：", self.category_edit)

        # 报价比率
        ratio_layout = QHBoxLayout()
        self.quote_ratio_spin = QDoubleSpinBox()
        self.quote_ratio_spin.setRange(QUOTE_RATIO_MIN, QUOTE_RATIO_MAX)
        self.quote_ratio_spin.setSingleStep(QUOTE_RATIO_STEP)
        self.quote_ratio_spin.setValue(QUOTE_RATIO_DEFAULT)
        self.quote_ratio_spin.setDecimals(2)
        ratio_layout.addWidget(self.quote_ratio_spin)

        self.ratio_up_btn = QPushButton("+0.05")
        self.ratio_up_btn.clicked.connect(self.increase_ratio)
        self.ratio_up_btn.setMaximumWidth(60)
        ratio_layout.addWidget(self.ratio_up_btn)

        self.ratio_down_btn = QPushButton("-0.05")
        self.ratio_down_btn.clicked.connect(self.decrease_ratio)
        self.ratio_down_btn.setMaximumWidth(60)
        ratio_layout.addWidget(self.ratio_down_btn)
        form.addRow("报价比率：", ratio_layout)

        # 询价数量（整数控件，杜绝非法输入）
        self.inquiry_qty_spin = QSpinBox()
        self.inquiry_qty_spin.setRange(0, 9999999)
        self.inquiry_qty_spin.setValue(0)
        form.addRow("询价数量：", self.inquiry_qty_spin)

        # 采购价格（浮点控件）
        self.purchase_price_spin = QDoubleSpinBox()
        self.purchase_price_spin.setRange(0, 1_000_000_000)
        self.purchase_price_spin.setDecimals(2)
        self.purchase_price_spin.setValue(0.00)
        form.addRow("采购价格(元)：", self.purchase_price_spin)

        # 询价单位（下拉选择常用单位，可手动输入自定义单位）
        self.inquiry_unit_combo = QComboBox()
        self.inquiry_unit_combo.setEditable(True)
        self.inquiry_unit_combo.addItems(COMMON_INQUIRY_UNITS)
        self.inquiry_unit_combo.setCurrentText("个")
        form.addRow("询价单位：", self.inquiry_unit_combo)

        self.project_edit = QLineEdit()
        form.addRow("投标项目：", self.project_edit)

        # 是否中标（下拉选择，用于中标分析）
        self.is_won_combo = QComboBox()
        self.is_won_combo.addItems(WON_CHOICES)
        self.is_won_combo.setCurrentText("")
        form.addRow("是否中标：", self.is_won_combo)

        self.quoter_edit = QLineEdit()
        form.addRow("报价人：", self.quoter_edit)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("产品描述")
        form.addRow("产品描述：", self.description_edit)

        image_layout = QHBoxLayout()
        self.image_label = QLabel("未选择图片")
        self.image_label.setStyleSheet("border: 1px solid #ccc; padding: 5px;")
        image_layout.addWidget(self.image_label)
        self.image_btn = QPushButton("上传图片")
        self.image_btn.clicked.connect(self.upload_image)
        image_layout.addWidget(self.image_btn)
        form.addRow("产品图片：", image_layout)

        drawing_layout = QHBoxLayout()
        self.drawing_label = QLabel("未选择图纸")
        self.drawing_label.setStyleSheet("border: 1px solid #ccc; padding: 5px;")
        drawing_layout.addWidget(self.drawing_label)
        self.drawing_btn = QPushButton("上传图纸")
        self.drawing_btn.clicked.connect(self.upload_drawing)
        drawing_layout.addWidget(self.drawing_btn)
        form.addRow("产品图纸：", drawing_layout)

        layout.addLayout(form)

        button_layout = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.save_btn.clicked.connect(self.save_product)
        button_layout.addWidget(self.save_btn)

        self.save_continue_btn = QPushButton("保存并继续")
        self.save_continue_btn.clicked.connect(lambda: self.save_product(continue_mode=True))
        button_layout.addWidget(self.save_continue_btn)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def upload_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择产品图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if file_path:
            self.image_path = self.copy_attachment(file_path, "img")
            self.image_label.setText(os.path.basename(file_path))

    def upload_drawing(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择产品图纸", "",
            "图纸文件 (*.pdf *.dwg *.dxf *.cad);;所有文件 (*.*)"
        )
        if file_path:
            self.drawing_path = self.copy_attachment(file_path, "dwg")
            self.drawing_label.setText(os.path.basename(file_path))

    def copy_attachment(self, src_path: str, prefix: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = os.path.splitext(src_path)[1]
        new_filename = f"{prefix}_{timestamp}{ext}"
        dst_path = self.attachments_dir / new_filename
        shutil.copy2(src_path, dst_path)
        return str(dst_path)

    def load_product(self):
        db = DatabaseManager()
        product = db.get_product(self.product_id)
        if not product:
            return

        self.name_edit.setText(product.get('name', ''))
        self.spec_edit.setText(product.get('spec', ''))
        self.category_edit.setText(product.get('category', ''))
        self.quote_ratio_spin.setValue(calc.safe_float(product.get('quote_ratio'), QUOTE_RATIO_DEFAULT))
        self.inquiry_qty_spin.setValue(calc.safe_int(product.get('inquiry_qty')))
        self.purchase_price_spin.setValue(calc.safe_float(product.get('purchase_price')))
        self.inquiry_unit_combo.setCurrentText(product.get('inquiry_unit', '') or '个')
        self.project_edit.setText(product.get('project', ''))
        self.is_won_combo.setCurrentText(product.get('is_won', '') or '')
        self.quoter_edit.setText(product.get('quoter', ''))
        self.description_edit.setText(product.get('description', ''))

        if product.get('image_path'):
            self.image_path = product['image_path']
            self.image_label.setText(os.path.basename(product['image_path']))
        if product.get('drawing_path'):
            self.drawing_path = product['drawing_path']
            self.drawing_label.setText(os.path.basename(product['drawing_path']))

    def increase_ratio(self):
        current = self.quote_ratio_spin.value()
        if current + QUOTE_RATIO_STEP <= QUOTE_RATIO_MAX:
            self.quote_ratio_spin.setValue(current + QUOTE_RATIO_STEP)

    def decrease_ratio(self):
        current = self.quote_ratio_spin.value()
        if current - QUOTE_RATIO_STEP >= QUOTE_RATIO_MIN:
            self.quote_ratio_spin.setValue(current - QUOTE_RATIO_STEP)

    def clear_form(self):
        self.name_edit.clear()
        self.spec_edit.clear()
        self.category_edit.clear()
        self.quote_ratio_spin.setValue(QUOTE_RATIO_DEFAULT)
        self.inquiry_qty_spin.setValue(0)
        self.purchase_price_spin.setValue(0.00)
        self.inquiry_unit_combo.setCurrentText("个")
        self.project_edit.clear()
        self.is_won_combo.setCurrentText("")
        self.quoter_edit.clear()
        self.description_edit.clear()
        self.image_label.setText("未选择图片")
        self.drawing_label.setText("未选择图纸")
        self.image_path = None
        self.drawing_path = None
        self.product_id = None

    def save_product(self, continue_mode=False):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "警告", "产品名称不能为空")
            return

        product_data = {
            'name': name,
            'spec': self.spec_edit.text().strip(),
            'category': self.category_edit.text().strip(),
            'quote_ratio': self.quote_ratio_spin.value(),
            'inquiry_qty': self.inquiry_qty_spin.value(),
            'purchase_price': self.purchase_price_spin.value(),
            'inquiry_unit': self.inquiry_unit_combo.currentText().strip(),
            'project': self.project_edit.text().strip(),
            'is_won': self.is_won_combo.currentText().strip(),
            'quoter': self.quoter_edit.text().strip(),
            'description': self.description_edit.text().strip(),
            'image_path': self.image_path,
            'drawing_path': self.drawing_path,
        }

        db = DatabaseManager()
        if self.product_id:
            db.update_product(self.product_id, product_data)
        else:
            db.add_product(product_data)

        if continue_mode:
            self.clear_form()
            self.name_edit.setFocus()
        else:
            self.accept()


class ProductDetailDialog(QDialog):
    """产品详情（含图片预览）"""

    def __init__(self, parent, product_id: int):
        super().__init__(parent)
        self.setWindowTitle("产品详情")
        self.setMinimumWidth(600)

        product = DatabaseManager().get_product(product_id)
        if not product:
            self.close()
            return

        layout = QVBoxLayout()

        info_table = QTableWidget(12, 2)
        info_table.setHorizontalHeaderLabels(["字段", "值"])
        info_table.verticalHeader().setVisible(False)
        info_table.setEditTriggers(QTableWidget.NoEditTriggers)

        pp = calc.safe_float(product.get('purchase_price'))
        qr = calc.safe_float(product.get('quote_ratio'), 1.0)
        qp = pp / qr if qr else 0.0

        rows = [
            ("产品名称", product.get('name', '')),
            ("规格型号", product.get('spec', '')),
            ("产品分类", product.get('category', '')),
            ("对外报价", f"{qp:.2f} 元"),
            ("报价比率", str(qr)),
            ("询价数量", str(calc.safe_int(product.get('inquiry_qty')))),
            ("询价单位", product.get('inquiry_unit', '') or '—'),
            ("采购价格", f"{pp:.2f} 元"),
            ("投标项目", product.get('project', '')),
            ("是否中标", product.get('is_won', '') or '未填写'),
            ("报价人", product.get('quoter', '')),
            ("创建时间", str(product.get('created_at', '')).split('.')[0]),
        ]
        for i, (label, value) in enumerate(rows):
            info_table.setItem(i, 0, QTableWidgetItem(label))
            info_table.setItem(i, 1, QTableWidgetItem(str(value)))
        info_table.resizeColumnsToContents()
        layout.addWidget(info_table)

        if product.get('description'):
            desc = QLabel(f"产品描述：{product['description']}")
            desc.setWordWrap(True)
            layout.addWidget(desc)

        image_path = product.get('image_path')
        if image_path and os.path.exists(image_path):
            try:
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    pixmap = pixmap.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    img_label = QLabel()
                    img_label.setPixmap(pixmap)
                    img_label.setAlignment(Qt.AlignCenter)
                    layout.addWidget(QLabel("产品图片："))
                    layout.addWidget(img_label)
            except Exception:  # noqa: BLE001
                pass

        if product.get('drawing_path'):
            layout.addWidget(QLabel(f"产品图纸：{os.path.basename(product['drawing_path'])}"))

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)
        self.setLayout(layout)


class ProjectSummaryDialog(QDialog):
    """投标项目汇总视图"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("投标项目汇总")
        self.setMinimumWidth(700)
        self.setMinimumHeight(400)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("按投标项目统计产品数量、采购/报价总额与毛利，并分析中标情况："))

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(
            ["投标项目", "产品数量", "已中标", "未中标", "中标率(%)",
             "采购总额(元)", "报价总额(元)", "毛利(元)", "毛利率(%)"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        self._load()

        btn_layout = QHBoxLayout()
        export_btn = QPushButton("导出汇总Excel")
        export_btn.clicked.connect(self.export_summary)
        btn_layout.addWidget(export_btn)
        btn_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def _load(self):
        groups = DatabaseManager().get_project_summary()
        self.table.setRowCount(len(groups))
        for row, g in enumerate(groups):
            self.table.setItem(row, 0, QTableWidgetItem(g['project']))
            self.table.setItem(row, 1, QTableWidgetItem(str(g['count'])))
            self.table.setItem(row, 2, QTableWidgetItem(str(g['won_count'])))
            self.table.setItem(row, 3, QTableWidgetItem(str(g['lost_count'])))
            self.table.setItem(row, 4, QTableWidgetItem(f"{g['win_rate']:.1f}"))
            self.table.setItem(row, 5, QTableWidgetItem(f"{g['total_purchase']:.2f}"))
            self.table.setItem(row, 6, QTableWidgetItem(f"{g['total_quote']:.2f}"))
            self.table.setItem(row, 7, QTableWidgetItem(f"{g['profit']:.2f}"))
            self.table.setItem(row, 8, QTableWidgetItem(f"{g['profit_rate']:.2f}"))

    def export_summary(self):
        groups = DatabaseManager().get_project_summary()
        if not groups:
            QMessageBox.information(self, "提示", "没有数据可导出")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出项目汇总",
            f"投标项目汇总_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            "Excel文件 (*.xlsx)"
        )
        if not file_path:
            return
        try:
            export_project_summary(groups, file_path)
            QMessageBox.information(self, "成功", f"汇总已导出到：\n{file_path}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(e))

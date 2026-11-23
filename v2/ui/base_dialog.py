#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弹窗外壳（v3.0 从 dialogs.py 抽出）

dialogs.py 与 schedule_dialog.py 都要用同一个 ThemedDialog：
拆成独立模块后，两者都从这里继承，isinstance 判断依旧成立，
也避免 dialogs ↔ schedule_dialog 之间的循环导入。
"""
from PySide6.QtWidgets import QDialog

from . import theme


class ThemedDialog(QDialog):
    """标题头 / 白卡片内容 / 底部按钮区，三段式与主窗口三栏同语言"""

    def __init__(self, parent, title: str, subtitle: str = "",
                 width: int = 760, height: int = 520, badge: str = theme.BADGE):
        super().__init__(parent)
        body_layout, footer_layout = theme.dialog_shell(
            self, title, subtitle, badge, width, height)
        self.body_layout = body_layout
        self.footer_layout = footer_layout

    def add_buttons(self, ok_text: str = "确定", cancel_text: str = "取消",
                    on_ok=None, on_cancel=None, show_cancel: bool = True):
        box = theme.dialog_buttons(self, ok_text, cancel_text, show_cancel)
        box.accepted.connect(on_ok or self.accept)
        box.rejected.connect(on_cancel or self.reject)
        self.footer_layout.addWidget(box)
        # 所有子控件（含按钮）已就位后刷新一次 QSS，让 ID 选择器生效
        theme.polish_tree(self)
        return box

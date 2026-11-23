#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单盈亏动作（ProfitActionsMixin · v3.5.0）

主窗口骨架有 <900 行的架构守卫，业务动作一律放动作层。
经营分析窗口只从 domains/profit.py 取数，本 Mixin 不算任何金额。
"""
from typing import Optional


class ProfitActionsMixin:

    def _profit_analysis(self, order_id: Optional[int] = None) -> str:
        """订单管理 / 生产排产 →「经营分析」：柱线组合图 + 汇总表 + 导出"""
        from ..profit_dialog import ProfitDialog

        dlg = ProfitDialog(self, self.db)
        dlg.exec()
        return dlg.summary_text()

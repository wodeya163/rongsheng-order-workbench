#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单盈亏柱线组合图（v3.5.0）

分组柱 = 销售额 / 采购额 / 毛利（左轴，含税金额）；
折线   = 利润率（右轴，百分比）。利润率不出数（含税口径混用）时该点留空不连。

★ 为什么自绘而不用 QtCharts / matplotlib：
  冻结包当前把 matplotlib 明确 EXCLUDES，PySide6.QtCharts 的 pyd 与
  Qt6Charts.dll 也没有被 PyInstaller 收集（本机有、dist 里没有）。自绘走
  Qt 基础模块，零新增依赖、零中文字体配置、包体不增，导出 PNG 直接 grab()。
  浅色主题下所有背景/网格/文字均为浅底深字，颜色与 ui/theme.py 同语系。
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QPainter, QPen)
from PySide6.QtWidgets import QToolTip, QWidget

C_SALES = QColor("#378ADD")      # 销售额 · 蓝
C_PURCHASE = QColor("#EF9F27")   # 采购额 · 琥珀
C_PROFIT = QColor("#D85A30")     # 正毛利 · 暖红（国内盈利用暖色）
C_LOSS = QColor("#639922")       # 负毛利 · 冷绿
C_RATE = QColor("#0F6E56")       # 利润率折线 · 青
C_GRID = QColor("#E3E8F0")
C_AXIS = QColor("#94A3B8")
C_TEXT = QColor("#334155")
C_SUB = QColor("#64748B")
C_BG = QColor("#FFFFFF")

SERIES = (("sales_total", "销售额", C_SALES),
          ("purchase_total", "采购额", C_PURCHASE))

_CN_FAMILIES = ("Microsoft YaHei", "Microsoft YaHei UI", "SimHei",
                "SimSun", "Noto Sans CJK SC")
_CN_FONT_READY = False


def _ensure_cn_font() -> None:
    """QPainter 自绘不读 QSS 的 font-family。

    Windows 平台正常运行时字体数据库里本来就有微软雅黑；但 offscreen 平台
    不注册系统字体（实测 families() 里一个中文字体都没有），中文会画成方块。
    这里在缺失时从系统字体目录补注册一次，保证离屏回归与真实界面一致。
    """
    global _CN_FONT_READY
    if _CN_FONT_READY:
        return
    _CN_FONT_READY = True
    try:
        if any(f in QFontDatabase.families() for f in _CN_FAMILIES):
            return
        if os.name != "nt":
            return
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc"):
            p = fonts / name
            if p.is_file():
                QFontDatabase.addApplicationFont(str(p))
                return
    except Exception:                                    # noqa: BLE001
        pass


def _chart_font(widget: QWidget) -> QFont:
    _ensure_cn_font()
    font = QFont(widget.font())
    avail = QFontDatabase.families()
    for family in _CN_FAMILIES:
        if family in avail:
            font.setFamily(family)
            break
    font.setPointSize(9)
    return font


def _nice_top(value: float) -> float:
    """把最大值向上取整到 1/2/5 × 10^n，轴刻度好看"""
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for m in (1, 2, 2.5, 5, 10):
        if value <= base * m:
            return base * m
    return base * 10


def _money_axis(v: float) -> str:
    if abs(v) >= 10000:
        return f"{v / 10000:g}万"
    if abs(v) >= 1000:
        return f"{v / 1000:g}千"
    return f"{v:g}"


class ProfitChart(QWidget):
    """零依赖柱线组合图；set_rows() 喂数据，grab() 即导出 PNG"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: List[Dict[str, Any]] = []
        self._hover = -1
        self._groups: List[QRectF] = []
        self.setMinimumHeight(320)
        self.setMouseTracking(True)
        self.setObjectName("ProfitChart")

    def set_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        self.rows = [dict(r) for r in rows]
        self._hover = -1
        self.update()

    def export_png(self, path: str) -> str:
        ok = self.grab().save(path, "PNG")
        if not ok:
            raise OSError(f"图表导出失败：{path}")
        return path

    # ── 交互 ────────────────────────────────────────────────

    def mouseMoveEvent(self, event):  # noqa: N802
        idx = -1
        for i, rect in enumerate(self._groups):
            if rect.contains(event.position()):
                idx = i
                break
        if idx != self._hover:
            self._hover = idx
            self.update()
        if 0 <= idx < len(self.rows):
            r = self.rows[idx]
            rate = r.get("profit_rate")
            rate_text = (f"{rate:.2f}%" if rate is not None
                         else "不出数（含税口径不一致）")
            text = (f"{r.get('label', '')}\n"
                    f"销售额（含税）：¥{r.get('sales_total', 0):,.2f}\n"
                    f"采购额（含税）：¥{r.get('purchase_total', 0):,.2f}\n"
                    f"毛利：¥{r.get('gross_profit', 0):,.2f}\n"
                    f"利润率：{rate_text}")
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hover = -1
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    # ── 绘制 ────────────────────────────────────────────────

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), C_BG)
        w, h = self.width(), self.height()
        left, right, top, bottom = 82, 64, 54, 62
        plot = QRectF(left, top, max(10, w - left - right),
                      max(10, h - top - bottom))
        font = _chart_font(self)
        p.setFont(font)
        self._legend(p, w)

        if not self.rows:
            p.setPen(C_SUB)
            p.drawText(plot, Qt.AlignCenter,
                       "暂无数据：请先登记订单与已签订的采购合同，并在「合同物资分配」页指定物资")
            p.end()
            return

        values = [float(r.get(k) or 0) for r in self.rows for k in
                  ("sales_total", "purchase_total", "gross_profit")]
        vmax = _nice_top(max([abs(v) for v in values] + [1.0]))
        vmin = -_nice_top(max([-min(values), 0.0])) if min(values) < 0 else 0.0
        span = vmax - vmin or 1.0

        def money_y(v: float) -> float:
            return plot.bottom() - (float(v) - vmin) / span * plot.height()

        rates = [float(r["profit_rate"]) for r in self.rows
                 if r.get("profit_rate") is not None]
        rmin = min([0.0] + rates) - 5 if rates else 0.0
        rmax = max([0.0] + rates) + 5 if rates else 100.0
        rspan = rmax - rmin or 100.0

        def rate_y(v: float) -> float:
            return plot.bottom() - (float(v) - rmin) / rspan * plot.height()

        # 网格 + 左右轴刻度
        p.setPen(QPen(C_GRID, 1))
        for i in range(6):
            v = vmin + span * i / 5
            y = money_y(v)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(C_SUB)
            p.drawText(QRectF(0, y - 9, left - 8, 18),
                       Qt.AlignRight | Qt.AlignVCenter, _money_axis(v))
            rv = rmin + rspan * i / 5
            p.drawText(QRectF(plot.right() + 6, y - 9, right - 8, 18),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{rv:.0f}%")
            p.setPen(QPen(C_GRID, 1))
        p.setPen(QPen(C_AXIS, 1))
        p.drawLine(QPointF(plot.left(), plot.top()),
                   QPointF(plot.left(), plot.bottom()))
        p.drawLine(QPointF(plot.right(), plot.top()),
                   QPointF(plot.right(), plot.bottom()))
        y0 = money_y(0)
        p.setPen(QPen(C_AXIS, 1.2))
        p.drawLine(QPointF(plot.left(), y0), QPointF(plot.right(), y0))

        n = len(self.rows)
        group_w = plot.width() / n
        bar_w = max(2.0, min(26.0, group_w / 4.4))
        gap = max(1.0, bar_w * 0.16)
        self._groups = []
        for i, r in enumerate(self.rows):
            cx = plot.left() + group_w * (i + 0.5)
            group_rect = QRectF(plot.left() + group_w * i, plot.top(),
                                group_w, plot.height())
            self._groups.append(group_rect)
            if i == self._hover:
                p.fillRect(group_rect, QColor("#F1F5FB"))
            base = cx - (3 * bar_w + 2 * gap) / 2
            for j, (key, _label, color) in enumerate(SERIES):
                v = float(r.get(key) or 0)
                rect = QRectF(base + j * (bar_w + gap), min(y0, money_y(v)),
                              bar_w, abs(money_y(v) - y0))
                rect.setHeight(max(1.0, rect.height()))
                p.fillRect(rect, color)
            profit = float(r.get("gross_profit") or 0)
            rect = QRectF(base + 2 * (bar_w + gap), min(y0, money_y(profit)),
                          bar_w, abs(money_y(profit) - y0))
            rect.setHeight(max(1.0, rect.height()))
            p.fillRect(rect, C_LOSS if profit < 0 else C_PROFIT)
            # X 轴标签：过密时隔位显示，过长截断
            label = str(r.get("label") or "—")
            if n <= 14 or i % max(1, math.ceil(n / 14)) == 0:
                fm = p.fontMetrics()
                if fm.horizontalAdvance(label) > group_w - 6:
                    while label and fm.horizontalAdvance(label + "…") > group_w - 6:
                        label = label[:-1]
                    label += "…"
                p.setPen(C_TEXT if i == self._hover else C_SUB)
                p.drawText(QRectF(group_rect.left(), plot.bottom() + 6,
                                  group_w, 20),
                           Qt.AlignHCenter | Qt.AlignTop, label)

        # 利润率折线（None 断点不连线）
        p.setPen(QPen(C_RATE, 2))
        points: List[QPointF] = []
        last_x = None
        for i, r in enumerate(self.rows):
            rate = r.get("profit_rate")
            if rate is None:
                last_x = None
                continue
            x = plot.left() + group_w * (i + 0.5)
            y = rate_y(float(rate))
            if last_x is not None:
                p.drawLine(QPointF(last_x, _last_y), QPointF(x, y))
            points.append(QPointF(x, y))
            last_x, _last_y = x, y
        for pt in points:
            p.setBrush(C_RATE)
            p.drawEllipse(pt, 3.2, 3.2)
            p.setBrush(Qt.NoBrush)
        p.setPen(C_SUB)
        p.drawText(QRectF(0, h - 17, w, 16), Qt.AlignCenter,
                   "左轴：含税金额（元）　右轴：利润率（%）　柱：销售额 / 采购额 / 毛利")
        p.end()

    def _legend(self, p: QPainter, width: float) -> None:
        items = [("销售额", C_SALES), ("采购额", C_PURCHASE),
                 ("毛利（亏损为绿）", C_PROFIT), ("利润率", C_RATE)]
        x = 12.0
        for name, color in items:
            p.fillRect(QRectF(x, 16, 12, 10), color)
            p.setPen(C_TEXT)
            r = p.fontMetrics().boundingRect(name)
            p.drawText(QRectF(x + 17, 11, r.width() + 8, 20),
                       Qt.AlignLeft | Qt.AlignVCenter, name)
            x += r.width() + 40

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交期自动提醒 · 主窗口动作（v3.3.0 · DueActionsMixin）

单独成模块的原因：主窗口骨架有「保持精简」的架构守卫（拆分前 2118 行，
现在要求 < 900 行）。提醒的扫描、去重、托盘、清单/设置弹窗都属于动作层，
只通过 self.db / self.due_badge / self._due_prefs 与主窗口协作，
不反向引用 main_window_v2（避免循环导入）。

链路：
  启动 → _init_due_reminder() → 徽标刷新 + 托盘 + 2.5s 自动检查 + 每小时复查
       → _auto_due_check() → 指纹去重 → DueReminderDialog
       → 「标记已发货」→ duereminder.mark_shipped（状态机 + 审计）
       → 立刻重算，该单从清单和徽标里消失
"""
from __future__ import annotations

import os

from ... import APP_TITLE                       # v3.4.3 品牌单一数据源
from .. import theme
from ...core import applog
from ...core import current_user as cur
from ...core import duereminder, reminder_prefs


def _headless_ui() -> bool:
    """离屏自检 / 自动化时不弹自动提醒窗口（徽标与扫描照常工作）"""
    if os.environ.get("PQS_NO_DUE_POPUP", "").strip() == "1":
        return True
    return os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in (
        "offscreen", "minimal", "minimalegl")


class DueActionsMixin:

    def _init_due_reminder(self):
        """启动挂载：徽标刷新 + 托盘气泡 + 2.5 秒自动检查 + 每小时复查"""
        from PySide6.QtCore import QTimer

        self._refresh_due_badge()
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            icon = theme.app_icon()
            if (self._due_prefs.get("tray") and icon is not None
                    and not _headless_ui()
                    and QSystemTrayIcon.isSystemTrayAvailable()):
                tray = QSystemTrayIcon(icon, self)
                tray.setToolTip(f"{APP_TITLE} · 交期提醒")
                tray.messageClicked.connect(self._show_due_reminders)
                tray.show()
                self._due_tray = tray
        except Exception:                                   # noqa: BLE001
            self._due_tray = None
        QTimer.singleShot(2500, self._auto_due_check)
        self._due_timer = QTimer(self)
        self._due_timer.setInterval(3600 * 1000)
        self._due_timer.timeout.connect(self._auto_due_check)
        self._due_timer.start()

    def _refresh_due_badge(self):
        """扫描一次全库，刷新状态栏常驻徽标；异常绝不影响主流程"""
        try:
            prefs = self._due_prefs
            if not prefs.get("enabled"):
                self._due_report = {
                    "rows": [], "long_overdue": [], "summary": {},
                    "days": prefs["days"], "signature": ""}
                text, level = "交期提醒：已关闭", "off"
            else:
                rep = duereminder.scan(
                    self.db, days=prefs["days"],
                    overdue_max_days=prefs["overdue_max_days"])
                self._due_report = rep
                s = rep["summary"]
                n = int(s.get("total") or 0)
                lo = int(s.get("long_overdue") or 0)
                text = f"交期提醒：{n} 单"
                if lo:
                    text += f"（长期逾期 {lo}）"
                level = "danger" if n else "ok"
            self.due_badge.setText(text)
            self.due_badge.setProperty("level", level)
            self.due_badge.setToolTip(
                "点击查看临期订单清单、标记已发货或调整提醒设置")
            self.due_badge.style().unpolish(self.due_badge)
            self.due_badge.style().polish(self.due_badge)
        except Exception:                                   # noqa: BLE001
            applog.get_logger("ui").exception("交期提醒徽标刷新失败")

    def _auto_due_check(self):
        """启动 / 每小时复查：按指纹去重弹清单，并发一条托盘气泡"""
        try:
            self._refresh_due_badge()
            rep = self._due_report
            sig = rep.get("signature") or ""
            if not sig:
                return
            if self._due_tray is not None and self._due_prefs.get("tray"):
                try:
                    from PySide6.QtWidgets import QSystemTrayIcon
                    self._due_tray.showMessage(
                        "交期提醒", duereminder.notice_text(rep),
                        QSystemTrayIcon.Information, 8000)
                except Exception:                           # noqa: BLE001
                    pass
            if _headless_ui():
                return
            if not reminder_prefs.should_popup(self._due_prefs, sig):
                return
            reminder_prefs.mark_popped(self._due_prefs, sig)
            reminder_prefs.save(self._ui_state_dir(), self._due_prefs)
            self._show_due_reminders(auto=True)
        except Exception:                                   # noqa: BLE001
            applog.get_logger("ui").exception("交期提醒自动检查失败")

    def _show_due_reminders(self, auto: bool = False):
        """状态栏徽标 / 菜单 / 导航 / 托盘气泡共用的清单入口"""
        from ..due_dialog import DueReminderDialog

        self._refresh_due_badge()
        dlg = DueReminderDialog(
            self, self._due_report, auto=auto,
            on_mark_shipped=self._due_mark_shipped,
            on_view_order=self._due_view_order,
            on_settings=self._due_open_settings,
            on_snooze=self._due_snooze)
        return dlg.exec()

    def _due_mark_shipped(self, order_id):
        """清单里一键发货：走状态机 + 审计，刷新后立即从清单移除"""
        o = duereminder.mark_shipped(self.db, order_id,
                                     cur.name(self.db.data_dir))
        self.refresh_all()
        self._refresh_due_badge()
        self.status_label.setText(
            f"{o['doc_no']} 已标记「已发货」，不再进行交期提醒")
        return self._due_report

    def _due_view_order(self, order_id):
        """切到订单视图并按系统单号过滤，避免分页里找不到那张单"""
        row = self.db.get_by_id("orders", int(order_id))
        doc_no = (row or {}).get("doc_no") or ""
        self.switch_view("orders", refresh=False)
        if doc_no:
            self.query_panel.apply_filters({"keyword": doc_no})
        self.filters = self.query_panel.values()
        self.page_offsets["orders"] = 0
        self.refresh_all()

    def _due_open_settings(self):
        from ..due_dialog import ReminderSettingsDialog

        dlg = ReminderSettingsDialog(self, self._due_prefs)
        if dlg.exec():
            self._due_prefs.update(dlg.values())
            reminder_prefs.save(self._ui_state_dir(), self._due_prefs)
            self._refresh_due_badge()
        return self._due_report

    def _due_snooze(self):
        reminder_prefs.snooze_today(self._due_prefs)
        reminder_prefs.save(self._ui_state_dir(), self._due_prefs)
        self.status_label.setText("今日不再弹出交期提醒（状态栏徽标仍显示数量）")

    def _reminder_settings(self):
        return self._due_open_settings()

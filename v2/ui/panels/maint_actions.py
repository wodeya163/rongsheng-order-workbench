#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⑤ 溯源 / ⑥ 维护与数据安全 动作（MaintActionsMixin）

含备份恢复、附件体检、一致性检查、单据编辑/作废/删除、审计与操作人。
本模块由 main_window_v2.py 拆分而来（P2-2），方法实现逐字迁移、未改行为；
跨模块协作一律走 self.xxx；禁止反向引用主窗口模块（会形成循环导入）。
"""
from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel, QComboBox,
    QHeaderView, QSplitter, QLineEdit, QFrame, QStatusBar,
    QTreeWidget, QTreeWidgetItem, QStackedWidget, QGroupBox, QScrollArea,
    QCheckBox, QSizePolicy, QAbstractItemView, QDialog, QMenu,
)  # noqa: F401
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QColor

from .. import dialogs, theme, query_panel
from ...core.db import Database, now
from ...core import models, excelio, applog
from ...core import query as qbuild
from ...core import current_user as cur
from ...core import fields as fdefs
from ...core import order_fields as ofdefs
from ...domains import (BidService, PartCodeService, OrderService,
                        ProductionService, TraceService, MaintenanceService,
                        OrderScheduleService)
from .common import (VIEW_NAMES, VIEW_KEYS, ACTION_GROUPS, TABLE_ENTITIES,
                     fill_table, make_readonly_table, selected_id, select_id,
                     _brief_json, _titled_table, _result_page)



class MaintActionsMixin:
    # ── ⑤ 溯源动作 ───────────────────────────────────────

    def _refresh_trace_entities(self):
        scope = self.trace_scope.currentData()
        current = self.trace_entity.currentData()
        self.trace_entity.blockSignals(True)
        self.trace_entity.clear()
        if scope == "production":
            rows = self.db.query("SELECT id, doc_no, title FROM production_projects ORDER BY id")
            label = lambda r: f"{r['doc_no']}  {r['title']}"
        elif scope == "code":
            rows = self.db.query("SELECT id, doc_no, project_name AS title FROM code_projects ORDER BY id")
            label = lambda r: f"{r['doc_no']}  {r['title']}"
        else:
            rows = self.db.query("SELECT id, doc_no, project_name AS title FROM bid_projects "
                                 "WHERE status = '中标' ORDER BY id")
            label = lambda r: f"{r['doc_no']}  {r['title']}"
        self.trace_entity.addItem("（全部）", None)
        for r in rows:
            self.trace_entity.addItem(label(r), r["id"])
        if current is not None:
            idx = self.trace_entity.findData(current)
            if idx >= 0:
                self.trace_entity.setCurrentIndex(idx)
        self.trace_entity.blockSignals(False)
        self.refresh_trace()

    def refresh_trace(self):
        scope = self.trace_scope.currentData()
        eid = self.trace_entity.currentData()
        self._trace_ids = []
        if scope == "production":
            if eid is None:
                rows = self.db.query(
                    """SELECT pp.doc_no AS project_no, pl.order_no_snapshot AS order_no,
                              pl.order_type_snapshot AS order_type,
                              pl.order_line_snapshot AS order_line,
                              pl.item_name_snapshot AS item_name, oi.qty_ordered,
                              pl.allocated_qty, pl.source_type_snapshot AS source_type,
                              pl.item_code_snapshot AS code_item,
                              pl.bid_line_snapshot AS bid_line,
                              pl.code_no_snapshot AS code_no,
                              pl.qty_reference_snapshot AS qty_reference,
                              bp.doc_no AS bid_no, oi.id AS order_item_id
                       FROM production_links pl
                       JOIN production_projects pp ON pp.id = pl.project_id
                       JOIN order_items oi ON oi.id = pl.order_item_id
                       LEFT JOIN bid_projects bp ON bp.id = pp.source_bid_project_id
                       ORDER BY pp.id, pl.id""")
            else:
                rows = self.trace.from_production(eid)
                for r in rows:
                    r.setdefault("order_no", r["order_no_snapshot"])
                    r["order_type"] = r["order_type_snapshot"]
                    r["order_line"] = r["order_line_snapshot"]
                    r["item_name"] = r["item_name_snapshot"]
                    r["source_type"] = r["source_type_snapshot"]
                    r["code_item"] = r["item_code_snapshot"] or r["bid_line_snapshot"]
                    r["code_no"] = r["code_no"] or r["code_no_snapshot"]
                    r["bid_no"] = r["bid_project_no"] or r["source_bid_project_no"]
                    r["qty_reference"] = r["qty_reference"] if r["qty_reference"] is not None \
                        else r["qty_reference_snapshot"]
            headers = ["生产项目", "订单号", "订单类型", "明细行", "物资名称",
                       "订单实值", "排产分配量", "来源类型", "来源项", "询配码项目",
                       "虚值(仅对照)", "中标项目"]
            data = [(r.get("project_no"), r.get("order_no"), r.get("order_type"),
                     r.get("order_line"), r.get("item_name"), r.get("qty_ordered"),
                     r.get("allocated_qty"), r.get("source_type"), r.get("code_item") or "—",
                     r.get("code_no") or "—", r.get("qty_reference") if r.get("qty_reference") is not None else "—",
                     r.get("bid_no") or "—") for r in rows]
            self._trace_ids = [r.get("order_item_id") for r in rows]
            hint = "每行回答：这个排产项目来自哪张订单的哪一项，再追到询配码项目/项或中标项目/项。"
        elif scope == "code":
            rows = []
            for cp in self.db.query("SELECT * FROM code_projects ORDER BY id"):
                if eid is not None and cp["id"] != eid:
                    continue
                for r in self.trace.from_code_project(cp["id"]):
                    r["project_no"] = cp["doc_no"]
                    rows.append(r)
            headers = ["询配码项目", "批次", "订单号", "订单状态", "明细行", "物资名称",
                       "订单实值", "已排量", "来源项", "虚值(仅对照)"]
            data = [(r["project_no"], r["batch_seq"], r["order_no"], r["order_status"],
                     r["line_no"], r["item_name"], r["qty_ordered"], r["qty_scheduled"],
                     r["source_origin"], r["qty_reference_virtual"]) for r in rows]
            hint = "一个询配码项目可产生多张订单；虚值只作参考，订单实值各自独立、可累计。"
        else:
            rows = []
            for bp in self.db.query("SELECT * FROM bid_projects WHERE status='中标' ORDER BY id"):
                if eid is not None and bp["id"] != eid:
                    continue
                for r in self.trace.bid_internal_order(bp["id"]):
                    r["project_no"] = bp["doc_no"]
                    rows.append(r)
            headers = ["中标项目", "内部订单", "订单状态", "明细行", "物资名称",
                       "订单实值", "单价", "已排量", "排产项目数", "来源中标明细行"]
            data = [(r["project_no"], r["order_no"], r["order_status"], r["line_no"],
                     r["item_name"], r["qty_ordered"], r["unit_price"],
                     r["qty_scheduled"], r["project_count"], r["bid_line_no"]) for r in rows]
            hint = "中标项目只生成一张内部订单；排产数量取自中标明细数量（实值），经内部订单进入守恒链路。"
        data, self._trace_ids = self._filter_display(data, self._trace_ids)
        self._trace_headers, self._trace_rows = headers, data
        fill_table(self.tbl_trace, headers, data, ids=self._trace_ids, stretch_col=4)
        self.trace_hint.setText(f"共 {len(data)} 行 · {hint}"
                                " 双击「按生产项目」视图的行可反查该订单明细被哪些生产项目领用。")

    def _trace_row_detail(self):
        if self.trace_scope.currentData() != "production":
            return
        row = self.tbl_trace.currentRow()
        if row < 0 or row >= len(self._trace_ids or []):
            return
        iid = self._trace_ids[row]
        if iid is None:
            return
        links = self.trace.from_order_item(iid)
        headers = ["生产项目", "项目名称", "状态", "分配量", "累计已排", "剩余可排"]
        table = [(l["project_no"], l["project_title"], l["project_status"],
                  l["allocated_qty"], l["allocated_total"], l["available_qty"])
                 for l in links]
        dialogs.TableViewDialog(self, "订单明细 → 排产项目", headers, table).exec()

    def _export_trace(self):
        return dialogs.export_sheets(
            self, {"溯源明细": (self._trace_headers, self._trace_rows)}, "溯源导出.xlsx")

    # ── ⑥ 总览与守恒 ─────────────────────────────────────

    # ── 数据安全：备份 / 恢复 / 体检（P0-2 / P0-3 / P0-4）──

    def _backup_db(self):
        path = self.db.backup(tag="manual")
        if not path:
            theme.notify(self, "无法备份", "当前为内存数据库，不产生备份文件。",
                         kind="warn")
            return None
        theme.notify(self, "备份完成", f"已保存 {path.name}",
                     detail=f"目录：{path.parent}")
        return f"已备份 {path.name}"

    def _restore_db(self):
        backups = self.db.list_backups()
        if not backups:
            theme.notify(self, "没有可用备份",
                         "备份目录为空。可先执行「备份数据库」，"
                         "或把外部备份文件放入 data/backup/ 后重试。", kind="warn")
            return None
        options = [
            (f"{b['name']}  ·  {b['mtime']}  ·  {b['size'] / 1024:.0f} KB", b["path"])
            for b in backups[:20]
        ]
        picked = dialogs.ask(self, "从备份恢复", [{
            "key": "path", "label": "选择备份", "type": "combo",
            "options": options, "required": True,
        }], subtitle="恢复前会自动备份当前库（tag=before-restore）")
        if not picked:
            return None
        if not theme.confirm(self, "确认恢复",
                             "将用所选备份覆盖当前数据库，未保存的修改会丢失。",
                             detail="恢复后请关闭并重新打开程序。"):
            return None
        saved = self.db.restore_from(picked["path"])
        theme.notify(self, "恢复完成",
                     "数据已恢复。请关闭程序后重新打开，确保界面全部重新加载。",
                     detail=f"恢复前现库已备份：{Path(saved).name}")
        return "已从备份恢复"

    def _attachment_check(self):
        fixed = self.schedules.repair_paths()
        missing = self.schedules.missing_files()
        if not missing:
            tail = f"（顺带修正 {fixed} 条历史绝对路径）" if fixed else ""
            theme.notify(self, "附件体检", f"全部附件均在位{tail}")
            return f"附件完整{'，修正 %d 条路径' % fixed if fixed else ''}"
        detail = "\n".join(
            f"· {m['schedule_no']} / {m['kind']} / {m['file_name']}"
            for m in missing[:12])
        if len(missing) > 12:
            detail += f"\n… 另有 {len(missing) - 12} 条"
        theme.notify(self, "附件体检 · 有缺失",
                     f"共 {len(missing)} 个附件在磁盘上找不到，登记仍在。",
                     kind="warn", detail=detail)
        return f"缺失 {len(missing)} 个附件"

    def _open_data_dir(self):
        import os
        theme_path = self.db.data_dir
        os.startfile(str(theme_path))      # noqa: S606 — Windows 单机工具
        return f"已打开 {theme_path}"

    def _open_logs(self):
        if not applog.open_log_dir():
            theme.notify(self, "打不开日志目录", str(applog.log_dir()), kind="warn")
            return None
        return f"日志目录：{applog.log_dir()}"

    def _consistency_check(self):
        from ..core.integrity import check
        report = check(self.db)
        theme.notify(self, "一致性检查", report["summary"])
        return report["summary"].replace("\n", " | ")

    def _export_all(self):
        tables = {
            "招投标项目": "SELECT * FROM bid_projects ORDER BY id",
            "投标明细": "SELECT * FROM bid_items ORDER BY id",
            "询配码项目": "SELECT * FROM code_projects ORDER BY id",
            "询配码明细": "SELECT * FROM code_items ORDER BY id",
            "订单": "SELECT * FROM orders ORDER BY id",
            "订单明细": "SELECT * FROM order_items ORDER BY id",
            "生产项目": "SELECT * FROM production_projects ORDER BY id",
            "工序任务": "SELECT * FROM production_tasks ORDER BY id",
            "排产关联": "SELECT * FROM production_links ORDER BY id",
            "订单排产": "SELECT * FROM order_schedules ORDER BY id",
            "订单排产附件": "SELECT * FROM order_schedule_files ORDER BY id",
            "审计日志": "SELECT * FROM audit_log ORDER BY id",
        }
        sheets = {}
        for name, sql in tables.items():
            rows = self.db.query(sql)
            headers = list(rows[0].keys()) if rows else ["(空表)"]
            sheets[name] = (headers, [list(r.values()) for r in rows])
        return dialogs.export_sheets(self, sheets, "管理系统全量导出.xlsx")

    def _download_templates(self):
        folder = theme.pick_path(self, "选择模板保存目录", mode="dir")
        if not folder:
            return
        names = {"code_item": "询配码明细导入模板.xlsx",
                 "bid_item": "投标明细导入模板.xlsx",
                 "order": "订单导入模板.xlsx"}
        for kind, name in names.items():
            excelio.export_template(str(Path(folder) / name), kind)
        return f"模板已保存到 {folder}"

    def _seed_demo(self):
        ok = theme.confirm(
            self, "载入演示数据",
            "将向当前数据库写入一组演示数据（中标项目/询配码/多张订单/排产），"
            "用于熟悉界面。已有数据不受影响。继续？")
        if not ok:
            return
        from .. import demo_data
        return demo_data.seed(self.db)

    # ── ⑥ 单据维护动作（编辑 / 作废 / 删除 / 审计） ──────

    def _edit_selected(self):
        target = self._pick_target()
        if target is None:
            return
        entity, eid, desc = target
        short = desc.split("  ")[0]
        fields = self.maint.field_defs(entity, eid)
        data = dialogs.ask(
            self, f"编辑 · {short}", fields,
            subtitle="锚点受保护 · 写入审计留痕")
        if not data:
            return
        row = self.maint.edit(entity, eid, data, operator=cur.name())
        return f"已编辑 {self.maint.describe(entity, row)}"

    def _void_selected(self):
        target = self._pick_target()
        if target is None:
            return
        entity, eid, desc = target
        if not self.maint.spec(entity).get("voidable"):
            theme.notify(self, "该记录不能作废",
                f"{desc} 是明细行，没有作废状态；录错请改用「删除选中记录」。",
                kind="warn")
            return
        refs = self.maint.references(entity, eid)
        if refs:
            detail = "\n".join(
                f"· {r['label']} {r['count']} 条：{'、'.join(r['samples'])}"
                for r in refs)
            theme.notify(self, "不能作废 · 已被下游引用",
                f"{desc} 已被下游单据引用，作废会造成链路断裂。",
                kind="warn", detail=detail)
            return
        data = dialogs.ask(self, f"作废 · {desc.split('  ')[0]}", [
            {"key": "reason", "label": "作废原因", "type": "textarea",
             "required": True},
        ], subtitle="作废是终态，数据保留；原因写入审计留痕")
        if not data:
            return
        if not theme.confirm(self, "确认作废",
                             f"确定作废 {desc} 吗？", detail=f"作废原因：{data['reason']}"):
            return
        self.maint.void(entity, eid, reason=data["reason"], operator=cur.name())
        return f"已作废 {desc}（原因：{data['reason']}）"

    def _delete_selected(self):
        target = self._pick_target()
        if target is None:
            return
        entity, eid, desc = target
        refs = self.maint.references(entity, eid)
        if refs:
            detail = "\n".join(
                f"· {r['label']} {r['count']} 条：{'、'.join(r['samples'])}"
                for r in refs)
            theme.notify(self, "不能删除 · 已被下游引用",
                f"{desc} 已被下游单据引用，请先解除关联或处理下游单据。",
                kind="warn", detail=detail)
            return
        children = self.maint.child_counts(entity, eid)
        child_labels = {t: lbl for t, _c, lbl
                        in self.maint.spec(entity)["children"]}
        child_text = ("、".join(f"{child_labels.get(t, t)} {n} 行"
                                for t, n in children.items())
                      if children else "无下级明细")
        if not theme.confirm(self, "确认删除",
                             f"确定删除 {desc} 吗？此操作不可撤销。",
                             detail=f"将一并删除：{child_text}\n"
                                    "删除前后完整快照写入审计留痕。"):
            return
        res = self.maint.delete(entity, eid, operator=cur.name())
        n = sum(res["deleted_children"].values())
        return f"已删除 {res['description']}" + (f"，级联删除 {n} 行明细" if n else "")

    def _show_audit(self):
        target = self._pick_target(silent=True)
        if target:
            entity, eid, desc = target
            rows = self.maint.audit_trail(entity, eid, limit=200)
            title = f"审计留痕 · {desc}"
        else:
            rows = self.maint.audit_trail(limit=200)
            title = "审计留痕 · 全系统最近 200 条"
        headers = ["时间", "单据类型", "操作", "对象 ID", "操作人", "变更前", "变更后"]
        table = [(r["at"], r["entity_label"], r["action_label"],
                  r["entity_id"] or "—", r["operator"] or "—",
                  _brief_json(r["before_json"]), _brief_json(r["after_json"]))
                 for r in rows]
        dialogs.TableViewDialog(self, title, headers, table, "审计留痕导出").exec()
        return f"共 {len(rows)} 条留痕"

    # ── 操作人 / 审计查询（P1-9）─────────────────────────

    def _set_operator(self):
        """设置当前操作人：写入 data/operator.txt，清空则回到 Windows 登录名"""
        current = cur.name(self.db.data_dir)
        data = dialogs.ask(
            self, "当前操作人",
            [{"key": "operator", "label": "操作人姓名", "default": current}],
            subtitle="审计留痕、订单负责人、排产附件登记人都会记录这个名字。"
                     "留空保存 = 恢复为 Windows 登录名。")
        if data is None:
            return None
        new_name = cur.set_name(data.get("operator", ""), self.db.data_dir)
        self.db_label.setText(f"数据库：{self.db.path}　|　操作人：{new_name}")
        return f"当前操作人：{new_name}"

    def _audit_query(self):
        dialogs.AuditQueryDialog(self, self.db, self.maint).exec()
        return "审计日志查询已关闭"

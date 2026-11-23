#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 5：订单排产（2026-11-07 v2.4）

入口变化：取消「新建排产项目」，改为在【订单明细的某一项】上右键 → 订单排产对话框。
一条订单明细 = 一张订单排产单（order_schedules.order_item_id UNIQUE，重复右键即打开既有单据）。

★ 数量口径【零改动】：建单时自动创建一个生产排产项目 + 一条 production_links，
  仍由 ProductionService.link_order_item() 统一校验
      Σ production_links.allocated_qty ≤ order_items.qty_ordered
  询配码虚值依旧不参与。order_schedules.allocated_qty 只是展示快照，不是第二个数量源。

★ 状态自动流转：当一张订单的【所有物资都有对应订单排产单】且【实值全部排满】时，
  订单状态自动置为「已排产」；不再满足时按 status_before 回退并留审计。
  作废 / 关闭为终态，不自动覆盖。

对话框动作（v3.1.2）：新建生产排产审批（只生成 .docx 模板并登记，不再启动本机 Word）、
导入生产排产审批（一个入口可多选任意类型文件，按扩展名自动分类），
附件统一登记到 order_schedule_files 并复制到 data/订单排产附件/<订单号>/<排产单号>/。
★ 已排产口径（v3.1.2 用户第 11 轮）：只要随单附件里存在【至少 1 个 Word/Excel 格式的
审批文件】，保存即默认本批物资已排产（自动补满可排量）；图片/图纸/PDF/其它文件只作附件，
不再单独触发状态流转。历史「≥2 类附件」口径由 AUTO_FILL_KINDS 保留为兼容常量。

v3.0 排产范围（用户给定）：任何入口都先进【订单排产对话框】，在窗口里勾选物资决定范围——
  分项 1 项 → 一张排产单 + 一份分项审批单；
  合并 ≥2 项且未覆盖全单 → 多张排产单共用一张合并审批单（明细多选合并排产）；
  整单 覆盖全部物资 → 全部排产单共用一张整单审批单。
  范围写入 order_schedules.scope，同批次写入同一个 batch_no；审批文件物理只存一份，
  登记到批次内每张排产单名下。整单排产不再直接弹 Word，而是先进对话框。
"""
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import cloudfile, codegen, models, worddoc
from ..core.db import Database, now
from ..core.errors import NotFoundError, StateError, ValidationError

KIND_IMAGE = "图片"
KIND_DRAWING = "图纸"
KIND_APPROVAL = "审批"
KIND_OTHER = "其他"
KINDS = (KIND_IMAGE, KIND_DRAWING, KIND_APPROVAL, KIND_OTHER)

# v3.0：排产范围标记（同时是 order_schedules.scope 的取值）
SCOPE_ITEM = "分项"
SCOPE_MERGE = "合并"
SCOPE_WHOLE = "整单"
SCOPES = (SCOPE_ITEM, SCOPE_MERGE, SCOPE_WHOLE)

IMAGE_FILTER = ("图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff);;"
                "所有文件 (*.*)")
DRAWING_FILTER = ("图纸/技术文件 (*.dwg *.dxf *.pdf *.step *.stp *.igs *.iges "
                  "*.ipt *.iam *.sldprt *.sldasm *.x_t *.sat *.stl *.png *.jpg);;"
                  "所有文件 (*.*)")
APPROVAL_FILTER = ("审批文档 (*.docx *.doc *.pdf *.wps);;所有文件 (*.*)")

# v2.9.3 历史口径：随单附件 ≥ 该类别数即默认已排产。
# v3.1.2 起不再作为判定条件（改为「存在 Word/Excel 审批文件」），常量仅为兼容
# 旧脚本 / 旧测试导入保留。
AUTO_FILL_KINDS = 2

# ── v3.1.2：单一导入入口的类型识别 ─────────────────────────
# Word 类（含 WPS 文字 / RTF / ODT）
_WORD_EXTS = {".doc", ".docx", ".docm", ".dot", ".dotx", ".wps", ".wpt",
              ".rtf", ".odt"}
# Excel 类（含 WPS 表格 / CSV / ODS）
_EXCEL_EXTS = {".xls", ".xlsx", ".xlsm", ".xlsb", ".xlt", ".xltx", ".et",
               ".ett", ".csv", ".ods"}
# ★ 触发「默认已排产」的唯一格式集合
APPROVAL_DOC_EXTS = _WORD_EXTS | _EXCEL_EXTS
# 归入「审批」类别但不触发状态流转的格式（PDF 审批扫描件 / 文本说明）
_APPROVAL_EXTS = APPROVAL_DOC_EXTS | {".pdf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
               ".tif", ".tiff", ".ico", ".svg", ".heic"}
_DRAWING_EXTS = {".dwg", ".dxf", ".step", ".stp", ".igs", ".iges", ".ipt",
                 ".iam", ".sldprt", ".sldasm", ".x_t", ".sat", ".stl",
                 ".prt", ".asm", ".catpart", ".catproduct", ".3ds", ".obj"}

# 「导入生产排产审批」一个入口的默认过滤器：常见格式全覆盖，
# 末尾保留「所有文件 (*.*)」，用户不用切过滤器也能选任意文件。
IMPORT_FILTER = (
    "审批与随单附件 (*.docx *.doc *.docm *.dotx *.wps *.wpt *.rtf *.odt "
    "*.xls *.xlsx *.xlsm *.xlsb *.et *.csv *.ods *.pdf "
    "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff "
    "*.dwg *.dxf *.step *.stp *.igs *.iges *.ipt *.iam *.sldprt *.sldasm "
    "*.x_t *.sat *.stl *.zip *.rar *.7z);;所有文件 (*.*)")


def classify_file(path: str) -> str:
    """按扩展名把导入文件归到附件类别（v3.1.2 单一导入入口的唯一分类口径）"""
    ext = Path(str(path)).suffix.lower()
    if ext in _APPROVAL_EXTS:
        return KIND_APPROVAL
    if ext in _IMAGE_EXTS:
        return KIND_IMAGE
    if ext in _DRAWING_EXTS:
        return KIND_DRAWING
    return KIND_OTHER


def is_approval_doc(file_name: str) -> bool:
    """是否 Word/Excel 格式的审批文件（唯一能触发「默认已排产」的格式）"""
    return Path(str(file_name)).suffix.lower() in APPROVAL_DOC_EXTS

_TERMINAL = (models.VOID_STATUS, models.OrderStatus.CLOSED)
_ALLOWED_HEAD = (models.OrderStatus.CONFIRMED, models.OrderStatus.WAIT_SHIP,
                 models.OrderStatus.SCHEDULED, models.OrderStatus.RUNNING,
                 models.OrderStatus.SHIPPING, models.OrderStatus.DELIVERED,
                 models.OrderStatus.ACCEPTED)


def _safe(name: str) -> str:
    """单号/文件名里可能出现的路径分隔符与空白一律清掉"""
    return re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(name or "")).strip() or "unnamed"


def scope_label(selected: int, total: int) -> str:
    """按勾选数量与全单物资数判定排产范围（唯一判定口径，界面与服务层共用）"""
    total = max(int(total or 0), 0)
    selected = max(int(selected or 0), 0)
    if selected > 1 and total > 1 and selected >= total:
        return SCOPE_WHOLE
    if selected > 1:
        return SCOPE_MERGE
    return SCOPE_ITEM


class OrderScheduleService:
    def __init__(self, db: Database, base_dir: Optional[str] = None):
        self.db = db
        from .order import OrderService
        from .production import ProductionService
        self.orders = OrderService(db)
        self.prod = ProductionService(db)
        root = Path(base_dir) if base_dir else (
            Path(db.path).resolve().parent if db.path and db.path != ":memory:"
            else Path.cwd() / "data")
        self.data_root = root
        self.root = root / "订单排产附件"

    # ── 路径换算（P0-3：库里只存相对 data/ 的路径）──────────

    def to_stored(self, path: Path) -> str:
        """绝对路径 → 相对 data/ 的 posix 路径；不在 data 下则原样保留"""
        p = Path(path)
        if not p.is_absolute():
            # 已是相对路径：统一成正斜杠，保证 repair_paths() 幂等
            return p.as_posix()
        try:
            return p.resolve().relative_to(self.data_root.resolve()).as_posix()
        except (ValueError, OSError):
            return str(p)

    def to_abs(self, stored: str) -> Path:
        """库里存的值 → 可直接打开的路径（兼容历史绝对路径）"""
        p = Path(str(stored or ""))
        return p if p.is_absolute() else (self.data_root / p)

    def repair_paths(self) -> int:
        """把历史绝对路径批量裁剪为相对路径，返回改动条数（幂等）"""
        changed = 0
        with self.db.tx():
            for f in self.db.query("SELECT id, file_path FROM order_schedule_files"):
                raw = f["file_path"] or ""
                stored = self.to_stored(Path(raw))
                if stored != raw:
                    self.db.update("order_schedule_files", {"file_path": stored},
                                   "id = ?", (f["id"],))
                    changed += 1
        return changed

    def missing_files(self, order_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """附件体检：返回登记在册但磁盘上找不到的文件（供界面提示/重挂）"""
        sql = ("SELECT f.*, s.doc_no AS schedule_no, s.order_id AS order_id "
               "FROM order_schedule_files f "
               "JOIN order_schedules s ON s.id = f.schedule_id ")
        params: tuple = ()
        if order_id is not None:
            sql += "WHERE s.order_id = ? "
            params = (order_id,)
        sql += "ORDER BY f.id"
        out = []
        for f in self.db.query(sql, params):
            target = self.to_abs(f["file_path"])
            if not target.is_file():
                out.append({
                    "id": f["id"],
                    "file_name": f["file_name"],
                    "kind": f["kind"],
                    "schedule_no": f["schedule_no"],
                    "path": str(target),
                })
        return out

    # ── 附件目录（v2.9.3：按订单归档 data/订单排产附件/<订单号>/<排产单号>/）──

    def order_dir(self, order_doc_no: str) -> Path:
        d = self.root / _safe(order_doc_no)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def schedule_dir(self, doc_no: str, order_doc_no: str = "") -> Path:
        if order_doc_no:
            d = self.order_dir(order_doc_no) / _safe(doc_no)
        else:  # 兼容旧调用
            d = self.root / _safe(doc_no)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _unique(self, folder: Path, name: str) -> Path:
        target = folder / _safe(name)
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        for i in range(2, 1000):
            cand = folder / f"{stem}({i}){suffix}"
            if not cand.exists():
                return cand
        return folder / f"{stem}_{datetime.now():%H%M%S}{suffix}"

    # ── 查询 ────────────────────────────────────────────────

    def get(self, schedule_id: int) -> Optional[Dict[str, Any]]:
        return self.db.get_by_id("order_schedules", schedule_id)

    def get_for_item(self, order_item_id: int) -> Optional[Dict[str, Any]]:
        return self.db.query_one(
            "SELECT * FROM order_schedules WHERE order_item_id = ?", (order_item_id,))

    def list(self, order_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if order_id is None:
            return self.db.query("SELECT * FROM order_schedules ORDER BY id")
        return self.db.query(
            "SELECT * FROM order_schedules WHERE order_id = ? ORDER BY id", (order_id,))

    def files(self, schedule_id: int) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM order_schedule_files WHERE schedule_id = ? ORDER BY id",
            (schedule_id,))

    def summary(self, order_id: int) -> Dict[str, Any]:
        items = self.orders.list_items(order_id)
        covered = {r["order_item_id"] for r in self.list(order_id)}
        qty_full = bool(items) and all(
            self.orders.available_qty(i["id"]) <= 1e-9 for i in items)
        order = self.db.get_by_id("orders", order_id)
        return {
            "item_count": len(items),
            "scheduled_count": sum(1 for i in items if i["id"] in covered),
            "all_covered": bool(items) and all(i["id"] in covered for i in items),
            "qty_full": qty_full,
            "status": order["status"] if order else "",
        }

    # ── 建单（幂等）────────────────────────────────────────

    def ensure(self, order_item_id: int, operator: str = "",
               at: Optional[datetime] = None) -> Dict[str, Any]:
        """
        右键某个订单物资 → 确保它有一张订单排产单：
          既有 → 直接返回（对话框用于补附件/审批）；
          新建 → 自动建生产项目 + 守恒关联 + 审计，之后重算订单状态。
        """
        hit = self.get_for_item(order_item_id)
        if hit:
            return hit
        item = self.db.get_by_id("order_items", order_item_id)
        if not item:
            raise NotFoundError(f"订单明细 {order_item_id} 不存在")
        order = self.db.get_by_id("orders", item["order_id"])
        if not order:
            raise NotFoundError(f"订单 {item['order_id']} 不存在")
        if order["status"] in _TERMINAL:
            raise StateError(f"订单 {order['doc_no']} 状态为 {order['status']}，不可排产")
        if order["status"] not in _ALLOWED_HEAD:
            raise StateError(
                f"订单 {order['doc_no']} 状态为 {order['status']}，需先「确认选中订单」才可排产")

        status_before = order["status"]
        available = round(self.orders.available_qty(order_item_id), 6)
        with self.db.tx():
            doc_no = codegen.next_doc_no(self.db, codegen.PREFIX_SCHEDULE, at)
            project_id: Optional[int] = None
            link_id: Optional[int] = None
            project_created = 0
            if available > 0:
                project = self.prod.create_project(
                    title=f"{order['doc_no']}-{item['line_no']:02d} {item['item_name']} · 订单排产",
                    source_bid_project_id=order.get("source_bid_project_id"),
                    workshop="", owner=operator, priority=1,
                    remark=f"订单排产 {doc_no} 自动生成", at=at)
                project_id = project["id"]
                project_created = 1
                link = self.prod.link_order_item(
                    project_id, order_item_id, available,
                    operator=operator, strict=False)
                link_id = link["id"]
                allocated = available
                # link_order_item 的旧行为会在首个关联时把「已确认」订单直接置
                # 「已排产」；v2.4 要求全物资覆盖才置位，先静默还原中间态，
                # 避免审计里出现一条并不存在的「撤销排产」。
                if self.db.get_by_id("orders", order["id"])["status"] != status_before:
                    self.db.update("orders",
                                   {"status": status_before, "updated_at": now()},
                                   "id = ?", (order["id"],))
            else:
                # 已被旧的「关联订单明细 / 中标一键排产」排满：复用既有 link，不重复分配
                links = self.db.query(
                    "SELECT * FROM production_links WHERE order_item_id = ? ORDER BY id",
                    (order_item_id,))
                if not links:
                    raise StateError(
                        "该物资可排量为 0 且无既有排产关联，无法建立订单排产")
                project_id = links[0]["project_id"]
                link_id = links[0]["id"]
                allocated = round(sum(float(x["allocated_qty"]) for x in links), 6)

            ts = now()
            sid = self.db.insert("order_schedules", {
                "doc_no": doc_no,
                "order_id": order["id"],
                "order_item_id": order_item_id,
                "project_id": project_id,
                "link_id": link_id,
                "project_created": project_created,
                "allocated_qty": allocated,
                "status": models.ScheduleStatus.DRAFT,
                "status_before": status_before,
                "remark": "",
                "created_by": operator,
                "created_at": ts,
                "updated_at": ts,
            })
            self.db.log_action(
                "order_schedule", sid, "create",
                after={"doc_no": doc_no, "order_no": order["doc_no"],
                       "line_no": item["line_no"], "allocated_qty": allocated,
                       "project_created": project_created}, operator=operator)
            # ★ link_order_item 的旧行为会在首个关联时把订单置「已排产」；
            #   这里按 v2.4 口径重算：只有全部物资都有订单排产单且排满才置位。
            self._refresh(order["id"], operator)
        return self.db.get_by_id("order_schedules", sid)

    # ── 状态自动流转 ───────────────────────────────────────

    def _refresh(self, order_id: int, operator: str = "") -> str:
        order = self.db.get_by_id("orders", order_id)
        if not order or order["status"] in _TERMINAL:
            return order["status"] if order else ""
        s = self.summary(order_id)
        fully = bool(s["all_covered"] and s["qty_full"])
        if fully and order["status"] != models.OrderStatus.SCHEDULED:
            self.db.update("orders",
                           {"status": models.OrderStatus.SCHEDULED, "updated_at": now()},
                           "id = ?", (order_id,))
            self.db.log_action("order", order_id, "auto_scheduled",
                               before={"status": order["status"]},
                               after={"status": models.OrderStatus.SCHEDULED,
                                      "scheduled_items": s["scheduled_count"]},
                               operator=operator)
            return models.OrderStatus.SCHEDULED
        if not fully and order["status"] == models.OrderStatus.SCHEDULED:
            rows = self.list(order_id)
            if not rows:
                return order["status"]
            restore = rows[0].get("status_before") or models.OrderStatus.CONFIRMED
            if restore == models.OrderStatus.SCHEDULED:
                # 建单前就是「已排产」（旧的关联订单明细/中标一键排产链路），
                # 不因只补了一张订单排产单而把状态倒退回「已确认」。
                return order["status"]
            self.db.update("orders", {"status": restore, "updated_at": now()},
                           "id = ?", (order_id,))
            self.db.log_action("order", order_id, "auto_unscheduled",
                               before={"status": models.OrderStatus.SCHEDULED},
                               after={"status": restore,
                                      "scheduled_items": s["scheduled_count"]},
                               operator=operator)
            return restore
        return order["status"]

    def recheck_order(self, order_id: int, operator: str = "") -> str:
        """公开入口：打开对话框/刷新时自愈状态（只影响有订单排产单的订单）"""
        with self.db.tx():
            return self._refresh(order_id, operator)

    # ── 附件 ────────────────────────────────────────────────

    def add_files(self, schedule_id: int, kind: str,
                  src_paths: Sequence[str], operator: str = "") -> List[Dict[str, Any]]:
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        if kind not in KINDS:
            raise ValidationError(f"附件类别必须是 {'/'.join(KINDS)}")
        order = self.db.get_by_id("orders", sched["order_id"])
        folder = self.schedule_dir(sched["doc_no"],
                                   order["doc_no"] if order else "")
        added: List[Dict[str, Any]] = []
        with self.db.tx():
            for src in src_paths:
                # v3.1.1：云盘「仅联机」占位文件先水合到本地缓存副本
                local = cloudfile.ensure_readable(str(src))
                p = Path(local)
                if not p.is_file():
                    raise ValidationError(f"文件不存在：{src}")
                target = self._unique(folder, Path(str(src)).name)
                shutil.copy2(str(p), str(target))
                fid = self.db.insert("order_schedule_files", {
                    "schedule_id": schedule_id,
                    "kind": kind,
                    "file_name": target.name,
                    "file_path": self.to_stored(target),
                    "file_size": target.stat().st_size,
                    "created_by": operator,
                    "created_at": now(),
                })
                self.db.log_action("order_schedule_file", fid, "import",
                                   after={"schedule_no": sched["doc_no"],
                                          "kind": kind, "file": target.name},
                                   operator=operator)
                added.append(self.db.get_by_id("order_schedule_files", fid))
        return added

    def remove_file(self, file_id: int, operator: str = "") -> int:
        """
        移除附件登记，返回一并移除的登记条数。
        合并/整单排产时同一物理文件会登记在多张排产单下（共用一张审批单），
        因此按 file_path 一并清理，避免兄弟排产单留下打不开的死链。
        """
        f = self.db.get_by_id("order_schedule_files", file_id)
        if not f:
            raise NotFoundError(f"附件 {file_id} 不存在")
        siblings = self.db.query(
            "SELECT id FROM order_schedule_files WHERE file_path = ?",
            (f["file_path"],))
        with self.db.tx():
            self.db.execute("DELETE FROM order_schedule_files WHERE file_path = ?",
                            (f["file_path"],))
            self.db.log_action("order_schedule_file", file_id, "remove",
                               before={"file": f["file_name"],
                                       "schedules": len(siblings)}, operator=operator)
        # 物理删除属尽力而为：被 Word/看图软件占用时只移除登记，不中断业务
        try:
            self.to_abs(f["file_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        return len(siblings)

    def open_file(self, file_id: int):
        f = self.db.get_by_id("order_schedule_files", file_id)
        if not f:
            raise NotFoundError(f"附件 {file_id} 不存在")
        return worddoc.open_file(str(self.to_abs(f["file_path"])))

    # ── v3.4.0：订单级「排产文件」查看 / 编辑 / 更改 ──────────
    # 合并 / 整单排产时，同一物理审批文档会登记在多张排产单名下；下面所有
    # 写操作都按 file_path 作用于【整组兄弟登记】，避免出现「改了 A 单、
    # B 单还是旧名字」的死链。

    def order_files(self, order_id: int) -> List[Dict[str, Any]]:
        """
        该订单全部排产单的随单附件，按 file_path 去重后返回。
        每条附带的键：file_ids / schedule_ids / schedule_nos / shared /
        exists / path —— 供「排产文件」对话框直接展示与定位。
        """
        rows = self.db.query(
            "SELECT f.*, s.doc_no AS schedule_no, s.order_item_id "
            "FROM order_schedule_files f "
            "JOIN order_schedules s ON s.id = f.schedule_id "
            "WHERE s.order_id = ? ORDER BY f.id", (order_id,))
        seen: Dict[str, Dict[str, Any]] = {}
        for f in rows:
            key = str(f["file_path"] or "")
            hit = seen.get(key)
            if hit is not None:
                hit["file_ids"].append(f["id"])
                hit["schedule_ids"].append(f["schedule_id"])
                hit["schedule_nos"].append(f["schedule_no"])
                continue
            p = self.to_abs(key)
            item = dict(f)
            item.update({
                "file_ids": [f["id"]],
                "schedule_ids": [f["schedule_id"]],
                "schedule_nos": [f["schedule_no"]],
                "path": str(p),
                "exists": p.is_file(),
            })
            seen[key] = item
        out = []
        for item in seen.values():
            item["shared"] = len(item["schedule_ids"])
            out.append(item)
        return out

    def order_schedule_rows(self, order_id: int) -> List[Dict[str, Any]]:
        """订单排产单清单（带物资行号/名称，供「排产文件」窗口上半区展示）"""
        return self.db.query(
            "SELECT s.*, oi.line_no, oi.item_name, "
            "(SELECT COUNT(*) FROM order_schedule_files f "
            " WHERE f.schedule_id = s.id) AS file_count "
            "FROM order_schedules s "
            "JOIN order_items oi ON oi.id = s.order_item_id "
            "WHERE s.order_id = ? ORDER BY s.id", (order_id,))

    @staticmethod
    def _siblings(db: Database, file_path: str) -> List[Dict[str, Any]]:
        return db.query(
            "SELECT * FROM order_schedule_files WHERE file_path = ? ORDER BY id",
            (file_path,))

    def rename_file(self, file_id: int, new_name: str,
                    operator: str = "") -> Dict[str, Any]:
        """重命名附件（物理文件 + 全部兄弟登记同步），保留扩展名不变"""
        f = self.db.get_by_id("order_schedule_files", file_id)
        if not f:
            raise NotFoundError(f"附件 {file_id} 不存在")
        name = _safe(new_name)
        if not name or name in (".", ".."):
            raise ValidationError("文件名不能为空")
        old = Path(str(f["file_name"]))
        if Path(name).suffix.lower() != old.suffix.lower():
            raise ValidationError(
                f"扩展名不可更改（原文件 {old.suffix or '无扩展名'}），"
                "否则已登记的审批文档会打不开")
        src = self.to_abs(f["file_path"])
        target = src.parent / name
        if target != src and target.exists():
            raise ValidationError(f"同目录下已存在同名文件：{name}")
        stored = self.to_stored(target)
        sibs = self._siblings(self.db, f["file_path"])
        with self.db.tx():
            if src.is_file():
                src.rename(target)
            for s in sibs:
                self.db.update("order_schedule_files",
                               {"file_name": name, "file_path": stored},
                               "id = ?", (s["id"],))
            self.db.log_action("order_schedule_file", file_id, "rename",
                               before={"file": f["file_name"],
                                       "path": f["file_path"]},
                               after={"file": name, "path": stored,
                                      "schedules": len(sibs)}, operator=operator)
        return dict(self.db.get_by_id("order_schedule_files", file_id) or {})

    def replace_file(self, file_id: int, src_path: str,
                     operator: str = "") -> Dict[str, Any]:
        """
        用新文件覆盖原附件内容（文件名与登记路径不变，全部兄弟登记共享新内容）。
        典型场景：审批单改完另存了一份，直接替换而不必删除重建。
        """
        f = self.db.get_by_id("order_schedule_files", file_id)
        if not f:
            raise NotFoundError(f"附件 {file_id} 不存在")
        local = cloudfile.ensure_readable(str(src_path))
        p = Path(local)
        if not p.is_file():
            raise ValidationError(f"文件不存在：{src_path}")
        target = self.to_abs(f["file_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(p), str(target))
        size = target.stat().st_size
        sibs = self._siblings(self.db, f["file_path"])
        with self.db.tx():
            for s in sibs:
                self.db.update("order_schedule_files", {"file_size": size},
                               "id = ?", (s["id"],))
            self.db.log_action("order_schedule_file", file_id, "replace",
                               before={"file": f["file_name"],
                                       "size": f["file_size"]},
                               after={"size": size, "schedules": len(sibs),
                                      "source": Path(str(src_path)).name},
                               operator=operator)
        return dict(self.db.get_by_id("order_schedule_files", file_id) or {})

    def set_file_kind(self, file_id: int, kind: str,
                      operator: str = "") -> Dict[str, Any]:
        """更改附件类别（同组兄弟登记同步，避免同一文件在不同排产单里类别打架）"""
        if kind not in KINDS:
            raise ValidationError(f"附件类别必须是 {'/'.join(KINDS)}")
        f = self.db.get_by_id("order_schedule_files", file_id)
        if not f:
            raise NotFoundError(f"附件 {file_id} 不存在")
        sibs = self._siblings(self.db, f["file_path"])
        with self.db.tx():
            for s in sibs:
                self.db.update("order_schedule_files", {"kind": kind},
                               "id = ?", (s["id"],))
            self.db.log_action("order_schedule_file", file_id, "set_kind",
                               before={"kind": f["kind"]},
                               after={"kind": kind, "schedules": len(sibs)},
                               operator=operator)
        return dict(self.db.get_by_id("order_schedule_files", file_id) or {})

    def add_files_for_order(self, order_id: int, src_paths: Sequence[str],
                            operator: str = "") -> Dict[str, Any]:
        """「排产文件」窗口的导入：登记到本订单全部排产单，按扩展名自动分类"""
        scheds = self.list(order_id)
        if not scheds:
            order = self.db.get_by_id("orders", order_id) or {}
            raise StateError(
                f"订单 {order.get('doc_no') or order_id} 还没有订单排产单，"
                "请先对物资做订单排产，再登记排产文件")
        return self.import_files([s["id"] for s in scheds], src_paths, operator)

    # ── 生产排产审批（本机 Word）────────────────────────────

    def approval_context(self, schedule_id: int) -> Dict[str, Any]:
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        order = self.db.get_by_id("orders", sched["order_id"])
        item = self.db.get_by_id("order_items", sched["order_item_id"])
        if not order or not item:
            raise NotFoundError("订单排产单关联的订单/明细已不存在")
        return {
            "schedule_no": sched["doc_no"],
            "created_at": str(sched["created_at"] or "")[:10],
            "created_by": sched.get("created_by") or "",
            "order_no": order["doc_no"],
            "project_no": order.get("project_no") or "",
            "customer": order.get("customer") or "",
            "order_type": order.get("order_type") or "",
            "status": order.get("status") or "",
            "erp_order_no": order.get("erp_order_no") or "",
            "required_date": order.get("required_date") or "",
            "amount_total": order.get("amount_total") or 0,
            "item_qty_total": order.get("item_qty_total") or 0,
            "item": {
                "line_no": item["line_no"],
                "item_code": item.get("item_code") or "",
                "item_name": item.get("item_name") or "",
                "production_name": item.get("production_name") or "",
                "spec": item.get("spec") or "",
                "unit": item.get("unit") or "",
                "qty_ordered": item.get("qty_ordered") or 0,
                "allocated_qty": sched.get("allocated_qty") or 0,
                "unit_price": item.get("unit_price") or 0,
                "amount": item.get("amount") or 0,
                "param_desc": item.get("param_desc") or "",
            },
            "files": [{"kind": f["kind"], "file_name": f["file_name"]}
                      for f in self.files(schedule_id)],
            "remark": sched.get("remark") or "",
        }

    def build_approval(self, schedule_id: int, operator: str = "") -> str:
        """生成《生产排产审批单》.docx、登记为「审批」附件，返回绝对路径"""
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        ctx = self.approval_context(schedule_id)
        order = self.db.get_by_id("orders", sched["order_id"])
        folder = self.schedule_dir(sched["doc_no"],
                                   order["doc_no"] if order else "")
        name = f"生产排产审批单_{sched['doc_no']}.docx"
        target = self._unique(folder, name)
        path = worddoc.build_approval_docx(str(target), ctx)
        with self.db.tx():
            fid = self.db.insert("order_schedule_files", {
                "schedule_id": schedule_id,
                "kind": KIND_APPROVAL,
                "file_name": Path(path).name,
                "file_path": self.to_stored(Path(path)),
                "file_size": Path(path).stat().st_size,
                "created_by": operator,
                "created_at": now(),
            })
            self.db.update("order_schedules",
                           {"status": models.ScheduleStatus.COMPILED,
                            "updated_at": now()}, "id = ?", (schedule_id,))
            self.db.log_action("order_schedule", schedule_id, "build_approval",
                               after={"file": Path(path).name, "file_id": fid},
                               operator=operator)
        return path

    def open_approval(self, path: str):
        """
        启动本机 Word（不抛异常，返回 (ok, message)）。
        v3.1.2：界面「新建生产排产审批」已不再自动调用本方法；保留供
        外部脚本 / 旧版兼容使用，用户可在随单附件里双击打开。
        """
        return worddoc.open_in_word(path)

    # ── v2.9.3：审批复用 / 附件自动排满 / 整单排产 ──────────

    def ensure_approval(self, schedule_id: int,
                        operator: str = "") -> Tuple[str, bool]:
        """
        点击「新建生产排产审批」的口径（v3.1.2）：
        已有审批文档且文件在盘 → 直接复用（不再启动 Word，界面只提示）；
        没有才生成新模板。返回 (路径, 是否新生成)。
        """
        for f in self.files(schedule_id):
            if f["kind"] == KIND_APPROVAL:
                p = self.to_abs(f["file_path"])
                if p.is_file():
                    return str(p), False
        return self.build_approval(schedule_id, operator), True

    def file_kinds(self, schedule_id: int) -> List[str]:
        """该排产单随单附件的类别集合（图片/图纸/审批/其他）"""
        return sorted({f["kind"] for f in self.files(schedule_id)})

    def approval_docs(self, schedule_id: int) -> List[str]:
        """该排产单里在盘的 Word/Excel 审批文件名（v3.1.2 状态判定依据）"""
        out = []
        for f in self.files(schedule_id):
            if (f["kind"] == KIND_APPROVAL
                    and is_approval_doc(f["file_name"])
                    and self.to_abs(f["file_path"]).is_file()):
                out.append(f["file_name"])
        return out

    def has_approval_doc(self, schedule_id: int) -> bool:
        return bool(self.approval_docs(schedule_id))

    def auto_fill_if_ready(self, schedule_id: int,
                           operator: str = "") -> Tuple[bool, int]:
        """
        ★ v3.1.2 口径：只要随单附件里存在【至少 1 个 Word/Excel 格式的
        审批文件】，保存即默认该物资已排产 → 若仍有可排量则自动补满。
        图片 / 图纸 / PDF / 其它类型附件只登记，不触发状态流转。
        返回 (保存后该物资是否已排产, 附件类别数)——返回值形状与 v2.9.3
        保持一致，方便旧调用与审计脚本平滑过渡。
        """
        kinds = self.file_kinds(schedule_id)
        if not self.has_approval_doc(schedule_id):
            return False, len(kinds)
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        available = self.orders.available_qty(sched["order_item_id"])
        if available > 1e-9 and sched.get("link_id"):
            filled = float(sched.get("allocated_qty") or 0) + available
            self.set_allocated_qty(schedule_id, filled, operator)
        scheduled = self.orders.available_qty(sched["order_item_id"]) <= 1e-9
        return scheduled, len(kinds)

    def order_approval_context(self, order_id: int,
                               item_ids: Optional[Sequence[int]] = None,
                               scope: str = SCOPE_WHOLE) -> Dict[str, Any]:
        """审批文档上下文：订单头 + 指定物资清单（缺省=全部，整单口径）"""
        order = self.db.get_by_id("orders", order_id)
        if not order:
            raise NotFoundError(f"订单 {order_id} 不存在")
        items = self.orders.list_items(order_id)
        if item_ids is not None:
            keep = {int(i) for i in item_ids}
            items = [it for it in items if it["id"] in keep]
        total = len(self.orders.list_items(order_id))
        return {
            "order_no": order["doc_no"],
            "project_no": order.get("project_no") or "",
            "customer": order.get("customer") or "",
            "order_type": order.get("order_type") or "",
            "status": order.get("status") or "",
            "erp_order_no": order.get("erp_order_no") or "",
            "required_date": order.get("required_date") or "",
            "amount_total": order.get("amount_total") or 0,
            "item_qty_total": order.get("item_qty_total") or 0,
            "created_at": str(now())[:10],
            "scope": scope,
            "scope_note": (f"{scope}排产 · 本单共 {total} 项物资，本审批单覆盖 "
                           f"{len(items)} 项"),
            "items": [{
                "line_no": it["line_no"],
                "item_code": it.get("item_code") or "",
                "item_name": it.get("item_name") or "",
                "production_name": it.get("production_name") or "",
                "spec": it.get("spec") or "",
                "unit": it.get("unit") or "",
                "qty_ordered": it.get("qty_ordered") or 0,
                "allocated_qty": self.orders.allocated_qty(it["id"]),
                "unit_price": it.get("unit_price") or 0,
                "amount": it.get("amount") or 0,
            } for it in items],
        }

    def build_order_approval(self, order_id: int,
                             operator: str = "") -> Dict[str, Any]:
        """
        整单排产审批（兼容入口）：一张文档覆盖订单全部物资。
        现在统一走 build_scope_approval（scope=整单），语义与 v2.9.3 一致。
        """
        scheds = self.list(order_id)
        if not scheds:
            order = self.db.get_by_id("orders", order_id)
            raise StateError(f"订单 {order['doc_no'] if order else order_id} "
                             "还没有任何订单排产单，请先对物资建立订单排产")
        path, created = self.build_scope_approval(
            order_id, [s["id"] for s in scheds], SCOPE_WHOLE, operator)
        scheds = self.list(order_id)
        return {"path": path, "created": created,
                "order": self.db.get_by_id("orders", order_id),
                "schedules": scheds}

    # ── v3.0：排产范围（分项 / 合并 / 整单）+ 批次共用审批文档 ──

    def item_map(self, order_id: int) -> Dict[int, Dict[str, Any]]:
        """订单明细 id → 订单排产单（未建单的物资不出现在结果里）"""
        return {s["order_item_id"]: s for s in self.list(order_id)}

    def scope_of(self, order_id: int, schedule_ids: Sequence[int]) -> str:
        """按「已勾选物资数 / 全单物资数」判定范围（与界面 scope_label 同一口径）"""
        total = len(self.orders.list_items(order_id))
        picked = {int(i) for i in schedule_ids}
        rows = [s for s in self.list(order_id) if s["id"] in picked]
        return scope_label(len({s["order_item_id"] for s in rows}), total)

    def batch_schedules(self, order_id: int,
                        batch_no: str) -> List[Dict[str, Any]]:
        if not batch_no:
            return []
        return self.db.query(
            "SELECT * FROM order_schedules WHERE order_id = ? AND batch_no = ? "
            "ORDER BY order_item_id", (order_id, batch_no))

    def _next_batch_no(self, order_id: int, order_doc_no: str) -> str:
        used = set()
        for r in self.db.query(
                "SELECT batch_no FROM order_schedules "
                "WHERE order_id = ? AND batch_no IS NOT NULL", (order_id,)):
            value = str(r["batch_no"] or "")
            if "-B" in value:
                try:
                    used.add(int(value.rsplit("-B", 1)[1]))
                except ValueError:
                    continue
        return f"{order_doc_no}-B{(max(used) + 1) if used else 1:02d}"

    def _tag_scope(self, order_id: int, scheds: Sequence[Dict[str, Any]],
                   scope: str, operator: str = "") -> str:
        """
        给一批排产单打范围标记与批次号，返回批次号（分项返回空串）。

        批次复用规则：若这些排产单已同属某个批次，且该批次的成员集合与本次
        勾选集合完全一致 → 沿用原批次号（幂等，不重复造批次、不重复生成文档）。
        """
        if not scheds:
            return ""
        ids = [s["id"] for s in scheds]
        target = {s["order_item_id"] for s in scheds}
        if scope == SCOPE_ITEM:
            with self.db.tx():
                for s in scheds:
                    if (s.get("scope") or SCOPE_ITEM) == SCOPE_ITEM and not s.get("batch_no"):
                        continue
                    # batch_no 要显式置 NULL：db.update 会丢弃 None 值，这里必须走裸 SQL
                    self.db.execute(
                        "UPDATE order_schedules SET scope = ?, batch_no = NULL, "
                        "updated_at = ? WHERE id = ?", (SCOPE_ITEM, now(), s["id"]))
                    self.db.log_action(
                        "order_schedule", s["id"], "set_scope",
                        before={"scope": s.get("scope"), "batch_no": s.get("batch_no")},
                        after={"scope": SCOPE_ITEM, "batch_no": None}, operator=operator)
            return ""

        existing: Dict[str, set] = {}
        for s in scheds:
            bno = str(s.get("batch_no") or "")
            if bno:
                existing.setdefault(bno, set()).add(s["order_item_id"])
        reuse = ""
        for bno in existing:
            members = {r["order_item_id"] for r in self.batch_schedules(order_id, bno)}
            if members == target:
                reuse = bno
                break
        batch_no = reuse or self._next_batch_no(
            order_id, (self.db.get_by_id("orders", order_id) or {}).get("doc_no") or "")
        with self.db.tx():
            for s in scheds:
                if (s.get("scope") == scope and (s.get("batch_no") or "") == batch_no):
                    continue
                self.db.update("order_schedules",
                               {"scope": scope, "batch_no": batch_no,
                                "updated_at": now()}, "id = ?", (s["id"],))
                self.db.log_action(
                    "order_schedule", s["id"], "set_scope",
                    before={"scope": s.get("scope"), "batch_no": s.get("batch_no")},
                    after={"scope": scope, "batch_no": batch_no}, operator=operator)
        return batch_no

    def ensure_scope(self, order_id: int, item_ids: Sequence[int],
                     operator: str = "", scope: str = "") -> Dict[str, Any]:
        """
        订单排产对话框的唯一落库入口（v3.0）：
          ① 对勾选的每项物资确保有一张订单排产单（幂等，已有则复用）；
          ② 按勾选数量判定范围（分项 / 合并 / 整单），同批次打 batch_no；
          ③ 不做数量调整——数量仍由 set_allocated_qty / auto_fill 单点控制。
        返回 {order, schedules, created, scope, batch_no, item_ids}
        """
        order = self.db.get_by_id("orders", order_id)
        if not order:
            raise NotFoundError(f"订单 {order_id} 不存在")
        items = self.orders.list_items(order_id)
        if not items:
            raise StateError(f"订单 {order['doc_no']} 没有物资明细，无法排产")
        valid = [it["id"] for it in items]
        picked: List[int] = []
        for iid in item_ids:
            if int(iid) in valid and int(iid) not in picked:
                picked.append(int(iid))
        if not picked:
            raise ValidationError("请至少勾选一项物资再排产")
        picked.sort(key=lambda x: valid.index(x))
        scope = scope if scope in SCOPES else scope_label(len(picked), len(items))

        created: List[str] = []
        for iid in picked:
            if self.get_for_item(iid):
                continue
            created.append(self.ensure(iid, operator)["doc_no"])
        scheds = [self.get_for_item(iid) for iid in picked]
        scheds = [s for s in scheds if s]
        batch_no = self._tag_scope(order_id, scheds, scope, operator)
        self.recheck_order(order_id, operator)
        return {"order": self.db.get_by_id("orders", order_id),
                "schedules": scheds, "created": created,
                "scope": scope, "batch_no": batch_no, "item_ids": picked}

    def scope_files(self, schedule_ids: Sequence[int]) -> List[Dict[str, Any]]:
        """本批排产单的附件集合（同一物理文件只出一条，附 covers 说明）"""
        ids = [int(i) for i in schedule_ids]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.db.query(
            f"SELECT f.*, s.doc_no AS schedule_no FROM order_schedule_files f "
            f"JOIN order_schedules s ON s.id = f.schedule_id "
            f"WHERE f.schedule_id IN ({marks}) ORDER BY f.id", tuple(ids))
        seen: Dict[str, Dict[str, Any]] = {}
        for f in rows:
            key = f["file_path"]
            hit = seen.get(key)
            if hit:
                hit["shared"] += 1
                continue
            item = dict(f)
            item["shared"] = 1
            seen[key] = item
        return list(seen.values())

    def add_files_to(self, schedule_ids: Sequence[int], kind: str,
                     src_paths: Sequence[str],
                     operator: str = "") -> List[Dict[str, Any]]:
        """把同一批文件登记到多张排产单（合并/整单：一张审批单覆盖多种物资）"""
        ids = [int(i) for i in schedule_ids]
        if not ids:
            raise ValidationError("请先勾选物资再导入附件")
        if len(ids) == 1:
            return self.add_files(ids[0], kind, src_paths, operator)
        first = self.get(ids[0])
        order = self.db.get_by_id("orders", first["order_id"])
        folder = self.schedule_dir(first["doc_no"], order["doc_no"] if order else "")
        added: List[Dict[str, Any]] = []
        with self.db.tx():
            for src in src_paths:
                # v3.1.1：云盘「仅联机」占位文件先水合到本地缓存副本
                local = cloudfile.ensure_readable(str(src))
                p = Path(local)
                if not p.is_file():
                    raise ValidationError(f"文件不存在：{src}")
                target = self._unique(folder, Path(str(src)).name)
                shutil.copy2(str(p), str(target))
                stored = self.to_stored(target)
                for sid in ids:
                    fid = self.db.insert("order_schedule_files", {
                        "schedule_id": sid,
                        "kind": kind,
                        "file_name": target.name,
                        "file_path": stored,
                        "file_size": target.stat().st_size,
                        "created_by": operator,
                        "created_at": now(),
                    })
                    self.db.log_action(
                        "order_schedule_file", fid, "import_shared",
                        after={"file": target.name, "kind": kind,
                               "schedules": len(ids)}, operator=operator)
                    added.append(self.db.get_by_id("order_schedule_files", fid))
        return added

    def scope_approval_docs(self, schedule_ids: Sequence[int]) -> List[str]:
        """本批排产单里在盘的 Word/Excel 审批文件名（同一物理文件只出一条）"""
        out = []
        for f in self.scope_files(schedule_ids):
            if (f["kind"] == KIND_APPROVAL
                    and is_approval_doc(f["file_name"])
                    and self.to_abs(f["file_path"]).is_file()):
                out.append(f["file_name"])
        return out

    def import_files(self, schedule_ids: Sequence[int],
                     src_paths: Sequence[str],
                     operator: str = "") -> Dict[str, Any]:
        """
        ★ v3.1.2 单一导入入口：一次可导入任意类型文件，按扩展名自动分类后
        分别登记到本批排产单（同一文件在合并/整单下共用一份物理副本）。

        返回 {"files": 文件数, "kinds": {类别: 文件数}, "added": 登记行}。
        分类口径与界面、状态判定共用 `classify_file()` / `is_approval_doc()`，
        避免「界面显示审批、状态却没流转」这类两边口径不一致的缺陷。
        """
        groups: Dict[str, List[str]] = {}
        for src in src_paths:
            groups.setdefault(classify_file(src), []).append(str(src))
        added: List[Dict[str, Any]] = []
        summary: Dict[str, int] = {}
        for kind, paths in groups.items():
            added.extend(self.add_files_to(schedule_ids, kind, paths, operator))
            summary[kind] = len(paths)
        return {"files": len(src_paths), "kinds": summary, "added": added}

    def build_scope_approval(self, order_id: int, schedule_ids: Sequence[int],
                             scope: str = "", operator: str = "") -> Tuple[str, bool]:
        """
        为当前范围生成/复用审批文档，返回 (绝对路径, 是否新生成)。
          · 分项：沿用 ensure_approval 语义（已有审批文件在盘就直接打开继续编辑）；
          · 合并/整单：一张文档覆盖本范围全部物资，物理文件只存一份，
            登记到范围内每张排产单（同批次共用一张审批文件）。
        """
        order = self.db.get_by_id("orders", order_id)
        if not order:
            raise NotFoundError(f"订单 {order_id} 不存在")
        ids = [int(i) for i in schedule_ids]
        if not ids:
            raise ValidationError("请先勾选物资再新建审批")
        scope = scope if scope in SCOPES else self.scope_of(order_id, ids)
        if scope == SCOPE_ITEM and len(ids) == 1:
            return self.ensure_approval(ids[0], operator)

        prefix = f"生产排产审批单_{scope}_"
        for f in self.scope_files(ids):
            if f["kind"] == KIND_APPROVAL and f["file_name"].startswith(prefix):
                p = self.to_abs(f["file_path"])
                if p.is_file():
                    self._register_approval(ids, str(p), operator, shared=True)
                    return str(p), False

        scheds = [self.get(i) for i in ids]
        scheds = [s for s in scheds if s]
        item_ids = [s["order_item_id"] for s in scheds]
        folder = self.order_dir(order["doc_no"])
        if scope == SCOPE_WHOLE:
            name = f"生产排产审批单_整单_{order['doc_no']}.docx"
        else:
            lines = "-".join(f"{self.db.get_by_id('order_items', i)['line_no']:02d}"
                             for i in item_ids)
            name = f"生产排产审批单_合并_{order['doc_no']}_第{lines}项.docx"
        target = self._unique(folder, name)
        ctx = self.order_approval_context(order_id, item_ids, scope)
        path = worddoc.build_order_approval_docx(str(target), ctx, scope=scope)
        self._register_approval(ids, path, operator, shared=len(ids) > 1)
        return path, True

    def _register_approval(self, schedule_ids: Sequence[int], path: str,
                           operator: str = "", shared: bool = False) -> None:
        """把一份审批文档登记到指定排产单（同单据同文件不重复登记）"""
        stored = self.to_stored(Path(path))
        size = Path(path).stat().st_size if Path(path).is_file() else 0
        with self.db.tx():
            for sid in schedule_ids:
                dup = self.db.scalar(
                    "SELECT COUNT(*) FROM order_schedule_files "
                    "WHERE schedule_id = ? AND file_path = ?", (sid, stored), default=0)
                if not dup:
                    fid = self.db.insert("order_schedule_files", {
                        "schedule_id": sid,
                        "kind": KIND_APPROVAL,
                        "file_name": Path(path).name,
                        "file_path": stored,
                        "file_size": size,
                        "created_by": operator,
                        "created_at": now(),
                    })
                    self.db.log_action(
                        "order_schedule", sid, "build_scope_approval",
                        after={"file": Path(path).name, "file_id": fid,
                               "shared": shared}, operator=operator)
                self.db.update("order_schedules",
                               {"status": models.ScheduleStatus.COMPILED,
                                "updated_at": now()}, "id = ?", (sid,))

    def fill_scope(self, schedule_ids: Sequence[int], operator: str = "") -> int:
        """把本范围物资按可排量排满（对话框「保存时排满」勾选项），返回实际补满张数"""
        filled = 0
        for sid in schedule_ids:
            s = self.get(sid)
            if not s or not s.get("link_id"):
                continue
            available = self.orders.available_qty(s["order_item_id"])
            if available > 1e-9:
                self.set_allocated_qty(
                    s["id"], float(s["allocated_qty"] or 0) + available, operator)
                filled += 1
        return filled

    def ensure_order(self, order_id: int, operator: str = "") -> Dict[str, Any]:
        """
        整单排产（v3.0）：订单全部物资各建一张订单排产单（幂等，标 scope=整单），
        逐张排满可排量，再生成一张共用整单审批文档。
        界面入口已改为「先打开订单排产对话框」，本方法保留给服务层与批量脚本。
        """
        order = self.db.get_by_id("orders", order_id)
        if not order:
            raise NotFoundError(f"订单 {order_id} 不存在")
        items = self.orders.list_items(order_id)
        if not items:
            raise StateError(f"订单 {order['doc_no']} 没有物资明细，无法排产")
        res = self.ensure_scope(order_id, [it["id"] for it in items],
                                operator, SCOPE_WHOLE)
        filled = self.fill_scope([s["id"] for s in res["schedules"]], operator)
        path, _created = self.build_scope_approval(
            order_id, [s["id"] for s in res["schedules"]], SCOPE_WHOLE, operator)
        self.recheck_order(order_id, operator)
        return {"order": self.db.get_by_id("orders", order_id),
                "created": res["created"], "auto_filled": filled,
                "approval_path": path, "batch_no": res["batch_no"],
                "schedule_count": len(self.list(order_id))}

    # ── 编辑 / 撤销 ────────────────────────────────────────

    def set_remark(self, schedule_id: int, remark: str, operator: str = "") -> None:
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        if (sched.get("remark") or "") == remark:
            return
        with self.db.tx():
            self.db.update("order_schedules",
                           {"remark": remark, "updated_at": now()},
                           "id = ?", (schedule_id,))
            self.db.log_action("order_schedule", schedule_id, "remark",
                               before={"remark": sched.get("remark")},
                               after={"remark": remark}, operator=operator)

    def set_allocated_qty(self, schedule_id: int, qty: float,
                          operator: str = "") -> Dict[str, Any]:
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        qty = float(qty)
        if qty <= 0:
            raise ValidationError("本次排产数量必须大于 0")
        if not sched.get("link_id"):
            raise StateError("该排产单没有可调整的守恒关联")
        with self.db.tx():
            self.prod.update_allocation(sched["link_id"], qty, operator)
            self.db.update("order_schedules",
                           {"allocated_qty": qty, "updated_at": now()},
                           "id = ?", (schedule_id,))
            self.db.log_action("order_schedule", schedule_id, "set_qty",
                               before={"qty": sched["allocated_qty"]},
                               after={"qty": qty}, operator=operator)
            self._refresh(sched["order_id"], operator)
        return self.db.get_by_id("order_schedules", schedule_id)

    def delete(self, schedule_id: int, operator: str = "") -> None:
        """撤销订单排产单：回收自动建的生产项目与关联、附件登记，再重算订单状态"""
        sched = self.get(schedule_id)
        if not sched:
            raise NotFoundError(f"订单排产单 {schedule_id} 不存在")
        order = self.db.get_by_id("orders", sched["order_id"])
        folder = self.schedule_dir(sched["doc_no"],
                                   order["doc_no"] if order else "")
        with self.db.tx():
            # 先删本单的附件登记，再判断物理文件是否还有兄弟排产单在用：
            # 合并/整单排产的审批文档是多单共用的，删一张排产单不该把别人的文件删掉。
            mine = self.files(schedule_id)
            self.db.execute(
                "DELETE FROM order_schedule_files WHERE schedule_id = ?", (schedule_id,))
            for f in mine:
                left = self.db.scalar(
                    "SELECT COUNT(*) FROM order_schedule_files WHERE file_path = ?",
                    (f["file_path"],), default=0)
                if left:
                    continue
                try:
                    self.to_abs(f["file_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            link_id = sched.get("link_id")
            project_id = sched.get("project_id")
            self.db.execute("DELETE FROM order_schedules WHERE id = ?", (schedule_id,))
            if link_id and self.db.get_by_id("production_links", link_id):
                self.prod.unlink(link_id, operator)
            if sched.get("project_created") and project_id:
                left = self.db.scalar(
                    "SELECT COUNT(*) FROM production_links WHERE project_id = ?",
                    (project_id,), default=0)
                if not left:
                    self.db.execute(
                        "DELETE FROM production_projects WHERE id = ?", (project_id,))
            self.db.log_action("order_schedule", schedule_id, "delete",
                               before={"doc_no": sched["doc_no"]}, operator=operator)
            self._refresh(sched["order_id"], operator)
        try:
            folder.rmdir()       # 目录空了才删，非空说明还有人工文件
            if folder.parent != self.root:
                folder.parent.rmdir()   # 订单目录也空了就一并收掉
        except Exception:
            pass

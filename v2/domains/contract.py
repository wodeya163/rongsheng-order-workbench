#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域 6：采购合同管理（v3.4.0 · 第 14 轮需求；v3.4.5 导入即签订；v3.4.6 金额取自文档）

挂载位置：生产排产操作模块。一个窗口完成「生成 / 导入 / 管理」：
  · 生成：按订单明细即时组装《采购合同》.docx（python-docx），登记为「合同正文」；
  · 导入：从资源管理器一次多选任意格式文件，自动按文件名解析合同编号 / 名称，
          归档到 data/采购合同/<合同号>/ 并把台账状态流转为「已签订」；
  · 管理：台账字段编辑、状态流转、附件打开/重命名/替换/改类别/移除、台账导出、删除。

★ 三条硬口径：
  1. 合同与订单是【可空关联】：可独立登记合同，订单删除时合同保留（ON DELETE SET NULL）；
  2. 合同物资清单【不落快照】，生成文档时直接读订单明细，避免制造第二个数量源，
     数量守恒链路（qty_ordered / production_links）零改动；
  3. 库里 file_path 只存相对 data/ 的 posix 路径，整体拷目录/更新后仍可打开。

★ v3.4.5 新口径（用户第 20 轮）：
  · 导入的文件名形如「CG20261103000221--国能浙江北仑第一发电有限公司--销售排产单-B.xlsx」，
    取【第一个中文字符之前】的 ASCII 段作为合同编号（尾部 -- / _ / 空白自动去掉），
    用【文件名（不含扩展名）】作为合同名称；解析不到编号则退回系统自动号；
  · 只有「新建合同」初始状态是「草稿」；凡经导入动作涉及的合同一律流转为「已签订」
    （执行中 / 已完成属更后阶段，不回退；作废恢复为已签订）；
  · 解析编号已在台账时复用该合同，不重复建档；改号时目录与 file_path 同步迁移。

★ v3.4.6 金额口径（用户第 21 轮）：导入合同文件时，台账「合同金额」一律
  从【合同文档本身】读取（core/contract_amount.py：明确总金额标签 / 中文大写
  金额 / 合计行，自动排除不含税金额、增值税金额等干扰项），**绝不取订单的
  amount_total**。识别不到就留空 0 并在备注提示人工补录；用户手工填过的金额
  不覆盖。历史由订单总额自动带出的错误金额会在同编号再次导入时被纠正，
  也可在管理窗口点「重读金额」直接按已归档文件刷新。

★ 表由 schema.sql 的 CREATE TABLE IF NOT EXISTS 在每次启动时幂等补建，
  schema_meta 目前为 v7（v3.5.0 新增 purchase_contract_items 桥表）。

★ v3.5.1 自动分配建议：suggest_items_from_file() 从已归档的合同文档里读
  【结构化明细表】（物资名称 / 数量 / 含税单价 / 总价），与订单明细按类型词、
  规格数字、数量打分匹配，产出只供预览的建议。**不写库**——写库仍由
  set_contract_items() 在用户点「保存分配」时完成，保持「不猜不摊」口径。

★ v3.5.2 导入即自动应用（用户第 24 轮）：开关落在 data/contract_prefs.json，
  导入合同成功后，仅在【全部行高置信 + 零未匹配 + 对账零差额 + 数量守恒 +
  无手填单价 / 无跨订单分配】同时成立时替用户写入物资分配。任何一条不满足
  都只返回诊断，绝不静默写库；已有非零单价分配行一律不覆盖。「物资分配」页
  的手动预填与「保存分配」链路完全保留，随时可关开关退回原行为。
"""
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core import (cloudfile, codegen, contract_amount, contract_items,
                    contract_prefs, money, worddoc)
from ..core.db import Database, now
from ..core.errors import NotFoundError, StateError, ValidationError

CONTRACT_TYPES = ("物资采购", "外协加工", "设备采购", "其他")
STATUS_DRAFT = "草稿"                      # v3.4.5：仅「新建合同」初始状态
STATUS_SIGNED = "已签订"                   # v3.4.5：导入合同文件后自动流转
STATUSES = (STATUS_DRAFT, STATUS_SIGNED, "执行中", "已完成", "作废")
# v3.4.1：订单页「采购合同编号」列只显示签订后的合同（草稿/作废不算）
SIGNED_STATUSES = ("已签订", "执行中", "已完成")
# 已处于签订之后的阶段，导入附件时不得回退状态
LATER_STATUSES = ("执行中", "已完成")
KIND_MAIN = "合同正文"
KIND_ATTACH = "合同附件"
KIND_SCAN = "扫描件"
KINDS = (KIND_MAIN, KIND_ATTACH, KIND_SCAN, "其他")

_WORD_EXTS = {".doc", ".docx", ".docm", ".wps", ".wpt", ".rtf", ".odt"}
_EXCEL_EXTS = {".xls", ".xlsx", ".xlsm", ".et", ".csv", ".ods"}
_PDF_EXTS = {".pdf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

CONTRACT_FILTER = (
    "采购合同与附件 (*.docx *.doc *.wps *.rtf *.odt *.xls *.xlsx *.xlsm *.et "
    "*.csv *.ods *.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff "
    "*.zip *.rar *.7z);;所有文件 (*.*)")

# 台账可编辑字段（表单与 update 共用一份白名单，避免拼进非法列名）
# v3.5.0：tax_included（统一含税口径标记）、amount_source（金额来源）可由表单维护
EDITABLE = ("contract_name", "contract_type", "order_id", "supplier", "buyer",
            "contact", "amount", "currency", "sign_date", "delivery_date",
            "status", "payment_terms", "remark",
            "tax_included", "amount_source")
AMOUNT_SOURCES = ("doc", "manual", "items")

# v3.5.2：导入即自动应用的判定阈值（不开放给用户配置，避免把口径配没了）
AUTO_APPLY_GAP_TOLERANCE = 0.01         # 合同明细合计 vs 台账金额 / 实际写入金额
AUTO_APPLY_CONFIDENCE = "高"             # 低于「高」一律不自动写库


def auto_apply_summary(result: Dict[str, Any]) -> str:
    """v3.5.2：把导入结果里的自动分配诊断拼成一行 UI 文案"""
    diag = result.get("auto_apply") or {}
    parts: List[str] = []
    for no, info in diag.items():
        if info.get("applied"):
            parts.append(
                f"{no}：已自动分配 {info.get('count', 0)} 项 "
                f"¥{float(info.get('amount') or 0):,.2f}"
                + (f"（替换 {info['replaced']} 条 0 单价占位行）"
                   if info.get("replaced") else ""))
        else:
            parts.append(f"{no}：未自动分配（{info.get('reason') or '条件不满足'}）")
    return "；".join(parts)


def classify_contract_file(path: str) -> str:
    """导入文件按扩展名归类：Word/Excel→合同正文，PDF/图片→扫描件，其余→合同附件"""
    ext = Path(str(path)).suffix.lower()
    if ext in _WORD_EXTS or ext in _EXCEL_EXTS:
        return KIND_MAIN
    if ext in _PDF_EXTS or ext in _IMAGE_EXTS:
        return KIND_SCAN
    return KIND_ATTACH


def amount_summary(result: Dict[str, Any]) -> str:
    """v3.4.6：把导入结果里的金额读取摘要拼成一行 UI 文案"""
    amounts = result.get("amounts") or {}
    parts: List[str] = []
    for no, info in amounts.items():
        value = info.get("amount")
        if info.get("manual_kept"):
            parts.append(f"{no}：{info.get('reason') or '保留手工金额'}")
        elif value:
            parts.append(
                f"{no}：¥{float(value):,.2f}"
                f"（文档「{info.get('label') or '总金额'}」）")
        else:
            parts.append(f"{no}：{info.get('reason') or '未识别到总金额'}")
    return "；".join(parts)


# ── v3.4.5：从合同文件名解析「编号 / 名称」────────────────────
# 中文（含 CJK 扩展 A、中文标点、全角字符）作为编号与名称的分界
_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uf900-\ufaff\uff00-\uffef]")
# 编号尾部常见的连接符 / 分隔空白，解析时统一去掉
_TRAILING_SEP_RE = re.compile(r"[\s\-_—－.·、,，]+$")


def contract_name_from_filename(filename: str) -> str:
    """文件名（不含扩展名）作合同名称；空值回退完整文件名"""
    stem = Path(str(filename or "")).stem.strip()
    return stem or Path(str(filename or "")).name or "采购合同"


def doc_no_from_filename(filename: str) -> str:
    """
    取文件名「第一个中文字符之前」的 ASCII 段作为合同编号。

        CG20261103000221--国能浙江北仑第一发电有限公司-- 销售排产单-B.xlsx
        → CG20261103000221

    规则：① 有中文 → 取中文前那一段；② 无中文 → 取整个 stem（纯英文/数字编号）；
    ③ 去掉首尾空白与尾部连接符；④ 必须含字母或数字，否则视为解析失败返回 ""。
    解析失败时调用方退回系统自动编号，绝不把中文名硬塞进 doc_no。
    """
    stem = Path(str(filename or "")).stem.strip()
    hit = _CJK_RE.search(stem)
    head = stem[:hit.start()] if hit else stem
    head = _TRAILING_SEP_RE.sub("", head.strip()).strip()
    head = re.sub(r"\s+", "", head)
    if not head or not re.search(r"[0-9A-Za-z]", head):
        return ""
    return head[:64]


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(name or "")).strip() or "unnamed"


class PurchaseContractService:
    def __init__(self, db: Database, base_dir: Optional[str] = None):
        self.db = db
        root = Path(base_dir) if base_dir else (
            Path(db.path).resolve().parent if db.path and db.path != ":memory:"
            else Path.cwd() / "data")
        self.data_root = root
        self.root = root / "采购合同"
        # v3.5.2：偏好落在 data/contract_prefs.json；内存库不落盘，
        # 用 _prefs_override 让离屏测试 / 调用方显式注入开关状态。
        self._prefs_dir = root if (
            getattr(db, "path", None) not in (None, "", ":memory:")) else None
        self._prefs_override: Optional[Dict[str, Any]] = None

    # ── v3.5.2：导入即自动应用开关 ──────────────────────────

    def prefs(self) -> Dict[str, Any]:
        """当前导入偏好（内存覆盖优先，其次 data/contract_prefs.json）"""
        if self._prefs_override is not None:
            return dict(self._prefs_override)
        return contract_prefs.load(self._prefs_dir)

    def set_auto_apply(self, enabled: bool) -> Dict[str, Any]:
        """切换「导入即自动分配」并持久化；两个导入入口同时生效"""
        state = self.prefs()
        state["auto_apply_items"] = bool(enabled)
        self._prefs_override = dict(state)
        contract_prefs.save(self._prefs_dir, state)
        return state

    def set_prefs(self, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """显式注入偏好（内存库 / 回归测试用；传 None 清除覆盖）"""
        if state is None:
            self._prefs_override = None
        else:
            self._prefs_override = dict(contract_prefs.DEFAULTS, **{
                k: v for k, v in state.items()
                if k in contract_prefs.DEFAULTS})
        return self.prefs()

    # ── 路径 ────────────────────────────────────────────────

    def to_stored(self, path: Path) -> str:
        p = Path(path)
        if not p.is_absolute():
            return p.as_posix()
        try:
            return p.resolve().relative_to(self.data_root.resolve()).as_posix()
        except (ValueError, OSError):
            return str(p)

    def to_abs(self, stored: str) -> Path:
        p = Path(str(stored or ""))
        return p if p.is_absolute() else (self.data_root / p)

    def contract_dir(self, doc_no: str) -> Path:
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

    # ── 台账查询 ────────────────────────────────────────────

    def get(self, contract_id: int) -> Optional[Dict[str, Any]]:
        return self.db.get_by_id("purchase_contracts", contract_id)

    def list(self, order_id: Optional[int] = None, keyword: str = "",
             status: str = "") -> List[Dict[str, Any]]:
        sql = ("SELECT c.*, o.doc_no AS order_no, o.customer, "
               "(SELECT COUNT(*) FROM purchase_contract_files f "
               " WHERE f.contract_id = c.id) AS file_count "
               "FROM purchase_contracts c "
               "LEFT JOIN orders o ON o.id = c.order_id WHERE 1 = 1 ")
        params: List[Any] = []
        if order_id is not None:
            sql += "AND c.order_id = ? "
            params.append(int(order_id))
        if status:
            sql += "AND c.status = ? "
            params.append(status)
        if keyword and str(keyword).strip():
            kw = f"%{str(keyword).strip()}%"
            sql += ("AND (c.doc_no LIKE ? OR c.contract_name LIKE ? "
                    "OR c.supplier LIKE ? OR o.doc_no LIKE ?) ")
            params.extend([kw] * 4)
        sql += "ORDER BY c.id DESC"
        return self.db.query(sql, tuple(params))

    def files(self, contract_id: int) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM purchase_contract_files WHERE contract_id = ? ORDER BY id",
            (contract_id,))
        for f in rows:
            p = self.to_abs(f["file_path"])
            f["path"] = str(p)
            f["exists"] = p.is_file()
        return rows

    # ── v3.5.0：合同物资分配（多供应商 / 订单盈亏归集）────────
    # 一条订单明细可分给多份合同（分批采购），一份合同也可跨多张订单（合并采购）。
    # 归集键只有 order_item_id，绝不依赖 purchase_contracts.order_id。

    def contract_items(self, contract_id: int) -> List[Dict[str, Any]]:
        """本合同已分配的物资明细（含订单与物资展示信息）"""
        return [dict(r) for r in self.db.query(
            "SELECT pci.*, oi.line_no, oi.item_name, oi.spec, oi.unit, "
            "oi.qty_ordered, oi.order_id, o.project_no, o.doc_no AS order_no "
            "FROM purchase_contract_items pci "
            "JOIN order_items oi ON oi.id = pci.order_item_id "
            "LEFT JOIN orders o ON o.id = oi.order_id "
            "WHERE pci.contract_id = ? ORDER BY oi.order_id, oi.line_no, pci.id",
            (contract_id,))]

    def candidate_items(self, contract_id: int,
                        order_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """可分配的订单明细，带「剩余可采购数量」。

        默认取合同关联订单；显式传 order_id 时支持一份合同覆盖另一张订单的物资。
        已分配数量按【其他合同】累计，避免把本合同自己占用的一份重复扣除。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        oid = int(order_id or c.get("order_id") or 0)
        if not oid:
            return []
        used = {int(r["order_item_id"]): float(r["used_qty"] or 0)
                for r in self.db.query(
                    "SELECT order_item_id, COALESCE(SUM(qty), 0) AS used_qty "
                    "FROM purchase_contract_items WHERE contract_id <> ? "
                    "GROUP BY order_item_id", (contract_id,))}
        mine = {int(r["order_item_id"]): float(r["qty"] or 0)
                for r in self.db.query(
                    "SELECT order_item_id, qty FROM purchase_contract_items "
                    "WHERE contract_id = ?", (contract_id,))}
        out: List[Dict[str, Any]] = []
        for r in self.db.query(
                "SELECT oi.*, o.project_no, o.doc_no AS order_no, o.customer "
                "FROM order_items oi LEFT JOIN orders o ON o.id = oi.order_id "
                "WHERE oi.order_id = ? ORDER BY oi.line_no", (oid,)):
            row = dict(r)
            iid = int(row["id"])
            remain = float(money.q2(money.to_dec(row["qty_ordered"])
                                    - money.to_dec(used.get(iid, 0))))
            row["used_qty"] = round(used.get(iid, 0), 3)
            row["mine_qty"] = round(mine.get(iid, 0), 3)
            row["remaining_qty"] = max(0.0, remain)
            out.append(row)
        return out

    def suggest_items_from_file(self, contract_id: int,
                                order_id: Optional[int] = None) -> Dict[str, Any]:
        """v3.5.1：从已归档合同文档的明细表生成【只供预览】的物资分配建议。

        只读解析、只读匹配，**绝不写库**；调用方（分配页）把 specs 预填进表格，
        用户核对后点「保存分配」才经 set_contract_items() 落库。
        一份合同跨多张订单时，按当前选中的订单分别生成建议。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        oid = int(order_id or c.get("order_id") or 0)
        parsed = None
        used_name = ""
        last_warnings: List[str] = []
        for f in self.files(contract_id):
            p = self.to_abs(f["file_path"])
            if not p.is_file():
                continue
            r = contract_items.read_contract_items(str(p))
            if r.ok:
                parsed, used_name = r, str(f.get("file_name") or p.name)
                break
            last_warnings = r.warnings or last_warnings
        if parsed is None:
            return {
                "ok": False, "source": "", "file_name": "", "order_id": oid,
                "lines": [], "suggestions": [], "unmatched": [], "specs": [],
                "total": 0.0, "contract_amount": float(c.get("amount") or 0),
                "gap": 0.0,
                "warnings": last_warnings or [
                    "该合同没有可用的归档文件，或文件里没有可识别的物资明细表。"],
            }
        candidates = self.candidate_items(contract_id, oid or None)
        result = contract_items.build_suggestions(
            parsed, candidates, float(c.get("amount") or 0),
            str(c.get("doc_no") or ""))
        result["file_name"] = used_name
        result["order_id"] = oid
        return result

    def set_contract_items(self, contract_id: int,
                           specs: Sequence[Dict[str, Any]],
                           operator: str = "") -> Dict[str, Any]:
        """全量替换本合同的物资分配行（UI 分配页保存即走这里）。

        spec = {order_item_id, qty, unit_price(含税), tax_rate, remark}
        ★ 数量守恒：本批 qty + 其他合同已占 qty ≤ order_items.qty_ordered。
        ★ 金额恒为派生值：amount = q2(qty × unit_price)，不接受外部传入。
        ★ 不自动改合同台账金额：金额仍以合同文档为准（v3.4.6 口径），
          需要对齐时用 sync_amount_from_items() 显式操作。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        prepared: List[Dict[str, Any]] = []
        seen: set = set()
        for s in specs:
            iid = int(s.get("order_item_id") or 0)
            if iid in seen:
                raise ValidationError("同一物资在本合同中不能重复分配")
            seen.add(iid)
            item = self.db.get_by_id("order_items", iid)
            if not item:
                raise NotFoundError(f"订单明细 {iid} 不存在")
            try:
                qty = float(money.q4(s.get("qty") or 0))
                price = float(money.q4(s.get("unit_price") or 0))
            except (TypeError, ValueError) as exc:
                raise ValidationError("采购数量 / 单价必须是数字") from exc
            if qty <= 0:
                raise ValidationError("采购数量必须大于 0")
            if price < 0:
                raise ValidationError("采购单价不能为负数")
            other = self.db.scalar(
                "SELECT COALESCE(SUM(qty), 0) FROM purchase_contract_items "
                "WHERE order_item_id = ? AND contract_id <> ?",
                (iid, contract_id), default=0) or 0
            if float(money.to_dec(qty) + money.to_dec(other)) > \
                    float(money.to_dec(item["qty_ordered"])) + 1e-9:
                raise ValidationError(
                    f"物资「{item['item_name']}」采购数量超订单实值："
                    f"本合同 {qty:g} + 其他合同 {float(other):g} > "
                    f"{float(item['qty_ordered']):g}")
            prepared.append({
                "contract_id": contract_id, "order_item_id": iid,
                "qty": qty, "unit_price": price,
                "tax_rate": float(money.q4(s.get("tax_rate") or 0)),
                "amount": float(money.mul(qty, price)),
                "currency": str(c.get("currency") or "CNY"),
                "remark": str(s.get("remark") or ""),
                "created_by": operator, "created_at": now(),
                "updated_at": now(),
            })
        total = money.sum2(x["amount"] for x in prepared)
        with self.db.tx():
            self.db.execute(
                "DELETE FROM purchase_contract_items WHERE contract_id = ?",
                (contract_id,))
            for row in prepared:
                self.db.insert("purchase_contract_items", row)
            self.db.log_action(
                "purchase_contract", contract_id, "set_contract_items",
                after={"count": len(prepared), "amount": float(total)},
                operator=operator)
        return {"count": len(prepared), "amount": float(total),
                "items": self.contract_items(contract_id)}

    def sync_amount_from_items(self, contract_id: int,
                               operator: str = "") -> Dict[str, Any]:
        """显式把合同台账金额对齐到 Σ 已分配明细（amount_source 置 items）"""
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        total = float(money.sum2(
            r["amount"] for r in self.db.query(
                "SELECT amount FROM purchase_contract_items WHERE contract_id = ?",
                (contract_id,))))
        return self.update(contract_id,
                           {"amount": total, "amount_source": "items"}, operator)

    # ── 建 / 改 / 状态 / 删 ─────────────────────────────────

    def create(self, spec: Dict[str, Any], operator: str = "",
               at: Optional[datetime] = None) -> Dict[str, Any]:
        name = str(spec.get("contract_name") or "").strip()
        if not name:
            raise ValidationError("合同名称不能为空")
        oid = spec.get("order_id") or None
        if oid is not None:
            oid = int(oid)
            if not self.db.get_by_id("orders", oid):
                raise NotFoundError(f"关联订单 {oid} 不存在")
        ctype = str(spec.get("contract_type") or CONTRACT_TYPES[0])
        if ctype not in CONTRACT_TYPES:
            ctype = CONTRACT_TYPES[0]
        status = str(spec.get("status") or STATUS_DRAFT)
        if status not in STATUSES:
            raise ValidationError(f"合同状态必须是 {'/'.join(STATUSES)}")
        try:
            amount = float(spec.get("amount") or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError("合同金额必须是数字") from exc
        if amount < 0:
            raise ValidationError("合同金额不能为负数")
        # v3.4.5：导入路径可带「文件名解析出的合同编号」；新建表单不传，
        # 一律走 codegen 自动号 + 初始状态「草稿」。
        raw_no = str(spec.get("doc_no") or "").strip()
        explicit_no = _safe(raw_no)[:64] if raw_no else ""
        ts = now()
        with self.db.tx():
            if explicit_no:
                if self.db.query_one(
                        "SELECT id FROM purchase_contracts WHERE doc_no = ?",
                        (explicit_no,)):
                    raise ValidationError(f"合同编号 {explicit_no} 已存在")
                doc_no = explicit_no
            else:
                doc_no = codegen.next_doc_no(self.db, codegen.PREFIX_CONTRACT, at)
            cid = self.db.insert("purchase_contracts", {
                "doc_no": doc_no,
                "contract_name": name,
                "contract_type": ctype,
                "order_id": oid,
                "supplier": str(spec.get("supplier") or "").strip(),
                "buyer": str(spec.get("buyer") or "").strip(),
                "contact": str(spec.get("contact") or "").strip(),
                "amount": amount,
                "currency": str(spec.get("currency") or "CNY"),
                # v3.5.0 统一含税口径；表单新建记 manual，导入链路显式传 doc
                "tax_included": 0 if str(spec.get("tax_included")) in ("0", "False", "false") else 1,
                "amount_source": (str(spec.get("amount_source"))
                                  if str(spec.get("amount_source")) in AMOUNT_SOURCES
                                  else "manual"),
                "sign_date": str(spec.get("sign_date") or ""),
                "delivery_date": str(spec.get("delivery_date") or ""),
                "status": status,
                "payment_terms": str(spec.get("payment_terms") or ""),
                "remark": str(spec.get("remark") or ""),
                "created_by": operator,
                "created_at": ts,
                "updated_at": ts,
            })
            self.db.log_action("purchase_contract", cid, "create",
                               after={"doc_no": doc_no, "name": name,
                                      "order_id": oid, "amount": amount},
                               operator=operator)
        return dict(self.get(cid) or {})

    def update(self, contract_id: int, changes: Dict[str, Any],
               operator: str = "") -> Dict[str, Any]:
        old = self.get(contract_id)
        if not old:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        data: Dict[str, Any] = {}
        for key in EDITABLE:
            if key not in changes:
                continue
            value = changes[key]
            if key == "order_id":
                value = int(value) if value not in (None, "") else None
                if value is not None and not self.db.get_by_id("orders", value):
                    raise NotFoundError(f"关联订单 {value} 不存在")
            elif key == "amount":
                try:
                    value = float(value or 0)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("合同金额必须是数字") from exc
                if value < 0:
                    raise ValidationError("合同金额不能为负数")
            elif key == "contract_name":
                value = str(value or "").strip()
                if not value:
                    raise ValidationError("合同名称不能为空")
            elif key == "status":
                value = str(value or STATUSES[0])
                if value not in STATUSES:
                    raise ValidationError(f"合同状态必须是 {'/'.join(STATUSES)}")
            elif key == "contract_type":
                value = str(value or CONTRACT_TYPES[0])
                if value not in CONTRACT_TYPES:
                    raise ValidationError(f"合同类型必须是 {'/'.join(CONTRACT_TYPES)}")
            elif key == "tax_included":
                value = 0 if str(value) in ("0", "False", "false", "不含税") else 1
            elif key == "amount_source":
                value = str(value or "manual")
                if value not in AMOUNT_SOURCES:
                    value = "manual"
            else:
                value = "" if value is None else str(value)
            data[key] = value
        if not data:
            return dict(old)
        # order_id 允许被清空：db.update 会丢弃 None，这里用裸 SQL 显式置 NULL
        clear_order = data.pop("order_id", "__NO_CHANGE__") if "order_id" in data else "__NO_CHANGE__"
        data["updated_at"] = now()
        with self.db.tx():
            if data:
                self.db.update("purchase_contracts", data, "id = ?", (contract_id,))
            if clear_order != "__NO_CHANGE__":
                self.db.execute(
                    "UPDATE purchase_contracts SET order_id = ?, updated_at = ? "
                    "WHERE id = ?", (clear_order, now(), contract_id))
            before = {k: old.get(k) for k in changes if k in old}
            self.db.log_action("purchase_contract", contract_id, "update",
                               before=before, after=changes, operator=operator)
        return dict(self.get(contract_id) or {})

    def set_status(self, contract_id: int, status: str,
                   operator: str = "") -> Dict[str, Any]:
        if status not in STATUSES:
            raise ValidationError(f"合同状态必须是 {'/'.join(STATUSES)}")
        old = self.get(contract_id)
        if not old:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        if old["status"] == status:
            return dict(old)
        with self.db.tx():
            self.db.update("purchase_contracts",
                           {"status": status, "updated_at": now()},
                           "id = ?", (contract_id,))
            self.db.log_action("purchase_contract", contract_id, "set_status",
                               before={"status": old["status"]},
                               after={"status": status}, operator=operator)
        return dict(self.get(contract_id) or {})

    # ── v3.4.5：导入即签订 / 编号升格 ────────────────────────

    def mark_signed(self, contract_id: int, operator: str = "") -> Dict[str, Any]:
        """
        导入合同文件后的状态口径：草稿 / 作废 → 已签订；
        已处于「执行中 / 已完成」的合同不回退（导入附件不改变其阶段）。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        if c["status"] in LATER_STATUSES:
            return dict(c)
        return self.set_status(contract_id, STATUS_SIGNED, operator)

    def rename_doc_no(self, contract_id: int, new_doc_no: str,
                      operator: str = "") -> Dict[str, Any]:
        """
        把（通常是刚自动登记的）合同编号改为文件名解析出的业务编号。
        同步迁移 data/采购合同/<旧号>/ 下已登记文件与 file_path，绝不留下死链。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        raw = str(new_doc_no or "").strip()
        new_no = _safe(raw)[:64] if raw else ""
        if not new_no:
            raise ValidationError("合同编号不能为空")
        old_no = str(c["doc_no"])
        if new_no == old_no:
            return dict(c)
        if self.db.query_one(
                "SELECT id FROM purchase_contracts WHERE doc_no = ? AND id <> ?",
                (new_no, contract_id)):
            raise ValidationError(f"合同编号 {new_no} 已存在")
        old_dir = self.root / _safe(old_no)
        new_dir = self.root / _safe(new_no)
        rows = self.db.query(
            "SELECT * FROM purchase_contract_files WHERE contract_id = ?",
            (contract_id,))
        moved: Dict[int, tuple] = {}
        if old_dir.is_dir():
            new_dir.mkdir(parents=True, exist_ok=True)
            for f in rows:
                src = self.to_abs(f["file_path"])
                try:
                    rel = src.resolve().relative_to(old_dir.resolve())
                except (ValueError, OSError):
                    continue                      # 绝对路径的外部文件不动
                dst = new_dir / rel
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        dst = self._unique(dst.parent, dst.name)
                    shutil.move(str(src), str(dst))
                moved[int(f["id"])] = (dst.name, self.to_stored(dst))
            known = {name for name, _ in moved.values()}
            for item in list(old_dir.iterdir()):
                # 目录里未登记的残留文件一并搬走，避免旧编号目录悬挂
                if item.is_file() and item.name not in known:
                    shutil.move(str(item), str(self._unique(new_dir, item.name)))
            try:
                old_dir.rmdir()
            except OSError:
                pass
        with self.db.tx():
            for fid, (name, stored) in moved.items():
                self.db.update("purchase_contract_files",
                               {"file_name": name, "file_path": stored},
                               "id = ?", (fid,))
            self.db.update("purchase_contracts",
                           {"doc_no": new_no, "updated_at": now()},
                           "id = ?", (contract_id,))
            self.db.log_action("purchase_contract", contract_id, "rename_doc_no",
                               before={"doc_no": old_no},
                               after={"doc_no": new_no, "files": len(moved)},
                               operator=operator)
        return dict(self.get(contract_id) or {})

    def delete(self, contract_id: int, operator: str = "") -> Dict[str, Any]:
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        files = self.files(contract_id)
        folder = self.contract_dir(c["doc_no"])
        with self.db.tx():
            self.db.execute(
                "DELETE FROM purchase_contract_files WHERE contract_id = ?",
                (contract_id,))
            self.db.execute("DELETE FROM purchase_contracts WHERE id = ?",
                            (contract_id,))
            self.db.log_action("purchase_contract", contract_id, "delete",
                               before={"doc_no": c["doc_no"],
                                       "name": c["contract_name"],
                                       "files": len(files)}, operator=operator)
        removed = 0
        for f in files:
            try:
                self.to_abs(f["file_path"]).unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
        try:
            folder.rmdir()
        except Exception:
            pass
        return {"doc_no": c["doc_no"], "files": len(files), "removed": removed}

    # ── 生成合同文档 ────────────────────────────────────────

    def context(self, contract_id: int) -> Dict[str, Any]:
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        order = (self.db.get_by_id("orders", c["order_id"])
                 if c.get("order_id") else None) or {}
        items: List[Dict[str, Any]] = []
        # v3.5.0：优先按【本合同已分配的物资明细】生成，单价/总价一律用采购口径。
        # 旧版把整单全部明细 + 销售单价写进每份合同，一单多供应商时必然算错；
        # 未做分配时保留旧回退（兼容历史流程），但明确标记 items_source 供 UI 提示。
        allocated = [dict(r) for r in self.db.query(
            "SELECT pci.qty, pci.unit_price, pci.amount, pci.remark, "
            "oi.line_no, oi.item_code, oi.item_name, oi.production_name, "
            "oi.spec, oi.unit, oi.order_id "
            "FROM purchase_contract_items pci "
            "JOIN order_items oi ON oi.id = pci.order_item_id "
            "WHERE pci.contract_id = ? ORDER BY oi.order_id, oi.line_no, pci.id",
            (contract_id,))]
        source = "contract_items"
        if allocated:
            items = [{
                "line_no": it["line_no"],
                "item_code": it.get("item_code") or "",
                "item_name": it.get("item_name") or "",
                "production_name": it.get("production_name") or "",
                "spec": it.get("spec") or "",
                "unit": it.get("unit") or "",
                "qty_ordered": it.get("qty") or 0,
                "unit_price": it.get("unit_price") or 0,
                "amount": it.get("amount") or 0,
            } for it in allocated]
        elif order:
            source = "order_items_fallback"
            items = [{
                "line_no": it["line_no"],
                "item_code": it.get("item_code") or "",
                "item_name": it.get("item_name") or "",
                "production_name": it.get("production_name") or "",
                "spec": it.get("spec") or "",
                "unit": it.get("unit") or "",
                "qty_ordered": it.get("qty_ordered") or 0,
                "unit_price": it.get("unit_price") or 0,
                "amount": it.get("amount") or 0,
            } for it in self.db.query(
                "SELECT * FROM order_items WHERE order_id = ? ORDER BY line_no",
                (order["id"],))]
        ctx = dict(c)
        ctx.update({
            "order_no": order.get("doc_no") or "",
            "project_no": order.get("project_no") or "",
            "customer": order.get("customer") or "",
            "erp_order_no": order.get("erp_order_no") or "",
            "required_date": order.get("required_date") or "",
            "items": items,
            "items_source": source,
            "created_at": str(now())[:10],
        })
        return ctx

    def generate(self, contract_id: int, operator: str = "",
                 overwrite: bool = False) -> Dict[str, Any]:
        """
        生成《采购合同》.docx 并登记为「合同正文」。
        已有同名文档在盘时默认复用（幂等，不会重复造文件）；overwrite=True 才重建。
        返回 {"path", "created", "file_id", "doc_no"}。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        folder = self.contract_dir(c["doc_no"])
        name = f"采购合同_{c['doc_no']}.docx"
        target = folder / name
        stored = self.to_stored(target)
        hit = self.db.query_one(
            "SELECT * FROM purchase_contract_files "
            "WHERE contract_id = ? AND file_path = ?", (contract_id, stored))
        if hit and target.is_file() and not overwrite:
            return {"path": str(target), "created": False,
                    "file_id": hit["id"], "doc_no": c["doc_no"]}
        path = worddoc.build_purchase_contract_docx(
            str(target), self.context(contract_id))
        if hit:
            with self.db.tx():
                self.db.update("purchase_contract_files",
                               {"file_size": Path(path).stat().st_size,
                                "kind": KIND_MAIN}, "id = ?", (hit["id"],))
                self.db.log_action("purchase_contract", contract_id,
                                   "regenerate_contract",
                                   after={"file": Path(path).name},
                                   operator=operator)
            fid = int(hit["id"])
        else:
            fid = self._register_file(contract_id, path, KIND_MAIN, operator)
        return {"path": path, "created": True, "file_id": fid,
                "doc_no": c["doc_no"]}

    def _register_file(self, contract_id: int, path: str, kind: str,
                       operator: str = "") -> int:
        p = Path(path)
        with self.db.tx():
            fid = self.db.insert("purchase_contract_files", {
                "contract_id": contract_id,
                "kind": kind,
                "file_name": p.name,
                "file_path": self.to_stored(p),
                "file_size": p.stat().st_size if p.is_file() else 0,
                "created_by": operator,
                "created_at": now(),
            })
            self.db.log_action("purchase_contract_file", fid, "import",
                               after={"contract_id": contract_id, "kind": kind,
                                      "file": p.name}, operator=operator)
        return fid

    # ── 导入 / 附件管理 ─────────────────────────────────────

    def import_files(self, contract_id: int, src_paths: Sequence[str],
                     operator: str = "") -> Dict[str, Any]:
        """一次导入任意格式文件，自动分类并复制到 data/采购合同/<合同号>/"""
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        folder = self.contract_dir(c["doc_no"])
        added: List[Dict[str, Any]] = []
        kinds: Dict[str, int] = {}
        for src in src_paths:
            local = cloudfile.ensure_readable(str(src))
            p = Path(local)
            if not p.is_file():
                raise ValidationError(f"文件不存在：{src}")
            target = self._unique(folder, Path(str(src)).name)
            shutil.copy2(str(p), str(target))
            kind = classify_contract_file(src)
            fid = self._register_file(contract_id, str(target), kind, operator)
            kinds[kind] = kinds.get(kind, 0) + 1
            added.append(self.db.get_by_id("purchase_contract_files", fid))
        return {"files": len(src_paths), "kinds": kinds, "added": added}

    # ── v3.4.6：合同金额只认合同文档 ────────────────────────

    def _linked_order_total(self, c: Optional[Dict[str, Any]],
                            fallback_order_id: Optional[int] = None) -> float:
        """只用于识别「历史自动带出的订单总额」，不作为合同金额来源"""
        oid = (c or {}).get("order_id") or fallback_order_id
        if not oid:
            return 0.0
        row = self.db.get_by_id("orders", int(oid))
        return float((row or {}).get("amount_total") or 0)

    def _amount_is_auto(self, c: Dict[str, Any], order_total: float) -> bool:
        """
        台账金额是否属于「自动带出的订单总额 / 留空」。
        用户手工填过（与订单总额不同且非 0）的金额一律不覆盖。
        """
        try:
            current = float(c.get("amount") or 0)
        except (TypeError, ValueError):
            return True
        return current == 0 or (order_total > 0
                                and abs(current - order_total) <= 0.005)

    def _apply_doc_amount(self, target: Dict[str, Any], hit: Dict[str, Any],
                          order_total: float, operator: str = ""
                          ) -> tuple:
        """
        把文档解析出的总金额写进台账，返回 (台账快照, 摘要)。

        · 解析到金额 + 台账金额为自动来源 → 写入；
        · 解析到金额 + 台账金额为用户手工填写 → 保留并说明；
        · 未解析到金额 + 台账金额正是订单总额 → 清 0（不再拿订单总额顶替）。
        """
        try:
            old = float(target.get("amount") or 0)
        except (TypeError, ValueError):
            old = 0.0
        label = str(hit.get("label") or "总金额")
        src = str(hit.get("source") or "")
        doc_amount = hit.get("amount")
        info: Dict[str, Any] = {
            "before": old, "amount": old, "label": label, "source": src,
            "changed": False, "manual_kept": False, "reason": "",
        }
        auto = self._amount_is_auto(target, order_total)
        if doc_amount is None:
            if auto and old > 0:
                target = self.update(int(target["id"]), {"amount": 0}, operator)
                info.update(amount=0.0, changed=True,
                            reason="未从合同文档识别到总金额，已清除原订单总额，请手工补录")
                self.db.log_action(
                    "purchase_contract", int(target["id"]), "clear_order_amount",
                    before={"amount": old}, after={"amount": 0}, operator=operator)
            else:
                info["reason"] = "未从合同文档识别到总金额，台账金额保持不变"
            return target, info

        amount = round(float(doc_amount), 2)
        if not auto and abs(old - amount) > 0.005:
            info.update(manual_kept=True,
                        reason=(f"台账金额 ¥{old:,.2f} 为手工填写，已保留"
                                f"（文档识别 ¥{amount:,.2f}）"))
            return target, info
        if abs(old - amount) <= 0.005:
            info.update(amount=amount, reason=f"合同金额与文档「{label}」一致")
            return target, info

        target = self.update(int(target["id"]), {"amount": amount}, operator)
        self.db.log_action(
            "purchase_contract", int(target["id"]), "set_amount_from_doc",
            before={"amount": old},
            after={"amount": amount, "label": label, "source": src},
            operator=operator)
        info.update(amount=amount, changed=True,
                    reason=f"合同金额按文档「{label}」读取")
        return target, info

    def refresh_amount_from_files(self, contract_id: int,
                                  operator: str = "") -> Dict[str, Any]:
        """
        「重读金额」：对该合同已归档的全部文件重新解析总金额并写回台账。
        用于纠正 v3.4.5 及以前被订单总额顶替的历史台账，无需重新选文件。
        """
        c = self.get(contract_id)
        if not c:
            raise NotFoundError(f"采购合同 {contract_id} 不存在")
        hits = []
        for f in self.files(contract_id):
            p = self.to_abs(f["file_path"])
            if p.is_file():
                hits.append(contract_amount.read_contract_amount(
                    str(p), allow_ocr=False))
        best = contract_amount.best_amount(hits)
        total = self._linked_order_total(c)
        try:
            old = float(c.get("amount") or 0)
        except (TypeError, ValueError):
            old = 0.0
        if not hits or best.get("amount") is None:
            # 「重读」是保守动作：读不到金额就保持原值，绝不清空历史台账
            return {"doc_no": c["doc_no"], "files": len(hits),
                    "before": old, "amount": old, "label": "",
                    "source": "", "changed": False, "manual_kept": False,
                    "reason": "未从已归档文件识别到总金额，台账金额保持不变",
                    "contract": dict(c)}
        updated, info = self._apply_doc_amount(c, best, total, operator)
        info.update({"doc_no": updated["doc_no"], "files": len(self.files(contract_id)),
                     "contract": dict(updated)})
        return info

    # ── v3.5.2：导入即自动应用 ──────────────────────────────

    def _auto_apply_items(self, contract_id: int, operator: str,
                          enabled: bool) -> Dict[str, Any]:
        """
        导入合同成功后尝试一次性写入物资分配（替用户省掉「保存分配」那一步）。

        严格条件，任一不满足即跳过且【零写库】：
          ① 开关开启；② 台账处于已签订之后；③ 关联了订单；
          ④ 本合同没有手填的非零单价分配行、没有跨订单分配行；
          ⑤ 结构化解析出明细表、零未匹配、全部行「高」置信；
          ⑥ 合同明细合计 = 台账金额 = 实际写入金额（差额 ≤ 0.01）；
          ⑦ set_contract_items() 的数量守恒校验通过。
        跳过时把原因写进返回诊断，由 UI 提示用户去「物资分配」页手工处理。
        """
        diag: Dict[str, Any] = {
            "applied": False, "reason": "", "doc_no": "",
            "count": 0, "amount": 0.0, "replaced": 0,
            "lines": 0, "unmatched": 0, "gap": 0.0,
            "confidence": "", "file_name": "", "source": "",
        }
        c = self.get(contract_id)
        if not c:
            diag["reason"] = "合同台账不存在"
            return diag
        diag["doc_no"] = str(c.get("doc_no") or "")
        if not enabled:
            diag["reason"] = "「导入即自动分配」未开启"
            return diag
        if c.get("status") not in SIGNED_STATUSES:
            diag["reason"] = f"台账状态为「{c.get('status')}」，非已签订之后"
            return diag
        oid = int(c.get("order_id") or 0)
        if not oid:
            diag["reason"] = "台账未关联订单，无法定位物资明细"
            return diag

        existing = self.contract_items(contract_id)
        priced = [a for a in existing
                  if float(a.get("unit_price") or 0) > 0]
        if priced:
            diag["reason"] = (f"已有 {len(priced)} 条手填单价的分配行，"
                              "自动应用不覆盖人工数据")
            return diag
        other_orders = [a for a in existing
                        if int(a.get("order_id") or 0) != oid]
        if other_orders:
            n = len({int(a.get("order_id") or 0) for a in other_orders})
            diag["reason"] = (f"本合同已覆盖另外 {n} 张订单的物资，"
                              "请在「物资分配」页逐单处理")
            return diag
        diag["replaced"] = len(existing)     # 允许覆盖的只会是 0 单价占位行

        try:
            sug = self.suggest_items_from_file(contract_id, oid)
        except Exception as exc:                             # noqa: BLE001
            diag["reason"] = f"合同明细解析异常：{exc}"
            return diag
        diag.update({
            "lines": len(sug.get("lines") or []),
            "unmatched": len(sug.get("unmatched") or []),
            "gap": float(sug.get("gap") or 0),
            "file_name": str(sug.get("file_name") or ""),
            "source": str(sug.get("source") or ""),
        })
        if not sug.get("ok") or not sug.get("specs"):
            warns = sug.get("warnings") or ["合同里没有可识别的物资明细表"]
            diag["reason"] = str(warns[0])
            return diag
        if sug.get("unmatched"):
            diag["reason"] = (f"还有 {len(sug['unmatched'])} 行明细"
                              "未匹配到订单物资")
            return diag
        low = [s for s in sug["suggestions"]
               if s.get("confidence") != AUTO_APPLY_CONFIDENCE]
        if low:
            diag["reason"] = f"{len(low)} 行仅为「需复核」，不自动写入"
            return diag
        if abs(float(sug.get("gap") or 0)) > AUTO_APPLY_GAP_TOLERANCE:
            diag["reason"] = (f"合同明细合计与台账金额相差 "
                              f"¥{float(sug['gap']):,.2f}，需人工核对")
            return diag

        specs = [dict(s) for s in sug["specs"]]
        apply_total = float(money.sum2(
            money.mul(s["qty"], s["unit_price"]) for s in specs))
        if abs(apply_total - float(sug.get("total") or 0)) > AUTO_APPLY_GAP_TOLERANCE:
            diag["reason"] = (f"实际可写入金额 ¥{apply_total:,.2f} 与合同明细合计 "
                              f"¥{float(sug['total']):,.2f} 不一致"
                              "（数量截断等），不自动写入")
            return diag
        for s in specs:
            s["remark"] = (f"导入自动应用：{sug.get('file_name') or '合同明细表'}"
                           f" 第 {sug.get('table_index') or 1} 张表")

        try:
            res = self.set_contract_items(contract_id, specs, operator)
        except Exception as exc:                             # noqa: BLE001
            diag["reason"] = f"写入被拦截：{exc}"
            return diag
        self.db.log_action(
            "purchase_contract", contract_id, "auto_apply_items",
            after={"file": sug.get("file_name") or "",
                   "source": sug.get("source") or "",
                   "lines": diag["lines"], "count": res["count"],
                   "amount": res["amount"], "gap": diag["gap"],
                   "replaced": diag["replaced"]},
            operator=operator)
        diag.update({
            "applied": True, "reason": "全部条件通过，已自动写入物资分配",
            "count": res["count"], "amount": res["amount"],
            "confidence": AUTO_APPLY_CONFIDENCE,
        })
        return diag

    # ── v3.4.5：导入即签订 ──────────────────────────────────

    def _claimable(self, c: Optional[Dict[str, Any]]) -> bool:
        """
        草稿台账允许被导入动作认领并升格（编号 / 名称按文件名、状态改已签订）。
        已经生成过 Word 模板正文的草稿也照常认领：rename_doc_no 会连同附件
        一起迁移目录与 file_path，不会留下第二份空台账或死链。
        """
        return bool(c) and c["status"] == STATUS_DRAFT

    def import_contract_files(self, order_id: Optional[int],
                              src_paths: Sequence[str], operator: str = "",
                              contract_id: Optional[int] = None,
                              auto_apply: Optional[bool] = None) -> Dict[str, Any]:
        """
        生产排产 / 合同管理两个「导入合同」入口共用的新链路（v3.4.5）：

          ① 文件名第一个中文字符之前的 ASCII 段 → 合同编号；
          ② 文件名（不含扩展名）→ 合同名称；
          ③ 涉及到的合同一律流转为「已签订」（执行中 / 已完成不回退）；
          ④ 相同编号的文件归到同一份合同，不同编号分别建档；
             编号已在台账时直接复用，不重复登记；
          ⑤ 解析不出编号时退到选中合同 / 该订单已有合同 / 系统自动号；
          ⑥ v3.4.6：每组文件先解析「合同文档总金额」，台账金额只认文档，
             绝不取订单 amount_total；解析不到留空 0 并提示人工补录；
          ⑦ v3.5.2：附件归档完成后尝试「导入即自动应用」物资分配；
             auto_apply=None 时读 data/contract_prefs.json 开关，调用方可
             显式传 True/False 覆盖。严格条件不满足只返回诊断、零写库。

        返回 {"files", "kinds", "added", "contracts", "created",
              "amounts", "amount_missing", "auto_apply", "auto_applied"}；
        contracts 是本批次涉及的全部合同快照，amounts 以合同号为键给出
        金额读取摘要，auto_apply 以合同号给出自动分配诊断，UI 用它汇总提示。
        """
        items: List[Dict[str, Any]] = []
        for src in src_paths:
            local = cloudfile.ensure_readable(str(src))
            p = Path(local)
            if not p.is_file():
                raise ValidationError(f"文件不存在：{src}")
            fname = Path(str(src)).name
            # v3.4.6：只读解析，任何异常由解析器内部吞掉，不影响归档
            hit = contract_amount.read_contract_amount(str(p), allow_ocr=False)
            items.append({
                "src": str(src), "local": str(p), "name": fname,
                "no": doc_no_from_filename(fname),
                "contract_name": contract_name_from_filename(fname),
                "amount_hit": hit,
            })
        empty = {"files": len(items), "kinds": {}, "added": [],
                 "contracts": [], "created": 0,
                 "amounts": {}, "amount_missing": [],
                 "auto_apply": {}, "auto_applied": 0}
        if not items:
            return empty
        apply_flag = (self.prefs().get("auto_apply_items", True)
                      if auto_apply is None else bool(auto_apply))

        order = (self.db.get_by_id("orders", int(order_id))
                 if order_id else None) or {}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for it in items:
            groups.setdefault(it["no"], []).append(it)

        selected = self.get(int(contract_id)) if contract_id else None
        claimed = False
        kinds: Dict[str, int] = {}
        added: List[Dict[str, Any]] = []
        contracts: List[Dict[str, Any]] = []
        amounts: Dict[str, Dict[str, Any]] = {}
        amount_missing: List[str] = []
        apply_diag: Dict[str, Dict[str, Any]] = {}
        auto_applied = 0
        created = 0

        for no, group in groups.items():
            # v3.4.6：同组多文件取置信度最高的金额命中
            hit = contract_amount.best_amount(
                [it["amount_hit"] for it in group])
            target = (self.db.query_one(
                "SELECT * FROM purchase_contracts WHERE doc_no = ?", (no,))
                if no else None)
            if (target is None and selected is not None and not claimed
                    and self._claimable(selected)):
                target, claimed = selected, True
            if target is None and not no and selected is not None:
                target = selected                 # 无编号文件归入当前选中合同
            if target is None and not no and order_id:
                rows = self.list(order_id=int(order_id))
                if rows:
                    target = self.get(int(rows[0]["id"]))
            if target is None:
                # ★ 不再把订单 amount_total 写进合同金额：先建 0，随后统一按文档写
                target = self.create({
                    "doc_no": no,
                    "contract_name": group[0]["contract_name"],
                    "contract_type": CONTRACT_TYPES[0],
                    "order_id": int(order_id) if order_id else None,
                    "buyer": order.get("customer") or "",
                    "delivery_date": order.get("required_date") or "",
                    "amount_source": "doc",       # v3.5.0 金额来自合同文档
                    "tax_included": 1,            # v3.5.0 统一含税口径
                    "status": STATUS_SIGNED,
                    "remark": ("由采购合同导入自动登记：编号 / 名称取自文件名，"
                               "状态已签订；合同金额取自合同文档（非订单总额），"
                               "供方与商务条款请在台账补充。"),
                }, operator)
                created += 1
            else:
                # 草稿台账（含「生成合同」时自动登记的那份）由导入动作升格：
                # 业务编号 / 名称一律以文件名为准，再流转为「已签订」。
                # 已签订 / 执行中 / 完成的台账不改名改号，只补附件。
                was_draft = target["status"] == STATUS_DRAFT
                if was_draft and no and no != target["doc_no"]:
                    target = self.rename_doc_no(int(target["id"]), no, operator)
                if was_draft:
                    target = self.update(
                        int(target["id"]),
                        {"contract_name": group[0]["contract_name"]}, operator)
                target = self.mark_signed(int(target["id"]), operator)

            # 订单总额仅用于识别「历史自动带出的错误金额」，不作为金额来源
            order_total = self._linked_order_total(target, order_id)
            target, info = self._apply_doc_amount(target, hit, order_total,
                                                  operator)
            amounts[target["doc_no"]] = info
            if hit.get("amount") is None:
                amount_missing.append(target["doc_no"])

            folder = self.contract_dir(target["doc_no"])
            for it in group:
                dst = self._unique(folder, it["name"])
                shutil.copy2(it["local"], str(dst))
                kind = classify_contract_file(it["src"])
                fid = self._register_file(int(target["id"]), str(dst),
                                          kind, operator)
                kinds[kind] = kinds.get(kind, 0) + 1
                added.append(self.db.get_by_id("purchase_contract_files", fid))
                self.db.log_action(
                    "purchase_contract", int(target["id"]), "import_signed",
                    after={"file": it["name"], "doc_no": target["doc_no"],
                           "status": target["status"],
                           "amount": target["amount"],
                           "amount_source": hit.get("source") or ""},
                    operator=operator)
            # v3.5.2：附件归档完成后才尝试自动应用——此时文件已登记，
            # suggest_items_from_file() 才读得到合同明细表。
            diag = self._auto_apply_items(int(target["id"]), operator,
                                          bool(apply_flag))
            apply_diag[str(target["doc_no"])] = diag
            if diag["applied"]:
                auto_applied += 1
            contracts.append(dict(self.get(int(target["id"])) or {}))

        return {"files": len(items), "kinds": kinds, "added": added,
                "contracts": contracts, "created": created,
                "amounts": amounts, "amount_missing": amount_missing,
                "auto_apply": apply_diag, "auto_applied": auto_applied}

    def open_file(self, file_id: int):
        f = self.db.get_by_id("purchase_contract_files", file_id)
        if not f:
            raise NotFoundError(f"合同附件 {file_id} 不存在")
        return worddoc.open_file(str(self.to_abs(f["file_path"])))

    def rename_file(self, file_id: int, new_name: str,
                    operator: str = "") -> Dict[str, Any]:
        f = self.db.get_by_id("purchase_contract_files", file_id)
        if not f:
            raise NotFoundError(f"合同附件 {file_id} 不存在")
        name = _safe(new_name)
        old = Path(str(f["file_name"]))
        if not name or name in (".", ".."):
            raise ValidationError("文件名不能为空")
        if Path(name).suffix.lower() != old.suffix.lower():
            raise ValidationError("扩展名不可更改，否则合同文件会打不开")
        src = self.to_abs(f["file_path"])
        target = src.parent / name
        if target != src and target.exists():
            raise ValidationError(f"同目录下已存在同名文件：{name}")
        with self.db.tx():
            if src.is_file():
                src.rename(target)
            self.db.update("purchase_contract_files",
                           {"file_name": name, "file_path": self.to_stored(target)},
                           "id = ?", (file_id,))
            self.db.log_action("purchase_contract_file", file_id, "rename",
                               before={"file": f["file_name"]},
                               after={"file": name}, operator=operator)
        return dict(self.db.get_by_id("purchase_contract_files", file_id) or {})

    def replace_file(self, file_id: int, src_path: str,
                     operator: str = "") -> Dict[str, Any]:
        f = self.db.get_by_id("purchase_contract_files", file_id)
        if not f:
            raise NotFoundError(f"合同附件 {file_id} 不存在")
        local = cloudfile.ensure_readable(str(src_path))
        p = Path(local)
        if not p.is_file():
            raise ValidationError(f"文件不存在：{src_path}")
        target = self.to_abs(f["file_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(p), str(target))
        size = target.stat().st_size
        with self.db.tx():
            self.db.update("purchase_contract_files", {"file_size": size},
                           "id = ?", (file_id,))
            self.db.log_action("purchase_contract_file", file_id, "replace",
                               before={"size": f["file_size"]},
                               after={"size": size,
                                      "source": Path(str(src_path)).name},
                               operator=operator)
        return dict(self.db.get_by_id("purchase_contract_files", file_id) or {})

    def set_file_kind(self, file_id: int, kind: str,
                      operator: str = "") -> Dict[str, Any]:
        if kind not in KINDS:
            raise ValidationError(f"附件类别必须是 {'/'.join(KINDS)}")
        f = self.db.get_by_id("purchase_contract_files", file_id)
        if not f:
            raise NotFoundError(f"合同附件 {file_id} 不存在")
        with self.db.tx():
            self.db.update("purchase_contract_files", {"kind": kind},
                           "id = ?", (file_id,))
            self.db.log_action("purchase_contract_file", file_id, "set_kind",
                               before={"kind": f["kind"]},
                               after={"kind": kind}, operator=operator)
        return dict(self.db.get_by_id("purchase_contract_files", file_id) or {})

    def remove_file(self, file_id: int, operator: str = "") -> int:
        f = self.db.get_by_id("purchase_contract_files", file_id)
        if not f:
            raise NotFoundError(f"合同附件 {file_id} 不存在")
        with self.db.tx():
            self.db.execute("DELETE FROM purchase_contract_files WHERE id = ?",
                            (file_id,))
            self.db.log_action("purchase_contract_file", file_id, "remove",
                               before={"file": f["file_name"]}, operator=operator)
        try:
            self.to_abs(f["file_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        return 1

    # ── 台账导出 ────────────────────────────────────────────

    def export_sheets(self) -> Dict[str, Any]:
        heads = self.db.query("SELECT * FROM purchase_contracts ORDER BY id")
        files = self.db.query("SELECT * FROM purchase_contract_files ORDER BY id")
        return {
            "采购合同台账": (list(heads[0].keys()) if heads else ["(空)"],
                        [list(r.values()) for r in heads]),
            "合同附件": (list(files[0].keys()) if files else ["(空)"],
                     [list(r.values()) for r in files]),
        }

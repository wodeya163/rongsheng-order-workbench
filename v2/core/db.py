#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据访问层：连接管理、事务边界、通用 CRUD、审计留痕。
业务层只依赖本模块，将来换 PostgreSQL / 多用户时只替换这里。
"""
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import current_user
from . import tables as tbl

# ── 结构版本（P0-2）─────────────────────────────────────────
# 1 = v2.5 及以前（schema.sql 全量 DDL + 补列，无版本表）
# 2 = v2.6：order_schedule_files.file_path 由绝对路径改为相对 data/ 的路径
# 3 = v2.7：金额量化到分 + orders.amount_manual/item_qty_manual 手工合计标记
# 4 = v2.8：query_presets（查询方案）+ 交期预警口径（不改表，仅默认数据）
# 5 = v2.9：orders.order_source（订单来源 / 销售渠道，预设四值 + 可手输）
# 6 = v3.0：order_schedules.scope / batch_no（分项 / 合并 / 整单排产标记 + 批次号）
# 7 = v3.5.0：purchase_contract_items（合同↔订单明细桥表）+ 合同含税/金额来源标记；
#     历史已签合同按「唯一明细」安全回填，不猜不摊。
CURRENT_SCHEMA_VERSION = 7
SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    note       TEXT
)
"""


def now() -> str:
    """统一时间戳"""
    return datetime.now().isoformat(timespec="seconds")


# v2.2 增量列：老库升级时先补列，再执行全量 DDL（DDL 里的索引依赖这些列）。
# 注意：ALTER TABLE 只补列，CHECK / FK 约束仅在【全新建库】时由 schema.sql 生效；
# 老库的服务层校验保证等价语义。新增列一律给 DEFAULT，保证旧数据可读。
SCHEMA_ADDITIONS = {
    # 2026-11-06：询配码明细按张哥给定 10 字段扩展，老库自动补列
    "code_items": [
        ("inquiry_item_name", "TEXT"),
        ("goods_code", "TEXT"),
        ("material_code", "TEXT"),
        ("factory_price", "REAL DEFAULT 0"),
        ("item_detail", "TEXT"),
    ],
    "orders": [
        ("order_type", "TEXT NOT NULL DEFAULT '外部订单'"),
        ("source_bid_project_id", "INTEGER"),
        ("batch_seq", "INTEGER NOT NULL DEFAULT 1"),
        # 2026-11-06 v2.3：订单头 8 字段新增 4 列（其余 4 列复用旧列）
        ("project_no", "TEXT"),
        ("amount_total", "REAL DEFAULT 0"),
        ("item_qty_total", "REAL DEFAULT 0"),
        ("erp_order_no", "TEXT"),
        # 2026-11-07 P1-5：手工合计保护标记（1 = 显式声明，不随明细自动重算）
        ("amount_manual", "INTEGER NOT NULL DEFAULT 0"),
        ("item_qty_manual", "INTEGER NOT NULL DEFAULT 0"),
        # 2026-11-07 v2.9：订单来源（预设四值 + 手输；老库补列，历史订单留空）
        ("order_source", "TEXT"),
    ],
    "order_items": [
        ("source_bid_item_id", "INTEGER"),
        # 2026-11-06 v2.3：订单明细 9 字段新增 4 列
        ("item_code", "TEXT"),
        ("production_name", "TEXT"),
        ("amount", "REAL DEFAULT 0"),
        ("param_desc", "TEXT"),
    ],
    "production_links": [
        ("order_type_snapshot", "TEXT NOT NULL DEFAULT '外部订单'"),
        ("source_type_snapshot", "TEXT"),
        ("bid_no_snapshot", "TEXT"),
        ("bid_line_snapshot", "TEXT"),
        ("bid_item_name_snapshot", "TEXT"),
    ],
    # v3.0：排产范围标记（老库补列，历史单据一律按「分项」读，不猜）
    "order_schedules": [
        ("scope", "TEXT NOT NULL DEFAULT '分项'"),
        ("batch_no", "TEXT"),
    ],
    # v3.5.0：合同金额口径标记（老库补列，历史合同一律按「含税 / 文档识别」读，不猜）
    "purchase_contracts": [
        ("tax_included", "INTEGER NOT NULL DEFAULT 1"),
        ("amount_source", "TEXT NOT NULL DEFAULT 'doc'"),
    ],
}


class Database:
    """SQLite 封装（isolation_level=None，事务由 tx() 显式控制）"""

    SCHEMA_PATH = Path(__file__).with_name("schema.sql")

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._connect()
        self.init_schema()

    # ── 连接 ────────────────────────────────────────────────

    def _connect(self) -> None:
        self.conn = sqlite3.connect(self.path, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    @property
    def data_dir(self) -> Path:
        """库所在目录 = 数据根目录（附件、备份都挂在这里）"""
        if self.path and self.path != ":memory:":
            return Path(self.path).resolve().parent
        return Path.cwd() / "data"

    # ── 结构 ────────────────────────────────────────────────

    def init_schema(self) -> None:
        """
        结构初始化（幂等）：
          1. 老库补列 → 2. 全量 DDL（IF NOT EXISTS）→ 3. 写/读 schema_meta → 4. 按版本跑迁移
        新库直接把版本写到 CURRENT；老库以 1 为基线，逐级升级。
        """
        is_new = not self._table_names()
        self._ensure_columns()
        ddl = self.SCHEMA_PATH.read_text(encoding="utf-8")
        self.conn.executescript(ddl)
        self._init_schema_meta(is_new)
        self._run_migrations()
        if is_new:
            # 新库直接落在最新版本，迁移不会跑；默认查询方案在这里补一次
            from .migrations import seed_default_presets
            seed_default_presets(self.conn)

    def _table_names(self) -> set:
        try:
            return {r["name"] for r in self.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error:
            return set()

    def _init_schema_meta(self, is_new: bool) -> None:
        self.conn.executescript(SCHEMA_META_DDL)
        row = self.query_one("SELECT version FROM schema_meta ORDER BY rowid DESC LIMIT 1")
        if row:
            return
        baseline = CURRENT_SCHEMA_VERSION if is_new else 1
        self.conn.execute(
            "INSERT INTO schema_meta(version, applied_at, note) VALUES (?, ?, ?)",
            (baseline, now(), "新建库" if is_new else "老库基线（v2.5 及以前）"))

    def schema_version(self) -> int:
        row = self.query_one("SELECT version FROM schema_meta ORDER BY rowid DESC LIMIT 1")
        return int(row["version"]) if row else 0

    def _run_migrations(self) -> None:
        """从当前版本逐级升到 CURRENT_SCHEMA_VERSION，每级单独事务并写版本号"""
        from .migrations import MIGRATIONS

        while True:
            current = self.schema_version()
            if current >= CURRENT_SCHEMA_VERSION:
                return
            target = current + 1
            fn = MIGRATIONS.get(target)
            with self.tx():
                if fn:
                    fn(self.conn, self.data_dir)
                self.conn.execute(
                    "INSERT INTO schema_meta(version, applied_at, note) VALUES (?, ?, ?)",
                    (target, now(), getattr(fn, "__name__", "noop")))

    def _ensure_columns(self) -> None:
        """老库升级：对已存在的表补齐 v2.2 新增列，避免 CREATE INDEX 依赖缺列而失败"""
        existing = {r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in SCHEMA_ADDITIONS.items():
            if table not in existing:
                continue                      # 新库：由 DDL 一次建全，跳过
            have = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ── 事务 ────────────────────────────────────────────────

    @contextmanager
    def tx(self):
        """
        事务上下文。支持嵌套调用：已在事务内则直接复用，由最外层统一提交。
        """
        nested = self.conn.in_transaction
        if not nested:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            if not nested:
                self.conn.commit()
        except Exception:
            if not nested:
                self.conn.rollback()
            raise

    # ── 基础查询 ────────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写语句，返回 lastrowid"""
        cur = self.conn.execute(sql, params)
        return cur.lastrowid or 0

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: tuple = (), default: Any = 0) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    # ── 通用 CRUD ───────────────────────────────────────────

    # P2-4：表名不能参数化，只能在拼接前校验白名单（core/tables.py）。
    # 列名同样来自内部代码，这里顺手拒绝含引号/空白的键，避免拼出畸形 SQL。

    @staticmethod
    def _safe_table(table: str) -> str:
        return tbl.check(table)

    @staticmethod
    def _safe_columns(data: Dict[str, Any]) -> Dict[str, Any]:
        for key in data:
            if not isinstance(key, str) or not key.replace("_", "").isalnum():
                raise ValueError(f"非法列名：{key!r}")
        return data

    def insert(self, table: str, data: Dict[str, Any]) -> int:
        table = self._safe_table(table)
        data = self._safe_columns({k: v for k, v in data.items() if v is not None})
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        return self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values())
        )

    def update(self, table: str, data: Dict[str, Any], where: str, params: tuple) -> None:
        table = self._safe_table(table)
        data = self._safe_columns({k: v for k, v in data.items() if v is not None})
        sets = ", ".join(f"{k} = ?" for k in data)
        self.execute(
            f"UPDATE {table} SET {sets} WHERE {where}", tuple(data.values()) + params
        )

    def get_by_id(self, table: str, entity_id: int) -> Optional[Dict[str, Any]]:
        table = self._safe_table(table)
        return self.query_one(f"SELECT * FROM {table} WHERE id = ?", (entity_id,))

    # ── 审计 ────────────────────────────────────────────────

    def log_action(
        self,
        entity: str,
        entity_id: Optional[int],
        action: str,
        before: Any = None,
        after: Any = None,
        operator: str = "",
    ) -> None:
        # P1-9：未显式给操作人时落当前登录人，避免审计表里一片空白
        operator = current_user.resolve(operator, self.data_dir)
        self.insert("audit_log", {
            "entity": entity,
            "entity_id": entity_id,
            "action": action,
            "before_json": json.dumps(before, ensure_ascii=False) if before is not None else None,
            "after_json": json.dumps(after, ensure_ascii=False) if after is not None else None,
            "operator": operator,
            "at": now(),
        })

    # ── 备份 / 恢复（P0-2）──────────────────────────────────

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    def backup(self, tag: str = "", keep: int = 30) -> Optional[Path]:
        """
        在线备份（sqlite3.Connection.backup，WAL 下也安全），返回备份文件路径。
        内存库不备份。keep>0 时按时间保留最近 keep 份。
        """
        if self.path == ":memory:":
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{tag}" if tag else ""
        target = self.backup_dir / f"erp-{stamp}{suffix}.db"
        dst = sqlite3.connect(str(target))
        try:
            with dst:
                self.conn.backup(dst)
        finally:
            dst.close()
        if keep > 0:
            self.prune_backups(keep)
        return target

    def list_backups(self) -> List[Dict[str, Any]]:
        # 内存库没有"自己的"备份目录（data_dir 落到 cwd/data），不能去捡别人的备份
        if self.path == ":memory:" or not self.backup_dir.is_dir():
            return []
        rows = []
        for p in self.backup_dir.glob("erp-*.db"):
            st = p.stat()
            rows.append({
                "path": str(p),
                "name": p.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        return sorted(rows, key=lambda r: r["mtime"], reverse=True)

    def prune_backups(self, keep: int = 30) -> List[str]:
        removed = []
        for old in self.list_backups()[keep:]:
            try:
                Path(old["path"]).unlink()
                removed.append(old["name"])
            except OSError:
                pass
        return removed

    def auto_backup(self, tag: str = "auto", keep: int = 30) -> Optional[Path]:
        """每天只备份一次（同一天已有 auto 备份则跳过），用于启动时兜底"""
        if self.path == ":memory:":
            return None
        today = datetime.now().strftime("%Y%m%d")
        if any(f"erp-{today}" in b["name"] and tag in b["name"]
               for b in self.list_backups()):
            return None
        return self.backup(tag=tag, keep=keep)

    def restore_from(self, src: str, safety_backup: bool = True) -> Path:
        """
        用备份覆盖当前库。恢复前默认先备份现库（tag=before-restore）。
        恢复后重建连接并跑一次结构迁移；调用方应提示用户重启界面。
        """
        src_path = Path(src)
        if not src_path.is_file():
            raise FileNotFoundError(f"备份文件不存在：{src_path}")
        if self.path == ":memory:":
            raise RuntimeError("内存数据库不支持恢复")
        saved = self.backup(tag="before-restore") if safety_backup else None
        try:
            self.conn.close()
        finally:
            for extra in (f"{self.path}-wal", f"{self.path}-shm"):
                try:
                    Path(extra).unlink(missing_ok=True)
                except OSError:
                    pass
        shutil.copy2(str(src_path), self.path)
        self._connect()
        self.init_schema()
        return saved or Path(self.path)

    def close(self) -> None:
        self.conn.close()

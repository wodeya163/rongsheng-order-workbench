#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构迁移脚本（P0-2）

规则：
  · 每个函数对应一个目标版本，签名 (conn, data_dir)，只做数据/结构变更；
  · 由 Database._run_migrations() 在独立事务内调用，成功后自动写 schema_meta；
  · 函数必须幂等（重复执行不产生副作用），因为可能被手工重跑。

版本线：
  1  基线（v2.5 及以前，schema.sql 全量 DDL + ALTER 补列）
  2  v2.6：订单排产附件改相对路径（绝对路径 → 相对 data/ 的 posix 路径）
  3  v2.7：金额量化到分 + 手工合计标记回填（P1-5 / P1-6）
  4  v2.8：查询方案默认数据
  5  v2.9：订单来源空白归一 + 内部订单回填「招投标」
  6  v3.0：订单排产 scope / batch_no 回填（历史共享审批文档识别为整单）
  7  v3.5.0：合同物资明细桥表回填（仅「一单一明细」的已签合同可安全推断）
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def v2_relative_attachment_paths(conn: sqlite3.Connection, data_dir: Path) -> None:
    """把 order_schedule_files.file_path 的绝对路径裁剪为相对 data/ 的路径。

    仅处理确实位于 data_dir 之下的路径；外部绝对路径（历史遗留、手工登记）
    保持原样，读取时仍可打开，不做破坏性修改。
    """
    if not _table_exists(conn, "order_schedule_files"):
        return
    root = Path(data_dir).resolve()
    rows = conn.execute("SELECT id, file_path FROM order_schedule_files").fetchall()
    for row in rows:
        raw = row["file_path"] if isinstance(row, sqlite3.Row) else row[1]
        if not raw:
            continue
        p = Path(str(raw))
        if not p.is_absolute():
            continue
        try:
            rel = p.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        conn.execute(
            "UPDATE order_schedule_files SET file_path = ? WHERE id = ?",
            (rel.as_posix(), row["id"] if isinstance(row, sqlite3.Row) else row[0]))


def v3_money_quantize_and_manual_flags(conn: sqlite3.Connection,
                                        data_dir: Path) -> None:
    """金额量化到分 + 回填手工合计标记（P1-5 / P1-6）。

    1) 明细 amount / 单价 unit_price 统一量化（2 位 / 4 位）；
    2) 订单头 amount_total 量化；
    3) 对每张订单判断「表头金额是否等于 Σ明细」：
       · 相等（±0.005）→ 视为派生值，amount_manual = 0；
       · 不相等       → 说明表头是独立声明的口径，amount_manual = 1，防止下次
                        增删明细时被自动汇总冲掉（这正是 P1-5 要解决的场景）。
    物资数量（item_qty_total）与明细行数不一致时同理标记 item_qty_manual。
    幂等：第二次执行不会改变任何值。
    """
    from .money import q2, q4, to_dec

    if not (_table_exists(conn, "orders") and _table_exists(conn, "order_items")):
        return

    # 1) 明细金额 / 单价
    rows = conn.execute(
        "SELECT id, qty_ordered, unit_price, amount FROM order_items").fetchall()
    for row in rows:
        iid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        qty = row["qty_ordered"] if isinstance(row, sqlite3.Row) else row[1]
        price = row["unit_price"] if isinstance(row, sqlite3.Row) else row[2]
        amount = row["amount"] if isinstance(row, sqlite3.Row) else row[3]
        new_price = float(q4(price or 0))
        new_amount = (float(q2(amount)) if amount not in (None, "")
                      else float(q2(to_dec(qty) * to_dec(price))))
        conn.execute(
            "UPDATE order_items SET unit_price = ?, amount = ? WHERE id = ?",
            (new_price, new_amount, iid))

    # 2)/3) 订单头金额与标记
    heads = conn.execute(
        "SELECT id, amount_total, item_qty_total FROM orders").fetchall()
    for row in heads:
        oid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        amount_total = row["amount_total"] if isinstance(row, sqlite3.Row) else row[1]
        qty_total = row["item_qty_total"] if isinstance(row, sqlite3.Row) else row[2]
        agg = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s "
            "FROM order_items WHERE order_id = ?", (oid,)).fetchone()
        n = agg["n"] if isinstance(agg, sqlite3.Row) else agg[0]
        declared_sum = q2(agg["s"] if isinstance(agg, sqlite3.Row) else agg[1])
        head_amount = q2(amount_total or 0)
        amount_manual = 0 if abs(head_amount - declared_sum) <= q2("0.005") else 1
        head_qty = q2(qty_total or 0)
        qty_manual = 0 if abs(head_qty - q2(n)) <= q2("0.005") else 1
        conn.execute(
            "UPDATE orders SET amount_total = ?, item_qty_total = ?, "
            "amount_manual = ?, item_qty_manual = ? WHERE id = ?",
            (float(head_amount), float(head_qty), amount_manual, qty_manual, oid))


def v4_query_presets(conn: sqlite3.Connection, data_dir: Path) -> None:
    """
    P2-5：建立 query_presets 并预置常用方案。
    表本身由 schema.sql 的 IF NOT EXISTS 建好，这里只负责铺默认数据（幂等：
    表里已有数据就跳过，避免用户删掉的默认方案又被加回来）。
    """
    seed_default_presets(conn)


def seed_default_presets(conn: sqlite3.Connection) -> None:
    """把默认方案铺进空表（新库与老库升级共用；已有数据则完全跳过）"""
    if not _table_exists(conn, "query_presets"):
        return
    row = conn.execute("SELECT COUNT(*) FROM query_presets").fetchone()
    count = row[0] if row else 0
    if count:
        return
    presets = [
        ("待发货订单", "orders", '{"status": "待发货"}', 10),
        ("逾期未交", "orders", '{"due": "overdue"}', 20),
        ("7 天内到期", "orders", '{"due": "soon"}', 30),
        ("还有可排量", "orders", '{"has_capacity": true}', 40),
    ]
    stamp = datetime.now().isoformat(timespec="seconds")
    for name, view, filters, order in presets:
        conn.execute(
            "INSERT INTO query_presets(name, view_key, filters, sort_order, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'system', ?)", (name, view, filters, order, stamp))


def v5_order_source_cleanup(conn: sqlite3.Connection, data_dir: Path) -> None:
    """v2.9 订单来源：列由 SCHEMA_ADDITIONS 补齐，这里只做数据归一（幂等）。

    1) 空白字符串（含全空格）→ NULL，避免「填了但等于没填」；
    2) 内部订单来源留空 → 回填「招投标」：内部订单只能由中标项目生成，
       来源语义唯一，不需要用户补录；
    3) 外部订单不回填：历史订单没有渠道信息，猜测会制造假数据，留空 = 未标注。
    """
    if not _table_exists(conn, "orders"):
        return
    cols = {r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "order_source" not in cols:
        return
    conn.execute("UPDATE orders SET order_source = NULL "
                 "WHERE order_source IS NOT NULL AND TRIM(order_source) = ''")
    conn.execute("UPDATE orders SET order_source = '招投标' "
                 "WHERE order_type = '内部订单' "
                 "AND (order_source IS NULL OR TRIM(order_source) = '')")


def v6_schedule_scope_backfill(conn: sqlite3.Connection, data_dir: Path) -> None:
    """v3.0 排产范围标记回填（幂等）。

    列由 SCHEMA_ADDITIONS 补齐（默认 '分项'），这里只做「历史上确实是整单」的识别：
    一张审批文档被同一订单的多张排产单共享 = 当年就是整单排产共用一个审批文件。
      · 涉及 2 张以上 → 标记 scope='整单'，并给这组排产单分一个批次号；
      · 其余保持「分项」不动（不猜测）。
    批次号形如 <订单号>-B01，若该订单已有同名批次则顺延。
    """
    if not _table_exists(conn, "order_schedules"):
        return
    cols = {r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(order_schedules)").fetchall()}
    if not {"scope", "batch_no"} <= cols:
        return
    if not _table_exists(conn, "order_schedule_files"):
        return

    shared = conn.execute(
        "SELECT s.order_id AS oid, f.file_path AS path, COUNT(DISTINCT s.id) AS n "
        "FROM order_schedule_files f JOIN order_schedules s ON s.id = f.schedule_id "
        "WHERE f.kind = '审批' GROUP BY s.order_id, f.file_path HAVING n > 1 "
        "ORDER BY s.order_id, f.file_path").fetchall()
    for row in shared:
        oid = row["oid"] if isinstance(row, sqlite3.Row) else row[0]
        path = row["path"] if isinstance(row, sqlite3.Row) else row[1]
        ids = [r["id"] if isinstance(r, sqlite3.Row) else r[0] for r in conn.execute(
            "SELECT DISTINCT s.id FROM order_schedules s "
            "JOIN order_schedule_files f ON f.schedule_id = s.id "
            "WHERE s.order_id = ? AND f.file_path = ? AND f.kind = '审批' "
            "ORDER BY s.id", (oid, path)).fetchall()]
        if len(ids) < 2:
            continue
        if all(_scalar(conn, "SELECT scope FROM order_schedules WHERE id = ?", (i,))
               == "整单" for i in ids):
            continue                                  # 已标记过，幂等跳过
        order_no = _scalar(conn, "SELECT doc_no FROM orders WHERE id = ?", (oid,)) or ""
        batch_no = _next_batch_no(conn, oid, order_no)
        for i in ids:
            conn.execute(
                "UPDATE order_schedules SET scope = '整单', batch_no = ? WHERE id = ?",
                (batch_no, i))


def v7_contract_items_backfill(conn: sqlite3.Connection, data_dir: Path) -> None:
    """v3.5.0 合同物资明细回填（幂等，只做能确证的历史数据）。

    背景：v3.4.x 的合同只挂 order_id，没有物资级分配。为了让老库升级后
    「订单利润率」能立刻看到有意义的结果，把历史上【唯一明细 + 已签订 +
    金额 > 0】的合同回填一条分配行：数量 = 明细实值，含税单价 = 金额 ÷ 数量，
    金额 = 合同金额。这三类条件同时满足时映射唯一，不存在猜测。

    ★ 明确不回填的情形（宁可标「不完整」，绝不制造假数据）：
      · 一张订单有多项物资 → 无法判断合同覆盖了哪几项，交给用户在分配页手工指定；
      · 草稿 / 作废合同 → 服务层本就不计入利润，回填没有意义；
      · 金额为 0（文档未识别）→ 不让 0 单价污染采购成本。
    幂等：INSERT OR IGNORE 命中 UNIQUE(contract_id, order_item_id) 即跳过。
    """
    from .money import q2, q4

    needed = ("purchase_contract_items", "purchase_contracts", "order_items")
    if not all(_table_exists(conn, t) for t in needed):
        return
    cols = {r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(purchase_contracts)").fetchall()}
    if "tax_included" in cols:
        conn.execute("UPDATE purchase_contracts SET tax_included = 1 "
                     "WHERE tax_included IS NULL")
    if "amount_source" in cols:
        conn.execute("UPDATE purchase_contracts "
                     "SET amount_source = 'doc' "
                     "WHERE amount_source IS NULL OR TRIM(amount_source) = ''")

    rows = conn.execute(
        "SELECT id, doc_no, order_id, amount, currency, status "
        "FROM purchase_contracts WHERE order_id IS NOT NULL AND amount > 0 "
        "AND status IN ('已签订', '执行中', '已完成') ORDER BY id").fetchall()
    stamp = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        cid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        oid = row["order_id"] if isinstance(row, sqlite3.Row) else row[2]
        amount = row["amount"] if isinstance(row, sqlite3.Row) else row[3]
        currency = row["currency"] if isinstance(row, sqlite3.Row) else row[4]
        items = conn.execute(
            "SELECT id, qty_ordered FROM order_items WHERE order_id = ? "
            "ORDER BY line_no", (oid,)).fetchall()
        if len(items) != 1:
            continue
        item = items[0]
        iid = item["id"] if isinstance(item, sqlite3.Row) else item[0]
        qty = item["qty_ordered"] if isinstance(item, sqlite3.Row) else item[1]
        try:
            qty_f = float(qty or 0)
            total = float(q2(amount))
            if qty_f <= 0 or total <= 0:
                continue
            unit = float(q4(total / qty_f))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO purchase_contract_items "
            "(contract_id, order_item_id, qty, unit_price, tax_rate, amount, "
            " currency, remark, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'system', ?, ?)",
            (cid, iid, qty_f, unit, total, currency or "CNY",
             "v3.5.0 历史合同按唯一明细自动回填", stamp, stamp))


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0] if not isinstance(row, sqlite3.Row) else row[0]


def _next_batch_no(conn: sqlite3.Connection, order_id: int, order_no: str) -> str:
    """<订单号>-B<两位序号>：取该订单已用批次的序号最大值 +1"""
    rows = conn.execute(
        "SELECT batch_no FROM order_schedules WHERE order_id = ? AND batch_no IS NOT NULL",
        (order_id,)).fetchall()
    used = set()
    for r in rows:
        value = r[0] if not isinstance(r, sqlite3.Row) else r["batch_no"]
        if value and "-B" in str(value):
            try:
                used.add(int(str(value).rsplit("-B", 1)[1]))
            except ValueError:
                continue
    return f"{order_no or order_id}-B{(max(used) + 1) if used else 1:02d}"


MIGRATIONS = {
    2: v2_relative_attachment_paths,
    3: v3_money_quantize_and_manual_flags,
    4: v4_query_presets,
    5: v5_order_source_cleanup,
    6: v6_schedule_scope_backfill,
    7: v7_contract_items_backfill,
}

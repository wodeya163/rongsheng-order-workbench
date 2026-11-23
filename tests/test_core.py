# -*- coding: utf-8 -*-
"""
产品询报价管理系统 —— 核心逻辑自动化测试
覆盖：数据库迁移 / CRUD / 多字段筛选 / 计算层 / 投标项目汇总与中标率 / Excel 导入导出往返 / 边界容错
运行：python tests/test_core.py
（GUI 需在本地双击 启动管理系统.bat 验证，本脚本不含界面测试）
"""
import os
import sys
import sqlite3
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db as dbmod
from app import calc
from app.excel_io import export_products, export_project_summary, import_products

_PASSED = 0
_FAILED = 0


def check(name, cond, detail=""):
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
        print(f"  [PASS] {name}")
    else:
        _FAILED += 1
        print(f"  [FAIL] {name}  {detail}")
    return cond


def _columns(dbm):
    c = sqlite3.connect(dbm.db_path)
    rows = c.execute("PRAGMA table_info(products)").fetchall()
    c.close()
    return [r[1] for r in rows]


# ---------------------------------------------------------------------------
print("== 1. 数据库初始化 & 新字段迁移（兼容旧表）==")
db_mig = dbmod.DatabaseManager(db_path=tempfile.mktemp(suffix=".db"))
# 模拟“旧版本”表结构：无 inquiry_unit / is_won 两列
conn = sqlite3.connect(db_mig.db_path)
conn.execute("ALTER TABLE products RENAME TO products_old")
conn.execute(
    """CREATE TABLE products(
        id INTEGER PRIMARY KEY, name TEXT, spec TEXT, category TEXT,
        quote_ratio REAL, inquiry_qty INTEGER, purchase_price REAL,
        description TEXT, project TEXT, quoter TEXT,
        image_path TEXT, drawing_path TEXT, created_at TEXT, updated_at TEXT)"""
)
conn.execute("INSERT INTO products(name) VALUES('旧数据')")
conn.commit()
conn.close()
db_mig.init_database()  # 重新触发迁移
cols = _columns(db_mig)
check("迁移后包含 inquiry_unit 列", "inquiry_unit" in cols, str(cols))
check("迁移后包含 is_won 列", "is_won" in cols, str(cols))
check("旧数据保留", len(db_mig.get_all_products()) == 1)

print("== 2~7. 功能测试（全新干净数据库，断言确定化）==")
db = dbmod.DatabaseManager(db_path=tempfile.mktemp(suffix=".db"))
check("新库初始为空", len(db.get_all_products()) == 0)

print("-- 2. 新增 / 查询 / 修改 / 删除 (CRUD) --")
pid = db.add_product({
    "name": "离心泵", "spec": "ISG50", "category": "泵", "quote_ratio": 0.85,
    "inquiry_qty": 10, "purchase_price": 1000, "inquiry_unit": "台",
    "project": "项目甲", "is_won": "是", "quoter": "张三", "description": "高温型",
})
check("add_product 返回有效 id", isinstance(pid, int) and pid > 0)
p = db.get_product(pid)
check("get_product 取回 inquiry_unit", p.get("inquiry_unit") == "台", str(p))
check("get_product 取回 is_won", p.get("is_won") == "是", str(p))

db.update_product(pid, {"is_won": "否", "purchase_price": 1200})
p2 = db.get_product(pid)
check("update is_won 生效", p2.get("is_won") == "否", str(p2))
check("update purchase_price 生效", abs(p2.get("purchase_price") - 1200) < 1e-6)

db.delete_product(pid)
check("delete 后不可查", db.get_product(pid) is None)

print("-- 3. 多字段筛选 --")
db.add_product({"name": "阀门A", "category": "阀", "quote_ratio": 0.8, "inquiry_qty": 5,
                "purchase_price": 200, "inquiry_unit": "套", "project": "项目甲", "is_won": "是", "quoter": "李四"})
db.add_product({"name": "阀门B", "category": "阀", "quote_ratio": 0.8, "inquiry_qty": 3,
                "purchase_price": 300, "inquiry_unit": "个", "project": "项目乙", "is_won": "是", "quoter": "王五"})
db.add_product({"name": "管件C", "category": "管件", "quote_ratio": 0.7, "inquiry_qty": 8,
                "purchase_price": 50, "inquiry_unit": "米", "project": "项目甲", "is_won": "否", "quoter": "张三"})

check("按 project 筛选", len(db.search_products({"project": "项目甲"})) == 2)
check("按 category 筛选", len(db.search_products({"category": "阀"})) == 2)
check("按 is_won 筛选(否)", len(db.search_products({"is_won": "否"})) == 1)
check("按 is_won 筛选(是)", len(db.search_products({"is_won": "是"})) == 2)
check("组合 project+is_won(项目甲+否)", len(db.search_products({"project": "项目甲", "is_won": "否"})) == 1)
check("按 name 模糊匹配", len(db.search_products({"name": "阀"})) == 2)

print("-- 4. 计算层 --")
check("compute_quote_price 100/0.85", abs(calc.compute_quote_price(100, 0.85) - 117.647058) < 1e-3)
check("safe_float 非法 -> 默认", calc.safe_float("abc", 7) == 7)
check("safe_float 空 -> 默认", calc.safe_float("", 0) == 0)
check("safe_int 非法 -> 默认", calc.safe_int("x", 1) == 1)

allp = db.get_all_products()
sm = calc.compute_summary(allp)
check("compute_summary count=3", sm["count"] == 3, str(sm))
check("compute_summary 总额>0", sm["total_purchase"] > 0 and sm["total_quote"] > 0)

ps = calc.compute_project_summary(allp)
proj_names = [g["project"] for g in ps]
check("项目汇总含 项目甲/项目乙", "项目甲" in proj_names and "项目乙" in proj_names, str(proj_names))
gjia = next(g for g in ps if g["project"] == "项目甲")
check("项目甲 数量=2", gjia["count"] == 2)
check("项目甲 已中标=1", gjia["won_count"] == 1)
check("项目甲 未中标=1", gjia["lost_count"] == 1)
check("项目甲 中标率=50%", abs(gjia["win_rate"] - 50.0) < 1e-6, str(gjia["win_rate"]))

print("-- 5. Excel 导出 / 汇总导出 --")
xlsx = tempfile.mktemp(suffix=".xlsx")
export_products(allp, xlsx)
check("导出产品文件存在且非空", os.path.exists(xlsx) and os.path.getsize(xlsx) > 0)
xlsx2 = tempfile.mktemp(suffix=".xlsx")
export_project_summary(ps, xlsx2)
check("导出项目汇总文件存在且非空", os.path.exists(xlsx2) and os.path.getsize(xlsx2) > 0)

print("-- 6. Excel 导入往返 --")
dst = dbmod.DatabaseManager(db_path=tempfile.mktemp(suffix=".db"))
res = import_products(dst, xlsx)
check("导入返回统计字典", isinstance(res, dict) and "inserted" in res and "skipped" in res, str(res))
imp = dst.get_all_products()
check("导入数量与原数据一致(3)", len(imp) == 3, f"got {len(imp)}")
first = imp[0]
check("导入保留 inquiry_unit", first.get("inquiry_unit") in ("台", "套", "个", "米"), str(first.get("inquiry_unit")))
check("导入保留 is_won", first.get("is_won") in ("是", "否", ""), str(first.get("is_won")))

print("-- 7. 导入容错：跳过合计行/空行 --")
import openpyxl
wb = openpyxl.Workbook()
ws = wb.active
ws.append(["产品名称", "规格型号", "采购价格", "投标项目", "是否中标", "询价单位"])
ws.append(["X泵", "X1", 100, "P测试", "是", "台"])
ws.append(["合计", "", 999, "", "", ""])   # 合计行
ws.append(["", "", "", "", "", ""])         # 空行
ws.append(["Y阀", "Y2", 200, "P测试", "否", "套"])
bad = tempfile.mktemp(suffix=".xlsx")
wb.save(bad)
dst2 = dbmod.DatabaseManager(db_path=tempfile.mktemp(suffix=".db"))
r2 = import_products(dst2, bad)
check("合计行/空行被跳过，仅导入2条", len(dst2.get_all_products()) == 2, str(r2))

print("== 8. 边界：空数据库导出/汇总 ==")
empty_db = dbmod.DatabaseManager(db_path=tempfile.mktemp(suffix=".db"))
empty_xlsx = tempfile.mktemp(suffix=".xlsx")
export_products(empty_db.get_all_products(), empty_xlsx)
check("空库导出不崩溃", os.path.exists(empty_xlsx))
empty_ps = calc.compute_project_summary(empty_db.get_all_products())
check("空库项目汇总为空列表", empty_ps == [])

print(f"\n==== 测试结果：通过 {_PASSED} / 失败 {_FAILED} ====")
sys.exit(1 if _FAILED else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyInstaller 打包脚本：生成独立可运行的 v2.2 桌面程序（onedir）。

    python build_v2.py            # 打包
    python build_v2.py --check    # 只检查依赖

产物：dist/订单管理工作台/订单管理工作台.exe（另含 ProductSuite_v2.2.exe 兼容别名）
数据：v2/core/schema.sql 会随包放进 v2/core/，首次启动自动建/升级 data/erp.db。

v3.4.3：程序集更名；--version-file 写入中文产品元数据；打包结束后复制一份
旧名 exe，保证 v3.4.2 及更早版本的更新器仍能定位新包里的主程序。
"""
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from v2 import ASSEMBLY_NAME, EXE_NAME, LEGACY_EXE_NAME   # noqa: E402

NAME = ASSEMBLY_NAME

# P1-8：运行期实测从未加载的重量级包（2026-11-07 用 sys.modules 验证）
# pandas 会连带 numpy / pyarrow / numba / llvmlite；fitz=PyMuPDF 与 pypdfium2 重复；
# 其余是科学计算 / 交互式环境，本程序完全不用。排除后不应出现 ImportError。
EXCLUDES = [
    "pandas", "numpy", "pyarrow", "numba", "llvmlite",
    "scipy", "matplotlib", "fitz", "pymupdf",
    "IPython", "jupyter", "notebook", "pytest", "sqlalchemy",
    "tkinter", "PIL.ImageQt",
    # 构建机装了 pygame / pywin32 IDE 组件，会被 hook 顺手收进来（运行期不用）
    "pygame", "SDL2", "Pythonwin", "pywin",
]


def rotate(path: Path) -> bool:
    """
    把旧产物目录改名保留，而不是让 PyInstaller 递归删除。
    本机 safe-delete 守卫会拦截 >50 文件的批量删除，导致 COLLECT 阶段中断。
    """
    if not path.exists():
        return True
    backup = path.parent / f"{path.name}_prev_{datetime.now():%Y%m%d_%H%M%S}"
    try:
        path.rename(backup)
        print(f"旧产物已保留：{backup}")
        return True
    except OSError as exc:
        print(f"无法重命名旧产物 {path}：{exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 v2.2 桌面程序")
    ap.add_argument("--check", action="store_true", help="只检查依赖")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
        print(f"PyInstaller: OK {PyInstaller.__version__} ({sys.executable})")
    except ImportError as exc:
        print(f"缺少 PyInstaller（{exc}），当前解释器：{sys.executable}", file=sys.stderr)
        print("请用同一解释器执行：py -m pip install pyinstaller", file=sys.stderr)
        return 1
    from v2.ui.main_window_v2 import PYSIDE_AVAILABLE
    print(f"PySide6: {'OK' if PYSIDE_AVAILABLE else '未安装'}")
    if args.check:
        return 0 if PYSIDE_AVAILABLE else 1

    # 先轮转旧产物，避免 PyInstaller 的递归清理被 safe-delete 守卫拦截
    if not rotate(ROOT / "dist" / NAME) or not rotate(ROOT / "build" / NAME):
        print("旧产物轮转失败，终止打包（请先手工关闭程序/清理旧目录）", file=sys.stderr)
        return 2
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--windowed",
        "--name", NAME,
        "--paths", str(ROOT),
        # v3.0.1：exe 与任务栏图标；运行期窗口图标取 _internal/v2/ui/app.ico
        "--icon", str(ROOT / "v2" / "ui" / "app.ico"),
        # v3.4.3：Windows 版本资源（产品名 / 文件说明 / 公司）
        "--version-file", str(ROOT / "version_info.txt"),
        "--add-data", f"{ROOT / 'v2' / 'ui' / 'app.ico'};v2/ui",
        "--add-data", f"{ROOT / 'v2' / 'core' / 'schema.sql'};v2/core",
        # P1-8：只收 v2.core（原先 "v2" 会把 v2/tests 一起收进来，
        # 测试里的 import fitz 会连带拉入 PyMuPDF 全家桶，白占 150 MB+）
        "--collect-submodules", "v2.core",
        # v2.4 订单排产：python-docx 的默认模板 (docx/templates/default.docx) 必须随包
        "--collect-data", "docx",
        "--hidden-import", "docx",
        "--hidden-import", "openpyxl",
        # v2.5「添加订单」：图片 OCR / PDF 抽表 / 词框还原
        "--hidden-import", "pytesseract",
        "--hidden-import", "PIL",
        "--hidden-import", "pdfplumber",
        "--hidden-import", "pypdfium2",
        "--collect-submodules", "pdfminer",
        "--collect-submodules", "pdfplumber",
        "--hidden-import", "v2.core.docread",
        "--hidden-import", "v2.core.excelio",
        "--hidden-import", "v2.core.fields",
        "--hidden-import", "v2.core.order_fields",
        "--hidden-import", "v2.ui.dialogs",
        "--hidden-import", "v2.ui.theme",
        "--hidden-import", "v2.ui.query_panel",
        "--hidden-import", "v2.ui.datefield",       # v3.2.0 弹出日历控件
        "--hidden-import", "v2.core.uistate",      # v3.2.0 导航折叠/隐藏偏好
        "--hidden-import", "v2.ui.customerfield",  # v3.4.2 客户名称检索控件
        "--hidden-import", "v2.ui.partyfield",     # v3.4.5 合同需方/供方检索控件
        "--hidden-import", "v2.core.contract_amount",  # v3.4.6 合同总金额解析
        "--hidden-import", "win32com.client",       # v3.4.6 旧版 .doc 增强通道
        "--hidden-import", "v2.domains.profit",     # v3.5.0 订单盈亏口径
        "--hidden-import", "v2.ui.profit_chart",    # v3.5.0 QPainter 柱线图
        "--hidden-import", "v2.ui.profit_dialog",   # v3.5.0 经营分析窗口
        "--hidden-import", "v2.ui.contract_items_dialog",  # v3.5.0 合同物资分配
        "--hidden-import", "v2.core.contract_items",  # v3.5.1 合同明细表解析/自动匹配
        "--hidden-import", "v2.core.contract_prefs",  # v3.5.2 导入即自动应用开关
        "--hidden-import", "v2.ui.panels.profit_actions",
        "--hidden-import", "v2.core.duereminder",  # v3.3.0 交期提醒引擎
        "--hidden-import", "v2.core.reminder_prefs",
        "--hidden-import", "v2.ui.due_dialog",     # v3.3.0 提醒清单 / 设置
        "--hidden-import", "v2.ui.panels.due_actions",
        "--hidden-import", "v2.ui.base_dialog",
        "--hidden-import", "v2.domains.maintenance",
        # v3.4.0：排产文件窗口 + 采购合同管理
        "--hidden-import", "v2.ui.schedule_files_dialog",
        "--hidden-import", "v2.ui.contract_dialog",
        "--hidden-import", "v2.ui.panels.contract_actions",
        "--hidden-import", "v2.domains.contract",
        "--hidden-import", "v2.core.applog",
        "--hidden-import", "v2.core.current_user",
        "--hidden-import", "v2.core.money",
        "--hidden-import", "v2.core.query",
        "--hidden-import", "v2.core.single_instance",
        "--hidden-import", "v2.core.updater",
        "--hidden-import", "v2.ui.update_dialog",
        "--hidden-import", "v2.ui.async_task",
        "--hidden-import", "v2.demo_data",
        *[arg for mod in EXCLUDES for arg in ("--exclude-module", mod)],
        str(ROOT / "v2" / "app_v2.py"),
    ]
    print("打包命令：", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode == 0:
        exe = ROOT / "dist" / NAME / EXE_NAME
        alias = ROOT / "dist" / NAME / LEGACY_EXE_NAME
        # onedir 的 exe 只是引导器，与文件名无关，复制一份即可作为兼容入口。
        # 旧版更新器按自己运行时的 exe 名在新包里找主程序，缺了这份会升级失败。
        shutil.copy2(exe, alias)
        print(f"\n打包完成：{exe}")
        print(f"兼容别名：{alias}（供旧版更新器 / 既有桌面快捷方式）")
        print("目录内为免安装版，整体拷贝即可使用；首次启动会在同目录 data/ 下建库。")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

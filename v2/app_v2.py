#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.2 桌面程序启动入口

    python -m v2.app_v2                      # 启动界面（数据库 data/erp.db）
    python -m v2.app_v2 --demo              # 首次写入演示数据并启动
    python -m v2.app_v2 --db data/test.db   # 指定数据库

也可用项目根目录的 `启动_v2.bat` 双击启动。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import APP_TITLE, __version__                          # noqa: E402
from v2.core.db import Database                             # noqa: E402
from v2.ui import theme                                     # noqa: E402


def default_db_path() -> Path:
    """打包后落在 exe 同目录 data/；源码运行落在项目根 data/"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data" / "erp.db"
    return ROOT / "data" / "erp.db"


def ensure_font(app) -> None:
    """无可用字体的精简/离屏环境下注入 Windows 中文字体，避免界面全是方块"""
    import os
    from PySide6.QtGui import QFontDatabase

    if QFontDatabase.families():
        return
    fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyhbd.ttc", "simsun.ttc"):
        path = fonts_dir / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


def main(argv=None) -> int:
    try:                                   # windowed exe 的控制台编码兜底
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=f"{APP_TITLE} v{__version__}")
    ap.add_argument("--db", default=str(default_db_path()), help="SQLite 数据库路径")
    ap.add_argument("--demo", action="store_true", help="启动前写入一组演示数据")
    ap.add_argument("--check", action="store_true", help="只做依赖检查，不启动界面")
    ap.add_argument("--ui-check", action="store_true",
                    help="离屏构建一次界面（不显示），验证打包后 UI 模块完整")
    ap.add_argument("--allow-multi", action="store_true",
                    help="允许多个实例共用同一个数据库（默认拒绝，P1-11）")
    ap.add_argument("--apply-update", metavar="MARKER_JSON",
                    help="更新器模式：执行目录交换后启动新版本（内部使用）")
    ap.add_argument("--parse-check", metavar="文件",
                    help="只跑一次文档识别并打印结果（排查「这个文件读不出来」）")
    args = ap.parse_args(argv)

    # v3.1.0 更新器模式：最先处理，不进 GUI、不占单实例锁。
    # 该进程由 staging 里的新版 exe 启动，完成目录交换后拉起新版并退出。
    if args.apply_update:
        from v2.core import updater
        return updater.run_apply_update(args.apply_update)

    # P0-4：日志先于一切业务初始化，保证后续任何异常都有落盘证据
    from v2.core import applog
    applog.setup(context={
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "db": args.db,
        "python": sys.version.split()[0],
        "argv": " ".join(argv if argv is not None else sys.argv[1:]),
    })

    try:
        from v2.ui.main_window_v2 import PYSIDE_AVAILABLE
    except Exception as exc:
        print(f"界面模块加载失败：{exc}", file=sys.stderr)
        return 1

    if args.parse_check:
        # 打包后验证 docread 六通道依赖是否齐全（docx / pdfplumber / pypdfium2 / OCR）
        from v2.core import docread
        try:
            src = docread.read_source(args.parse_check)
        except Exception as exc:                       # noqa: BLE001
            print(f"识别失败：{exc}")
            return 1
        print(f"通道：{src['kind_label']}　行数：{len(src['rows'])}　"
              f"命中字段：{src['score']}　OCR：{'可用' if docread.ocr_available() else '不可用'}")
        return 0

    if args.check:
        from v2.core import excelio, docread
        db = Database(":memory:")           # 真正执行一遍 DDL，打包后能验证 schema.sql 随包可用
        tables = db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        db.close()
        print(f"版本 v{__version__} | PySide6: {'OK' if PYSIDE_AVAILABLE else '未安装'}")
        print(f"DDL 自检: {len(tables)} 张表可创建（schema.sql 路径正确）")
        print(excelio.backend_info())
        print(f"文档识别: docx={'OK' if docread.docx_available() else '缺失'}　"
              f"PDF={'OK' if docread.pdf_available() else '缺失'}　"
              f"OCR={'OK ' + docread.ocr_langs() if docread.ocr_available() else '不可用'}")
        return 0 if PYSIDE_AVAILABLE and tables else 1
    if args.ui_check:
        if not PYSIDE_AVAILABLE:
            print("未检测到 PySide6，无法构建界面", file=sys.stderr)
            return 1
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from v2.ui.main_window_v2 import MainWindowV2
        from v2 import demo_data
        app = QApplication.instance() or QApplication(sys.argv)
        ensure_font(app)
        theme.apply_theme(app)
        db = Database(":memory:")
        demo_data.seed(db)
        w = MainWindowV2(db)
        print(f"UI 自检: 菜单 {len(w.menuBar().actions())} 组 / "
              f"结果页 {w.result_stack.count()} 页 / "
              f"操作分组 {w.action_tree.topLevelItemCount()} 组 / "
              f"查询条件 {len(w.query_panel.rows)} 类 / "
              f"主结果 {w.result_tables[w.current_view].rowCount()} 行")
        w.close()
        db.close()
        return 0
    if not PYSIDE_AVAILABLE:
        print("未检测到 PySide6，界面不可用。请执行：pip install PySide6", file=sys.stderr)
        print("纯逻辑链路仍可运行：python v2/main_v2.py --smoke", file=sys.stderr)
        return 1

    from PySide6.QtWidgets import QApplication
    from v2.ui.main_window_v2 import MainWindowV2

    # P1-11：同一个库只允许一个实例，避免两个窗口互相覆盖
    from v2.core import single_instance
    if not args.allow_multi and not single_instance.acquire(args.db):
        applog.get_logger("app").warning("检测到重复启动，已取消：%s", args.db)
        single_instance.warn_already_running(args.db)
        return 2

    db = Database(args.db)
    # P1-9：绑定数据目录，之后 current_user.name() 能读到 data/operator.txt 覆盖值
    from v2.core import current_user
    current_user.configure(db.data_dir)
    applog.get_logger("app").info("当前操作人：%s", current_user.name(db.data_dir))
    # P0-2：每日首次启动自动备份（同一天已有 auto 备份则跳过）；失败不阻断启动
    try:
        saved = db.auto_backup()
        if saved:
            applog.get_logger("app").info("每日自动备份：%s", saved)
    except Exception:
        applog.get_logger("app").exception("每日自动备份失败（不影响使用）")
    if args.demo:
        from v2 import demo_data
        print(demo_data.seed(db))
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    ensure_font(app)
    theme.apply_theme(app)
    window = MainWindowV2(db)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""
GUI 模块导入冒烟测试（需 PySide6）
仅验证 app.main_window / app.dialogs / app.main 能被成功 import，
捕捉模块级 ImportError、未定义引用、错误的相对导入等问题。
注意：不创建 QApplication、不打开窗口（无显示器环境无法运行界面）。
运行：python tests/test_gui_import.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("[SKIP] 未安装 PySide6，跳过 GUI 导入冒烟测试（请在本地安装后运行）")
        return 0

    # 仅 import，不实例化任何 Widget（避免需要显示器）
    import app.main_window as mw
    import app.dialogs as dlg
    import app.main as appmain

    # 校验关键类/函数存在
    assert hasattr(mw, "MainWindow"), "MainWindow 缺失"
    assert hasattr(dlg, "ProductDialog"), "ProductDialog 缺失"
    assert hasattr(dlg, "ProductDetailDialog"), "ProductDetailDialog 缺失"
    assert hasattr(dlg, "ProjectSummaryDialog"), "ProjectSummaryDialog 缺失"
    assert hasattr(appmain, "main"), "main() 入口缺失"

    # 校验对话框常量（常用询价单位下拉项应存在）
    assert hasattr(dlg, "COMMON_INQUIRY_UNITS"), "COMMON_INQUIRY_UNITS 缺失"
    assert hasattr(dlg, "WON_CHOICES"), "WON_CHOICES 缺失"

    print("[PASS] app.main_window 导入成功")
    print("[PASS] app.dialogs 导入成功")
    print("[PASS] app.main 导入成功")
    print(f"[PASS] 常用单位 {len(dlg.COMMON_INQUIRY_UNITS)} 项，是否中标选项 {dlg.WON_CHOICES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

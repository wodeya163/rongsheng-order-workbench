#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
明亮配色的统一样式表（QSS）与控件工厂。

设计基调：白卡片 + 浅灰蓝底 + 品牌蓝主色，避免深色面板与密集按钮条。
纯字符串常量不依赖 PySide6；所有工厂函数在函数内局部导入 Qt，
因此零依赖环境下本模块仍可安全导入（PYSIDE_AVAILABLE=False 时勿调用工厂）。

主窗口与所有弹窗共用同一份 QSS：由 apply_theme() 挂到 QApplication，
不能只设在主窗口上，否则 QMessageBox / QFileDialog 等顶层弹窗会漏样式。
"""
from typing import Dict

from .. import __version__ as APP_VERSION  # noqa: E402  纯字符串，无副作用

# 所有弹窗右上角版本徽标唯一来源：升版本只改 v2/__init__.py 一处
BADGE = f"v{APP_VERSION}"

BRAND = "#2563EB"
BRAND_DARK = "#1D4ED8"
INK = "#1E293B"
MUTED = "#64748B"
LINE = "#E2E8F0"
BG = "#F4F7FC"
CARD = "#FFFFFF"
OK = "#0E9F6E"
WARN = "#D97706"
DANGER = "#DC2626"

PALETTE: Dict[str, str] = {
    "brand": BRAND, "brand_dark": BRAND_DARK, "ink": INK, "muted": MUTED,
    "line": LINE, "bg": BG, "card": CARD, "ok": OK, "warn": WARN,
}


def _check_png_url() -> str:
    """
    16x16 白色对勾 PNG 的本地路径（v3.0.1）。
    勾选框选中态以前是一块纯色填充，用户反馈「选了看不清」；改成
    品牌蓝底 + 白色对勾图片，一眼可辨。PNG 以 base64 内嵌，
    首次调用时写到系统临时目录（幂等），QSS 直接引用该路径。
    """
    import base64
    import tempfile
    from pathlib import Path

    global _CHECK_PNG_URL
    if _CHECK_PNG_URL:
        return _CHECK_PNG_URL
    b64 = ("iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAA3UlEQVR4nL2TTQ4B"
           "QRCFn5mJlZ3ERkLiClzAxjHECZzAFWxFhJVDOICFJWtbLuACPqlMdbTxFyTepmZq"
           "+quq190j/VtA6WcYSH+Bq9/AqccecASGlgOSpNil6NEWSToDNUkLSXVJTctJSl5u"
           "jhfMvNuaXKuo8J2/in0Ikxjs+bHDe9uDm0nJXwxqADtg6fmyxz5gFk5A51H3zOOIq"
           "+aeaztoGsTrb0Q+gXmdRkVmwNafJ09hU+zJwVgbb5C+vIXkRcJ5h0kOQCvs01O4UC"
           "RM0vWz//z+x8DXP89bz64Lx9A2fNeVSz8AAAAASUVORK5CYII=")
    target = Path(tempfile.gettempdir()) / "productsuite_check_on.png"
    try:
        if not target.exists() or target.stat().st_size == 0:
            target.write_bytes(base64.b64decode(b64))
        # 注意：file:/// 形式的 URL 实测加载为 null 图，裸绝对路径（正斜杠 +
        # 引号包裹）才可靠 → QSS 里 url("C:/Users/.../check_on.png")
        _CHECK_PNG_URL = str(target).replace("\\", "/")
    except Exception:
        _CHECK_PNG_URL = ""          # 写不进临时目录就退回纯色填充
    return _CHECK_PNG_URL


_CHECK_PNG_URL = ""
_CHECKED_IMG = (f' image: url("{_check_png_url()}");'
                if _check_png_url else "")

QSS = f"""
* {{
    font-family: "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: {INK};
}}
QMainWindow, QWidget#Root {{ background: {BG}; }}

/* ── 菜单栏 ───────────────────────────────────────── */
QMenuBar {{
    background: #FFFFFF;
    border-bottom: 1px solid {LINE};
    padding: 3px 6px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 7px 15px;
    border-radius: 6px;
    color: #334155;
}}
QMenuBar::item:selected {{ background: #E8F0FE; color: {BRAND_DARK}; }}
QMenuBar::item:pressed  {{ background: #DCE8FD; }}
QMenu {{
    background: #FFFFFF;
    border: 1px solid #D8E1EE;
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 30px 7px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: #E8F0FE; color: {BRAND_DARK}; }}
QMenu::item:disabled {{ color: #AAB4C4; }}
QMenu::separator {{ height: 1px; background: #E8EDF5; margin: 5px 8px; }}

/* ── 卡片 / 标题 ─────────────────────────────────── */
QFrame#Card, QGroupBox#Card {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 10px;
}}
QLabel#SectionTitle {{ font-size: 15px; font-weight: 700; color: #0F2A5C; }}
QLabel#PageTitle    {{ font-size: 20px; font-weight: 700; color: #0F2A5C; }}
QLabel#SubTitle     {{ color: {MUTED}; font-size: 12px; }}
QLabel#Hint         {{ color: {MUTED}; }}
QLabel#KpiNum       {{ font-size: 21px; font-weight: 700; color: {BRAND_DARK}; }}
QLabel#KpiLabel     {{ color: {MUTED}; font-size: 12px; }}
QLabel#Banner {{
    background: #EAF2FF;
    border: 1px solid #C9DDFB;
    border-radius: 8px;
    padding: 10px 12px;
    color: #1E40AF;
}}
QLabel#ViewBadge {{
    background: {BRAND}; color: #FFFFFF;
    border-radius: 11px; padding: 3px 12px; font-weight: 600;
}}
QLabel#CountBadge {{
    background: #EEF4FF; color: {BRAND_DARK};
    border: 1px solid #C9DDFB; border-radius: 11px; padding: 2px 10px;
}}
QLabel#GroupBadge {{ color: {MUTED}; font-size: 12px; }}
QFrame#HLine {{ background: {LINE}; max-height: 1px; border: none; }}

/* ── 按钮 ────────────────────────────────────────── */
QPushButton {{
    background: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    padding: 6px 14px;
    color: #334155;
}}
QPushButton:hover  {{ border-color: #60A5FA; background: #F5F9FF; color: {BRAND_DARK}; }}
QPushButton:pressed{{ background: #E8F0FE; }}
QPushButton#Primary {{
    background: {BRAND}; border: 1px solid {BRAND};
    color: #FFFFFF; font-weight: 600; padding: 7px 18px;
}}
QPushButton#Primary:hover {{ background: {BRAND_DARK}; color: #FFFFFF; }}
QPushButton#Quiet {{ background: transparent; border: 1px solid transparent; color: {MUTED}; }}
QPushButton#Quiet:hover {{ background: #EEF4FF; color: {BRAND_DARK}; }}
QToolButton {{ border: none; padding: 4px; }}

/* ── 输入控件 ────────────────────────────────────── */
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit {{
    background: #FFFFFF;
    border: 1px solid #D7E0EC;
    border-radius: 6px;
    padding: 6px 9px;
    selection-background-color: #BFDBFE;
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border-color: #60A5FA; }}
QLineEdit[readOnly="true"] {{ background: #F8FAFD; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #64748B;
    margin-right: 7px;
}}

/* ── 下拉弹层（v2.9.4）：原来未写样式，深色系统下是黑底深字，
      这里与主窗口统一成白底 / 浅蓝悬停 / 蓝字选中 ─────────── */
QComboBox QAbstractItemView {{
    background: #FFFFFF;
    border: 1px solid #D8E1EE;
    border-radius: 8px;
    padding: 4px;
    outline: 0;
    color: {INK};
    selection-background-color: #E8F0FE;
    selection-color: {BRAND_DARK};
    alternate-background-color: #FFFFFF;
}}
QComboBox QAbstractItemView::item {{
    min-height: 26px;
    padding: 4px 10px;
    border-radius: 5px;
    color: {INK};
    background: transparent;
}}
QComboBox QAbstractItemView::item:hover {{
    background: #F5F9FF; color: {BRAND_DARK};
}}
QComboBox QAbstractItemView::item:selected {{
    background: #E8F0FE; color: {BRAND_DARK};
}}
QComboBox QAbstractItemView::item:disabled {{ color: #AAB4C4; }}

/* 可编辑下拉的输入框：与外框同色，避免出现第二层边框 */
QComboBox QLineEdit {{
    background: transparent; border: none; padding: 0; color: {INK};
}}

/* QDateEdit 的日历弹层：同样不能交给深色系统调色板 */
QCalendarWidget QWidget {{ background: #FFFFFF; color: {INK}; }}
QCalendarWidget QAbstractItemView {{
    background: #FFFFFF; color: {INK};
    selection-background-color: #DBEAFE; selection-color: #0F2A5C;
}}
QCalendarWidget QToolButton {{
    background: transparent; color: {INK}; border: none; padding: 4px 8px;
}}
QCalendarWidget QToolButton:hover {{ background: #EEF4FF; color: {BRAND_DARK}; }}
QCalendarWidget QMenu {{ background: #FFFFFF; color: {INK}; }}
QCalendarWidget QSpinBox {{
    background: #FFFFFF; color: {INK}; border: 1px solid #D7E0EC;
}}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px; border: 1.6px solid #94A3B8;
    border-radius: 4px; background: #FFFFFF;
}}
QCheckBox::indicator:hover {{ border-color: {BRAND}; }}
QCheckBox::indicator:checked {{
    background: {BRAND}; border-color: {BRAND};{_CHECKED_IMG}
}}
QCheckBox::indicator:disabled {{
    background: #F1F5F9; border-color: #CBD5E1;
}}

/* ── 表格 ────────────────────────────────────────── */
QTableWidget {{
    background: #FFFFFF;
    border: 1px solid {LINE};
    border-radius: 8px;
    gridline-color: #F0F4FA;
}}
QTableWidget::item {{ padding: 5px 7px; }}
QTableWidget::item:alternate {{ background: #FAFCFF; }}
QTableWidget::item:selected {{ background: #DBEAFE; color: #0F2A5C; }}
QHeaderView::section {{
    background: #EDF3FB;
    color: #334155;
    font-weight: 600;
    padding: 7px 9px;
    border: none;
    border-right: 1px solid #E4EBF5;
    border-bottom: 1px solid #D9E2EF;
}}
QHeaderView::section:horizontal:first {{ border-top-left-radius: 8px; }}
QTableCornerButton::section {{ background: #EDF3FB; border: none; }}

/* ── 左侧操作树 ─────────────────────────────────── */
QTreeWidget {{
    background: #FFFFFF;
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 7px 4px;
    show-decoration-selected: 1;
}}
QTreeWidget::item {{ padding: 3px 5px; border-radius: 5px; }}
QTreeWidget::item:hover    {{ background: #F1F6FF; }}
QTreeWidget::item:selected {{ background: #E4EEFF; color: {BRAND_DARK}; }}
QTreeWidget::branch {{ background: transparent; }}

/* ── 分组框 / 分割 / 状态栏 ─────────────────────── */
QGroupBox {{
    background: #FFFFFF;
    border: 1px solid {LINE};
    border-radius: 10px;
    margin-top: 15px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 13px; padding: 0 6px;
    color: {BRAND_DARK};
}}
QSplitter::handle {{ background: #E6ECF5; }}
QSplitter::handle:horizontal {{ width: 5px; }}
QSplitter::handle:vertical   {{ height: 5px; }}
QStatusBar {{ background: #FFFFFF; border-top: 1px solid {LINE}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {MUTED}; }}
QScrollArea {{ border: none; background: transparent; }}
QWidget#FormScrollContent {{ background: transparent; }}
QScrollBar:vertical   {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #CBD5E1; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: #0F2A5C; color: #FFFFFF; border: none; padding: 5px; }}

/* ── 数字输入（QDoubleSpinBox 不是 QSpinBox 子类，必须单列） ── */
QAbstractSpinBox {{
    background: #FFFFFF;
    border: 1px solid #D7E0EC;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #BFDBFE;
}}
QAbstractSpinBox:focus {{ border-color: #60A5FA; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    background: #F1F6FF; border: none; width: 17px; border-radius: 3px;
}}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: #DBEAFE;
}}

/* ── 弹窗外壳：与主窗口同源的浅灰蓝底 + 白卡片 ───────── */
QDialog, QMessageBox, QFileDialog {{ background: {BG}; }}
QFrame#DialogHeader, QFrame#DialogBody, QFrame#DialogFooter {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 10px;
}}
QFrame#DialogHeader {{ border-bottom: 2px solid #DBEAFE; }}
QFrame#DialogFooter {{ border-top: 1px solid {LINE}; }}
QLabel#DialogTitle    {{ font-size: 17px; font-weight: 700; color: #0F2A5C; }}
QLabel#DialogSubTitle {{ color: {MUTED}; font-size: 12px; }}
QLabel#DialogHint     {{ color: {MUTED}; }}
QLabel#DialogError    {{ color: {DANGER}; font-weight: 600; }}
QLabel#DialogCount    {{ color: {BRAND_DARK}; font-weight: 600; }}
QDialogButtonBox {{ background: transparent; }}
QDialogButtonBox QPushButton {{ min-width: 78px; }}
QMessageBox QLabel {{ color: {INK}; }}
QMessageBox QPushButton {{ min-width: 78px; }}

/* ── 文件对话框：Qt 自绘模式下统一内部列表/树/表头 ───── */
QFileDialog QTreeView, QFileDialog QListView, QFileDialog QTableView {{
    background: #FFFFFF;
    border: 1px solid {LINE};
    border-radius: 8px;
    alternate-background-color: #FAFCFF;
    selection-background-color: #DBEAFE;
    selection-color: #0F2A5C;
}}
QFileDialog QHeaderView::section {{
    background: #EDF3FB; color: #334155; font-weight: 600;
    padding: 6px 9px; border: none; border-right: 1px solid #E4EBF5;
}}
QFileDialog QLabel, QFileDialog QCheckBox {{ color: {INK}; }}
QFileDialog QSplitter::handle {{ background: #E6ECF5; }}
QFileDialog QFrame {{ background: transparent; border: none; }}
QFileDialog QListView#sidebar {{
    background: #FFFFFF; border: 1px solid {LINE}; border-radius: 8px;
}}

/* ── 日期选择控件（v3.2.0）：只读输入框 + 弹出日历 ─────── */
QWidget#DatePicker QLineEdit#DateField {{ background: #F8FAFD; color: {INK}; }}
QPushButton#DatePick {{ padding: 6px 10px; }}
QPushButton#DateClear {{
    padding: 4px 6px; color: {MUTED}; font-weight: 700;
}}
QPushButton#DateClear:disabled {{ color: #CBD5E1; }}
QCalendarWidget {{
    background: #FFFFFF; border: 1px solid {LINE}; border-radius: 8px;
}}
QCalendarWidget QWidget#qt_calendar_navigationbar {{
    background: #F5F9FF; border-bottom: 1px solid {LINE};
}}
QCalendarWidget QAbstractItemView:enabled {{
    color: {INK};
    selection-background-color: {BRAND}; selection-color: #FFFFFF;
}}
QCalendarWidget QAbstractItemView:disabled {{ color: #AAB4C4; }}
QCalendarWidget QHeaderView::section {{
    background: #EDF3FB; color: #334155; border: none;
}}

/* ── 操作导航（v3.2.0）：隐藏后的恢复条 + 主界面快捷栏 ── */
QPushButton#NavRestore {{
    background: #EEF4FF; border: 1px solid #C9DDFB;
    color: {BRAND_DARK}; font-weight: 600; padding: 8px 2px;
    border-radius: 8px; line-height: 1.5;
}}
QPushButton#NavRestore:hover {{ background: #E4EEFF; }}
QWidget#ActionBar QPushButton {{ padding: 5px 12px; }}
QPushButton#MoreActions {{ color: {BRAND_DARK}; font-weight: 600; }}

/* ── 交期提醒状态栏徽标（v3.3.0）：属性 level 由主窗口动态切换 ── */
QPushButton#DueBadge {{
    background: #FFFFFF; border: 1px solid {LINE}; border-radius: 12px;
    color: {MUTED}; padding: 3px 12px; font-size: 12px;
}}
QPushButton#DueBadge:hover {{ background: #EEF4FF; border-color: #C9DDFB; }}
QPushButton#DueBadge[level="danger"] {{
    background: #FFF1F0; border-color: #FCA5A5; color: #B91C1C; font-weight: 600;
}}
QPushButton#DueBadge[level="ok"] {{ color: #334155; }}
QPushButton#DueBadge[level="off"] {{ color: #94A3B8; }}
"""


def style_table(table):
    """统一的只读结果表外观（局部导入，保证零依赖环境可导入本模块）"""
    from PySide6.QtWidgets import QAbstractItemView, QHeaderView

    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(31)
    table.horizontalHeader().setHighlightSections(False)
    table.horizontalHeader().setMinimumSectionSize(58)
    table.setObjectName("ResultTable")
    return table


def card(widget, object_name="Card"):
    """把 QWidget/QFrame 标记为白卡片"""
    widget.setObjectName(object_name)
    return widget


def kpi_card(number: str, label: str):
    """返回一个 (widget, number_label)，数字可由调用方后续刷新"""
    from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel
    from PySide6.QtCore import Qt

    frame = QFrame()
    frame.setObjectName("Card")
    frame.setMinimumHeight(64)
    v = QVBoxLayout(frame)
    v.setContentsMargins(12, 8, 12, 8)
    v.setSpacing(1)
    num = QLabel(number)
    num.setObjectName("KpiNum")
    num.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    cap = QLabel(label)
    cap.setObjectName("KpiLabel")
    v.addWidget(num)
    v.addWidget(cap)
    return frame, num


def label(text: str, object_name: str = "Hint", word_wrap: bool = False):
    from PySide6.QtWidgets import QLabel

    lab = QLabel(text)
    lab.setObjectName(object_name)
    lab.setWordWrap(word_wrap)
    return lab


# ── 主题单一入口 ────────────────────────────────────────────

_QT_TRANSLATOR = None
_APP_ICON = None


def app_icon():
    """
    应用图标（v3.0.1）：v2/ui/app.ico，源码运行与打包后同路径规则——
    打包时 build_v2.py 用 --add-data 把它放到 _internal/v2/ui/ 下，与 theme.py 同目录。
    找不到文件返回 None（调用方跳过设置，不报错）。
    """
    global _APP_ICON
    if _APP_ICON is not None:
        return _APP_ICON if _APP_ICON is not False else None
    try:
        from pathlib import Path
        from PySide6.QtGui import QIcon
        path = Path(__file__).resolve().parent / "app.ico"
        _APP_ICON = QIcon(str(path)) if path.is_file() else False
    except Exception:
        _APP_ICON = False
    return _APP_ICON if _APP_ICON else None


def apply_theme(app) -> bool:
    """
    把 QSS 挂到 QApplication（幂等）。所有顶层弹窗都是 QApplication 的后代，
    因此自动继承同一套配色；不要改成 window.setStyleSheet(QSS)。
    顺带安装 Qt 中文翻译，让文件对话框等 Qt 内置文本不再是英文。

    v2.9.4：同时装一份【浅色调色板】。只挂 QSS 不够——QComboBox 下拉弹层、
    QDateEdit 日历等由 QStyle 自绘的弹出部件仍读系统调色板，Windows 深色模式下
    会变黑底深字（用户报「下拉选项黑底选不中」）。调色板 + QSS 双管齐下。
    """
    global _QT_TRANSLATOR
    if app is None:
        return False
    try:
        from PySide6.QtGui import QColor, QPalette
        role = QPalette.ColorRole
        pal = app.palette()
        for key, color in ((role.Window, BG), (role.WindowText, INK),
                           (role.Base, CARD), (role.AlternateBase, "#FAFCFF"),
                           (role.Text, INK), (role.Button, CARD),
                           (role.ButtonText, "#334155"),
                           (role.Highlight, "#DBEAFE"),
                           (role.HighlightedText, "#0F2A5C"),
                           (role.ToolTipBase, "#0F2A5C"),
                           (role.ToolTipText, "#FFFFFF"),
                           (role.Light, "#FFFFFF"), (role.Midlight, "#F1F6FF"),
                           (role.Mid, LINE), (role.Dark, "#CBD5E1")):
            pal.setColor(key, QColor(color))
        pal.setColor(QPalette.ColorGroup.Disabled, role.Text, QColor("#AAB4C4"))
        pal.setColor(QPalette.ColorGroup.Disabled, role.WindowText,
                     QColor("#AAB4C4"))
        pal.setColor(QPalette.ColorGroup.Disabled, role.ButtonText,
                     QColor("#AAB4C4"))
        app.setPalette(pal)
    except Exception:
        pass                          # 调色板异常不阻断 QSS 挂载
    if app.styleSheet() != QSS:
        app.setStyleSheet(QSS)
    # v3.0.1：应用全局图标（任务栏 / 标题栏 / Alt+Tab 一致）
    icon = app_icon()
    if icon is not None:
        try:
            app.setWindowIcon(icon)
        except Exception:
            pass
    if _QT_TRANSLATOR is None:
        try:
            import sys
            from pathlib import Path
            from PySide6.QtCore import QTranslator, QLibraryInfo
            roots = [Path(QLibraryInfo.path(QLibraryInfo.TranslationsPath))]
            # PyInstaller 打包后 QLibraryInfo 可能仍指向开发机路径，补随包目录
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                roots.append(Path(meipass) / "PySide6" / "translations")
            if getattr(sys, "frozen", False):
                roots.append(Path(sys.executable).parent / "_internal"
                             / "PySide6" / "translations")
            qm = next((r / "qtbase_zh_CN.qm" for r in roots
                       if (r / "qtbase_zh_CN.qm").is_file()), None)
            tr = QTranslator(app)
            if qm is not None and tr.load(str(qm)):
                app.installTranslator(tr)
                _QT_TRANSLATOR = tr
        except Exception:
            pass                      # 翻译缺失不影响主题与业务
    return True


def polish_tree(root) -> None:
    """
    刷新整棵控件树的 QSS。控件创建后再 setObjectName() 时，Qt 的
    QStyleSheetStyle 会缓存旧名字，#Primary / #DialogBody 这类 ID 选择器
    不会自动重算（症状：主按钮仍是白色）。构造完弹窗后必须显式刷一次。
    """
    from PySide6.QtWidgets import QWidget

    targets = [root] + root.findChildren(QWidget)
    for w in targets:
        try:
            w.style().unpolish(w)
            w.style().polish(w)
        except Exception:
            pass


# ── 弹窗外壳 ────────────────────────────────────────────────

def dialog_shell(dialog, title: str, subtitle: str = "", badge: str = BADGE,
                 width: int = 760, height: int = 520):
    """
    给 QDialog 套三段白卡片：标题头 / 内容 / 底部按钮区。
    返回 (body_layout, footer_layout)，由调用方往里放控件。
    与主窗口 _brand_bar() 使用同一组 PageTitle / SubTitle / ViewBadge 语义。

    v3.0.1：badge 默认取 BADGE（= v2/__init__.__version__），所有弹窗
    版本号自动随全局版本走，不再各自硬编码；同时给窗口挂应用图标。
    """
    from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel

    dialog.setObjectName("ThemedDialog")
    dialog.setWindowTitle(title)
    icon = app_icon()
    if icon is not None:
        try:
            dialog.setWindowIcon(icon)
        except Exception:
            pass
    dialog.resize(width, height)
    dialog.setMinimumWidth(540)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(14, 12, 14, 12)
    outer.setSpacing(10)

    header = QFrame()
    header.setObjectName("DialogHeader")
    header.setMinimumHeight(54)      # 长副标题可撑高，避免被压成竖排
    hh = QHBoxLayout(header)
    hh.setContentsMargins(16, 6, 16, 6)
    hh.setSpacing(10)
    t = QLabel(title if len(title) <= 36 else title[:36] + "…")
    t.setToolTip(title)
    t.setObjectName("DialogTitle")
    hh.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("DialogSubTitle")
        s.setWordWrap(True)
        s.setMinimumWidth(160)
        hh.addWidget(s, 1)
    else:
        hh.addStretch(1)
    if badge:
        b = QLabel(badge)
        b.setObjectName("ViewBadge")
        hh.addWidget(b)
    outer.addWidget(header)

    body = QFrame()
    body.setObjectName("DialogBody")
    bl = QVBoxLayout(body)
    bl.setContentsMargins(16, 14, 16, 14)
    bl.setSpacing(10)
    outer.addWidget(body, 1)

    footer = QFrame()
    footer.setObjectName("DialogFooter")
    fl = QHBoxLayout(footer)
    fl.setContentsMargins(14, 9, 14, 9)
    fl.setSpacing(8)
    fl.addStretch(1)
    outer.addWidget(footer)

    # 挂到 dialog 上，便于离屏测试断言与后续扩展
    dialog.header = header
    dialog.body = body
    dialog.body_layout = bl
    dialog.footer = footer
    dialog.footer_layout = fl
    return bl, fl


def dialog_buttons(parent, ok_text: str = "确定", cancel_text: str = "取消",
                   show_cancel: bool = True):
    """中文化的按钮组；主按钮挂 #Primary，与主窗口强调按钮同款"""
    from PySide6.QtWidgets import QDialogButtonBox

    box = QDialogButtonBox(parent)
    ok = box.addButton(ok_text, QDialogButtonBox.AcceptRole)
    ok.setObjectName("Primary")
    ok.setDefault(True)
    if show_cancel:
        box.addButton(cancel_text, QDialogButtonBox.RejectRole)
    return box


# ── 消息框 ──────────────────────────────────────────────────

def make_message(parent, title: str, text: str, kind: str = "info",
                 detail: str = "", yes_no: bool = False):
    """只构造不 exec，便于离屏测试；按钮中文 + 主按钮主题化"""
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setObjectName("ThemedMessageBox")
    box.setWindowTitle(title)
    box.setText(text)
    icons = {
        "info": QMessageBox.Information,
        "warn": QMessageBox.Warning,
        "error": QMessageBox.Critical,
        "question": QMessageBox.Question,
    }
    box.setIcon(icons.get(kind, QMessageBox.Information))
    if detail:
        box.setDetailedText(detail)
    if yes_no:
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        yes = box.button(QMessageBox.Yes)
        yes.setText("确定")
        yes.setObjectName("Primary")
        box.button(QMessageBox.No).setText("取消")
        box.setDefaultButton(QMessageBox.Yes)
    else:
        box.setStandardButtons(QMessageBox.Ok)
        ok = box.button(QMessageBox.Ok)
        ok.setText("知道了")
        ok.setObjectName("Primary")
    box.setMinimumWidth(430)
    polish_tree(box)
    return box


def notify(parent, title: str, text: str, kind: str = "info", detail: str = ""):
    from PySide6.QtWidgets import QMessageBox
    return make_message(parent, title, text, kind, detail).exec() == QMessageBox.Ok


def confirm(parent, title: str, text: str, detail: str = "") -> bool:
    from PySide6.QtWidgets import QMessageBox
    box = make_message(parent, title, text, "question", detail, yes_no=True)
    return box.exec() == QMessageBox.Yes


# ── 文件对话框 ─────────────────────────────────────────────
# v3.1.1：Windows 默认用【资源管理器原生对话框】。Qt 自绘框不走 Shell，
# 对 OneDrive「仅联机」占位文件既不显示同步状态、也没有云盘节点，
# 用户从资源管理器选到云盘文件后程序读不出来。原生框由 Shell 提供，
# 自带 OneDrive / SharePoint 集成（选中即触发水合），这是根治办法。
# 离屏/自检环境仍退回自绘（原生框在 offscreen 平台无法创建）。

def use_native_file_dialog() -> bool:
    """是否使用 Windows 原生文件框；可用环境变量 PQS_FILE_DIALOG=qt 强制自绘"""
    import os
    import sys
    from PySide6.QtWidgets import QApplication
    if sys.platform != "win32":
        return False
    if os.environ.get("PQS_FILE_DIALOG", "").strip().lower() == "qt":
        return False
    # 离屏自检 / CI 没有 Shell，直接看环境变量（此时 QApplication 可能还没建）
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() in (
            "offscreen", "minimal", "minimalegl"):
        return False
    app = QApplication.instance()
    if app is not None and app.platformName().lower() in ("offscreen", "minimal"):
        return False
    return True


def cloud_roots() -> list:
    """文件框侧边栏的云盘根目录（OneDrive 等），core 层统一探测"""
    try:
        from ..core import cloudfile
        return cloudfile.onedrive_dirs()
    except Exception:
        return []


def _sidebar_urls() -> list:
    from pathlib import Path
    from PySide6.QtCore import QDir, QUrl
    roots = [QDir.homePath(), QDir.currentPath(), QDir.currentPath() + "/data"]
    roots.extend(cloud_roots())           # v3.1.1 云盘入口
    urls, seen = [], set()
    for p in roots:
        try:
            q = str(Path(p).resolve())
        except Exception:
            continue
        if q.lower() in seen or not Path(q).is_dir():
            continue
        seen.add(q.lower())
        urls.append(QUrl.fromLocalFile(q))
    return urls


def make_file_dialog(parent, title: str, mode: str = "open",
                     default_name: str = "", filters: str = ""):
    from PySide6.QtWidgets import QFileDialog

    # v3.0.1 防御：历史 bug——过滤器常量带尾逗号变成 1 元组，
    # setNameFilter(元组) 抛 ValueError 且被 Qt 吞掉，表现为「点了没反应」。
    if isinstance(filters, (tuple, list)):
        filters = filters[0] if filters else ""
    dlg = QFileDialog(parent, title)
    dlg.setObjectName("ThemedFileDialog")
    if not use_native_file_dialog():
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
    dlg.setViewMode(QFileDialog.Detail)
    if mode == "save":
        dlg.setAcceptMode(QFileDialog.AcceptSave)
        dlg.setFileMode(QFileDialog.AnyFile)
        if default_name:
            dlg.selectFile(default_name)
    elif mode == "dir":
        dlg.setAcceptMode(QFileDialog.AcceptOpen)
        dlg.setFileMode(QFileDialog.Directory)
        dlg.setOption(QFileDialog.ShowDirsOnly, True)
    elif mode == "multi":
        # v2.4 订单排产：图片 / 图纸一次可多选
        dlg.setAcceptMode(QFileDialog.AcceptOpen)
        dlg.setFileMode(QFileDialog.ExistingFiles)
    else:
        dlg.setAcceptMode(QFileDialog.AcceptOpen)
        dlg.setFileMode(QFileDialog.ExistingFile)
    if filters:
        dlg.setNameFilter(filters)
    # 侧边栏常用位置 + OneDrive（原生框与自绘框都生效）
    sidebar = _sidebar_urls()
    if sidebar:
        dlg.setSidebarUrls(sidebar)
    # Qt 内置文案显式中文化（不依赖翻译文件是否随包）
    for label, text in ((QFileDialog.LookIn, "查找范围："),
                        (QFileDialog.FileName, "文件名："),
                        (QFileDialog.FileType, "文件类型："),
                        (QFileDialog.Accept, "选择"),
                        (QFileDialog.Reject, "取消")):
        dlg.setLabelText(label, text)
    polish_tree(dlg)
    return dlg


def _materialize_paths(parent, paths: list) -> list:
    """
    v3.1.1：选中云盘占位文件后**先水合预热**（复制到本地缓存，带超时）。
    返回**原始路径列表**，不改变业务语义 —— 路径字段（如询配码文件路径）
    存的仍是用户选的真实位置；真正读内容时由 docread / excelio / schedule
    各自调用 cloudfile.ensure_readable() 命中同一份缓存副本。
    失败弹明确提示并跳过该文件，不让「点了没反应」或界面无限卡死复现。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ..core import cloudfile
    from ..core.errors import ValidationError

    cloud = [p for p in paths if cloudfile.is_placeholder(p)]
    if not cloud:
        return list(paths)                # 全本地文件，零等待零开销

    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        out = []
        for p in paths:
            try:
                # 等待云端同步时每 50ms 刷一次事件，界面保持可响应
                cloudfile.ensure_readable(
                    p, tick=lambda: QApplication.processEvents())
                out.append(p)
            except ValidationError as exc:
                notify(parent, "云端文件未就绪", str(exc), kind="warn")
            except Exception as exc:        # noqa: BLE001
                notify(parent, "读取文件失败", f"{p}\n{exc}", kind="error")
        return out
    finally:
        QApplication.restoreOverrideCursor()


def pick_path(parent, title: str, mode: str = "open", default_name: str = "",
              filters: str = "") -> str:
    """返回选中路径（字符串），取消返回空串；替代原 QFileDialog 静态函数的 (path, _) 二元组"""
    from PySide6.QtWidgets import QFileDialog

    if isinstance(filters, (tuple, list)):
        filters = filters[0] if filters else ""
    try:
        dlg = make_file_dialog(parent, title, mode, default_name, filters)
        if dlg.exec() == QFileDialog.Accepted:
            files = dlg.selectedFiles()
            path = files[0] if files else ""
        else:
            return ""
    except Exception:
        # 对话框创建/执行异常 → 退回 Qt 静态函数（会自动用原生框），
        # 绝不让「点了没反应」复现
        if mode == "save":
            path, _ = QFileDialog.getSaveFileName(parent, title, default_name, filters)
        elif mode == "dir":
            path = QFileDialog.getExistingDirectory(parent, title)
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, "", filters)
        path = path or ""
    if not path or mode in ("save", "dir"):
        return path                      # 保存目标 / 目录不需要水合
    done = _materialize_paths(parent, [path])
    return done[0] if done else ""


def pick_paths(parent, title: str, filters: str = "") -> list:
    """v2.4 订单排产：多选图片 / 图纸；v3.1.1 选中后自动水合云盘占位文件"""
    from PySide6.QtWidgets import QFileDialog

    if isinstance(filters, (tuple, list)):
        filters = filters[0] if filters else ""
    try:
        dlg = make_file_dialog(parent, title, "multi", "", filters)
        if dlg.exec() == QFileDialog.Accepted:
            paths = list(dlg.selectedFiles())
        else:
            return []
    except Exception:
        paths, _ = QFileDialog.getOpenFileNames(parent, title, "", filters)
        paths = [p for p in paths if p]
    return _materialize_paths(parent, paths)

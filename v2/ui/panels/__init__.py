"""主窗口的视图与动作组合（P2-2 拆分产物）。

刻意不在包初始化时 import 子模块：
  · 让 `panels.common` 能在无 Qt 环境单独导入（常量与纯数据）；
  · 避免包初始化就把 PySide6 拉进来，破坏 core 层的零依赖约定。
按需从具体模块导入：from .panels.views import ViewsMixin
"""

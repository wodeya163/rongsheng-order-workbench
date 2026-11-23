#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面异步任务（P1-7）

背景：PDF 抽表 + OCR 是纯 CPU 的重活，之前全在主线程跑，
      30 页 PDF 或手机大图会让窗口「未响应」，用户以为程序死了。

用法：
    job = run_task(self, lambda: docread.read_source(path),
                   on_done=self._apply, on_error=self._fail,
                   on_progress=self._hint)
    # 回调都在【主线程】执行（Qt 信号跨线程自动排队），可以安全改界面

约定：
  · fn 里不要碰任何 QWidget；
  · 任务对象挂在 owner 上（owner._jobs），防止 QRunnable 被 GC；
  · 完成后自动从 owner 上摘除，不需要手工清理。
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, List, Optional

try:
    from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
    PYSIDE_AVAILABLE = True
except ImportError:                                   # 纯逻辑环境仍可导入本模块
    PYSIDE_AVAILABLE = False


if PYSIDE_AVAILABLE:

    class TaskSignals(QObject):
        done = Signal(object)
        failed = Signal(str)
        progress = Signal(str)

    class Task(QRunnable):
        def __init__(self, fn: Callable[[], Any]):
            super().__init__()
            self.fn = fn
            self.signals = TaskSignals()
            self.setAutoDelete(True)

        def run(self) -> None:                       # 在子线程执行
            try:
                result = self.fn()
            except Exception as exc:                 # noqa: BLE001 业务异常一律回主线程展示
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                self.signals.failed.emit(detail)
                return
            self.signals.done.emit(result)

    def run_task(owner, fn: Callable[[], Any], on_done: Callable[[Any], None],
                 on_error: Optional[Callable[[str], None]] = None,
                 on_progress: Optional[Callable[[str], None]] = None) -> "Task":
        """把 fn 丢到全局线程池，回调在主线程；任务引用挂在 owner 上防 GC"""
        job = Task(fn)
        jobs: List[Task] = getattr(owner, "_jobs", None)
        if jobs is None:
            jobs = []
            setattr(owner, "_jobs", jobs)
        jobs.append(job)

        def _drop() -> None:
            try:
                jobs.remove(job)
            except ValueError:
                pass

        def _done(result: Any) -> None:
            _drop()
            on_done(result)

        def _fail(message: str) -> None:
            _drop()
            if on_error:
                on_error(message)

        job.signals.done.connect(_done)
        job.signals.failed.connect(_fail)
        if on_progress:
            job.signals.progress.connect(on_progress)
        QThreadPool.globalInstance().start(job)
        return job

    def busy(owner) -> bool:
        """该 owner 是否有未完成的后台任务"""
        return bool(getattr(owner, "_jobs", None))

    def wait_all(owner, timeout_ms: int = 30000) -> None:
        """测试/退出时等待：轮询处理事件直到任务清空"""
        from PySide6.QtCore import QDeadlineTimer, QCoreApplication
        timer = QDeadlineTimer(timeout_ms)
        while busy(owner) and not timer.hasExpired():
            QCoreApplication.processEvents()

else:

    def run_task(*_args, **_kwargs):                 # pragma: no cover
        raise RuntimeError("未安装 PySide6，异步任务不可用")

    def busy(_owner) -> bool:
        return False

    def wait_all(_owner, timeout_ms: int = 0) -> None:
        return None

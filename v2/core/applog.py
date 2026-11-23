#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用日志与崩溃捕获（v2.6 · P0-4）

目标：任何"点了没反应 / 闪退 / 报错弹窗"都必须在 logs/ 里留下可分析证据。

  1. 文件日志：logs/app-YYYYMMDD.log，单文件 5MB × 5 份滚动，UTF-8
  2. 未捕获异常：sys.excepthook / threading.excepthook 全量记录堆栈
  3. Qt 警告：qInstallMessageHandler 转进同一日志（绑定错误默认只输出到调试器）
  4. 上下文：启动时写入版本、是否打包、数据库路径、数据目录、Python 版本

约定：业务代码用 get_logger(__name__)，不要在 core/domains 里直接 print。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER_NAME = "pqs"
_FMT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5

_configured = False
_hooks_installed = False
_log_dir: Optional[Path] = None


# ── 路径 ────────────────────────────────────────────────────


def default_log_dir() -> Path:
    """打包后 exe 同目录 logs/；源码运行项目根 logs/"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parents[2] / "logs"


def log_dir() -> Path:
    return _log_dir or default_log_dir()


def current_log_file() -> Path:
    return log_dir() / f"app-{__import__('datetime').date.today():%Y%m%d}.log"


# ── 初始化 ──────────────────────────────────────────────────


def setup(level: str = "INFO", directory: Optional[str] = None,
          context: Optional[Dict[str, Any]] = None) -> Path:
    """
    初始化日志（幂等）。返回日志目录。

    context 里的键值会以一行 "Context: k=v" 写入日志，便于定位环境问题。
    """
    global _configured, _hooks_installed, _log_dir

    target = Path(directory) if directory else default_log_dir()
    target.mkdir(parents=True, exist_ok=True)
    _log_dir = target

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    if not _configured:
        fmt = logging.Formatter(_FMT, _DATEFMT)

        fh = RotatingFileHandler(
            target / f"app-{__import__('datetime').date.today():%Y%m%d}.log",
            maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

        # 只在真正的控制台里回显：windowed exe / 管道重定向下 stderr 是 cp936，
        # 直接写中文会变成乱码噪音（文件日志始终 UTF-8，不受影响）
        try:
            if sys.stderr is not None and sys.stderr.isatty():
                sh = logging.StreamHandler(sys.stderr)
                sh.setFormatter(fmt)
                root.addHandler(sh)
        except Exception:
            pass

        if not _hooks_installed:
            install_excepthook()
            install_qt_handler()
            _hooks_installed = True
        _configured = True

    if context:
        root.info("=" * 62)
        root.info("应用启动 | " + " | ".join(f"{k}={v}" for k, v in context.items()))
    return target


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def reset() -> None:
    """仅测试/重定向用：摘掉现有 handler，允许换目录重新初始化（hook 不重复安装）"""
    global _configured, _log_dir
    root = logging.getLogger(LOGGER_NAME)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _configured = False
    _log_dir = None


# ── 崩溃捕获 ────────────────────────────────────────────────


def install_excepthook() -> None:
    """主线程 + 子线程未捕获异常全量落盘（保留原 hook 行为）"""
    prev_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            prev_hook(exc_type, exc_value, exc_tb)
            return
        logging.getLogger(f"{LOGGER_NAME}.crash").critical(
            "未捕获异常（主线程）", exc_info=(exc_type, exc_value, exc_tb))
        prev_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    prev_thread_hook = getattr(threading, "excepthook", None)

    def _thread_hook(args: "threading.ExceptHookArgs"):
        logging.getLogger(f"{LOGGER_NAME}.crash").critical(
            "未捕获异常（线程 %s）", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        if prev_thread_hook:
            prev_thread_hook(args)

    threading.excepthook = _thread_hook


def install_qt_handler() -> None:
    """Qt 自己的警告（含 QSS/绑定/布局告警）转进日志；Qt 未装时静默跳过"""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def _handler(mode, _ctx, message):
        logging.getLogger(f"{LOGGER_NAME}.qt").log(
            levels.get(mode, logging.INFO), "%s", message)

    qInstallMessageHandler(_handler)


# ── 便捷入口 ────────────────────────────────────────────────


def open_log_dir() -> bool:
    """在资源管理器里打开日志目录（失败返回 False）"""
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))          # noqa: S606 — Windows 专用，个人单机工具
        return True
    except Exception:
        get_logger("applog").exception("打开日志目录失败")
        return False


def log_exception(label: str, exc: BaseException) -> None:
    """界面统一动作包装用：一条记录带完整堆栈，label 即操作名"""
    get_logger("ui.action").error("操作失败：%s | %s", label, exc, exc_info=exc)

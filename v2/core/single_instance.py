#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单实例检测（P1-11）

为什么要做：
    SQLite 的 WAL 不跨网络共享盘；即使本机，两个窗口同时编辑同一张订单也会
    互相覆盖（后保存的赢），而用户看不出是谁改的。个人单机使用时，
    正确做法是「同一数据库只允许一个实例」，第二个实例明确提示并退出。

实现：
    Windows 具名互斥体（Local\\ 命名空间，按数据库绝对路径取哈希），
    进程退出由系统自动释放，不产生残留锁文件；
    非 Windows（或调用失败）退化为 data/.app.lock 里的 pid 探测。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

_handle: Optional[int] = None
_lock_path: Optional[Path] = None


def _key(db_path: str) -> str:
    raw = str(Path(db_path).resolve()).lower().encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def _acquire_windows(name: str) -> Optional[bool]:
    """返回 True=拿到锁，False=已被占用，None=当前平台不支持"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, True, name)
        if not handle:
            return None
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        global _handle
        _handle = handle
        return True
    except Exception:                            # 极端环境下不阻断启动
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_file(db_path: str) -> bool:
    """兜底：data/.app.lock 里写 pid，进程在就视为已占用"""
    global _lock_path
    data_dir = Path(db_path).resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / ".app.lock"
    _lock_path = lock
    if lock.is_file():
        try:
            pid = int(lock.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            return False
    try:
        lock.write_text(f"{os.getpid()} {os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass
    return True


def acquire(db_path: str) -> bool:
    """True = 可以启动；False = 已有实例在用同一个库"""
    if _handle is not None:
        return True                              # 本进程已持有
    # 互斥体名保持 ProductSuite_ 前缀不变：新旧两个 exe 必须命中同一把锁，
    # 否则更名后旧版仍在运行时会被当成两个独立程序，互相覆盖同一个库。
    name = f"Local\\ProductSuite_{_key(db_path)}"
    result = _acquire_windows(name)
    if result is not None:
        return result
    return _acquire_file(db_path)


def release() -> None:
    global _handle, _lock_path
    if _handle is not None and os.name == "nt":
        try:
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(_handle))
        except Exception:
            pass
    _handle = None
    if _lock_path and _lock_path.is_file():
        try:
            content = _lock_path.read_text(encoding="utf-8").split()
            if content and int(content[0]) == os.getpid():
                _lock_path.unlink()
        except (OSError, ValueError):
            pass
    _lock_path = None


def warn_already_running(db_path: str, title: str = "榕盛商贸订单管理工作台") -> None:
    """在 GUI 尚未建立时也能弹出提示（优先 Win32 MessageBox，退回 stderr）"""
    text = (f"检测到已有一个实例正在使用同一个数据库：\n{db_path}\n\n"
            "为避免两个窗口互相覆盖数据，本次启动已取消。\n"
            "如果确需并行（例如对比数据），请加参数 --allow-multi 再启动。")
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x0)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)

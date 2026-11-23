"""云同步盘占位文件适配（OneDrive / SharePoint / 坚果云 等，v3.1.1）

问题（2026-11-08 本机实测）
--------------------------
Windows「按需文件 / Files On-Demand」让云盘文件在本地只留一个壳：
  · `os.lstat().st_file_attributes` 带 `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`
    (0x400000) 或 `RECALL_ON_OPEN` (0x40000)，实测 `D:\\OneDrive` 189 个文件
    里 166 个是这种云占位文件；
  · `Path.is_file()` / `stat()` 都正常，但**第一次真正读数据**会触发云端水合
    （快的 1.6s，慢的会无限阻塞），OneDrive 未登录 / 离线时直接抛
    `OSError [Errno 22] Invalid argument`；
  · Qt 自绘 QFileDialog (`DontUseNativeDialog`) 不走 Windows Shell，看不到
    OneDrive 的「仅联机」状态，也拿不到 Shell 自带的水合能力，侧边栏也没有云盘入口。

对策
----
1. `ensure_readable()`：云占位文件 → 在**守护线程**里复制到本地缓存副本
   （复制即触发水合并把实体落到本地），主线程最多等 N 秒；成功返回本地副本路径，
   后续 openpyxl / python-docx / pdfplumber / PIL 全部读副本，快且稳；
   超时或失败抛 `ValidationError` 并给出可操作提示，**绝不无限卡死界面**。
   普通本地文件直接原样返回，零开销。
2. `is_placeholder()`：只读属性位（`lstat`，不触发水合），零成本探测。
3. 文件框默认改 Windows 原生对话框（Shell 原生支持云盘），侧边栏补 OneDrive 目录。
4. 所有「读文档 / 表格 / 图片」「复制附件」「读 manifest」入口统一先过
   `ensure_readable()`。

缓存副本按 (原路径 + 大小 + mtime) 命中复用，避免同一文件反复水合；
统一放在系统临时目录 `pqs_cloud_cache/`，不污染业务数据目录。
本模块不依赖 PySide6，可单独被 core / domains / 打包自检引用。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from .errors import ValidationError

# Windows 文件属性位（非 Windows 平台全为 0，is_placeholder 恒 False）
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_OFFLINE = 0x00001000
_CLOUD_MASK = (FILE_ATTRIBUTE_RECALL_ON_OPEN
               | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
               | FILE_ATTRIBUTE_OFFLINE)

DEFAULT_TIMEOUT = 30.0          # 单个云占位文件水合最长等待（秒）
CACHE_ROOT = Path(tempfile.gettempdir()) / "pqs_cloud_cache"

_CLOUD_HINT = (
    "该文件是云盘「仅联机」文件（OneDrive / SharePoint 按需同步），"
    "本地还没有实际内容。请在资源管理器里右键此文件 →「始终保留在此设备上」，"
    "或确认 OneDrive 已登录且网络正常后重试。"
)


def file_attributes(path: os.PathLike | str) -> int:
    """只读属性位；任何异常都返回 0（当作普通本地文件处理，不阻断业务）"""
    try:
        st = os.lstat(os.fspath(path))
        return int(getattr(st, "st_file_attributes", 0) or 0)
    except Exception:
        return 0


def is_placeholder(path: os.PathLike | str) -> bool:
    """是否云同步占位文件（本地只有壳、数据在云端）"""
    if sys.platform != "win32":
        return False
    attrs = file_attributes(path)
    return bool(attrs) and bool(attrs & _CLOUD_MASK)


def _cache_path(p: Path, st) -> Path:
    """缓存 key = 原路径 + 大小 + mtime；同名不同内容不会串"""
    key = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]
    return CACHE_ROOT / digest / p.name


def _materialize(src: str, dst: Path, box: list) -> None:
    """子线程：复制即触发云盘水合，最后把实体内容落到本地缓存"""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, str(dst))
        # 副本大小必须与源文件一致；源文件本来就是 0 字节属合法（如空白工作簿），
        # 不能因为副本是 0 字节就误判成水合失败。
        src_size = Path(src).stat().st_size
        got = dst.stat().st_size if dst.is_file() else -1
        box.append("ok" if got == src_size else
                   f"size-mismatch: 源 {src_size} B / 副本 {got} B")
    except Exception as exc:                # noqa: BLE001
        box.append(f"{type(exc).__name__}: {exc}")
        try:
            dst.unlink(missing_ok=True)
        except Exception:
            pass


def ensure_readable(path: os.PathLike | str,
                    timeout: float = DEFAULT_TIMEOUT,
                    tick=None) -> str:
    """
    返回一份**本地实体可读**的文件路径（字符串）。

    · 普通本地文件 → 原样返回（只做一次 lstat，零拷贝）。
    · 云占位文件 → 守护线程复制水合到 `pqs_cloud_cache/`，主线程最多等 `timeout` 秒；
      命中缓存直接复用；超时/失败抛 ValidationError（附可操作提示），绝不无限阻塞。
    · `tick` 是可选的「每 50ms 回调」，UI 层传 `QApplication.processEvents`，
      等待云端同步期间界面保持可响应（core 层不依赖 PySide6）。
    · 线程是 daemon：超时后即使底层仍在下载，也不阻止程序退出；下次再选同一
      文件时水合多半已完成，会立刻成功。

    返回路径可能与入参不同（缓存副本），但**文件名与扩展名保持不变**，
    依赖后缀判断格式的库（openpyxl / docx / pdfplumber / PIL）无需改动。
    """
    p = Path(path)
    raw = os.fspath(path)                # 原样保留入参字符串（斜杠/大小写不变）
    if not is_placeholder(p):
        return raw
    try:
        st = p.stat()
        cached = _cache_path(p, st)
        if cached.is_file() and cached.stat().st_size == st.st_size:
            return str(cached)
    except Exception:
        cached = CACHE_ROOT / "misc" / p.name

    box: list = []
    th = threading.Thread(target=_materialize,
                          args=(raw, cached, box), daemon=True)
    th.start()
    deadline = time.monotonic() + max(0.1, float(timeout))
    while th.is_alive() and time.monotonic() < deadline:
        th.join(0.05)
        if tick:
            try:
                tick()
            except Exception:
                pass
    if not box:
        raise ValidationError(
            f"同步云端文件超时（>{timeout:g} 秒）：{p.name}\n{_CLOUD_HINT}")
    if box[0] != "ok":
        raise ValidationError(
            f"无法读取云端文件：{p.name}\n{box[0]}\n{_CLOUD_HINT}")
    return str(cached)


def ensure_all(paths, timeout: float = DEFAULT_TIMEOUT) -> list:
    """批量版本，返回本地可读路径列表；任一个失败即抛（由调用方统一提示）"""
    return [ensure_readable(p, timeout) for p in paths]


def onedrive_dirs() -> list:
    """
    本机云盘根目录（供文件对话框侧边栏用）。
    取环境变量 OneDrive / OneDriveCommercial / OneDriveConsumer 与
    %USERPROFILE%\\OneDrive，去重后只返回真实存在的目录。
    """
    candidates = []
    for key in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        val = os.environ.get(key, "").strip()
        if val:
            candidates.append(val)
    home = os.environ.get("USERPROFILE", "").strip()
    if home:
        candidates.append(os.path.join(home, "OneDrive"))
    out, seen = [], set()
    for c in candidates:
        try:
            q = str(Path(c).resolve())
        except Exception:
            continue
        if q.lower() in seen or not Path(q).is_dir():
            continue
        seen.add(q.lower())
        out.append(q)
    return out


def cloud_hint() -> str:
    """给 UI 用的统一提示文案"""
    return _CLOUD_HINT

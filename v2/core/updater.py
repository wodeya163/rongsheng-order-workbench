#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动更新（v3.1.0 · 方案 B：清单 + 全量 zip + 目录交换）

形态约束：程序是 PyInstaller onedir 绿色目录（订单管理工作台/订单管理工作台.exe），
data/（数据库 / 附件 / 日志）在程序目录内。Windows 不允许覆盖运行中的 exe，
所以更新采用【目录交换】，并把冻结 exe 自身当作更新器。

v3.4.3 更名兼容：
  · 新包顶层目录与正式主 exe 都叫「订单管理工作台」，同时保留一份
    ProductSuite_v2.2.exe 兼容别名（供 v3.4.2 及更早版本的更新器定位）；
  · prepare_update() 用 find_main_exe() 智能发现，marker 里写 launch_exe；
  · apply_update_from_marker() 优先启动 launch_exe（正式中文名），
    所以从旧版升级完成后自动拉起的是新名 exe，旧桌面快捷方式也不失效。

v3.4.4 更新链路修复（两个真实故障）：
  · 故障 A「更新包里找不到主程序」：<base>/_update_staging/ 里残留了历次
    失败/回滚留下的旧程序目录，旧版 unpack_zip() 只在"恰好一个子目录"时
    才返回该目录，见到两个目录就返回暂存根，于是找不到 exe。
    → prepare_update() 解压前清空暂存区；unpack_zip() 改为按"含主 exe"
      智能挑选（按修改时间取最新），不再依赖目录数量。
  · 故障 B「WinError 5 拒绝访问 _internal/PySide6/MSVCP140_1.dll」：
    更新器进程自己就运行在 staging 目录内，其加载的 DLL 让 Windows 拒绝
    重命名/移动该目录。→ 第⑥步由 shutil.move 改为 shutil.copytree，
    复制到新建的 target（读自身占用的文件是允许的），staging 留待下次清理。

  主程序（旧版）
    ① 后台拉 manifest.json（file/UNC/HTTP 均可）→ 比对版本 → 弹窗
    ② 下载 zip（进度回调）→ SHA256 校验 → 清空并解压到 <base>/_update_staging/
    ③ 写 marker（记录 staging / target / base）→ 启动 <staging>/xxx.exe --apply-update
       → 主程序退出
  更新器进程（新版冻结 exe，从 staging 运行，不占目标目录）
    ④ 等旧进程退出（data 目录可移动即为信号，SQLite WAL 锁随进程释放）
    ⑤ target/data → <base>/_update_data_keep（同卷移动，瞬时）
    ⑥ target → <base>/_old_<时间戳>（备份）；staging 内容【复制】到 target
    ⑦ _update_data_keep → target/data；启动新 exe；清理旧备份与下载残留
    任何一步失败 → 尽力回滚（旧目录原位、data 归位），旧程序照常可用

发布侧：tools/release.py 一条命令出 zip + SHA256 + manifest.json。
manifest.json 字段：version / zip / sha256 / note / date
（zip 为文件路径或 URL；相对路径按 manifest 所在目录解析）。

v3.5.4 GitHub Releases 通道：
  更新源可填 github:owner/repo[@tag]，读 Release 的 latest/download/manifest.json
  资产直链（公开仓匿名可读、零 API 配额、程序内不内置 token）；清单里的 zip 由
  tools/publish_github.py 写成该 Release 的资产绝对 URL。旧版本地发布目录源
  自动迁移到 GitHub 并留作 fallback，check() 在主源失败时自动回退。
  下载带纯 ASCII User-Agent + 60 秒读超时；本机 / UNC / HTTP 源全部保持兼容。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .errors import ValidationError
from .. import EXE_NAME, LEGACY_EXE_NAME, __version__   # v3.4.3 程序集名称单一数据源

DATA_DIR_NAME = "data"
STAGING_NAME = "_update_staging"
DOWNLOADS_NAME = "_update_downloads"
DATA_KEEP_NAME = "_update_data_keep"
OLD_PREFIX = "_old_"
CONFIG_NAME = "updater.json"
HTTP_TIMEOUT = 10            # 清单拉取超时（秒）
DOWNLOAD_TIMEOUT = 60        # v3.5.4：Release 大包下载的单次读超时（秒）
SWAP_TIMEOUT = 60            # 等旧进程退出的上限（秒）
CHUNK = 1 << 20              # 下载分块 1 MB
MAIN_EXE_NAME = EXE_NAME     # 订单管理工作台.exe（正式主程序）

# ── v3.5.4 GitHub Releases 更新源 ────────────────────────────
# 形态：github:owner/repo            → 最新 Release 的资产直链
#       github:owner/repo@v3.5.4     → 固定 tag（回滚 / 灰度用）
# 直链 https://github.com/<owner>/<repo>/releases/latest/download/<asset>
# 是 GitHub 官方的「最新发布资产」入口，公开仓库匿名可读、不消耗 API 配额。
GITHUB_SOURCE_PREFIX = "github:"
DEFAULT_GITHUB_REPO = "wodeya163/rongsheng-order-workbench"
DEFAULT_GITHUB_SOURCE = GITHUB_SOURCE_PREFIX + DEFAULT_GITHUB_REPO
GITHUB_MANIFEST_ASSET = "manifest.json"
LEGACY_SOURCE_MARKERS = ("ProductSuite_发布",)   # 旧版本地发布目录，升级后自动迁移
# HTTP 头必须是纯 ASCII（urllib 用 latin-1 编码 header，中文会抛 UnicodeEncodeError）
GITHUB_USER_AGENT = (f"RongshengOrderWorkbench/{__version__} "
                     f"(+https://github.com/{DEFAULT_GITHUB_REPO})")

Progress = Callable[[str], None]          # 文本进度（阶段提示）
BytesProgress = Callable[[int, int], None]  # (已传字节, 总字节)


# ── 更新源配置 ──────────────────────────────────────────────

def config_path(data_dir: Path) -> Path:
    return Path(data_dir) / CONFIG_NAME


def _read_config(data_dir: Path) -> Dict[str, Any]:
    """容错读整个 updater.json（v3.5.4 起多一个 fallback 字段）"""
    try:
        data = json.loads(config_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def load_source(data_dir: Path) -> str:
    """读更新源（manifest 的路径 / URL / github:owner/repo）；未配置返回空串"""
    return str(_read_config(data_dir).get("manifest") or "").strip()


def load_fallback(data_dir: Path) -> str:
    """v3.5.4：主源（GitHub）不可用时的备用清单，通常是旧版本地发布目录"""
    return str(_read_config(data_dir).get("fallback") or "").strip()


def save_source(data_dir: Path, source: str, fallback: Optional[str] = None) -> None:
    """
    写更新源。fallback=None 时保留已有备用源；显式传空串可清掉。
    兼容旧版只有 {"manifest": ...} 的配置文件。
    """
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _read_config(data_dir)
    cfg["manifest"] = str(source or "").strip()
    if fallback is not None:
        fallback = str(fallback or "").strip()
        if fallback:
            cfg["fallback"] = fallback
        else:
            cfg.pop("fallback", None)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def migrate_source(data_dir: Path) -> str:
    """
    v3.5.4：旧版本地发布目录的源自动切到 GitHub（只发生一次）。

    只对「指向旧发布目录 manifest.json 的本地路径」生效；用户自定义的
    共享目录 / HTTP 源 / 已经配好的 github: 源一律不动。旧源写入 fallback，
    断网或 GitHub 不可达时 check() 会自动回退，不会把用户卡死。
    """
    src = load_source(data_dir)
    if not src or src.lower().startswith(GITHUB_SOURCE_PREFIX) or _http_url(src):
        return src
    if any(marker in src for marker in LEGACY_SOURCE_MARKERS):
        save_source(data_dir, DEFAULT_GITHUB_SOURCE, fallback=src)
        return DEFAULT_GITHUB_SOURCE
    return src


# ── 清单 ────────────────────────────────────────────────────

def _http_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


_GITHUB_SOURCE_RE = re.compile(
    r"^github:\s*([A-Za-z0-9_.-]+)\s*/\s*([A-Za-z0-9_.-]+)"
    r"\s*(?:@\s*([^\s/]+))?\s*$", re.IGNORECASE)


def parse_github_source(source: str) -> Optional[Dict[str, str]]:
    """'github:owner/repo[@tag]' → {owner, repo, tag}；不是该形态返回 None"""
    m = _GITHUB_SOURCE_RE.match(str(source or "").strip())
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2),
            "tag": (m.group(3) or "").strip()}


def github_asset_url(owner: str, repo: str, asset: str, tag: str = "") -> str:
    """
    生成 Release 资产直链。asset 只允许纯文件名（拒绝路径穿越），
    中文资产名做 percent 编码；tag 为空时走 latest/download。
    """
    name = str(asset or "").strip()
    if (not name or "/" in name or "\\" in name or name in (".", "..")
            or "\x00" in name):
        raise ValidationError(f"GitHub 资产名不合法：{name!r}")
    enc = quote(name, safe="")
    base = f"https://github.com/{owner}/{repo}/releases/"
    if tag:
        return base + f"download/{quote(tag, safe='')}/{enc}"
    return base + f"latest/download/{enc}"


def _http_get(url: str, timeout: int = HTTP_TIMEOUT):
    """带 User-Agent 的 GET。GitHub 对无 UA 请求直接 403，header 必须 ASCII"""
    req = Request(url, headers={"User-Agent": GITHUB_USER_AGENT,
                                "Accept": "*/*"})
    return urlopen(req, timeout=timeout)


def fetch_manifest(source: str) -> Dict[str, Any]:
    """
    拉取并校验 manifest.json。source 可以是：
      · github:owner/repo（v3.5.4，走 GitHub Releases latest 资产直链）
      · github:owner/repo@v3.5.4（固定 tag）
      · https://…/manifest.json（urllib，10 秒超时）
      · 本机 / UNC 路径（如 \\\\server\\share\\manifest.json）
    返回规范化的 {version, zip, sha256, note, date}；
    zip 相对路径按 manifest 所在目录（HTTP 源按 URL、GitHub 源按资产名）解析。
    """
    source = str(source or "").strip()
    if not source:
        raise ValidationError("尚未配置更新源，请先在「帮助 → 设置更新源…」里填写")
    gh = parse_github_source(source)
    base_dir: Optional[Path] = None
    if gh is not None:
        url = github_asset_url(gh["owner"], gh["repo"],
                               GITHUB_MANIFEST_ASSET, gh["tag"])
        try:
            with _http_get(url) as resp:
                raw = resp.read()
        except Exception as exc:                            # noqa: BLE001
            raise ValidationError(
                f"无法从 GitHub 发布仓库读取更新清单：{exc}\n"
                f"请确认 {gh['owner']}/{gh['repo']} 已公开并发布过含 "
                f"manifest.json 的 Release；离线时会自动回退本地备用源。") from exc
    elif _http_url(source):
        with _http_get(source) as resp:
            raw = resp.read()
    else:
        path = Path(source)
        if not path.is_file():
            raise ValidationError(f"更新源清单不存在：{source}")
        # v3.1.1：清单也可能放在 OneDrive「仅联机」，先水合到本地副本
        from . import cloudfile
        local = Path(cloudfile.ensure_readable(str(path)))
        raw = local.read_bytes()
        base_dir = path.resolve().parent   # zip 相对路径仍按原清单所在目录解析
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValidationError(f"清单不是合法 JSON：{exc}") from exc
    for key in ("version", "zip", "sha256"):
        if not str(data.get(key) or "").strip():
            raise ValidationError(f"清单缺少必填字段：{key}")
    zip_ref = str(data["zip"]).strip()
    if not _http_url(zip_ref):
        if gh is not None:
            # 发布脚本写的是绝对资产 URL；若人工写成了相对名，按资产名补全
            zip_ref = github_asset_url(gh["owner"], gh["repo"],
                                       zip_ref, gh["tag"])
        elif base_dir is not None and not Path(zip_ref).is_absolute():
            zip_ref = str(base_dir / zip_ref)
        elif _http_url(source):
            zip_ref = urljoin(source, zip_ref)
    elif gh is not None:
        # 清单可被第三方篡改，下载地址限定在 GitHub 域内；内容另有 SHA256 兜底
        host = (urlparse(zip_ref).hostname or "").lower()
        if host and not (host == "github.com" or host.endswith(".github.com")
                         or host.endswith("githubusercontent.com")):
            raise ValidationError(f"GitHub 清单里的更新包地址不在 GitHub 域名下：{host}")
    return {
        "version": str(data["version"]).strip(),
        "zip": zip_ref,
        "sha256": str(data["sha256"]).strip().lower(),
        "note": str(data.get("note") or "").strip(),
        "date": str(data.get("date") or "").strip(),
    }


def parse_version(text: str) -> Tuple[int, ...]:
    """'3.0.1' / 'v2.9.4-beta' → (3,0,1) / (2,9,4)；无数字段跳过，保证可比较"""
    parts: List[int] = []
    for seg in re.split(r"[.\-_+ ]+", str(text or "").strip().lstrip("vV")):
        digits = re.sub(r"\D", "", seg)
        if digits:
            parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def compare_versions(local: str, remote: str) -> int:
    """远端新于本地 → 正数；相同 → 0；远端旧 → 负数"""
    return -1 if parse_version(local) < parse_version(remote) else (
        1 if parse_version(local) > parse_version(remote) else 0)


def check(source: str, local_version: str,
          fallback: str = "") -> Dict[str, Any]:
    """
    check() 的总入口（UI 直接调）：永不抛异常。
    v3.5.4：主源失败时自动尝试备用清单（离线 / GitHub 不可达场景），
    备用源由 updater.json 的 fallback 字段提供，通常是旧版本地发布目录。
    返回 {"status": "update"|"latest"|"error", ...}
    """
    try:
        manifest = fetch_manifest(source)
    except Exception as exc:                       # noqa: BLE001
        alt = str(fallback or "").strip()
        if alt and alt != source:
            try:
                manifest = fetch_manifest(alt)
            except Exception:                      # noqa: BLE001
                return {"status": "error", "message": str(exc)}
        else:
            return {"status": "error", "message": str(exc)}
    has = compare_versions(local_version, manifest["version"]) < 0
    return {"status": "update" if has else "latest", "manifest": manifest}


# ── 下载 / 校验 / 解压 ──────────────────────────────────────

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def download_zip(manifest: Dict[str, Any], dest_dir: Path,
                 progress: Optional[BytesProgress] = None) -> Path:
    """
    下载（或复制）更新包到 dest_dir 并做 SHA256 校验。
    校验不过 → 删除残留并抛 ValidationError，绝不进入解压环节。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"update_{manifest['version']}.zip"

    zip_ref = str(manifest["zip"])
    if _http_url(zip_ref):
        # v3.5.4：GitHub Release 资产会 302 到 CDN；带 UA + 60 秒读超时，
        # 中途断线删掉半截包并给出可读提示（避免只报「SHA256 不匹配」）。
        try:
            with _http_get(zip_ref, DOWNLOAD_TIMEOUT) as resp, \
                    open(target, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total or done)
        except Exception as exc:                        # noqa: BLE001
            try:
                target.unlink()
            except OSError:
                pass
            raise ValidationError(
                f"更新包下载中断：{exc}\n已删除不完整文件，请检查网络后重试。") from exc
    else:
        from . import cloudfile
        src = Path(cloudfile.ensure_readable(zip_ref))   # v3.1.1 云盘更新包
        if not src.is_file():
            raise ValidationError(f"更新包不存在：{zip_ref}")
        if progress:
            total = src.stat().st_size
            done = 0
            with open(src, "rb") as fin, open(target, "wb") as fout:
                while True:
                    chunk = fin.read(CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    done += len(chunk)
                    progress(done, total)
        else:
            shutil.copyfile(src, target)

    actual = sha256_of(target)
    if actual != str(manifest["sha256"]).lower():
        try:
            target.unlink()
        except OSError:
            pass
        raise ValidationError(
            f"更新包校验失败（SHA256 不匹配）：\n期望 {manifest['sha256']}\n"
            f"实际 {actual}\n已删除损坏的下载文件，请重试或联系发布人。")
    return target


def unpack_zip(zip_path: Path, staging_dir: Path) -> Path:
    """
    解压到 staging_dir，返回解压出的【程序目录】。
    zip 顶层应是一个程序文件夹（如 订单管理工作台/…）；
    兼容直接把文件解在顶层的情况（此时 staging_dir 本身即程序目录）。

    v3.4.4：暂存区可能残留历次失败/回滚留下的旧程序目录，不能再靠
    "恰好一个子目录"判定。优先挑【含主 exe】且最新修改的目录；
    旧版 3.4.2 也依赖本函数返回值去找 ProductSuite_v2.2.exe，
    所以这里必须稳定地指向本次解压出的新目录。
    """
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging_dir)
    entries = [e for e in staging_dir.iterdir()]
    dirs = [e for e in entries if e.is_dir()]
    # 含主 exe 的目录优先；按修改时间倒序，本次新解压的目录排最前
    for d in sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True):
        if find_main_exe(d) is not None:
            return d
    if len(dirs) == 1 and len(entries) == 1:
        return dirs[0]
    return staging_dir


# ── 目录交换（更新器进程执行） ───────────────────────────────

def find_main_exe(program_dir: Path, preferred: str = "") -> Optional[Path]:
    """
    v3.4.3：程序集更名后的主程序发现。

    顺序：preferred（通常是当前运行中的 exe 名）→ 正式中文名 → 历史兼容别名
    → 目录里任意 .exe。旧版 3.4.2 升级时 preferred 是 ProductSuite_v2.2.exe，
    新包内保留了兼容别名，所以更新器进程仍能被定位；目录交换完成后，
    marker.launch_exe（或本函数的正式名优先）保证拉起中文名主程序。
    """
    program_dir = Path(program_dir)
    for name in (preferred, MAIN_EXE_NAME, LEGACY_EXE_NAME):
        if not name:
            continue
        candidate = program_dir / name
        if candidate.is_file():
            return candidate
    exes = sorted(program_dir.glob("*.exe"))
    return exes[0] if exes else None


def _swap_log(marker: Dict[str, Any], message: str) -> None:
    try:
        log = marker.get("log")
        if log:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except Exception:
        pass


def _move(src: Path, dst: Path) -> None:
    shutil.move(str(src), str(dst))


def apply_update_from_marker(marker: Dict[str, Any],
                             launch: Optional[bool] = None) -> Dict[str, Any]:
    """
    更新器主流程（由 --apply-update 分支调用；测试可直接传 marker）。
    marker = {target_dir, staging_dir, exe_name, base_dir, log, launch?}
    launch=None 时：冻结环境启动新 exe，源码/测试环境只换目录不启动。
    """
    target = Path(marker["target_dir"]).resolve()
    staging = Path(marker["staging_dir"]).resolve()
    base = Path(marker.get("base_dir") or target.parent).resolve()
    if launch is None:
        launch = bool(getattr(sys, "frozen", False))

    result: Dict[str, Any] = {"ok": False, "launched": False, "message": ""}
    data_keep = base / DATA_KEEP_NAME
    if data_keep.exists():                     # 上次中断的残留，先清掉
        shutil.rmtree(data_keep, ignore_errors=True)
    backup: Optional[Path] = None
    data_moved = False

    def rollback(reason: str) -> Dict[str, Any]:
        _swap_log(marker, f"失败回滚：{reason}")
        try:
            if backup is not None and backup.exists() and not target.exists():
                _move(backup, target)
                _swap_log(marker, "已还原旧程序目录")
            if data_moved and data_keep.exists() and not (target / DATA_DIR_NAME).exists():
                _move(data_keep, target / DATA_DIR_NAME)
                _swap_log(marker, "已还原 data 目录")
            result["message"] = f"更新失败已回滚：{reason}"
        except Exception as exc:                   # noqa: BLE001
            result["message"] = (f"更新失败且回滚不完整（{reason}）：{exc}；"
                                 f"data 备份在 {data_keep}")
        _swap_log(marker, result["message"])
        return result

    try:
        # ① 等旧进程退出：data 可整体移动 = SQLite/WAL 锁已释放
        deadline = time.time() + SWAP_TIMEOUT
        if (target / DATA_DIR_NAME).exists():
            while True:
                try:
                    _move(target / DATA_DIR_NAME, data_keep)
                    data_moved = True
                    break
                except Exception:                  # noqa: BLE001 旧进程还没退干净
                    if time.time() > deadline:
                        return rollback("等待旧程序退出超时（data 被占用）")
                    time.sleep(0.5)
        _swap_log(marker, "data 已移出目标目录")

        # ② 旧程序目录 → 备份
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = base / f"{OLD_PREFIX}{ts}"
        while True:
            try:
                _move(target, backup)
                break
            except Exception:                      # noqa: BLE001 exe 句柄尚未释放
                if time.time() > deadline:
                    return rollback("旧程序目录重命名超时")
                time.sleep(0.5)
        _swap_log(marker, f"旧目录已备份为 {backup.name}")

        # ③ staging 内容 → target
        # v3.4.4：本更新器进程就运行在 staging 内，其 _internal/*.dll 被进程
        # 占用，Windows 拒绝重命名/移动整个目录（历史 WinError 5）。改为
        # 复制到新建的 target：读自身占用的文件是允许的；此刻旧 target 已
        # 备份移走，copytree 的目标不存在，可以直接创建。
        try:
            shutil.copytree(staging, target)
        except Exception as exc:                   # noqa: BLE001
            # 先清掉可能已复制了一半的 target，否则 _move(backup, target)
            # 会把旧目录塞进 target 内部而不是还原成 target
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            _move(backup, target)
            backup = None
            return rollback(f"新目录就位失败：{exc}")
        _swap_log(marker, "新程序目录已就位（复制方式，规避 DLL 占用锁）")

        # ④ data 归位
        if data_moved:
            _move(data_keep, target / DATA_DIR_NAME)
            data_moved = False
        _swap_log(marker, "data 已归位")

        # ⑤ 清理：更早的历史备份 + 下载残留（本次 backup 留作回滚保险，
        #    下次更新时由 cleanup_old_backups 回收）
        cleanup_old_backups(base, keep=backup.name)
        cleanup_old_downloads(base)
        # staging 里还运行着本更新器进程，不能自删；由下次 prepare_update 清空
        _swap_log(marker, f"暂存目录 {staging} 保留至下次更新前清理")
        result.update(ok=True, message="更新完成")

        # ⑥ 启动新版（从最终位置）：优先正式中文名主 exe，回退兼容别名
        if launch:
            exe = find_main_exe(target, str(marker.get("launch_exe") or ""))
            if exe is not None:
                subprocess.Popen([str(exe)], cwd=str(target),
                                 close_fds=True)
                result["launched"] = True
                _swap_log(marker, f"已启动新版本：{exe.name}")
            else:
                _swap_log(marker, "未找到可启动的主程序 exe")
        _swap_log(marker, "更新器退出（成功）")
        return result
    except Exception as exc:                       # noqa: BLE001
        return rollback(str(exc))


def cleanup_old_backups(base: Path, keep: str = "") -> int:
    """删除历史 _old_* 备份（保留 keep），返回删除数；尽力而为不抛异常"""
    n = 0
    try:
        for p in Path(base).glob(f"{OLD_PREFIX}*"):
            if p.name == keep or not p.is_dir():
                continue
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                n += 1
    except Exception:                              # noqa: BLE001
        pass
    return n


def cleanup_old_downloads(base: Path, keep: int = 2) -> int:
    """v3.4.4：保留最近 keep 个更新包 zip，删掉更早的；尽力而为不抛异常"""
    n = 0
    try:
        folder = Path(base) / DOWNLOADS_NAME
        zips = sorted((p for p in folder.glob("update_*.zip") if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for p in zips[keep:]:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    except Exception:                              # noqa: BLE001
        pass
    return n


def run_apply_update(marker_path: str) -> int:
    """app_v2 --apply-update 分支：读 marker → 执行交换 → 返回退出码"""
    try:
        marker = json.loads(Path(marker_path).read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001
        print(f"更新标记读取失败：{exc}", file=sys.stderr)
        return 3
    _swap_log(marker, f"更新器启动（frozen={bool(getattr(sys, 'frozen', False))}）")
    # marker 显式带 launch 字段时以其为准（E2E 测试用），否则冻结环境启动新版
    launch = marker["launch"] if "launch" in marker else None
    result = apply_update_from_marker(marker, launch=launch)
    print(("OK: " if result["ok"] else "FAIL: ") + result["message"])
    return 0 if result["ok"] else 4


# ── 主程序侧编排（UI 调用） ─────────────────────────────────

def app_dir() -> Path:
    """当前运行中的程序目录（冻结 = exe 所在目录；源码 = v2 上一级）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def prepare_update(manifest: Dict[str, Any],
                   progress: Optional[Progress] = None,
                   bytes_progress: Optional[BytesProgress] = None) -> Dict[str, Any]:
    """
    下载 → 校验 → 解压 → 写 marker。
    返回 {"staging_exe": 新版 exe 路径, "marker": marker 路径, "dir": 程序目录}。
    只做准备工作，不退出主程序；调用方随后 spawn_apply_update() 并退出。
    """
    if progress:
        progress("下载更新包…")
    base = app_dir().parent
    zip_path = download_zip(manifest, base / DOWNLOADS_NAME, bytes_progress)
    if progress:
        progress("校验通过，清理更新暂存区…")
    staging_root = base / STAGING_NAME
    # v3.4.4：历次失败/回滚会在暂存区留下旧程序目录，旧版 unpack_zip 见到
    # 多个子目录就会返回暂存根，导致「找不到主程序」。每次解压前先清空。
    # 主程序此时运行在 target 而非 staging，删除是安全的；删不掉也不致命
    # （unpack_zip 的智能挑选会兜底）。
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)
    if progress:
        progress("解压新版本…")
    program_dir = unpack_zip(zip_path, staging_root)
    running_name = (Path(sys.executable).name if getattr(sys, "frozen", False)
                    else MAIN_EXE_NAME)
    staging_exe = find_main_exe(program_dir, running_name)
    if staging_exe is None:
        raise ValidationError(f"更新包里找不到主程序：{running_name}")
    # 目录交换后要拉起正式中文名主 exe（旧版升级时 preferred 是兼容别名）
    launch_exe = find_main_exe(program_dir, MAIN_EXE_NAME)
    marker = {
        "target_dir": str(app_dir()),
        "staging_dir": str(program_dir),
        "exe_name": staging_exe.name,
        "launch_exe": launch_exe.name if launch_exe else staging_exe.name,
        "base_dir": str(base),
        "log": str(base / "_update_apply.log"),
        "version": manifest["version"],
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
    }
    marker_path = base / "_update_marker.json"
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    if progress:
        progress("准备完成")
    return {"staging_exe": staging_exe, "marker": marker_path,
            "dir": program_dir}


def spawn_apply_update(staging_exe: Path, marker_path: Path) -> None:
    """启动更新器进程（新版 exe --apply-update），主程序随后自行退出"""
    subprocess.Popen([str(staging_exe), "--apply-update", str(marker_path)],
                     cwd=str(staging_exe.parent), close_fds=True)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本地发布产物发成 GitHub Release（v3.5.4 · GitHub 自动更新的发布侧）

    python tools/release.py --skip-build --note "本次改了什么"   # 先出 zip + manifest.json
    python tools/publish_github.py                              # 再一键发 GitHub Release

做四件事：
  1. 读 dist/release/manifest.json，按 manifest.sha256 实算校验 zip（不一致直接拒绝）；
  2. 生成 GitHub 专用清单：zip 字段改写成该 Release 的资产绝对直链，
     客户端 github:owner/repo 拉到清单后可直接下载，无需调用 GitHub API、无需 token；
  3. 创建 / 复用 tag=v<版本> 的 Release，上传「安装包 zip」+「manifest.json」两个资产
     （同名旧资产先删，保证重复发布不会留脏包）；
  4. 回读 Release 资产清单与资产直链，逐项核对大小、内容与 SHA256。

凭据：从本机 `git credential fill` 读 github.com 凭据，脚本绝不打印 token，
也不把 token 写进任何文件；可用环境变量 GITHUB_TOKEN 覆盖（CI 场景）。

网络：默认【直连】GitHub；必须走代理时设 GITHUB_USE_PROXY=1。
本机上行到 uploads.github.com 实测仅约 48 KB/s（95MB 需半小时以上且长连接易断），
因此日常发版推荐用 .github/workflows/release.yml 在 GitHub Actions 里
「云端打包 → 内网上传」，本脚本在 runner 上由 workflow 调用；本机手动发布
仅作离线兜底。

资产名：GitHub 会重命名含非字母数字字符的文件名（实测中文
「订单管理工作台_v3.5.4.zip」被吞成「_v3.5.4.zip」，直链随即 404），
因此 Release 资产统一用 ASCII 名 RongshengOrderWorkbench_v<版本>.zip；
本地发布目录仍用中文原名，两条通道互不干扰。

本地发布目录（D:/Programs/ProductSuite_发布/）仍用相对 zip 名的原版 manifest，
两条发布通道互不干扰；离线时客户端会自动回退到该本地清单。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from v2 import APP_TITLE, __version__                     # noqa: E402
from v2.core import updater                              # noqa: E402

API_BASE = "https://api.github.com"
UPLOAD_BASE = "https://uploads.github.com"
DEFAULT_REPO = "wodeya163/rongsheng-order-workbench"
UA = f"RongshengOrderWorkbench-Publisher/{__version__}"
MANIFEST_ASSET = "manifest.json"

# 直连优先：本机沙箱代理对 95MB POST 会重置连接，直连实测正常。
# GITHUB_USE_PROXY=1 可切回系统代理（企业内网必须走代理时用）。
USE_PROXY = os.environ.get("GITHUB_USE_PROXY", "0") == "1"
_DIRECT = build_opener(ProxyHandler({}))


def _open(req, timeout: int):
    """直连（默认）或系统代理（GITHUB_USE_PROXY=1）"""
    return (urlopen(req, timeout=timeout) if USE_PROXY
            else _DIRECT.open(req, timeout=timeout))


# ── 凭据 ────────────────────────────────────────────────────

def github_token() -> str:
    """优先环境变量；否则从本机 git 凭据管理器读（绝不回显）"""
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        return tok
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True, timeout=30, env=env)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            tok = line.split("=", 1)[1].strip()
            if tok:
                return tok
    raise RuntimeError(
        "读不到 github.com 凭据：请先在 Git 凭据管理器里登录 GitHub，"
        "或设置环境变量 GITHUB_TOKEN（需 repo 权限）")


# ── GitHub REST ─────────────────────────────────────────────

def api(method: str, path: str, token: str,
        payload: Optional[Dict[str, Any]] = None,
        base: str = API_BASE, timeout: int = 60) -> Any:
    data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(base + path, data=data, method=method, headers=headers)
    with _open(req, timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def upload_asset(token: str, owner: str, repo: str, release_id: int,
                 data: bytes, name: str, content_type: str,
                 timeout: int = 600) -> Dict[str, Any]:
    """上传单个 Release 资产；data 走内存，token 只出现在 HTTP 头里"""
    path = (f"/repos/{owner}/{repo}/releases/{release_id}/assets"
            f"?name={quote(name, safe='')}")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
    }
    req = Request(UPLOAD_BASE + path, data=data, method="POST", headers=headers)
    with _open(req, timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def github_zip_name(version: str) -> str:
    """Release 资产名必须 ASCII；中文会被 GitHub 改名导致直链 404"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "", str(version))
    return f"RongshengOrderWorkbench_v{safe}.zip"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 主流程 ──────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="发布 GitHub Release（自动更新）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    ap.add_argument("--tag", default="", help="默认 v<manifest.version>")
    ap.add_argument("--manifest", default=str(ROOT / "dist" / "release" / "manifest.json"))
    ap.add_argument("--zip", default="", help="默认取 manifest.zip（相对 manifest 目录）")
    ap.add_argument("--note", default="", help="覆盖 Release 说明")
    ap.add_argument("--dry-run", action="store_true", help="只校验与打印计划，不上传")
    ap.add_argument("--prerelease", action="store_true")
    args = ap.parse_args()

    owner, _, repo = args.repo.partition("/")
    if not owner or not repo:
        print("--repo 必须是 owner/repo 形式", file=sys.stderr)
        return 2

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"找不到发布清单：{manifest_path}\n"
              f"请先执行 python tools/release.py --skip-build --note \"…\"",
              file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "").strip()
    tag = args.tag or f"v{version}"
    zip_ref = str(manifest.get("zip") or "").strip()
    zip_path = Path(args.zip) if args.zip else manifest_path.parent / zip_ref
    if not zip_path.is_file():
        print(f"找不到更新包：{zip_path}", file=sys.stderr)
        return 2

    # ① 实算校验，传错包直接拒绝
    digest = sha256_of(zip_path)
    want = str(manifest.get("sha256") or "").strip().lower()
    if digest != want:
        print(f"SHA256 不一致，拒绝发布：\n  清单 {want}\n  实算 {digest}",
              file=sys.stderr)
        return 3
    zip_name = zip_path.name
    asset_name = github_zip_name(version)      # ASCII，避免 GitHub 改名导致直链 404

    # ② GitHub 专用清单：zip 改成该 Release 的资产绝对直链
    gh_manifest = dict(manifest)
    gh_manifest["zip"] = updater.github_asset_url(owner, repo, asset_name, tag)
    gh_bytes = json.dumps(gh_manifest, ensure_ascii=False, indent=2).encode("utf-8")
    note = args.note or str(manifest.get("note") or "").strip() \
        or f"{APP_TITLE} v{version}"

    plan = {
        "repo": f"{owner}/{repo}", "tag": tag, "version": version,
        "zip_asset": asset_name, "local_zip": zip_name,
        "zip_bytes": zip_path.stat().st_size,
        "sha256": digest, "manifest_asset": MANIFEST_ASSET,
        "manifest_zip_url": gh_manifest["zip"],
        "note_chars": len(note),
    }
    print("=== 发布计划 ===")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("--dry-run：未创建 Release、未上传任何文件")
        return 0

    token = github_token()
    print(f"凭据已读取（长度 {len(token)}，不回显）")

    # ③ 复用 / 创建 Release
    rel: Optional[Dict[str, Any]] = None
    try:
        rel = api("GET", f"/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}",
                  token)
    except Exception as exc:                              # noqa: BLE001
        if "404" not in str(exc):
            raise
    if rel is None or not rel.get("id"):
        rel = api("POST", f"/repos/{owner}/{repo}/releases", token, {
            "tag_name": tag,
            "name": f"{APP_TITLE} {tag}",
            "body": note,
            "draft": False,
            "prerelease": bool(args.prerelease),
            "make_latest": "true",
        })
        print(f"已创建 Release {tag} (id={rel['id']})")
    else:
        print(f"复用已有 Release {tag} (id={rel['id']})")
        # 全量清空旧资产：包括上传中断留下的 starter 空壳、旧版中文名资产
        # 与探测文件，避免同名冲突 422 或直链指向半截包。
        for a in rel.get("assets", []):
            api("DELETE", f"/repos/{owner}/{repo}/releases/assets/{a['id']}",
                token)
            print(f"  清理旧资产：{a['name']}（state={a.get('state')}）")

    release_id = int(rel["id"])
    print(f"上传安装包 {asset_name}（源 {zip_name}，"
          f"{zip_path.stat().st_size / 1e6:.1f} MB）…")
    upload_asset(token, owner, repo, release_id, zip_path.read_bytes(),
                 asset_name, "application/zip")
    print(f"上传清单 {MANIFEST_ASSET} …")
    upload_asset(token, owner, repo, release_id, gh_bytes,
                 MANIFEST_ASSET, "application/json")

    # ④ 回读核对：资产大小 + 固定 tag 直链内容
    rel = api("GET", f"/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}",
              token)
    assets = {a["name"]: a for a in rel.get("assets", [])}
    ok = True
    for name, size in ((asset_name, zip_path.stat().st_size),
                       (MANIFEST_ASSET, len(gh_bytes))):
        got = assets.get(name, {}).get("size")
        print(f"  回读 {name}: {got} 字节（期望 {size}）")
        ok = ok and got == size
    # 回读走 api.github.com 资产端点：不依赖 github.com 主站可达，
    # 也不受 latest/download 直链的 CDN 生效延迟影响。
    mid = assets.get(MANIFEST_ASSET, {}).get("id")
    fixed_url = (f"{API_BASE}/repos/{owner}/{repo}/releases/assets/{mid}"
                 if mid else updater.github_asset_url(
                     owner, repo, MANIFEST_ASSET, tag))
    remote: Optional[Dict[str, Any]] = None
    for attempt in range(4):
        try:
            req = Request(fixed_url, headers={
                "User-Agent": UA,
                "Accept": "application/octet-stream",
                "Cache-Control": "no-cache"})
            with _open(req, 60) as resp:
                remote = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:                                  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(3)
    print(f"  回读直链清单: version={remote.get('version')} "
          f"zip={str(remote.get('zip'))[:90]}…")
    ok = ok and remote == gh_manifest
    if not ok:
        print("回读校验未通过，请检查 Release 资产", file=sys.stderr)
        return 4
    print(f"\n=== 发布完成 ===\n{rel.get('html_url')}\n"
          f"客户端更新源：github:{owner}/{repo}\n"
          f"latest 清单直链：{updater.github_asset_url(owner, repo, MANIFEST_ASSET)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

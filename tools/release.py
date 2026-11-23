#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布脚本（v3.1.0 · 方案 B 自动更新的发布侧）

    python tools/release.py --note "修复了什么、新增了什么"
    python tools/release.py --skip-build        # 复用现有 dist/订单管理工作台

一条命令完成：
    1. 调 build_v2.py 打包（--skip-build 跳过）；
    2. 把 dist/订单管理工作台 压成 订单管理工作台_v<版本>.zip
       （自动排除 data/ / logs/ / _update_staging / _update_downloads / _old_*）；
    3. 计算 SHA256，生成 manifest.json（version / zip / sha256 / note / date）；
    4. 打印发布指引：把 zip + manifest.json 放进更新源位置即完成发版。

更新源是任意「用户程序能读到」的位置：本机目录、局域网共享（\\\\server\\share\\）、
HTTP 静态目录、网盘同步盘均可 —— 程序端在「帮助 → 设置更新源…」里填 manifest.json 的位置。
"""
import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from v2 import APP_TITLE, ASSEMBLY_NAME, EXE_NAME       # noqa: E402

NAME = ASSEMBLY_NAME
# 这些不进更新包：数据 / 日志 / 更新过程目录随用户环境走，不属于程序
EXCLUDE_DIRS = {"data", "logs", "__pycache__",
                "_update_staging", "_update_downloads", "_update_data_keep"}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build() -> int:
    return subprocess.run([sys.executable, str(ROOT / "build_v2.py")],
                          cwd=str(ROOT)).returncode


def make_zip(dist_dir: Path, out_dir: Path, version: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{NAME}_v{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(dist_dir.rglob("*")):
            rel = path.relative_to(dist_dir)
            top = rel.parts[0] if rel.parts else ""
            if any(part in EXCLUDE_DIRS or part.startswith("_old_")
                   for part in rel.parts):
                continue
            if path.is_file():
                zf.write(path, str(Path(NAME) / rel))
                n += 1
    print(f"更新包：{zip_path}（{n} 个文件，{zip_path.stat().st_size / 1e6:.0f} MB）")
    return zip_path


def main() -> int:
    from v2 import __version__
    ap = argparse.ArgumentParser(description="发布自动更新包")
    ap.add_argument("--note", default="", help="更新说明（显示给用户）")
    ap.add_argument("--out", default=str(ROOT / "dist" / "release"),
                    help="产物目录（默认 dist/release）")
    ap.add_argument("--skip-build", action="store_true",
                    help="复用现有 dist/订单管理工作台，不再打包")
    args = ap.parse_args()

    dist_dir = ROOT / "dist" / NAME
    exe = dist_dir / EXE_NAME
    if not exe.is_file():
        print(f"未找到 {exe}，先执行打包…", file=sys.stderr)
        args.skip_build = False
    if not args.skip_build:
        code = build()
        if code != 0:
            print("打包失败，终止发布", file=sys.stderr)
            return code

    out_dir = Path(args.out)
    zip_path = make_zip(dist_dir, out_dir, __version__)
    digest = sha256_of(zip_path)
    manifest = {
        "version": __version__,
        "zip": zip_path.name,                 # 相对 manifest 所在目录解析
        "sha256": digest,
        "note": args.note or f"{APP_TITLE} v{__version__}",
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    print("\n=== 发布清单 ===")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"""
=== 发布指引 ===
1. 把这两个文件放到更新源位置（本机目录 / \\\\服务器\\共享\\ / HTTP 静态目录 / 网盘同步盘）：
     {zip_path.name}
     manifest.json
   （两文件必须同目录；manifest 里的 "zip" 是相对路径，改名要同步改）
2. 用户程序：帮助 → 设置更新源… 填 manifest.json 的路径或网址（首次设置后记在 data/updater.json）。
3. 用户程序启动 3 秒后自动检查；也可 帮助 → 检查更新… 手动触发。
4. 历史版本 zip 可留在更新源目录，不被引用；本地 _old_* 备份由更新器自动滚动清理。
5. 【GitHub 通道 v3.5.4】python tools/publish_github.py
   校验 SHA256 后把这两个文件发成 GitHub Release 资产（清单里的 zip 会改写成
   该 Release 的绝对直链）；客户端「设置更新源…」填 github:owner/repo 即可。
   两条通道互不干扰，本地这份 manifest 仍是相对 zip 名。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

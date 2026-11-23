#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键校验（P2-3）

以前每次改完要手工按顺序敲四条命令，漏一条就可能把坏代码打进 exe。
现在一条命令跑完「语法 → 单测 → 旧版回归 → 打包自检 → 文档识别」，
每步给耗时与结果，最后打印汇总表；任何一步失败立即以非 0 退出。

用法：
    py tools/verify.py                # 全量（有 dist 就跑打包自检）
    py tools/verify.py --fast         # 只跑语法 + 单测（改代码时用）
    py tools/verify.py --build        # 全量 + 重新打包（发布前用）
    py tools/verify.py --no-exe       # 全量但不碰 exe
"""
from __future__ import annotations

import argparse
import compileall
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from v2 import APP_TITLE, ASSEMBLY_NAME, EXE_NAME, LEGACY_EXE_NAME  # noqa: E402

V2 = ROOT / "v2"
EXE = ROOT / "dist" / ASSEMBLY_NAME / EXE_NAME
ALIAS_EXE = ROOT / "dist" / ASSEMBLY_NAME / LEGACY_EXE_NAME
SAMPLES = ROOT / "build" / "_parsecheck"
PYTHON = sys.executable


def resolve_python() -> str:
    """
    选一个「装了 PySide6 + pytest」的解释器。
    本机踩过的坑：`py script.py` 时 sys.executable 可能是工具链自带的精简 Python，
    里面没有 pytest/PySide6；直接用它会得到 "No module named pytest"。
    优先级：环境变量 → 当前解释器 → 常见安装路径。
    """
    candidates = [os.environ.get("PRODUCT_SUITE_PYTHON"), sys.executable,
                  r"D:\Program Files\Python313\python.exe",
                  r"C:\Program Files\Python313\python.exe",
                  r"C:\Users\%s\AppData\Local\Programs\Python\Python313\python.exe"
                  % os.environ.get("USERNAME", "")]
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if not Path(cand).is_file():
            continue
        try:
            probe = subprocess.run([cand, "-c", "import pytest, PySide6"],
                                   capture_output=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return cand
    return sys.executable

OK, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Step:
    def __init__(self, name):
        self.name = name
        self.status = SKIP
        self.detail = ""
        self.seconds = 0.0


def run(cmd, timeout=900, env=None, cwd=None):
    """跑子进程；返回 (code, 末 12 行输出)"""
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env)
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-12:])
    if proc.returncode != 0 and proc.stderr:
        tail += "\n" + "\n".join(proc.stderr.strip().splitlines()[-12:])
    return proc.returncode, tail.strip(), time.perf_counter() - started


def step_compile(step: Step):
    started = time.perf_counter()
    ok = compileall.compile_dir(str(V2), quiet=1, force=True, legacy=False)
    step.seconds = time.perf_counter() - started
    step.status = OK if ok else FAIL
    step.detail = f"编译 {len(list(V2.rglob('*.py')))} 个文件"


def step_pytest(step: Step, target: Path, label: str, timeout=900):
    code, tail, secs = run([PYTHON, "-m", "pytest", str(target), "-q"], timeout=timeout)
    step.seconds = secs
    summary = [l for l in tail.splitlines() if "passed" in l or "failed" in l or "error" in l]
    step.status = OK if code == 0 else FAIL
    step.detail = (summary[-1] if summary else tail.splitlines()[-1] if tail else label)
    if code != 0:
        step.detail += "\n" + tail


def step_legacy(step: Step):
    """v1.5 老系统回归（独立脚本，不是 pytest 用例）"""
    code, tail, secs = run([PYTHON, "tests/test_core.py"], timeout=600)
    step.seconds = secs
    step.status = OK if code == 0 else FAIL
    step.detail = tail.splitlines()[-1] if tail else ""
    if code != 0:
        step.detail += "\n" + tail


def step_exe_check(step: Step):
    if not EXE.is_file():
        step.status = SKIP
        step.detail = "未找到打包产物（先跑 build_v2.py）"
        return
    code, tail, secs = run([str(EXE), "--check"], timeout=300)
    step.seconds = secs
    step.status = OK if code == 0 else FAIL
    step.detail = " | ".join(tail.splitlines()[:4]) if tail else ""
    if step.status == OK:
        # v3.4.3：新包必须同时带旧名兼容 exe，否则 v3.4.2 升级会找不到主程序
        if ALIAS_EXE.is_file():
            step.detail += " | 兼容别名 OK"
        else:
            step.status = FAIL
            step.detail += " | 缺少兼容别名 exe（旧版无法升级，请重跑 build_v2.py）"


def step_exe_ui(step: Step):
    if not EXE.is_file():
        step.status = SKIP
        step.detail = "未找到打包产物"
        return
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    code, tail, secs = run([str(EXE), "--ui-check"], timeout=300, env=env)
    step.seconds = secs
    step.status = OK if code == 0 else FAIL
    step.detail = tail.splitlines()[-1] if tail else ""


def step_parse(step: Step):
    if not EXE.is_file():
        step.status = SKIP
        step.detail = "未找到打包产物"
        return
    samples = [p for p in (SAMPLES.glob("*.xlsx"), SAMPLES.glob("*.docx"),
                           SAMPLES.glob("*.pdf"), SAMPLES.glob("*.png"))]
    samples = [p for group in samples for p in group]
    if not samples:
        step.status = SKIP
        step.detail = "没有识别样本（build/_parsecheck）"
        return
    started = time.perf_counter()
    bad = []
    for s in samples:
        code, tail, _ = run([str(EXE), "--parse-check", str(s)], timeout=180)
        if code != 0:
            bad.append(f"{s.name}: {tail.splitlines()[-1] if tail else '失败'}")
    step.seconds = time.perf_counter() - started
    step.status = OK if not bad else FAIL
    step.detail = (f"{len(samples)} 个样本四通道全部识别" if not bad
                   else "\n".join(bad))


def step_build(step: Step):
    started = time.perf_counter()
    code, tail, _ = run([PYTHON, str(ROOT / "build_v2.py")], timeout=1800)
    step.seconds = time.perf_counter() - started
    step.status = OK if code == 0 else FAIL
    lines = [l for l in tail.splitlines() if "BUILD" in l.upper() or "MB" in l]
    step.detail = lines[-3] if lines else (tail.splitlines()[-1] if tail else "")
    if code != 0:
        step.detail += "\n" + tail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{APP_TITLE} · 一键校验")
    ap.add_argument("--fast", action="store_true", help="只跑语法与单测")
    ap.add_argument("--no-exe", action="store_true", help="跳过打包产物自检")
    ap.add_argument("--build", action="store_true", help="全量通过后重新打包")
    args = ap.parse_args(argv)

    global PYTHON
    PYTHON = resolve_python()

    plan = [("语法编译", step_compile),
            ("pytest v2", lambda s: step_pytest(s, V2 / "tests", "v2"))]
    if not args.fast:
        plan += [("v1.5 回归", step_legacy)]
        if not args.no_exe:
            plan += [("exe --check", step_exe_check),
                     ("exe --ui-check", step_exe_ui),
                     ("文档识别四通道", step_parse)]
    if args.build:
        plan.append(("重新打包", step_build))

    results = []
    print(f"\n{APP_TITLE} · 一键校验　({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"解释器：{PYTHON}\n")
    for name, fn in plan:
        step = Step(name)
        try:
            fn(step)
        except subprocess.TimeoutExpired:
            step.status, step.detail = FAIL, "超时"
        except Exception as exc:                       # noqa: BLE001
            step.status, step.detail = FAIL, f"{type(exc).__name__}: {exc}"
        results.append(step)
        print(f"  [{step.status}] {name:<14} {step.seconds:6.1f}s  "
              f"{step.detail.splitlines()[0] if step.detail else ''}")
        if step.status == FAIL:
            for line in step.detail.splitlines()[1:]:
                print(f"         {line}")

    failed = [s for s in results if s.status == FAIL]
    print("\n" + "─" * 68)
    total = sum(s.seconds for s in results)
    if failed:
        print(f"结果：{len(failed)} 项失败 / 共 {len(results)} 项，耗时 {total:.1f}s")
        print("失败项：" + "、".join(s.name for s in failed))
        return 1
    skipped = [s for s in results if s.status == SKIP]
    print(f"结果：全部通过（{len(results) - len(skipped)} 项，跳过 {len(skipped)} 项），"
          f"耗时 {total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

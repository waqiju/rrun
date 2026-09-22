"""doctor：单机健康检查（ssh 连通性 + 认证 + 远端 python 环境）。

默认只查显式指定的一台机器——检查会产生真实 ssh 连接（副作用），
连全部机器必须显式 --all。
"""

import time
from dataclasses import dataclass, field

from .executor import (
    POSIX_PYTHON_CANDIDATES,
    PS_REMOTE_CMD,
    VENV_PY_POSIX,
    VENV_PY_WIN,
    WINDOWS_PYTHON_CANDIDATES,
    _ssh_run,
    build_ps_wrapper,
    probe_python,
)
from .registry import resolve_machine


@dataclass
class DoctorResult:
    host: str
    ok: bool = False
    ssh_ok: bool = False
    latency_s: float = 0.0
    python_path: str = ""
    python_version: str = ""
    is_unified_venv: bool = False
    checks: "list[str]" = field(default_factory=list)  # 已通过的检查项描述
    message: str = ""                                   # 失败原因 / 建议


def check_machine(host: str, mux: bool = True, timeout: float = 30.0) -> DoctorResult:
    """检查单台机器：ssh 回显 → 远端 python 探测。不抛异常，结果全部汇入 DoctorResult。"""
    try:
        machine = resolve_machine(host)
    except KeyError as e:
        return DoctorResult(host=host, message=str(e))
    r = DoctorResult(host=machine.name)

    # 1) ssh 连通 + 认证（一个最小回显脚本）
    t0 = time.time()
    try:
        if machine.is_windows:
            rc, out, err, _ = _ssh_run(machine, PS_REMOTE_CMD,
                                       build_ps_wrapper("Write-Output rrun-ok"), timeout, mux)
        else:
            rc, out, err, _ = _ssh_run(machine, "bash -s", b"echo rrun-ok\n", timeout, mux)
    except RuntimeError as e:
        r.message = str(e)
        return r
    r.latency_s = time.time() - t0
    if rc != 0 or b"rrun-ok" not in out:
        detail = err.decode("utf-8", "replace").strip()[:200]
        r.message = (f"ssh check failed (exit={rc})"
                     + (f": {detail}" if detail else ""))
        return r
    r.ssh_ok = True
    r.checks.append(f"ssh ok ({r.latency_s:.2f}s)")

    # 2) 远端 python 探测（exec 热路径同款候选链；不命中不算 ssh 失败）
    venv_py = VENV_PY_WIN if machine.is_windows else VENV_PY_POSIX
    candidates = WINDOWS_PYTHON_CANDIDATES if machine.is_windows else POSIX_PYTHON_CANDIDATES
    try:
        hit = probe_python(machine, candidates, mux)
    except RuntimeError as e:
        r.message = str(e)
        return r
    if hit:
        r.python_path, r.python_version = hit
        # POSIX 候选含 $HOME，远端展开后回显的是绝对路径，不能与常量直接等值比较；
        # 用后缀判定（Windows 候选是绝对路径常量，等值即可）。
        if machine.is_windows:
            r.is_unified_venv = r.python_path.lower() == venv_py.lower()
        else:
            r.is_unified_venv = r.python_path == venv_py or r.python_path.endswith(
                "/.remote-machine/venv/bin/python")
        tag = "unified venv" if r.is_unified_venv else "fallback (not the unified venv)"
        r.checks.append(f"python {r.python_version} at {r.python_path} [{tag}]")
        if not r.is_unified_venv:
            r.message = f"unified venv missing; run: rrun setup {machine.name}"
    else:
        r.message = f"no python 3.12 found; run: rrun setup {machine.name}"
    r.ok = r.ssh_ok and bool(hit) and r.is_unified_venv
    return r

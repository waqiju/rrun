"""rrun — 远程机器执行模块（Run Remote）。

约定：read/write/edit 等文件操作始终发生在本地；任何远程执行都是
「本地写脚本 → stdin 管道送远端解释器执行 → 收回 stdout/stderr/退出码」。

支持语言：bash（Mac/Linux）、powershell（Windows）、python（跨平台）。
"""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("rrun-cli")
except Exception:  # noqa: BLE001 - 未安装（源码直跑）时回退
    __version__ = "0.2.0"

from .doctor import DoctorResult, check_machine
from .executor import (
    AUDIT_LOG,
    EXIT_TIMEOUT,
    EXIT_TRANSPORT_ERROR,
    RRUN_HOME,
    ExecResult,
    build_ps_wrapper,
    close_mux,
    detect_remote_python,
    run,
)
from .registry import Machine, SourceInfo, candidate_sources, load_machines, resolve_machine, scan_sources
from .setup import SetupResult, load_requirements, setup_machine

__all__ = [
    "AUDIT_LOG", "EXIT_TIMEOUT", "EXIT_TRANSPORT_ERROR", "RRUN_HOME",
    "DoctorResult", "ExecResult", "Machine", "SetupResult", "SourceInfo",
    "build_ps_wrapper", "candidate_sources", "check_machine", "close_mux", "detect_remote_python",
    "load_machines", "load_requirements", "resolve_machine", "run", "scan_sources", "setup_machine",
]

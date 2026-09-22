"""远程执行核心：本地脚本 → ssh stdin 管道 → 远端解释器执行。

设计要点（Phase 0 spike 实测验证）：
- 全程 stdin 管道，脚本内容/中文不走命令行，规避多层转码转义问题。
- Windows PowerShell：-Command - 按行执行 stdin，故载荷编码为
  **单行纯 ASCII wrapper**（内联 base64，UTF-8 解码后 ScriptBlock 执行），
  支持 workdir / env / args($args) / 退出码透传 / terminating error -> 1。
- python：`python -X utf8 -`；bash：`bash -s --`。
- ControlMaster 连接复用：首连 ~0.5s，复用 ~0.02s。
- 退出码约定：远端脚本退出码原样透传；255 = ssh 传输层错误；124 = 本地超时。
- 本地状态目录：~/.rrun/（RRUN_HOME 环境变量可改根目录）。
"""

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .registry import Machine, resolve_machine


def _rrun_home() -> Path:
    """本地状态根目录：$RRUN_HOME 或 ~/.rrun（放审计日志/内联落盘/用户配置）。"""
    override = os.environ.get("RRUN_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".rrun"


RRUN_HOME = _rrun_home()
AUDIT_LOG = RRUN_HOME / "log" / "remote-exec.jsonl"

LANGS = ("bash", "powershell", "python")
EXT_TO_LANG = {".py": "python", ".ps1": "powershell", ".sh": "bash"}
LANG_TO_EXT = {"python": ".py", "powershell": ".ps1", "bash": ".sh"}

SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
]
CONTROL_PATH_DIR = Path.home() / ".ssh"
CONTROL_PATH = str(CONTROL_PATH_DIR / "rrun-%C")
MUX_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={CONTROL_PATH}",
    "-o", "ControlPersist=600",
]

EXIT_TRANSPORT_ERROR = 255  # ssh 自身失败（连不上/认证失败/掉线）
EXIT_TIMEOUT = 124          # 本地超时杀掉 ssh 进程

# 统一环境布局（setup 子命令初始化；基线版本锁 3.12）：
#   Windows: C:\tools\remote-machine\venv\Scripts\python.exe  —— 统一 venv，常态命中
#            C:\tools\remote-machine\python312\python.exe     —— standalone 基座（venv 损坏时降级）
#   Mac:     ~/.remote-machine/venv/bin/python                  —— 统一 venv
#            ~/.remote-machine/python312/bin/python3            —— standalone 基座（仅裸机安装）
# 候选按序探测，命中后校验版本 == 3.12.x（不符继续找）；全灭 -> 报错提示跑 setup。
REQUIRED_PY_MAJOR_MINOR = (3, 12)

VENV_PY_WIN = r"C:\tools\remote-machine\venv\Scripts\python.exe"
BASE_PY_WIN = r"C:\tools\remote-machine\python312\python.exe"
VENV_PY_POSIX = "$HOME/.remote-machine/venv/bin/python"
BASE_PY_POSIX = "$HOME/.remote-machine/python312/bin/python3"

WINDOWS_PYTHON_CANDIDATES = [
    VENV_PY_WIN,
    BASE_PY_WIN,
    r"C:\Python\Python312\python.exe",  # 存量装机（降级，无第三方库）
]
# setup 创建 venv 时的基座候选（不含 venv 自身）：机器已装的优先，standalone 兜底
WINDOWS_BASE_CANDIDATES = [r"C:\Python\Python312\python.exe", BASE_PY_WIN]
POSIX_PYTHON_CANDIDATES = [
    VENV_PY_POSIX,
    "python3.12", "/usr/local/bin/python3.12",
]
POSIX_BASE_CANDIDATES = ["python3.12", "/usr/local/bin/python3.12", BASE_PY_POSIX]

PS_REMOTE_CMD = "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command -"

_python_cache: "dict[tuple[str, str], tuple[str, str]]" = {}  # (ip, override) -> (路径, 版本)


@dataclass
class ExecResult:
    host: str
    ip: str
    lang: str
    exit_code: int
    duration: float
    stdout: bytes = b""
    stderr: bytes = b""
    script_path: str = ""       # 本地脚本路径（content 模式为落盘后的路径）
    content_sha1: str = ""
    remote_python: str = ""     # 实际使用的远端 python（lang=python 时）
    remote_python_version: str = ""  # 远端 python 版本（如 3.12.14）
    transport_error: bool = False  # ssh 层失败（exit 255）
    timed_out: bool = False

    def stdout_text(self, errors: str = "replace") -> str:
        return self.stdout.decode("utf-8", errors)

    def stderr_text(self, errors: str = "replace") -> str:
        return self.stderr.decode("utf-8", errors)


def _check_ascii(values, what: str) -> None:
    for v in values:
        if not all(ord(c) < 128 for c in v):
            raise ValueError(f"{what} must be ASCII-only, got: {v!r}; put CJK/special characters in the script content or a JSON file, not on the command line")


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串转义（入参已保证 ASCII）。"""
    return "'" + s.replace("'", "''") + "'"


def build_ps_wrapper(code: str, args=(), workdir: str = "", env: "dict | None" = None) -> bytes:
    """把 PowerShell 脚本编码为单行纯 ASCII wrapper（-Command - 按行执行，禁止多行）。"""
    _check_ascii(args, "arguments")
    parts = [
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8",
        "$OutputEncoding=[Text.Encoding]::UTF8",
        "$ProgressPreference='SilentlyContinue'",
    ]
    if workdir:
        parts.append(f"Set-Location {_ps_quote(workdir)}")
    for k, v in (env or {}).items():
        parts.append(f"$env:{k}={_ps_quote(v)}")
    b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    parts.append(f"$__code=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))")
    parts.append("$__args=@(" + ",".join(_ps_quote(a) for a in args) + ")")
    parts.append(
        "try{& ([ScriptBlock]::Create($__code)) @__args}"
        "catch{[Console]::Error.WriteLine($_.Exception.ToString());exit 1}"
    )
    parts.append("if($null -ne $LASTEXITCODE){exit $LASTEXITCODE}else{exit 0}")
    line = ";".join(parts)
    assert all(ord(c) < 128 for c in line), "powershell wrapper 必须纯 ASCII"
    return (line + "\n").encode("ascii")


def _posix_prefix(workdir: str = "", env: "dict | None" = None) -> str:
    """posix 远端命令前缀：cd + env（拼在实际命令前）。"""
    prefix = ""
    if workdir:
        prefix += f"cd {shlex.quote(workdir)} && "
    if env:
        prefix += "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "
    return prefix


def build_bash_command(args=(), workdir: str = "", env: "dict | None" = None) -> str:
    _check_ascii(args, "arguments")
    cmd = _posix_prefix(workdir, env) + "bash -s"
    if args:
        cmd += " -- " + " ".join(shlex.quote(a) for a in args)
    return cmd


def _ssh_args(machine: Machine, mux: bool) -> "list[str]":
    """组装 ssh/sshpass 参数：认证（密码走 sshpass，否则 key 链 + BatchMode）、端口、mux。"""
    args: list[str] = []
    if machine.password:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            raise RuntimeError("sshpass not found on this machine (install: sudo apt install sshpass / brew install hudochenkov/sshpass/sshpass)")
        args += [sshpass, "-p", machine.password]
    args.append("ssh")
    if machine.password:
        args += ["-o", "NumberOfPasswordPrompts=1"]  # 密码错误快速失败，不反复提示
    else:
        # key 认证：禁交互提示（防密码 prompt 把 stdin 脚本吃掉/挂起）
        args += ["-o", "BatchMode=yes"]
    if machine.identity_file:
        args += ["-i", str(Path(machine.identity_file).expanduser())]
    args += SSH_BASE_OPTS
    if machine.port != 22:
        args += ["-p", str(machine.port)]
    if mux:
        CONTROL_PATH_DIR.mkdir(mode=0o700, exist_ok=True)
        args += MUX_OPTS
    return args


def _ssh_run(machine: Machine, remote_cmd: str, stdin: bytes, timeout: "float | None",
             mux: bool) -> "tuple[int, bytes, bytes, bool]":
    """返回 (exit_code, stdout, stderr, timed_out)。exit 255 即传输层错误。"""
    args = [*_ssh_args(machine, mux), machine.target, remote_cmd]
    try:
        p = subprocess.run(args, input=stdin, capture_output=True,
                           timeout=timeout if timeout and timeout > 0 else None)
        return p.returncode, p.stdout, p.stderr, False
    except subprocess.TimeoutExpired as e:
        return EXIT_TIMEOUT, e.stdout or b"", (e.stderr or b"") + f"\n[remote-exec] local timeout {timeout}s\n".encode(), True


# 注意：代码内不得出现任何引号（bash 探测用单引号包裹此代码；chr(46)='.'）
_PY_VER_PRINT_CODE = "import sys;print(*sys.version_info[:3],sep=chr(46))"
_PY_VER_CHECK_CODE = (
    _PY_VER_PRINT_CODE + f";sys.exit(0 if sys.version_info[:2]=={REQUIRED_PY_MAJOR_MINOR} else 1)"
)
# 基座资格加码：ensurepip 必须可 import（Debian/Ubuntu 把 ensurepip 整体拆进 python3.x-venv
# 包，缺失时系统 python 版本对却建不了 venv——不算合格基座）。import 失败即非零退出，候选跳过。
_PY_BASE_CHECK_CODE = "import ensurepip;" + _PY_VER_CHECK_CODE


def _ps_probe_script(candidates, require_ensurepip: bool = False) -> str:
    """Windows 探测脚本：逐候选校验（版本==3.12.x，require_ensurepip 时加 ensurepip 资格），
    命中输出 path|x.y.z，全灭 exit 1。"""
    check = _PY_BASE_CHECK_CODE if require_ensurepip else _PY_VER_CHECK_CODE
    arr = ",".join(_ps_quote(c) for c in candidates)
    return (
        f"foreach($c in @({arr})){{"
        "if(Test-Path $c){"
        f"$v=& $c -X utf8 -c \"{check}\" 2>$null;"
        "if($LASTEXITCODE -eq 0){Write-Output ($c+'|'+($v -join ''));exit 0}"
        "}}"
    )


def _bash_probe_script(candidates, require_ensurepip: bool = False) -> str:
    """posix 探测脚本：同 Windows 语义。候选为可信常量（$HOME 需保留展开，不可 shlex.quote）。"""
    check = _PY_BASE_CHECK_CODE if require_ensurepip else _PY_VER_CHECK_CODE
    items = " ".join(f'"{c}"' for c in candidates)
    return (
        f"for p in {items}; do\n"
        '  command -v "$p" >/dev/null 2>&1 || continue\n'
        f'  v=$("$p" -X utf8 -c \'{check}\' 2>/dev/null) || continue\n'
        '  echo "$p|$v"\n'
        "  exit 0\n"
        "done\n"
        "exit 1\n"
    )


def probe_python(machine: Machine, candidates, mux: bool = True,
                 require_ensurepip: bool = False) -> "tuple[str, str] | None":
    """在候选中找版本==3.12.x 的远端 python，单次往返；返回 (路径, 版本)，全灭返回 None。

    require_ensurepip=True 用于 setup/doctor 的基座资格审查：无 ensurepip 的 python
    建不了 venv（Debian/Ubuntu 拆包 python3.x-venv 的典型场景），跳过。
    """
    if machine.is_windows:
        rc, out, _, _ = _ssh_run(machine, PS_REMOTE_CMD,
                                 build_ps_wrapper(_ps_probe_script(candidates, require_ensurepip)), 60, mux)
    else:
        rc, out, _, _ = _ssh_run(machine, "bash -s",
                                 _bash_probe_script(candidates, require_ensurepip).encode("utf-8"), 60, mux)
    if rc != 0 or not out.strip():
        return None
    line = out.decode("utf-8", "replace").strip().splitlines()[-1].strip()
    path, sep, ver = line.partition("|")
    return (path.strip(), ver.strip()) if sep and path.strip() else None


def probe_python_version(machine: Machine, python_path: str, mux: bool = True) -> str:
    """尽力探测指定 python 的版本字符串（失败返回空串）。posix 路径可含 $HOME（shell 展开）。"""
    code = _PY_VER_PRINT_CODE
    if machine.is_windows:
        script = f"$v=& {_ps_quote(python_path)} -X utf8 -c \"{code}\" 2>$null;if($LASTEXITCODE -eq 0){{Write-Output ($v -join '')}}"
        rc, out, _, _ = _ssh_run(machine, PS_REMOTE_CMD, build_ps_wrapper(script), 30, mux)
    else:
        rc, out, _, _ = _ssh_run(machine, "bash -s",
                                 f'"{python_path}" -X utf8 -c \'{code}\' 2>/dev/null\n'.encode(), 30, mux)
    if rc != 0 or not out.strip():
        return ""
    return out.decode("utf-8", "replace").strip().splitlines()[-1].strip()


def check_remote_pip(machine: Machine, python_path: str, mux: bool = True) -> bool:
    """检查指定远端 python 是否带 pip（半拉子 venv 检测：venv python 能跑，
    但若创建时 ensurepip 失败过，venv 里就没有 pip）。"""
    if machine.is_windows:
        payload = build_ps_wrapper(f"& {_ps_quote(python_path)} -m pip --version *>$null")
        rc, _, _, _ = _ssh_run(machine, PS_REMOTE_CMD, payload, 30, mux)
    else:
        rc, _, _, _ = _ssh_run(machine, "bash -s",
                               f'"{python_path}" -m pip --version >/dev/null 2>&1\n'.encode(), 30, mux)
    return rc == 0


def detect_remote_python(machine: Machine, override: str = "", mux: bool = True) -> "tuple[str, str]":
    """解析远端 python，返回 (路径, 版本)。基线锁 3.12；machines.json "python" 字段/参数可覆盖（跳过校验）。"""
    key = (machine.ip, override or machine.python)
    if key in _python_cache:
        return _python_cache[key]
    if override or machine.python:
        path = override or machine.python
        result = (path, probe_python_version(machine, path, mux))
    else:
        candidates = WINDOWS_PYTHON_CANDIDATES if machine.is_windows else POSIX_PYTHON_CANDIDATES
        found = probe_python(machine, candidates, mux)
        if not found:
            raise RuntimeError(
                f"[{machine.name}] no python {REQUIRED_PY_MAJOR_MINOR[0]}.{REQUIRED_PY_MAJOR_MINOR[1]}"
                f" found (baseline is pinned). Provision first: rrun setup {machine.name}"
            )
        result = found
    _python_cache[key] = result
    return result


def run(host: str, lang: str = "", file: str = "", content: str = "", args=(),
        workdir: str = "", env: "dict | None" = None, timeout: "float | None" = None,
        remote_python: str = "", utf8: bool = True, mux: bool = True,
        audit: bool = True) -> ExecResult:
    """在远端机器执行本地脚本（或内联内容），返回 ExecResult。

    host: 机器 name/ip/hostname；lang: bash|powershell|python（缺省按文件扩展名或机器 OS 推断）
    file/content 二选一；args 仅允许 ASCII。
    """
    machine = resolve_machine(host)
    if file and content:
        raise ValueError("pass either file or content, not both")
    if not file and not content:
        raise ValueError("file or content is required")

    if file:
        script_path = str(Path(file).resolve())
        raw = Path(file).read_bytes()
        suffix = Path(file).suffix.lower()
    else:
        script_path = ""
        raw = content.encode("utf-8")
        suffix = ""
    if not lang:
        lang = EXT_TO_LANG.get(suffix, machine.default_lang)
    if lang not in LANGS:
        raise ValueError(f"unsupported language: {lang} (choose from {LANGS})")
    _check_ascii(args, "arguments")
    _check_ascii([x for kv in (env or {}).items() for x in kv], "env")

    t0 = time.time()
    remote_python_used = ""
    remote_py_version = ""
    if lang == "powershell":
        remote_cmd = PS_REMOTE_CMD
        stdin = build_ps_wrapper(raw.decode("utf-8"), args, workdir, env)
    elif lang == "bash":
        remote_cmd = build_bash_command(args, workdir, env)
        stdin = raw
    else:  # python
        remote_python_used, remote_py_version = detect_remote_python(machine, remote_python, mux)
        py_cmd = remote_python_used + (" -X utf8" if utf8 else "") + " -"
        if args:
            py_cmd += " " + " ".join(shlex.quote(a) for a in args)
        if env or workdir:
            if machine.is_windows:
                raise ValueError("workdir/env are not supported for Windows+python (by design); use os.chdir/os.environ inside the script")
            remote_cmd = _posix_prefix(workdir, env) + py_cmd
        else:
            remote_cmd = py_cmd
        stdin = raw

    exit_code, stdout, stderr, timed_out = _ssh_run(machine, remote_cmd, stdin, timeout, mux)
    result = ExecResult(
        host=machine.name, ip=machine.ip, lang=lang, exit_code=exit_code,
        duration=time.time() - t0, stdout=stdout, stderr=stderr,
        script_path=script_path, content_sha1=hashlib.sha1(raw).hexdigest()[:12],
        remote_python=remote_python_used, remote_python_version=remote_py_version,
        transport_error=(exit_code == EXIT_TRANSPORT_ERROR), timed_out=timed_out,
    )
    if audit:
        _write_audit(result, args, workdir, env, timeout)
    return result


def _write_audit(result: ExecResult, args, workdir: str, env: "dict | None",
                 timeout: "float | None") -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "host": result.host, "ip": result.ip, "lang": result.lang,
            "script": result.script_path, "sha1": result.content_sha1,
            "args": list(args), "workdir": workdir,
            "env_keys": sorted((env or {}).keys()),  # 只记 key，防泄密
            "timeout": timeout, "exit_code": result.exit_code,
            "python": result.remote_python, "python_version": result.remote_python_version,
            "duration_s": round(result.duration, 2),
            "transport_error": result.transport_error, "timed_out": result.timed_out,
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 审计失败不阻断执行


def close_mux(host: str) -> "tuple[int, str]":
    """关闭某台机器的 ControlMaster 复用连接。"""
    machine = resolve_machine(host)
    args = ["ssh", "-O", "exit", "-o", f"ControlPath={CONTROL_PATH}"]
    if machine.port != 22:
        args += ["-p", str(machine.port)]
    if machine.identity_file:
        args += ["-i", str(Path(machine.identity_file).expanduser())]
    args.append(machine.target)
    p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return p.returncode, (p.stdout + p.stderr).strip()

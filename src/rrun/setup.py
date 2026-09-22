"""setup：初始化远端统一 python 环境（基线 3.12 + 统一 venv + 阿里云 pip 源）。

布局（幂等，可重复执行）：
  Windows: C:\\tools\\remote-machine\\venv\\Scripts\\python.exe  —— 统一 venv
           C:\\tools\\remote-machine\\python312\\python.exe     —— standalone 基座（仅裸机安装）
  Mac:     ~/.remote-machine/venv/bin/python
           ~/.remote-machine/python312/bin/python3

原则：
  - 机器已有 3.12 则直接用作 venv 基座；没有才装 python-build-standalone
    （免安装压缩包：零注册表、零 PATH、不改 python/py 指向，纯新增目录）。
  - standalone 包先下载到本机缓存 ~/.cache/rrun/，再经 ssh stdin
    推流到远端解压，远端无需访问 GitHub。
  - pip 源（阿里云镜像）写在 venv 内 pip.ini/pip.conf，不污染机器全局配置。
  - 标准依赖清单：默认用包内置 remote-requirements.txt；
    ~/.rrun/remote-requirements.txt 存在时覆盖（加依赖改文件后重跑 setup）。
"""

import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .executor import (
    AUDIT_LOG,
    BASE_PY_POSIX,
    BASE_PY_WIN,
    POSIX_BASE_CANDIDATES,
    PS_REMOTE_CMD,
    RRUN_HOME,
    VENV_PY_POSIX,
    VENV_PY_WIN,
    WINDOWS_BASE_CANDIDATES,
    _check_ascii,
    _ssh_run,
    build_ps_wrapper,
    check_remote_pip,
    probe_python,
    probe_python_version,
)
from .registry import Machine, resolve_machine

# 标准依赖：用户级覆盖 > 包内置默认
REQUIREMENTS_OVERRIDE = RRUN_HOME / "remote-requirements.txt"
REQUIREMENTS_BUILTIN = Path(__file__).resolve().with_name("remote-requirements.txt")

# python-build-standalone（astral-sh）固定版本，免安装压缩包
PBS_TAG = "20260901"
PBS_VER = "3.12.14"
PBS_ASSETS = {
    ("Windows", "x86_64"): f"cpython-{PBS_VER}+{PBS_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz",
    ("Mac", "arm64"): f"cpython-{PBS_VER}+{PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz",
    ("Mac", "x86_64"): f"cpython-{PBS_VER}+{PBS_TAG}-x86_64-apple-darwin-install_only.tar.gz",
    ("Linux", "x86_64"): f"cpython-{PBS_VER}+{PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz",
    ("Linux", "aarch64"): f"cpython-{PBS_VER}+{PBS_TAG}-aarch64-unknown-linux-gnu-install_only.tar.gz",
}
PBS_DOWNLOAD_BASE = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_TAG}/"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", "") or Path.home() / ".cache") / "rrun"

PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"

TOOLS_DIR_WIN = r"C:\tools\remote-machine"
VENV_DIR_WIN = TOOLS_DIR_WIN + r"\venv"


@dataclass
class SetupResult:
    host: str
    ok: bool
    venv_python: str = ""
    version: str = ""
    base_python: str = ""
    created_venv: bool = False
    installed_standalone: bool = False
    packages: "list[str]" = field(default_factory=list)
    message: str = ""
    duration: float = 0.0


def load_requirements() -> "list[str]":
    """读取标准依赖清单（过滤注释/空行）。~/.rrun/remote-requirements.txt 优先于包内置默认。"""
    path = REQUIREMENTS_OVERRIDE if REQUIREMENTS_OVERRIDE.is_file() else REQUIREMENTS_BUILTIN
    reqs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            reqs.append(line)
    return reqs


def _req_import_name(req: str) -> str:
    """requests>=2.31 -> requests（用于装后 import 自检）。"""
    return re.split(r"[<>=!~\[ ]", req, maxsplit=1)[0].strip()


def _ensure_local_tarball(os_name: str, arch: str) -> Path:
    """standalone 包下载到本机缓存（远端不访问 GitHub）。"""
    name = PBS_ASSETS[(os_name, arch)]
    path = CACHE_DIR / name
    if path.exists() and path.stat().st_size > 1024 * 1024:
        return path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url = PBS_DOWNLOAD_BASE + name
    print(f"[setup] local cache miss, downloading {url} ...", file=sys.stderr)
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(url, tmp)  # noqa: S310 - 固定官方源
    tmp.rename(path)
    return path


def _pbs_platform(uname_sm: str) -> "tuple[str, str]":
    """`uname -sm` 输出 -> PBS_ASSETS 键（'Linux x86_64'->('Linux','x86_64')，'Darwin arm64'->('Mac','arm64')）。"""
    parts = uname_sm.split()
    sys_name, arch = (parts + ["", ""])[:2]
    return {"Darwin": "Mac", "Linux": "Linux"}.get(sys_name, sys_name), arch


def _install_standalone(machine: Machine, mux: bool, timeout: float) -> str:
    """推送 standalone python 到远端并解压，返回基座 python 路径。"""
    if machine.is_windows:
        os_name, arch = "Windows", "x86_64"
        # 注意 cmd 陷阱：if exist 不带括号会把后续 & 链全吞进分支体，
        # 条件为假时整行跳过且 exit=0（假成功），故条件放最后用 && 门控
        extract_cmd = (
            f'mkdir "{TOOLS_DIR_WIN}" 2>nul '
            f'& rmdir /s /q "{TOOLS_DIR_WIN}\\python312" 2>nul '
            f'& tar -xzf - -C "{TOOLS_DIR_WIN}" '
            f'&& ren "{TOOLS_DIR_WIN}\\python" python312'
        )
        base_py = BASE_PY_WIN
    else:
        rc, out, _, _ = _ssh_run(machine, "uname -sm", b"", 30, mux)
        os_name, arch = _pbs_platform(out.decode("utf-8", "replace"))
        if (os_name, arch) not in PBS_ASSETS:
            raise RuntimeError(f"[{machine.name}] no standalone python build for platform: {os_name} {arch}")
        extract_cmd = (
            'tools="$HOME/.remote-machine"; mkdir -p "$tools" '
            '&& rm -rf "$tools/python312" '
            '&& tar -xzf - -C "$tools" '
            '&& mv "$tools/python" "$tools/python312"'
        )
        base_py = BASE_PY_POSIX
    tarball = _ensure_local_tarball(os_name, arch)
    print(f"[setup] [{machine.name}] streaming standalone python ({tarball.name}, "
          f"{tarball.stat().st_size // 1024 // 1024}MB) ...", file=sys.stderr)
    rc, _, err, timed_out = _ssh_run(machine, extract_cmd, tarball.read_bytes(), timeout, mux)
    if rc != 0:
        raise RuntimeError(f"[{machine.name}] standalone push/extract failed (exit={rc}): "
                           f"{err.decode('utf-8', 'replace')[:300]}")
    ver = probe_python_version(machine, base_py, mux)
    if not ver:
        raise RuntimeError(f"[{machine.name}] standalone python failed verification after extraction: {base_py}")
    return base_py


def _build_ps_setup(base_py: str, pkgs: "list[str]", force: bool) -> str:
    """Windows 一键脚本：建 venv（缺/坏/force 时）→ pip.ini → 装依赖 → 自检。全 ASCII。

    “坏” = bin/python 在但 pip 不在：上次 venv 创建中途失败（如 ensurepip 缺失）
    留下的半拉子 venv，直接重建（幂等自愈）。
    """
    pkg_args = " ".join(pkgs)
    imports = ";".join(f"import {_req_import_name(p)}" for p in pkgs) or "pass"
    force_lit = "$true" if force else "$false"
    return f"""
$tools={_ps_quote_const(TOOLS_DIR_WIN)}
$venv="$tools\\venv"
$venvPy="$venv\\Scripts\\python.exe"
New-Item -ItemType Directory -Force $tools | Out-Null
if({force_lit} -and (Test-Path $venv)){{Remove-Item -Recurse -Force $venv}}
if(Test-Path $venvPy){{
  & $venvPy -m pip --version *>$null
  if($LASTEXITCODE -ne 0){{Remove-Item -Recurse -Force $venv}}
}}
if(-not (Test-Path $venvPy)){{
  if('{base_py}' -eq ''){{Write-Error "no base python";exit 1}}
  & '{base_py}' -m venv $venv
  if($LASTEXITCODE -ne 0){{Write-Error "venv create failed";exit 1}}
}}
[System.IO.File]::WriteAllText("$venv\\pip.ini","[global]`nindex-url = {PIP_INDEX_URL}`n")
& $venvPy -m pip install --quiet --disable-pip-version-check {pkg_args}
if($LASTEXITCODE -ne 0){{Write-Error "pip install failed";exit 1}}
& $venvPy -X utf8 -c "import sys;{imports};print('verify ok', '.'.join(map(str,sys.version_info[:3])))"
if($LASTEXITCODE -ne 0){{Write-Error "verify failed";exit 1}}
"""


def _ps_quote_const(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _build_bash_setup(base_py: str, pkgs: "list[str]", force: bool) -> str:
    pkg_args = " ".join(pkgs)
    imports = ";".join(f"import {_req_import_name(p)}" for p in pkgs) or "pass"
    return f"""set -e
tools="$HOME/.remote-machine"
venv="$tools/venv"
mkdir -p "$tools"
if [ "{'1' if force else '0'}" = "1" ]; then rm -rf "$venv"; fi
if [ -x "$venv/bin/python" ] && ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
  rm -rf "$venv"
fi
if [ ! -x "$venv/bin/python" ]; then
  "{base_py}" -m venv "$venv"
fi
cat > "$venv/pip.conf" <<'EOF'
[global]
index-url = {PIP_INDEX_URL}
EOF
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check {pkg_args}
"$venv/bin/python" -X utf8 -c "import sys;{imports};print('verify ok', '.'.join(map(str,sys.version_info[:3])))"
"""


def setup_machine(host: str, force: bool = False, mux: bool = True,
                  timeout: float = 600.0) -> SetupResult:
    """初始化单台机器的统一 python 环境。幂等；force 重建 venv（不动 python 本体）。"""
    t0 = time.time()
    result = SetupResult(host=host, ok=False)
    try:
        machine = resolve_machine(host)
        reqs = load_requirements()
        _check_ascii(reqs, "requirements")
        venv_py = VENV_PY_WIN if machine.is_windows else VENV_PY_POSIX
        base_cands = WINDOWS_BASE_CANDIDATES if machine.is_windows else POSIX_BASE_CANDIDATES

        hit = probe_python(machine, [venv_py], mux)
        # 半拉子 venv（python 能跑但 pip 不在——上次创建中途失败的残留）也视为待重建；
        # 否则远端脚本删掉坏 venv 后会拿空 base_py 去重建，必然失败
        venv_broken = bool(hit) and not check_remote_pip(machine, hit[0], mux)
        need_create = force or not hit or venv_broken
        base_py = ""
        if need_create:
            # 基座资格：版本 3.12 且带 ensurepip（缺 ensurepip 的系统 python 建不了 venv，
            # Debian/Ubuntu 未装 python3.x-venv 的典型场景）——不合格则装 standalone 基座
            base_hit = probe_python(machine, base_cands, mux, require_ensurepip=True)
            if base_hit:
                base_py = base_hit[0]
            else:
                print(f"[setup] [{machine.name}] no venv-capable python 3.12 base found; "
                      f"installing standalone python ...", file=sys.stderr)
                base_py = _install_standalone(machine, mux, timeout)
                result.installed_standalone = True
            result.created_venv = True
            result.base_python = base_py

        if machine.is_windows:
            payload = build_ps_wrapper(_build_ps_setup(base_py, reqs, force))
            rc, out, err, _ = _ssh_run(machine, PS_REMOTE_CMD, payload, timeout, mux)
        else:
            payload = _build_bash_setup(base_py, reqs, force).encode("utf-8")
            rc, out, err, _ = _ssh_run(machine, "bash -s", payload, timeout, mux)
        if rc != 0:
            tail = (err or out).decode("utf-8", "replace").strip()[-400:]
            raise RuntimeError(f"setup script failed (exit={rc}): {tail}")

        final = probe_python(machine, [venv_py], mux)
        if not final:
            raise RuntimeError("setup finished but the venv python probe failed")
        result.venv_python, result.version = final
        result.packages = reqs
        result.ok = True
    except Exception as e:  # noqa: BLE001 - 汇总为 result，--all 继续遍历
        result.message = str(e)
    result.duration = time.time() - t0
    _write_setup_audit(result)
    return result


def _write_setup_audit(result: SetupResult) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "host": result.host, "lang": "setup",
            "exit_code": 0 if result.ok else 1,
            "venv_python": result.venv_python, "python_version": result.version,
            "base_python": result.base_python,
            "created_venv": result.created_venv,
            "installed_standalone": result.installed_standalone,
            "packages": result.packages, "message": result.message,
            "duration_s": round(result.duration, 2),
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            import json
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def venv_python_or_die(host: str, mux: bool = True) -> str:
    """返回统一 venv 的 python 路径；未初始化则报错指路 setup。"""
    machine = resolve_machine(host)
    venv_py = VENV_PY_WIN if machine.is_windows else VENV_PY_POSIX
    hit = probe_python(machine, [venv_py], mux)
    if not hit:
        raise RuntimeError(f"[{machine.name}] unified venv not provisioned yet; run first: rrun setup {machine.name}")
    return hit[0]

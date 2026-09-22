"""`rrun config` 子命令组：机器清单的初始化 / 追加 / 编辑 + 来源链诊断。

设计要点：
- init/add/edit 只操作「用户级主清单」（registry 来源链中的 user 位），路径从
  candidate_sources() 派生而非写死 ~/.rrun，保证写入位置就是加载位置；
  其他来源（env/cwd/machines.d/legacy）只读不动。
- 模板内嵌为 package data（src/rrun/machines.template.json），离线可用、随发行版本
  锁定；写盘即收紧权限（目录 700 / 文件 600，明文密码场景，Windows chmod 语义有限跳过）。
- add 双模式：不给 --name/--ip → 交互向导（getpass 隐藏密码，旗帜值作预填默认）；
  给了 → 纯旗帜模式（可脚本化）；-i 强制向导。
- edit 走 $VISUAL/$EDITOR（Windows 回退 notepad，POSIX 回退 vi），退出后校验 JSON；
  文件缺失时先按模板创建（crontab -e 风格）。
- 无子命令时（bare `rrun config`）保持 v0.2.x 行为：诊断来源链；空清单附 init 引导。
"""

import getpass
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .registry import (
    EMPTY_INVENTORY_HINT,
    _load_file,
    candidate_sources,
    load_machines,
    resolve_machine,
    scan_sources,
)

TEMPLATE_FILE = Path(__file__).resolve().with_name("machines.template.json")

_OS_ALIASES = {
    "w": "Windows", "windows": "Windows",
    "m": "Mac", "mac": "Mac", "macos": "Mac", "osx": "Mac", "darwin": "Mac",
    "l": "Linux", "linux": "Linux",
}


def user_inventory_path() -> Path:
    """用户级主清单路径（从来源链派生，不写死 ~/.rrun）。"""
    for label, p in candidate_sources():
        if label == "user":
            return p
    raise RuntimeError("unreachable: candidate_sources() always includes the user source")  # pragma: no cover


def _shorten(path: Path) -> str:
    """显示用：home 前缀折叠为 ~。"""
    s = str(path)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def _write_secure(path: Path, text: str) -> None:
    """写清单并收紧权限（目录 700 / 文件 600；Windows 上跳过 chmod）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)


def _build_entry(name: str, ip: str, os_name: str, user: str, *,
                 password: str = "", hostname: str = "", identity_file: str = "",
                 port: int = 22, python: str = "", description: str = "") -> dict:
    """组装机器条目并校验必填（空可选字段不落盘，保持清单干净）。"""
    if not name or any(c.isspace() for c in name):
        raise SystemExit("[config] machine name must be non-empty and contain no whitespace (--name)")
    if not ip:
        raise SystemExit("[config] ip is required (--ip, or run with no flags for the interactive wizard)")
    if not user:
        raise SystemExit("[config] ssh user is required (--user, or run with no flags for the interactive wizard)")
    if not 1 <= port <= 65535:
        raise SystemExit(f"[config] port must be 1-65535, got {port}")
    entry = {"name": name, "ip": ip, "os": os_name, "user": user}
    if password:
        entry["password"] = password
    if identity_file:
        entry["identity_file"] = identity_file
    if port != 22:
        entry["port"] = port
    if hostname:
        entry["hostname"] = hostname
    if python:
        entry["python"] = python
    if description:
        entry["description"] = description
    return entry


def _append_machine(path: Path, entry: dict) -> None:
    """追加机器到清单文件；文件已存在但损坏时拒绝写入（绝不覆盖用户数据）。"""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SystemExit(f"[config] {path} is not valid JSON ({e}); fix it first with 'rrun config edit'") from None
        if not isinstance(data, dict):
            raise SystemExit(f"[config] {path} must contain a JSON object; fix it first with 'rrun config edit'")
    else:
        data = {}
    machines = data.setdefault("machines", [])
    if not isinstance(machines, list):
        raise SystemExit(f"[config] {path} has a non-list 'machines' field; fix it first with 'rrun config edit'")
    if any(isinstance(m, dict) and m.get("name") == entry["name"] for m in machines):
        raise SystemExit(f"[config] machine [{entry['name']}] already exists in {_shorten(path)}"
                         " — edit it with 'rrun config edit'")
    machines.append(entry)
    _write_secure(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _report_added(path: Path, name: str) -> None:
    """成功提示 + 高优先级来源遮蔽预警。"""
    print(f"[config] added [{name}] to {_shorten(path)}")
    try:
        m = resolve_machine(name)
    except KeyError:
        return
    if m.source != str(path):
        print(f"[config] warning: higher-priority source {m.source} also defines [{name}]"
              " and shadows this entry", file=sys.stderr)
    print(f"[config] next: rrun doctor {name}   # connectivity + python check")


def cmd_config_init(ns) -> int:
    """按内嵌模板创建用户级主清单；已存在时拒绝覆盖（--force 除外）。"""
    path = user_inventory_path()
    if path.exists() and not ns.force:
        print(f"[config] {_shorten(path)} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    _write_secure(path, TEMPLATE_FILE.read_text(encoding="utf-8"))
    perms = "" if os.name == "nt" else ", mode 600"
    print(f"[config] created {_shorten(path)} from the bundled template{perms}")
    print("[config] next: replace the my-* example entries with your machines"
          " — 'rrun config edit' or 'rrun config add'")
    print("[config] then: rrun machines   # verify the inventory is picked up (redacted)")
    return 0


def _ask(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt = f"  {label}{suffix}: "
    value = getpass.getpass(prompt) if secret else input(prompt)
    return value.strip() or default


def _ask_required(label: str, default: str = "") -> str:
    while True:
        value = _ask(label, default)
        if value:
            return value
        print("    (required)")


def _ask_name(default: str = "") -> str:
    while True:
        value = _ask_required("name (unique, no spaces)", default)
        if not any(c.isspace() for c in value):
            return value
        print("    name must not contain whitespace")


def _ask_os(default: str = "Windows") -> str:
    while True:
        value = _ask("os (Windows/Mac/Linux)", default).lower()
        if value in _OS_ALIASES:
            return _OS_ALIASES[value]
        print("    please answer Windows, Mac or Linux (w/m/l)")


def _ask_port(default: int = 22) -> int:
    while True:
        try:
            port = int(_ask("ssh port", str(default)))
        except ValueError:
            print("    port must be a number")
            continue
        if 1 <= port <= 65535:
            return port
        print("    port must be 1-65535")


def _wizard(ns, path: Path) -> "dict | None":
    """交互问答收集机器信息（旗帜值作预填默认）；用户放弃时返回 None。"""
    if not sys.stdin.isatty():
        raise SystemExit("[config] interactive add needs a TTY; use flags instead: "
                         "rrun config add --name N --ip A --user U [--os Windows] [--password ...]")
    print(f"Add a machine to {_shorten(path)} (Ctrl-C to abort):")
    name = _ask_name(ns.name or "")
    ip = _ask_required("ip", ns.ip or "")
    hostname = _ask("hostname (optional, also resolvable)", ns.hostname or "")
    os_name = _ask_os(ns.os or "Windows")
    user = _ask_required("ssh user", ns.user or "")
    password = _ask("password (empty = key auth)", ns.password or "", secret=True)
    identity_file = ""
    if not password:
        identity_file = _ask("identity_file (optional; empty = default key chain)", ns.identity_file or "")
    port = ns.port if ns.port is not None else _ask_port()
    python = _ask("remote python path (optional, skips auto-detection)", ns.python or "")
    description = _ask("description (optional)", ns.description or "")
    entry = _build_entry(name, ip, os_name, user,
                         password=password, hostname=hostname, identity_file=identity_file,
                         port=port, python=python, description=description)
    auth = "password" if password else "key"
    print(f"\n  {name}: {user}@{ip}:{port} os={os_name} auth={auth}")
    if _ask(f"append to {_shorten(path)}?", "Y").lower() not in ("y", "yes"):
        print("[config] aborted, nothing written")
        return None
    return entry


def cmd_config_add(ns) -> int:
    """追加一台机器：无 --name/--ip 时走交互向导，否则纯旗帜模式。"""
    path = user_inventory_path()
    if ns.interactive or (not ns.name and not ns.ip):
        entry = _wizard(ns, path)
        if entry is None:
            return 1
    else:
        entry = _build_entry(
            ns.name or "", ns.ip or "", ns.os or "Windows", ns.user or "",
            password=ns.password or "", hostname=ns.hostname or "",
            identity_file=ns.identity_file or "", port=ns.port if ns.port is not None else 22,
            python=ns.python or "", description=ns.description or "",
        )
    _append_machine(path, entry)
    _report_added(path, entry["name"])
    return 0


def cmd_config_edit(ns) -> int:
    """用 $VISUAL/$EDITOR 打开用户级主清单（缺失时先按模板创建），退出后校验 JSON。"""
    path = user_inventory_path()
    if not path.exists():
        _write_secure(path, TEMPLATE_FILE.read_text(encoding="utf-8"))
        print(f"[config] created {_shorten(path)} from the bundled template")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    rc = subprocess.call(shlex.split(editor) + [str(path)])
    if rc:
        raise SystemExit(f"[config] editor exited with code {rc}")
    try:
        machines = _load_file(path)
    except Exception as e:  # noqa: BLE001 - 校验场景需要把原始解析错误带给用户
        print(f"[config] warning: {_shorten(path)} does not parse: {e}", file=sys.stderr)
        return 1
    print(f"[config] {_shorten(path)} ok — {len(machines)} machines")
    return 0


def cmd_config_chain(ns) -> int:
    """无子命令时的默认行为：诊断来源链（即 v0.2.x 的 bare `rrun config`）。"""
    infos = scan_sources()
    print("machines.json source chain (high -> low priority; same-name machines are")
    print("overridden by higher-priority sources):")
    raw_total = 0
    for i, info in enumerate(infos, 1):
        if not info.exists:
            state = "- missing"
        elif info.error:
            state = f"x read failed: {info.error}"
        else:
            state = f"ok, {info.machine_count} machines"
            raw_total += info.machine_count
        print(f"  [{i}] {info.label:<22} {info.path}  {state}")
    merged = load_machines()
    print(f"{len(merged)} machines total ({raw_total} across all sources before same-name overrides)")
    if not merged:
        print(f"hint: {EMPTY_INVENTORY_HINT}")
    return 0

# -*- coding: utf-8 -*-
"""机器注册表：多来源加载 machines.json，按优先级 merge，解析 name/ip/hostname → Machine。

来源链（高 → 低优先级；同名机器高优先级覆盖；同名/ip 冲突静默处理，不刷警告）：
    1. $RRUN_CONFIG（os.pathsep 分隔，可多个文件）
    2. $REMOTE_MACHINE_CONFIG（旧名，兼容）
    3. ./machines.json（当前工作目录）
    4. ~/.rrun/machines.json（用户级主清单）
    5. 旧位置兼容：Windows C:\\tools\\remote-machine\\machines.json
                   POSIX   ~/.remote-machine/machines.json

规则：
    - 所有来源可选；不存在 / 读失败的来源静默跳过（诊断见 scan_sources / CLI config 子命令）。
    - 每个文件先按 os 应用自己的 defaults 段，再按 name merge（先出现者胜）。
    - 显式传 path 给 load_machines 则退化为单文件模式（测试 / 钉死用）。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_MACHINES_JSON = "RRUN_CONFIG"
ENV_MACHINES_JSON_LEGACY = "REMOTE_MACHINE_CONFIG"

_KNOWN_KEYS = {"name", "ip", "user", "password", "os", "hostname", "description", "python"}


@dataclass
class Machine:
    name: str
    ip: str
    user: str
    password: str = field(repr=False)  # 凭据不进 repr/日志
    os: str = "Windows"                # 原样取值：Windows / Mac / Linux
    hostname: str = ""
    description: str = ""
    python: str = ""                   # 可选：显式指定远端 python 路径，跳过自动探测
    source: str = ""                   # 来源文件路径（多来源 merge 时记录出处）

    @property
    def is_windows(self) -> bool:
        return self.os.lower() == "windows"

    @property
    def default_lang(self) -> str:
        return "powershell" if self.is_windows else "bash"

    @property
    def target(self) -> str:
        return f"{self.user}@{self.ip}"

    def public_dict(self) -> dict:
        """脱敏视图（不含密码），用于 CLI 展示 / prompt 注入。"""
        return {
            "name": self.name, "ip": self.ip, "os": self.os, "user": self.user,
            "hostname": self.hostname, "description": self.description,
            "default_lang": self.default_lang, "source": self.source,
        }


@dataclass
class SourceInfo:
    """单个来源的扫描诊断（CLI config 子命令用）。"""
    label: str
    path: Path
    exists: bool = False
    machine_count: int = 0
    error: str = ""


def candidate_sources() -> "list[tuple[str, Path]]":
    """返回 (标签, 路径) 有序来源链（高→低优先级），按 resolved path 去重。"""
    cands: "list[tuple[str, Path]]" = []
    for env_name in (ENV_MACHINES_JSON, ENV_MACHINES_JSON_LEGACY):
        env = os.environ.get(env_name, "")
        env_paths = [x for x in env.split(os.pathsep) if x.strip()]
        for i, p in enumerate(env_paths):
            label = f"${env_name}" if i == 0 else f"${env_name}[{i}]"
            cands.append((label, Path(p).expanduser()))
    cands.append(("cwd", Path.cwd() / "machines.json"))
    cands.append(("user", Path.home() / ".rrun" / "machines.json"))
    if os.name == "nt":
        cands.append(("legacy", Path(r"C:\tools\remote-machine\machines.json")))
    else:
        cands.append(("legacy", Path.home() / ".remote-machine" / "machines.json"))
    seen: "set[str]" = set()
    uniq: "list[tuple[str, Path]]" = []
    for label, p in cands:
        try:
            key = os.path.normcase(str(p.resolve()))
        except OSError:
            key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            uniq.append((label, p))
    return uniq


def _load_file(path: Path) -> "list[Machine]":
    """加载单个文件：先按 os 应用该文件自己的 defaults 段，再打 source 标签。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    defaults = data.get("defaults") or {}
    result = []
    for m in data.get("machines", []):
        base = defaults.get(str(m.get("os", "")).lower()) or {}
        merged = {**base, **m}  # 机器自身字段优先于 defaults
        kwargs = {k: v for k, v in merged.items() if k in _KNOWN_KEYS}
        machine = Machine(**kwargs)
        machine.source = str(path)
        result.append(machine)
    return result


def load_machines(path: "str | os.PathLike | None" = None) -> "list[Machine]":
    """加载机器清单。显式传 path = 单文件模式；否则按来源链 merge（同名高优先级胜）。"""
    if path:
        return _load_file(Path(path))
    merged: "dict[str, Machine]" = {}
    for _label, p in candidate_sources():
        if not p.is_file():
            continue
        try:
            machines = _load_file(p)
        except Exception:  # noqa: BLE001 - 坏来源静默跳过，诊断见 scan_sources()
            continue
        for m in machines:
            if m.name and m.name not in merged:
                merged[m.name] = m
    return list(merged.values())


def scan_sources() -> "list[SourceInfo]":
    """扫描来源链，返回每个来源的诊断信息（存在性/机器数/错误）。"""
    infos = []
    for label, p in candidate_sources():
        info = SourceInfo(label=label, path=p)
        if p.is_file():
            info.exists = True
            try:
                info.machine_count = len(_load_file(p))
            except Exception as e:  # noqa: BLE001 - 诊断场景需要原始错误
                info.error = f"{type(e).__name__}: {e}"
        infos.append(info)
    return infos


def resolve_machine(host: str, path: "str | os.PathLike | None" = None) -> Machine:
    """按 name / ip / hostname 解析机器；未命中时报错并列出可用机器。"""
    machines = load_machines(path)
    for m in machines:
        if host and host in (m.name, m.ip, m.hostname):
            return m
    available = ", ".join(f"{m.name}({m.ip})" for m in machines) or "<空>"
    sources = ", ".join(str(p) for _lbl, p in candidate_sources() if p.is_file()) or "<无可用来源>"
    raise KeyError(f"机器 [{host}] 未找到。已扫描来源: {sources}。可用: {available}")

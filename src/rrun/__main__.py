# -*- coding: utf-8 -*-
"""rrun — 远程机器执行器（Run Remote）。

核心约定：脚本内容（可含中文，UTF-8）经 **stdin 管道**送远端解释器执行，
全程不走命令行参数，规避 bash->ssh->cmd 多层转码/转义问题。

用法:
    rrun exec <host> <script.py|.ps1|.sh> [ascii_args...]
    rrun exec <host> -c "Write-Output 中文"          # lang 按 OS 推断
    rrun exec pc_build temp/x.py --timeout 60
    rrun exec mac_mini --lang python temp/x.py
    rrun setup <host|--all> [--force]   # 初始化统一 python 环境（3.12 venv + 阿里源）
    rrun pip <host> -- list             # 在统一 venv 中执行 pip
    rrun machines                       # 列出可用机器（脱敏，含来源）
    rrun config                         # 查看 machines.json 来源链解析
    rrun close [<host>|--all]           # 关闭 ssh 复用连接

约定:
    - lang 推断：文件扩展名（.py/.ps1/.sh）优先，否则按机器 OS（Windows=powershell，其他=bash）。
    - 命令行参数只允许 ASCII，透传远端（python=sys.argv；bash=$@；powershell=$args）；
      中文/特殊字符一律写进脚本内容或 JSON 文件。
    - python 基线锁 3.12：探测命中统一 venv（优先）/standalone 基座/存量 3.12，
      并校验版本号；全灭则报错提示先跑 setup（exec 热路径不做隐式安装）。
    - 退出码：远端脚本退出码原样透传；255=ssh 传输层错误；124=本地超时。
    - 内联 -c 内容自动落盘 ~/.rrun/drops/ 留档，可复跑（RRUN_HOME 可改根目录）。
    - 审计：每次执行追加 ~/.rrun/log/remote-exec.jsonl。

退出码即远端退出码，可直接管道使用：远端 stdout→本机 stdout，stderr→stderr。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from .executor import RRUN_HOME, _check_ascii, _ssh_run, close_mux, run
from .registry import load_machines, resolve_machine, scan_sources
from .setup import setup_machine, venv_python_or_die

INLINE_DROP_DIR = RRUN_HOME / "drops"


def _parse_env(pairs) -> dict:
    env = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"[remote-exec] --env 格式应为 KEY=VAL: {p!r}")
        k, v = p.split("=", 1)
        env[k] = v
    return env


def _drop_inline(host: str, lang: str, content: str):
    """内联内容落盘 ~/.rrun/drops/ 留档（可复跑/可 edit 后重跑）。"""
    ext = {"python": ".py", "powershell": ".ps1", "bash": ".sh"}.get(lang, ".txt")
    INLINE_DROP_DIR.mkdir(parents=True, exist_ok=True)
    path = INLINE_DROP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{host}{ext}"
    path.write_text(content, encoding="utf-8")
    return path


def cmd_exec(ns) -> int:
    try:
        machine = resolve_machine(ns.host)
    except KeyError as e:
        raise SystemExit(f"[remote-exec] {e}")

    content = ns.content
    file = ns.script or ""
    lang = ns.lang or ""
    if content and file:
        raise SystemExit("[remote-exec] 脚本文件与 -c/--content 只能二选一")
    if not content and not file:
        raise SystemExit("[remote-exec] 缺少脚本：给文件路径或 -c/--content")

    # content 模式先确定 lang 再落盘（扩展名需要 lang）
    script_for_log = file
    if content:
        if not lang:
            lang = machine.default_lang
        dropped = _drop_inline(machine.name, lang, content)
        script_for_log = str(dropped)
        print(f"[remote-exec] 内联内容已落盘: {dropped}", file=sys.stderr)

    args = ns.args

    try:
        result = run(
            ns.host, lang=lang, file=file, content=content, args=args,
            workdir=ns.workdir or "", env=_parse_env(ns.env),
            timeout=ns.timeout, remote_python=(ns.python or ""), utf8=not ns.no_utf8,
            mux=not ns.no_mux,
        )
    except (ValueError, RuntimeError) as e:
        raise SystemExit(f"[remote-exec] {e}")

    if not ns.quiet:
        via = script_for_log or "<stdin>"
        extra = f" python={result.remote_python}({result.remote_python_version})" if result.lang == "python" else ""
        print(f"[remote-exec] host={result.host} ({result.ip}) lang={result.lang}{extra} "
              f"script={via} sha1={result.content_sha1}", file=sys.stderr)
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(result.stderr)
    sys.stderr.buffer.flush()

    if result.timed_out:
        print(f"[remote-exec] 超时（{ns.timeout}s），本地已终止", file=sys.stderr)
    elif result.transport_error:
        print(f"[remote-exec] ssh 传输层错误（连接失败/认证失败/掉线），exit=255", file=sys.stderr)
    if not ns.quiet:
        print(f"[remote-exec] exit={result.exit_code} 耗时 {result.duration:.1f}s", file=sys.stderr)
    return result.exit_code


def _short_source(path_str: str) -> str:
    """来源路径缩短显示：home → ~。"""
    if not path_str:
        return "-"
    s = str(path_str)
    home = str(Path.home())
    if s.startswith(home):
        return "~" + s[len(home):]
    return s


def cmd_machines(ns) -> int:
    machines = load_machines()
    if ns.json:
        print(json.dumps([m.public_dict() for m in machines], ensure_ascii=False, indent=2))
        return 0
    for m in machines:
        desc = f"  # {m.description}" if m.description else ""
        print(f"{m.name:<24} {m.ip:<16} {m.os:<8} {m.user:<12} {m.default_lang:<11} {_short_source(m.source)}{desc}")
    return 0


def cmd_config(ns) -> int:
    infos = scan_sources()
    print("machines.json 来源链（高 → 低优先级，同名机器高优先级覆盖）:")
    raw_total = 0
    for i, info in enumerate(infos, 1):
        if not info.exists:
            state = "- 不存在"
        elif info.error:
            state = f"✗ 读取失败: {info.error}"
        else:
            state = f"✓ {info.machine_count} 台"
            raw_total += info.machine_count
        print(f"  [{i}] {info.label:<22} {info.path}  {state}")
    merged = load_machines()
    print(f"合计 {len(merged)} 台（各来源原始共 {raw_total} 台，同名覆盖后 {len(merged)} 台）")
    return 0


def cmd_setup(ns) -> int:
    if ns.all:
        hosts = [m.name for m in load_machines()]
    elif ns.host:
        hosts = [ns.host]
    else:
        raise SystemExit("[setup] 需要指定 host 或 --all")
    results = []
    if len(hosts) == 1:
        print(f"[setup] {hosts[0]} 初始化中...", file=sys.stderr)
        results.append(setup_machine(hosts[0], force=ns.force))
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=ns.jobs) as pool:
            futs = {pool.submit(setup_machine, h, force=ns.force): h for h in hosts}
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                print(f"[setup] {r.host}: {'ok' if r.ok else 'FAIL'} ({r.duration:.0f}s)", file=sys.stderr)
    print(f"\n{'host':<24} {'结果':<6} {'python':<10} {'venv':<46} 备注")
    for r in sorted(results, key=lambda x: x.host):
        if r.ok:
            note = []
            if r.installed_standalone:
                note.append("新装standalone")
            if r.created_venv:
                note.append("新建venv")
            if not note:
                note.append("已存在，仅校验/补装依赖")
            print(f"{r.host:<24} {'ok':<6} {r.version:<10} {r.venv_python:<46} {'，'.join(note)}")
        else:
            print(f"{r.host:<24} {'FAIL':<6} {'':<10} {'':<46} {r.message[:80]}")
    return 0 if all(r.ok for r in results) else 1


def cmd_pip(ns) -> int:
    try:
        machine = resolve_machine(ns.host)
    except KeyError as e:
        raise SystemExit(f"[pip] {e}")
    args = list(ns.pargs or []) + list(getattr(ns, "args", []) or [])
    if not args:
        raise SystemExit("[pip] 缺少 pip 参数，例：rrun pip <host> -- list")
    try:
        _check_ascii(args, "pip 参数")
        py = venv_python_or_die(ns.host)
    except (ValueError, RuntimeError) as e:
        raise SystemExit(f"[pip] {e}")
    joined = " ".join(args)
    if machine.is_windows:
        remote_cmd = f'"{py}" -m pip {joined}'
    else:
        remote_cmd = f"{py} -m pip {joined}"
    t0 = time.time()
    rc, out, err, timed_out = _ssh_run(machine, remote_cmd, b"", ns.timeout, True)
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.buffer.flush()
    print(f"[pip] {machine.name} exit={rc} 耗时 {time.time() - t0:.1f}s", file=sys.stderr)
    return rc


def cmd_close(ns) -> int:
    if ns.all:
        hosts = [m.name for m in load_machines()]
    elif ns.host:
        hosts = [ns.host]
    else:
        raise SystemExit("[remote-exec] close 需要指定 host 或 --all")
    rc = 0
    for h in hosts:
        try:
            code, msg = close_mux(h)
        except Exception as e:  # noqa: BLE001 - close 尽量遍历完
            code, msg = 1, str(e)
        print(f"[remote-exec] close {h}: {'ok' if code == 0 else msg}")
        rc = rc or code
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="rrun",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="subcmd", required=True)

    ep = sub.add_parser("exec", help="在远端机器执行本地脚本/内联内容")
    ep.add_argument("host", help="机器 name/ip/hostname（见 machines 子命令）")
    ep.add_argument("script", nargs="?", help="本地脚本路径（.py/.ps1/.sh，UTF-8）")
    ep.add_argument("--lang", choices=["bash", "powershell", "python"],
                    help="远端解释语言（缺省：按扩展名，否则按机器 OS）")
    ep.add_argument("-c", "--content", help="内联脚本内容（自动落盘 ~/.rrun/drops/ 留档）")
    ep.add_argument("args", nargs="*",
                    help="脚本参数（仅 ASCII；建议用 -- 与选项分隔，选项可放任意位置）")
    ep.add_argument("--workdir", help="远端工作目录（bash/powershell 支持）")
    ep.add_argument("--env", action="append", metavar="KEY=VAL",
                    help="远端环境变量（可多次；仅 ASCII）")
    ep.add_argument("--timeout", type=float, help="本地超时秒数（超时 exit=124）")
    ep.add_argument("--python", dest="python", help="远端 python 路径（跳过自动探测）")
    ep.add_argument("--no-utf8", action="store_true", help="远端 python 不加 -X utf8")
    ep.add_argument("--no-mux", action="store_true", help="禁用 ssh ControlMaster 复用")
    ep.add_argument("-q", "--quiet", action="store_true", help="不打印 [remote-exec] 信息行")
    ep.set_defaults(func=cmd_exec)

    mp = sub.add_parser("machines", help="列出全部来源合并后的机器（脱敏，含来源）")
    mp.add_argument("--json", action="store_true")
    mp.set_defaults(func=cmd_machines)

    cf = sub.add_parser("config", help="查看 machines.json 来源链解析（哪些文件生效、各贡献几台）")
    cf.set_defaults(func=cmd_config)

    sp = sub.add_parser("setup", help="初始化远端统一 python 环境（3.12 venv + 阿里云源）")
    sp.add_argument("host", nargs="?", help="机器 name/ip；--all 表示全部")
    sp.add_argument("--all", action="store_true", help="对 machines.json 所有机器执行")
    sp.add_argument("--force", action="store_true", help="重建 venv（不动已装的 python 本体）")
    sp.add_argument("--jobs", type=int, default=6, help="--all 时的并发数（默认 6）")
    sp.set_defaults(func=cmd_setup)

    pp = sub.add_parser("pip", help="在远端统一 venv 中执行 pip（ad-hoc 装包）")
    pp.add_argument("host", help="机器 name/ip")
    pp.add_argument("pargs", nargs="*", help="pip 参数；含 - 开头选项时放 -- 之后")
    pp.add_argument("--timeout", type=float, default=300.0, help="本地超时秒数（默认 300）")
    pp.set_defaults(func=cmd_pip)

    cp = sub.add_parser("close", help="关闭 ssh ControlMaster 复用连接")
    cp.add_argument("host", nargs="?", help="机器 name/ip；省略时需 --all")
    cp.add_argument("--all", action="store_true", help="关闭所有机器的复用连接")
    cp.set_defaults(func=cmd_close)

    # argparse 对 -- 的处理与子解析器/位置参数组合有 quirk，手动切分更可靠：
    # 第一个 -- 之后的全部内容原样作为脚本参数
    argv = sys.argv[1:]
    passthrough: "list[str] | None" = None
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    ns = ap.parse_args(argv)
    if passthrough is not None:
        ns.args = list(getattr(ns, "args", []) or []) + passthrough
    sys.exit(ns.func(ns))


if __name__ == "__main__":
    main()

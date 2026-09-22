# -*- coding: utf-8 -*-
"""rrun CLI — run local scripts on remote machines over SSH stdin pipes.

Core contract: script content (UTF-8, CJK welcome) is piped to the remote
interpreter via stdin, never via command-line arguments — sidestepping the
bash -> ssh -> cmd quoting/encoding gauntlet.

Usage:
    rrun exec <host> <script.py|.ps1|.sh> [ascii_args...]
    rrun exec <host> -c "Write-Output hello"        # lang inferred from remote OS
    rrun exec pc_build temp/x.py --timeout 60
    rrun exec mac_mini --lang python temp/x.py
    rrun setup <host|--all> [--force]   # provision the unified python env (3.12 venv)
    rrun pip <host> -- list             # run pip in the unified venv
    rrun machines                       # list machines (redacted, with source)
    rrun config                         # diagnose the machines.json source chain
    rrun close [<host>|--all]           # close ssh multiplexed connections

Conventions:
    - Language inference: file extension (.py/.ps1/.sh) first, then remote OS
      (Windows=powershell, others=bash).
    - Command-line args are ASCII-only, passed through remotely
      (python=sys.argv; bash=$@; powershell=$args); put CJK/special characters
      in the script content or a JSON file instead.
    - Remote python is pinned to 3.12: probes the unified venv (preferred),
      the standalone base, then existing installs; all missing -> run `rrun setup`
      first (exec never installs implicitly on the hot path).
    - Exit codes: the remote exit code passes through unchanged;
      255=ssh transport error; 124=local timeout.
    - Inline -c content is archived under ~/.rrun/drops/ for replay.
    - Audit: every execution appends to ~/.rrun/log/remote-exec.jsonl.

Remote stdout -> local stdout, stderr -> stderr; the exit code is the remote one.
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
            raise SystemExit(f"[remote-exec] --env expects KEY=VAL, got: {p!r}")
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
        raise SystemExit("[remote-exec] give either a script file or -c/--content, not both")
    if not content and not file:
        raise SystemExit("[remote-exec] nothing to run: pass a script file or -c/--content")

    # content 模式先确定 lang 再落盘（扩展名需要 lang）
    script_for_log = file
    if content:
        if not lang:
            lang = machine.default_lang
        dropped = _drop_inline(machine.name, lang, content)
        script_for_log = str(dropped)
        print(f"[remote-exec] inline content archived to {dropped}", file=sys.stderr)

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
        print(f"[remote-exec] local timeout ({ns.timeout}s), ssh client killed", file=sys.stderr)
    elif result.transport_error:
        print(f"[remote-exec] ssh transport error (unreachable / auth failure / dropped), exit=255", file=sys.stderr)
    if not ns.quiet:
        print(f"[remote-exec] exit={result.exit_code} took {result.duration:.1f}s", file=sys.stderr)
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
    return 0


def cmd_setup(ns) -> int:
    if ns.all:
        hosts = [m.name for m in load_machines()]
    elif ns.host:
        hosts = [ns.host]
    else:
        raise SystemExit("[setup] specify a host or --all")
    results = []
    if len(hosts) == 1:
        print(f"[setup] provisioning {hosts[0]} ...", file=sys.stderr)
        results.append(setup_machine(hosts[0], force=ns.force))
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=ns.jobs) as pool:
            futs = {pool.submit(setup_machine, h, force=ns.force): h for h in hosts}
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                print(f"[setup] {r.host}: {'ok' if r.ok else 'FAIL'} ({r.duration:.0f}s)", file=sys.stderr)
    print(f"\n{'host':<24} {'result':<6} {'python':<10} {'venv':<46} note")
    for r in sorted(results, key=lambda x: x.host):
        if r.ok:
            note = []
            if r.installed_standalone:
                note.append("standalone installed")
            if r.created_venv:
                note.append("venv created")
            if not note:
                note.append("already present, verified/topped-up deps")
            print(f"{r.host:<24} {'ok':<6} {r.version:<10} {r.venv_python:<46} {', '.join(note)}")
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
        raise SystemExit("[pip] missing pip args, e.g.: rrun pip <host> -- list")
    try:
        _check_ascii(args, "pip args")
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
    print(f"[pip] {machine.name} exit={rc} took {time.time() - t0:.1f}s", file=sys.stderr)
    return rc


def cmd_close(ns) -> int:
    if ns.all:
        hosts = [m.name for m in load_machines()]
    elif ns.host:
        hosts = [ns.host]
    else:
        raise SystemExit("[remote-exec] close needs a host or --all")
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

    ep = sub.add_parser("exec", help="execute a local script / inline content on a remote machine")
    ep.add_argument("host", help="machine name/ip/hostname (see the machines subcommand)")
    ep.add_argument("script", nargs="?", help="local script path (.py/.ps1/.sh, UTF-8)")
    ep.add_argument("--lang", choices=["bash", "powershell", "python"],
                    help="remote interpreter language (default: by extension, else by remote OS)")
    ep.add_argument("-c", "--content", help="inline script content (archived to ~/.rrun/drops/)")
    ep.add_argument("args", nargs="*",
                    help="script arguments (ASCII only; use -- to separate from options)")
    ep.add_argument("--workdir", help="remote working directory (bash/powershell only)")
    ep.add_argument("--env", action="append", metavar="KEY=VAL",
                    help="remote environment variable (repeatable; ASCII only)")
    ep.add_argument("--timeout", type=float, help="local timeout in seconds (exit=124 on expiry)")
    ep.add_argument("--python", dest="python", help="remote python path (skips auto-detection)")
    ep.add_argument("--no-utf8", action="store_true", help="do not pass -X utf8 to remote python")
    ep.add_argument("--no-mux", action="store_true", help="disable ssh ControlMaster multiplexing")
    ep.add_argument("-q", "--quiet", action="store_true", help="suppress [remote-exec] info lines")
    ep.set_defaults(func=cmd_exec)

    mp = sub.add_parser("machines", help="list machines merged from all sources (redacted, with source)")
    mp.add_argument("--json", action="store_true")
    mp.set_defaults(func=cmd_machines)

    cf = sub.add_parser("config", help="show the machines.json source chain (which files apply, how many machines each)")
    cf.set_defaults(func=cmd_config)

    sp = sub.add_parser("setup", help="provision the unified remote python environment (3.12 venv)")
    sp.add_argument("host", nargs="?", help="machine name/ip; use --all for every machine")
    sp.add_argument("--all", action="store_true", help="run against all machines in machines.json")
    sp.add_argument("--force", action="store_true", help="recreate the venv (keeps the python base)")
    sp.add_argument("--jobs", type=int, default=6, help="concurrency for --all (default 6)")
    sp.set_defaults(func=cmd_setup)

    pp = sub.add_parser("pip", help="run pip inside the remote unified venv (ad-hoc installs)")
    pp.add_argument("host", help="machine name/ip")
    pp.add_argument("pargs", nargs="*", help="pip arguments; put options starting with - after --")
    pp.add_argument("--timeout", type=float, default=300.0, help="local timeout in seconds (default 300)")
    pp.set_defaults(func=cmd_pip)

    cp = sub.add_parser("close", help="close ssh ControlMaster multiplexed connections")
    cp.add_argument("host", nargs="?", help="machine name/ip; omit with --all")
    cp.add_argument("--all", action="store_true", help="close connections for all machines")
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

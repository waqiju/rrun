# rrun

Run local scripts on remote machines over SSH — **via stdin pipes, never via command-line arguments** — so quoting, escaping, and CJK/UTF-8 encoding survive the `bash → ssh → cmd/powershell` journey intact.

```bash
rrun exec my-win-box ./deploy.ps1          # powershell (inferred from .ps1)
rrun exec my-mac ./build.sh                # bash
rrun exec my-win-box ./report.py           # python (remote 3.12, -X utf8)
rrun exec my-win-box -c "Get-Date"         # inline content (archived to ~/.rrun/drops/)
rrun exec my-mac ./etl.py -- arg1 arg2     # ASCII args pass-through
```

Remote stdout → your stdout, remote stderr → your stderr, and the **exit code is passed through** unchanged (255 = ssh transport failure, 124 = local `--timeout`). Pipes, `&&`, and CI integration just work.

## Why

Running commands on remote Windows machines from a POSIX shell is a minefield: sshd lands you in `cmd` with a GBK codepage, quotes and `$` get eaten by one of three shells along the way, and any non-ASCII argument gets mojibake'd. rrun's rules:

- Script content (UTF-8, Chinese welcome) is always piped through **stdin** — never the command line.
- For PowerShell, the payload is base64-wrapped into a **single-line pure-ASCII wrapper** (`powershell -Command -` executes stdin line-by-line, multi-line fails silently), decoded and invoked as a ScriptBlock remotely.
- Command-line arguments are restricted to ASCII and passed through safely (`sys.argv` / `$@` / `$args`). Put anything fancier in the script or a JSON file.

See [docs/remote-exec-conventions.md](docs/remote-exec-conventions.md) (中文) for the full set of hard-won conventions.

## Install

```bash
pipx install rrun-cli    # recommended: isolated global CLI (provides the `rrun` command)
# or: pip install rrun-cli
```

> The PyPI distribution is named `rrun-cli` (plain `rrun` is on PyPI's prohibited-name list
> as it's confusable with `run`), but the installed command is `rrun` — plus the legacy alias
> `remote-machine`.

Both `rrun` and the legacy alias `remote-machine` are installed.

**Control machine requirements:** `ssh` + `sshpass` (`sudo apt install sshpass`), Python ≥ 3.10. Zero third-party Python dependencies.
**Remote machines:** OpenSSH server. Windows remotes execute via PowerShell; Mac/Linux via bash.

## Configuration: machines.json

Credentials live in a local `machines.json` (never committed — it's a local file, `chmod 600` recommended). See [machines.template.json](machines.template.json):

```json
{
  "defaults": { "windows": { "os": "Windows" } },
  "machines": [
    { "name": "my-win-box", "ip": "192.168.1.10", "os": "Windows",
      "user": "admin", "password": "secret" }
  ]
}
```

Sources are merged by machine name, highest priority first (all optional, failures skipped silently):

1. `$RRUN_CONFIG` (os.pathsep-separated, multiple files allowed)
2. `$REMOTE_MACHINE_CONFIG` (legacy name, still honored)
3. `./machines.json` (current working directory)
4. `~/.rrun/machines.json`
5. `~/.rrun/machines.d/*.json` (sorted by filename — point each inventory at its own file/symlink)
6. Legacy: `~/.remote-machine/machines.json` (POSIX) / `C:\tools\remote-machine\machines.json` (Windows)

Per-file `defaults` sections (keyed by `os`, lowercased) are applied before merging. Inspect the chain with `rrun config`; list machines (redacted) with `rrun machines`.

## Subcommands

| Command | Purpose |
|---|---|
| `rrun exec <host> <script\|-c ...>` | Execute a local script / inline content remotely |
| `rrun machines [--json]` | List merged machines (redacted, with source) |
| `rrun config` | Diagnose the machines.json source chain |
| `rrun setup <host\|--all> [--force]` | Provision the unified remote Python 3.12 venv (idempotent) |
| `rrun pip <host> -- list` | Run pip inside the remote unified venv |
| `rrun close [<host>\|--all]` | Close ssh ControlMaster multiplexed connections |

Useful `exec` flags: `--lang bash|powershell|python`, `--workdir`, `--env K=V`, `--timeout`, `--python <path>` (skip detection), `--no-mux`, `-q`.

## Remote Python: unified 3.12 environment

For `python` scripts, rrun requires a **3.12.x** interpreter on the remote, probed in order:

1. Unified venv: `C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`
2. Standalone base: `...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`
3. Existing installs: `C:\Python\Python312\python.exe` / `python3.12` on PATH

If none match, run `rrun setup <host>`. It is idempotent and non-destructive: if the machine lacks Python 3.12, a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) tarball is downloaded to the local cache (`~/.cache/rrun/`) and streamed over ssh stdin — no registry, no PATH changes, no admin rights, and the remote never touches GitHub. The venv gets an Alibaba Cloud pip mirror in its own `pip.ini`/`pip.conf` (global config untouched) plus the standard packages from `remote-requirements.txt` (override with `~/.rrun/remote-requirements.txt`).

## Auditing & state

- Every execution appends a JSON line to `~/.rrun/log/remote-exec.jsonl` (host, lang, script sha1, exit code, duration — never passwords; env vars recorded as keys only).
- Inline `-c` payloads are archived under `~/.rrun/drops/` for replay.
- Root directory overridable via `$RRUN_HOME`.

---

## 中文简介

rrun 解决「从 POSIX shell 在远程机器（尤其 Windows）执行脚本」的转码/转义地狱：
脚本内容一律走 **stdin 管道**（UTF-8，中文随便用），命令行参数只放行 ASCII；
PowerShell 载荷编码为单行纯 ASCII wrapper，退出码原样透传。

- 安装：`pipx install rrun-cli`（控制端需 `ssh` + `sshpass`；装好后命令是 `rrun`）
- 机器清单 `machines.json` 来源链：`$RRUN_CONFIG` → `./machines.json` → `~/.rrun/machines.json` → `~/.rrun/machines.d/*.json` → 旧位置兼容
- 远程 python 基线锁 3.12，`rrun setup <host>` 一键幂等初始化统一 venv（standalone 基座经 ssh 推流安装，远端无需访问 GitHub，pip 走阿里云镜像）
- 更多踩坑约定见 [docs/remote-exec-conventions.md](docs/remote-exec-conventions.md)

## License

MIT

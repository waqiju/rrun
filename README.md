# rrun

[![PyPI](https://img.shields.io/pypi/v/rrun-cli)](https://pypi.org/project/rrun-cli/)
[![Python](https://img.shields.io/pypi/pyversions/rrun-cli)](https://pypi.org/project/rrun-cli/)
[![License](https://img.shields.io/pypi/l/rrun-cli)](https://github.com/waqiju/rrun/blob/main/LICENSE)
[![publish](https://github.com/waqiju/rrun/actions/workflows/publish.yml/badge.svg)](https://github.com/waqiju/rrun/actions/workflows/publish.yml)

**[中文文档](README.zh-CN.md)**

Run local scripts on remote machines over SSH — **via stdin pipes, never via command-line arguments** — so quoting, escaping, and CJK/UTF-8 encoding survive the `bash → ssh → cmd/powershell` journey intact.

- python / powershell / bash, inferred from the file extension or the remote OS
- Remote stdout/stderr stream back verbatim; the **exit code passes through**, so pipes and CI just work
- ssh ControlMaster multiplexing: ~0.5s on first contact, ~0.02s afterwards
- One-command provisioning of a unified **remote Python 3.12 venv** (`rrun setup`) — the remote never touches the internet
- Zero third-party Python dependencies

## Install

```bash
pipx install rrun-cli    # provides the `rrun` command (plus a `remote-machine` alias)
# or: pip install rrun-cli
```

> The PyPI distribution is named `rrun-cli` (`rrun` sits on PyPI's prohibited-name list as it
> is confusable with `run`); the installed command is plain `rrun`.

**Control machine:** Linux or macOS (Windows works via WSL), Python ≥ 3.10, plus `ssh` and `sshpass`:

```bash
sudo apt install sshpass                        # Debian/Ubuntu
brew install hudochenkov/sshpass/sshpass        # macOS (sshpass is not in homebrew-core)
```

**Remote machines:** an OpenSSH server with password auth enabled. Windows remotes execute via PowerShell, Mac/Linux remotes via bash.

## Quickstart

Create `~/.rrun/machines.json` (full format in [Configuration](#configuration-machinesjson)):

```json
{
  "machines": [
    { "name": "my-win-box", "ip": "192.168.1.10", "os": "Windows", "user": "admin", "password": "secret" }
  ]
}
```

Then:

```bash
rrun machines                            # verify the inventory is picked up (redacted)
printf 'Write-Output "hello 中文"\n' > demo.ps1
rrun exec my-win-box demo.ps1            # powershell, inferred from .ps1
```

That's the whole loop: write a script locally → it runs remotely → you get its output and exit code.

## Why

Running commands on remote Windows machines from a POSIX shell is a minefield: sshd lands you in `cmd` with a GBK codepage, quotes and `$` get eaten by one of the three shells along the way, and any non-ASCII argument gets mojibake'd. rrun's rules:

- Script content (UTF-8, CJK welcome) always travels through **stdin** — never the command line.
- PowerShell payloads are base64-wrapped into a **single-line pure-ASCII wrapper** (`powershell -Command -` executes stdin line-by-line; multi-line input fails silently), then decoded and invoked as a ScriptBlock remotely.
- Command-line arguments are restricted to ASCII and passed through safely (`sys.argv` / `$@` / `$args`). Put anything fancier in the script itself or in a JSON file.

The full set of hard-won conventions and internals: [docs/remote-exec-conventions.md](docs/remote-exec-conventions.md) ([中文](docs/remote-exec-conventions.zh-CN.md)).

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

### Exit codes

| Code | Meaning |
|---|---|
| `0`–`254` | The remote script's own exit code, passed through unchanged |
| `255` | ssh transport failure (unreachable / auth failure / connection dropped) |
| `124` | local `--timeout` expired; the local ssh client was killed |

## Configuration: machines.json

Credentials live in local `machines.json` files — see [machines.template.json](machines.template.json):

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

## Remote Python: unified 3.12 environment

For `python` scripts, rrun requires a **3.12.x** interpreter on the remote, probed in order:

1. Unified venv: `C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`
2. Standalone base: `...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`
3. Existing installs: `C:\Python\Python312\python.exe` / `python3.12` on PATH

If none match, run `rrun setup <host>`. It is idempotent and non-destructive: if the machine lacks Python 3.12, a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) tarball is downloaded to the local cache (`~/.cache/rrun/`) and streamed over ssh stdin — no registry, no PATH changes, no admin rights, and the remote never touches GitHub. The venv gets its own `pip.ini`/`pip.conf` (global pip config untouched) plus the standard packages from the bundled `remote-requirements.txt` (override with `~/.rrun/remote-requirements.txt`).

## Security

- `machines.json` stores **plaintext passwords**. Keep it local, `chmod 600`, never commit it.
- rrun authenticates with passwords via `sshpass`; **key-based authentication is not supported yet** (on the roadmap).
- The audit log never records passwords, and `--env` values are logged as keys only.

## Auditing & state

- Every execution appends a JSON line to `~/.rrun/log/remote-exec.jsonl` (host, lang, script sha1, exit code, duration).
- Inline `-c` payloads are archived under `~/.rrun/drops/` for replay.
- The state root defaults to `~/.rrun/` and is overridable via `$RRUN_HOME`.

## Upgrading / uninstalling

```bash
pipx upgrade rrun-cli
pipx uninstall rrun-cli
```

## Contributing

Issues and PRs are welcome. Development setup: clone → `python3.12 -m venv .venv && .venv/bin/pip install -e .` → hack → smoke-test with `rrun machines`. Releases are cut by pushing a `vX.Y.Z` tag; CI builds and publishes to PyPI via trusted publishing. See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)

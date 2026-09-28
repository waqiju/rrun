# rrun

[![PyPI](https://img.shields.io/pypi/v/rrun-cli)](https://pypi.org/project/rrun-cli/)
[![Python](https://img.shields.io/pypi/pyversions/rrun-cli)](https://pypi.org/project/rrun-cli/)
[![License](https://img.shields.io/pypi/l/rrun-cli)](https://github.com/waqiju/rrun/blob/main/LICENSE)
[![publish](https://github.com/waqiju/rrun/actions/workflows/publish.yml/badge.svg)](https://github.com/waqiju/rrun/actions/workflows/publish.yml)

**[中文文档](README.zh-CN.md)**

**Agent-friendly remote execution over SSH.** rrun runs your local scripts on remote machines — Windows, macOS, or Linux — via stdin pipes, never via command-line arguments. Quoting, escaping, and CJK/UTF-8 content arrive intact; credentials stay in a local `machines.json` (mode 600) and never enter your agent's context.

- Built for AI agents and humans alike: write a script file, `rrun exec host file`, get verbatim output plus the remote exit code — no quoting puzzles, no passwords in prompts
- python / powershell / bash, inferred from the file extension or the remote OS
- ssh ControlMaster multiplexing: ~0.5s on first contact, ~0.02s afterwards
- One-command provisioning of a unified **remote Python 3.12 venv** (`rrun setup`) — the remote never touches the internet
- Zero third-party Python dependencies

## What it looks like

```console
$ cat hello.py
print("你好，Windows")

$ rrun exec pc_build hello.py      # CJK rides the stdin pipe, arrives intact
你好，Windows

$ rrun push pc_build dist.zip C:/builds/
[push] pc_build: dist.zip -> C:/builds/ (12.4 MB, 3.2s, sha256 verified)
```

No agent to configure on the remote, no quoting puzzles, no passwords on the command line — a host name is all you need.

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

Scaffold the machine inventory, fill in your machines, verify:

```bash
rrun config init                         # create ~/.rrun/machines.json from the bundled template (mode 600)
rrun config edit                         # open it in $EDITOR — replace the my-* example entries
# or: rrun config add                    # interactive wizard that appends one machine

rrun machines                            # verify the inventory is picked up (redacted)
printf 'Write-Output "hello 中文"\n' > demo.ps1
rrun exec my-win-box demo.ps1            # powershell, inferred from .ps1
```

That's the whole loop: write a script locally → it runs remotely → you get its output and exit code.

## Use with AI agents

rrun ships an [Agent Skills](https://agentskills.io/specification) skill —
[skills/rrun](skills/rrun) ([中文](skills/rrun/SKILL.zh-CN.md)) — that teaches any coding
agent the whole workflow: bootstrap checks, credential discipline, exec/push/pull patterns,
exit codes, and the CJK/quoting rules.

Tell your agent:

> Install the rrun skill from https://github.com/waqiju/rrun

or run the cross-agent installer yourself (Claude Code, Codex, Cursor, pi, and 75+ more):

```bash
npx skills add waqiju/rrun          # detects your agents; add -g for user-level
npx skills add waqiju/rrun --list   # preview without installing
```

pi users can alternatively install this repo as a pi package:
`pi install git:github.com/waqiju/rrun`. Manual fallback: copy `skills/rrun/` into your
agent's skills directory (e.g. `~/.agents/skills/rrun/`).

With the skill installed, agents also know to keep your passwords out of the conversation —
when a machine is missing they will ask *you* to run `rrun config add` in your own terminal
instead of asking for the password.

## Why

Running commands on remote Windows machines from a POSIX shell is a minefield: sshd lands you in `cmd` with a GBK codepage, quotes and `$` get eaten by one of the three shells along the way, and any non-ASCII argument gets mojibake'd. rrun's rules:

- Script content (UTF-8, CJK welcome) always travels through **stdin** — never the command line.
- PowerShell payloads are base64-wrapped into a **single-line pure-ASCII wrapper** (`powershell -Command -` executes stdin line-by-line; multi-line input fails silently), then decoded and invoked as a ScriptBlock remotely.
- Command-line arguments are restricted to ASCII and passed through safely (`sys.argv` / `$@` / `$args`). Put anything fancier in the script itself or in a JSON file.

Agents get a second dividend: no credential ever has to appear in a conversation, prompt, or
log — the agent calls `rrun exec my-host ...` and the password stays inside the local
`machines.json`. And because "write a file, then exec it" is deterministic, the agent burns
zero turns on quoting debugging.

The full set of hard-won conventions and internals: [docs/remote-exec-conventions.md](docs/remote-exec-conventions.md) ([中文](docs/remote-exec-conventions.zh-CN.md)).

## Is rrun for you?

**Good fit:**

- A handful to a couple dozen machines you drive by hand or with an agent — build boxes, a homelab, cloud VMs
- A mixed fleet where some remotes are Windows (PowerShell) and others are macOS/Linux (bash)
- You want a coding agent to operate those machines without ever seeing a password
- You've been burned by ssh quoting/escaping/CJK-encoding one time too many

**Not the right tool:**

- Fleet-scale configuration management (hundreds of machines, desired state, idempotent playbooks) → use Ansible
- Incremental sync or resumable transfers → use rsync
- A pure-POSIX one-off command → plain `ssh host cmd` is fine
- A long-lived MCP server is a hard requirement → rrun is deliberately a stateless CLI plus an agent skill, so it works with any agent, MCP-capable or not

## Subcommands

| Command | Purpose |
|---|---|
| `rrun exec <host> <script\|-c ...>` | Execute a local script / inline content remotely |
| `rrun push <host> <local> <remote>` | Copy a file to the remote (atomic temp+rename, sha256-verified; `-` = stdin; parents auto-created) |
| `rrun pull <host> <remote> <local>` | Copy a file from the remote (atomic, sha256-verified; `-` = stdout) |
| `rrun machines [--json]` | List merged machines (redacted, with source) |
| `rrun config [init\|add\|edit]` | Manage the user inventory (`init` scaffolds from the bundled template, `add` appends via wizard/flags, `edit` opens `$EDITOR`); bare `config` diagnoses the source chain |
| `rrun setup <host\|--all> [--force]` | Provision the unified remote Python 3.12 venv (idempotent) |
| `rrun pip <host> -- list` | Run pip inside the remote unified venv |
| `rrun doctor <host\|--all>` | Health-check ssh + auth + remote python (`--all` opens real connections to every machine) |
| `rrun close [<host>\|--all]` | Close ssh ControlMaster multiplexed connections |

Useful `exec` flags: `--lang bash|powershell|python`, `--workdir`/`--env K=V` (not for Windows+python), `--timeout`, `--python <path>` (skip detection), `--no-mux`, `-q`.

`push`/`pull` transfer single files (directories are rejected — tar over the pipe is the
planned extension). A trailing `/` or an existing directory keeps the source basename; `~`
expands on the remote. Design rationale and the OpenSSH-Windows stdio pitfalls behind the
Windows transport: [docs/push-pull-design.md](docs/push-pull-design.md).

### Exit codes

| Code | Meaning |
|---|---|
| `0`–`254` | The remote script's own exit code, passed through unchanged |
| `3` | `push`/`pull` integrity check failed (sha256 mismatch; temp file deleted, nothing renamed) |
| `255` | ssh transport failure (unreachable / auth failure / connection dropped) |
| `124` | local `--timeout` expired; the local ssh client was killed |

## Configuration: machines.json

Credentials live in local `machines.json` files. `rrun config init` writes the bundled template to `~/.rrun/machines.json` (also browsable at [src/rrun/machines.template.json](src/rrun/machines.template.json)):

```json
{
  "defaults": { "windows": { "os": "Windows" } },
  "machines": [
    { "name": "my-win-box", "ip": "192.168.1.10", "os": "Windows",
      "user": "admin", "password": "secret" },
    { "name": "cloud-vm", "ip": "1.2.3.4", "port": 2222, "os": "Linux",
      "user": "root", "identity_file": "~/.ssh/id_ed25519" }
  ]
}
```

Per-machine fields: `name`/`ip`/`user` are required; `password` (plaintext, via sshpass) or leave it empty for **key-based auth** (`identity_file` optional — the default ssh key chain / agent / `~/.ssh/config` applies, with `BatchMode=yes` so a missing key fails fast instead of prompting); `port` (default `22`); `os` (`Windows` / `Mac` / `Linux`); optional `hostname` (also resolvable), `description`, `python` (explicit remote interpreter, skips auto-detection).

Sources are merged by machine name, highest priority first (all optional, failures skipped silently):

1. `$RRUN_CONFIG` (os.pathsep-separated, multiple files allowed)
2. `$REMOTE_MACHINE_CONFIG` (legacy name, still honored)
3. `./machines.json` (current working directory)
4. `~/.rrun/machines.json`
5. `~/.rrun/machines.d/*.json` (sorted by filename — point each inventory at its own file/symlink; `rrun config init` creates this directory with a README inside)
6. Legacy: `~/.remote-machine/machines.json` (POSIX) / `C:\tools\remote-machine\machines.json` (Windows)

Per-file `defaults` sections (keyed by `os`, lowercased) are applied before merging. Inspect the chain with `rrun config`; list machines (redacted) with `rrun machines`.

## Remote Python: unified 3.12 environment

For `python` scripts, rrun requires a **3.12.x** interpreter on the remote, probed in order:

1. Unified venv: `C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`
2. Standalone base: `...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`
3. Existing installs: `C:\Python\Python312\python.exe` / `python3.12` on PATH

If none match, run `rrun setup <host>`. It is idempotent and non-destructive: if the machine lacks a **venv-capable** Python 3.12 — none installed, or the interpreter cannot create venvs (Debian/Ubuntu split `ensurepip` into the separate `python3.12-venv` package) — a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) tarball (Windows/macOS/Linux) is downloaded to the local cache (`~/.cache/rrun/`) and streamed over ssh stdin — no registry, no PATH changes, no admin rights, and the remote never touches GitHub. A half-created venv left by an interrupted earlier setup (python runs but pip is missing) is detected and recreated automatically. `rrun doctor <host>` reports all of these states before you provision. The venv gets its own `pip.ini`/`pip.conf` (global pip config untouched) plus the standard packages from the bundled `remote-requirements.txt` (override with `~/.rrun/remote-requirements.txt`).

## Security

- `machines.json` stores **plaintext passwords**. Keep it local and never commit it — `rrun config init`/`add` already write the user inventory with mode `600` — or leave `password` empty and use key-based auth instead.
- Key-based auth runs ssh with `BatchMode=yes` (no interactive prompts; a missing/unauthorized key fails fast instead of eating the script from stdin).
- The audit log never records passwords, and `--env` values are logged as keys only.
- Agent workflows: agents only ever invoke `rrun <host> ...`, so passwords never enter a
  conversation, prompt, or log. The bundled skill explicitly forbids reading credential
  files — see [skills/rrun/SKILL.md](skills/rrun/SKILL.md).

## FAQ

**Is storing plaintext passwords in machines.json safe?**
It is a deliberate trade-off, stated honestly: passwords sit in a local file written with mode `600`, and never appear in scripts, shell history, command lines, or agent conversations — the audit log doesn't record them either. If you'd rather not store passwords at all, leave `password` empty and rrun uses your ssh key chain (`identity_file`, agent, `~/.ssh/config`) with `BatchMode=yes`. The bundled agent skill explicitly forbids agents from reading credential files.

**Can I run rrun *from* a Windows machine?**
Windows **remotes** are first-class (PowerShell execution, chunked file transfer). The **control side** needs POSIX: password auth relies on `sshpass` and connection reuse on ssh ControlMaster, neither of which exists natively on Windows. On a Windows daily driver, install rrun inside **WSL** and control everything from there.

**Why is remote Python pinned to 3.12?**
One known-good baseline beats "whatever happens to be installed". `rrun setup` provisions an isolated 3.12 venv per machine without touching the system Python, so every script targets exactly one interpreter.

**Can it transfer directories or large files?**
`push`/`pull` move single files, atomically, with sha256 verification — tens of MB are routine. Directories are rejected by design; a tar-over-the-pipe mode is the planned extension. For heavy sync jobs, use rsync.

**How fast is it?**
The first connection to a host costs ~0.5 s; afterwards ssh ControlMaster multiplexing makes each invocation ~0.02 s. There is no agent or daemon on either side — just ssh.

## Auditing & state

- Every execution appends a JSON line to `~/.rrun/log/remote-exec.jsonl` (host, lang, script sha1, exit code, duration).
- Inline `-c` payloads are archived under `~/.rrun/drops/` for replay.
- The state root defaults to `~/.rrun/` and is overridable via `$RRUN_HOME`.

## Upgrading / uninstalling

```bash
pipx upgrade rrun-cli
pipx uninstall rrun-cli
```

## Roadmap

rrun is feature-complete for its current scope. The next agreed iteration — a unified
secret store covering machine *and* service credentials, designed so secrets never enter
agent context — is written down in [ROADMAP.md](ROADMAP.md) ([中文](ROADMAP.zh-CN.md)) and
awaits a future release window.

## Contributing

Issues and PRs are welcome. Development setup: clone → `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"` → hack → `pytest` + `ruff check .` → smoke-test with `rrun machines`. CI runs unit tests, ruff, and an end-to-end suite against a real sshd container. Releases are cut by pushing a `vX.Y.Z` tag; CI builds and publishes to PyPI via trusted publishing. The version lives in three places: `pyproject.toml`, the fallback in `src/rrun/__init__.py`, and `package.json` (agent-skill metadata) — bump all three. See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)

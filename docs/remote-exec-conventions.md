# Remote execution conventions & rrun internals

**[中文](remote-exec-conventions.zh-CN.md)**

Hard-won rules for running things on remote machines — especially Windows — over SSH, plus how
rrun implements them. For day-to-day usage see the [README](../README.md).

**Core principle: content containing CJK or special characters never travels via the command
line — it goes through files or stdin pipes.**

SSH'ing into Windows lands you in `cmd` with a GBK/cp936 codepage, and any argument passes
through up to three shells (`bash → ssh → cmd/powershell`), each with its own quoting,
escaping, and encoding rules. Quotes, `$`, `;`, `%VAR%` all have their own traps.

## The rules

| Rule | Do | Don't (all of these bit us) |
|---|---|---|
| CJK arguments | Never via command line — write them into a py/bat/json file (UTF-8) and execute that | `tool.exe --branch "【Testbed】Main->Dev"` gets mangled by cmd transcoding |
| Remote Python | `python -X utf8 -`, reading the script from **stdin** | `python -c "...中文..."` breaks every time |
| Remote execution | Prefer `rrun exec` (automatic piping + credentials + arg pass-through) | Hand-rolled scp + ssh retry loops |
| PowerShell | `powershell -ExecutionPolicy Bypass -File x.ps1` with the .ps1 saved as UTF-8; never write PS code on the ssh command line | Inline `powershell -Command "$b=..."` from bash: `$` gets eaten by the local shell |
| cmd sequencing | Chain with `&` (not `;`); write a .bat when you need exit codes | `cmd; echo %ERRORLEVEL%` — `;` is treated as an argument separator |
| bat variables | `set X=... & %X%` on one line never expands (substitution happens at parse time); write a .bat file | `set P=...&& %P%` → "'%P%' is not recognized" |
| GUI programs | Must be awaited via `subprocess.run()` (Python) or `start /wait` | Launching a GUI app bare from cmd returns instantly; `%ERRORLEVEL%` is the launcher code, not the app's |

## How rrun works

### PowerShell payloads

`powershell -Command -` reads commands from stdin **line by line** — a multi-line here-string
fails silently. rrun therefore base64-encodes the script and inlines it into a **single-line
pure-ASCII wrapper**, which the remote decodes as UTF-8 and invokes via `ScriptBlock`.
Console output encoding is forced to UTF-8, so CJK round-trips cleanly in both directions;
pass-through arguments are available in the script as `$args`.

### Python payloads

`python -X utf8 -` reads the program from stdin. The interpreter is resolved by
`detect_remote_python`: probe the candidate paths in one round trip, verify `3.12.x`, cache
the result. Candidate order:

1. Unified venv (`C:\tools\remote-machine\venv\Scripts\python.exe` / `~/.remote-machine/venv/bin/python`)
2. Standalone base (`...\python312\python.exe` / `~/.remote-machine/python312/bin/python3`)
3. Existing installs (`C:\Python\Python312` / `python3.12` on PATH)

If all probes fail, exec refuses to install anything implicitly on the hot path and tells you
to run `rrun setup`. A `"python"` field in machines.json or `--python` skips probing and
version checks entirely.

### bash payloads

`bash -s -- args...`, with an optional `cd`/`env` prefix for `--workdir`/`--env`.

### Transport

- Arguments may only be ASCII (python → `sys.argv`, bash → `$@`, powershell → `$args`).
- ssh ControlMaster multiplexing (10-minute persist): first connection ~0.5s, reuse ~0.02s.
  `rrun close <host>|--all` tears connections down.
- Exit codes: the remote script's code passes through unchanged; `255` = ssh transport
  failure; `124` = local `--timeout`.

## Provisioning: `rrun setup`

Idempotent initializer for the unified remote Python environment:

- If the machine already has a 3.12 interpreter, it becomes the venv base directly.
- Otherwise a [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  tarball is downloaded to the local cache (`~/.cache/rrun/`) and streamed over ssh stdin for
  remote extraction — no registry, no PATH edits, no admin rights, and the remote never needs
  to reach GitHub.
- The venv receives its own `pip.ini`/`pip.conf` (mirror-configurable in `setup.py`, global
  pip config untouched), the standard packages from `remote-requirements.txt` (bundled
  default; `~/.rrun/remote-requirements.txt` overrides), and an import self-check.
- `--force` recreates the venv without touching the base interpreter.

Once the venv exists, remote scripts are **no longer restricted to the stdlib** (`requests`
ships by default). Adding a dependency: edit `~/.rrun/remote-requirements.txt` → re-run
`rrun setup <host>` (installs only the delta) — or ad-hoc: `rrun pip <host> -- install xxx`.

## Auditing

Every execution appends one JSON line to `~/.rrun/log/remote-exec.jsonl` (host, lang, script
sha1, exit code, duration). Passwords are never logged; `--env` is recorded as keys only.
Inline `-c` payloads are archived under `~/.rrun/drops/` so they can be inspected and replayed.

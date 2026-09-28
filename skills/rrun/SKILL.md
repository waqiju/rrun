---
name: rrun
description: >-
  Execute scripts and transfer files on remote machines over SSH with the rrun CLI
  (Windows via PowerShell, macOS/Linux via bash, Python 3.12 anywhere). Use when running
  commands or scripts on a remote host, provisioning remote Python, pushing/pulling files
  to/from a remote, or fighting ssh quoting/escaping/CJK-encoding issues. Credentials stay
  on disk in machines.json — never read them into the conversation.
license: MIT
compatibility: Control machine runs Linux/macOS (WSL on Windows), Python >= 3.10, plus ssh and sshpass.
metadata:
  homepage: https://github.com/waqiju/rrun
---

# rrun — agent-friendly remote execution over SSH

[中文镜像](SKILL.zh-CN.md)

[rrun](https://github.com/waqiju/rrun) runs a **local** script on a remote machine by piping
it through ssh stdin — never via command-line arguments — so quoting, escaping, and
CJK/UTF-8 content arrive intact across `bash → ssh → cmd/powershell`. Think of it as
`ssh host < script`, plus a machine inventory, atomic file transfer, and a provisioned
remote Python 3.12.

The contract for agents: **you write a file, rrun delivers and runs it, you read the output
and the exit code.** No quoting puzzles, and no passwords in your context — ever.

## Credential discipline (non-negotiable)

- **Never read, print, or open** `machines.json` or anything that may hold credentials:
  `~/.rrun/machines.json`, `~/.rrun/machines.d/*.json`, `./machines.json`, files listed by
  `$RRUN_CONFIG`. `rrun machines` shows the inventory **redacted** — that is your only view.
- **Never ask the user to paste a password** into the conversation. If a machine is missing
  or auth fails (exit 255), ask the user to run `rrun config add` (interactive wizard) or
  `rrun config edit` **in their own terminal**, then retry.
- Never embed passwords, tokens, or other secrets in the scripts you write either. Note that
  inline `-c` payloads are archived to `~/.rrun/drops/`.

## Bootstrap

1. `command -v rrun` — if missing: `pipx install rrun-cli` (or `pip install rrun-cli`; the
   PyPI distribution is named `rrun-cli`, the installed command is `rrun`). The control
   machine also needs `ssh` + `sshpass` (`sudo apt install sshpass`; macOS:
   `brew install hudochenkov/sshpass/sshpass`).
2. `rrun machines` — lists the merged inventory (redacted, with sources). If your target
   host is absent, stop and hand the configuration step to the user (see above).
3. `rrun doctor <host>` — checks ssh + auth + remote python before you depend on them.

## Core workflow: exec

```bash
# 1. Write the script locally (UTF-8; CJK welcome) — e.g. under a temp/ dir or mktemp.
# 2. Run it remotely. Language is inferred from the extension (.py/.ps1/.sh),
#    else from the remote OS (Windows = powershell, others = bash).
rrun exec <host> script.py
rrun exec <host> script.py arg1 arg2        # args: ASCII-only, enforced
rrun exec <host> -c 'Write-Output hello'    # short inline payload
rrun exec <host> script.sh --timeout 600    # no timeout by default; expiry => exit 124
```

- **Remote stdout/stderr come back verbatim; the exit code passes through** (0–254 is the
  remote script's own). Additional codes: `255` = ssh transport/auth failure (do not
  blind-retry — run `rrun doctor <host>`), `124` = local `--timeout` expired,
  `3` = push/pull integrity mismatch.
- **CJK and special characters never travel as command-line arguments** — put them in the
  script content, or write a JSON file and let the script read it.
- `--workdir` / `--env K=V` exist but are **not supported for Windows + python** (by design).
- `-q` hides the `[remote-exec]` info lines; stdout stays clean for piping.
- Prefer python for anything non-trivial: remotes run a unified **Python 3.12** venv.
  Missing? `rrun setup <host>` provisions it (idempotent, non-destructive; the remote never
  touches the internet). Extra packages: `rrun pip <host> -- install requests`.

## File transfer

```bash
rrun push <host> ./local.bin /remote/path   # atomic: temp file + sha256 verify + rename
rrun pull <host> /remote/path ./local.bin   # parent dirs auto-created; ~ expands remotely
rrun pull <host> /remote/path - | tar xz    # '-' streams to stdout / from stdin
```

Single files only — directories are rejected (tar-over-pipe is the planned extension).
Overwrite is the default and safe (atomic rename).

## Troubleshooting

| Symptom | Move |
|---|---|
| exit 255 | `rrun doctor <host>`; auth problems → the user runs `rrun config edit` themselves |
| everything hangs after a killed/timed-out transfer | `rrun close <host>` (wedged mux master), then retry |
| "python 3.12 not found" on the remote | `rrun setup <host>` |
| unsure which inventory is in effect | `rrun config` (source-chain diagnosis) |
| what ran earlier | `~/.rrun/log/remote-exec.jsonl` (audit log; records no secrets) |

## Deep references

- README (install, machines.json schema, full subcommand reference):
  https://github.com/waqiju/rrun/blob/main/README.md
- Hard-won conventions & internals (the encoding/quoting rule table):
  https://github.com/waqiju/rrun/blob/main/docs/remote-exec-conventions.md
- push/pull transport design: https://github.com/waqiju/rrun/blob/main/docs/push-pull-design.md

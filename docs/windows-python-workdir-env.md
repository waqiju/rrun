# Why Windows+python has no `--workdir`/`--env`

**[中文](windows-python-workdir-env.zh-CN.md)**

Decision record. `--workdir`/`--env` are **permanently unsupported** for Windows+python —
the `ValueError` raised by `rrun exec` is deliberate, not a backlog gap. Set them inside the
script (`os.chdir` / `os.environ`) instead.

## Support matrix

| | bash | powershell | python |
|---|---|---|---|
| POSIX | ✅ `cd`/`env` prefix | (n/a by default) | ✅ `cd`/`env` prefix |
| Windows | (n/a by default) | ✅ wrapper: `Set-Location` / `$env:` | ❌ **unsupported (by design)** |

## Why this one cell is different

`--workdir`/`--env` need an injection layer that rrun fully controls:

- **POSIX (any language):** the remote command is parsed by `sh` (guaranteed by POSIX), so a
  `cd X && env K=V ...` prefix always parses.
- **Windows+powershell:** the remote command is a fixed ASCII constant
  (`powershell ... -Command -`); the payload on stdin is a wrapper **rrun generates** in
  PowerShell syntax, so `Set-Location` / `$env:` lines can be injected freely.
- **Windows+python:** the payload is the user's script itself — there is nothing to inject
  in shell syntax. The remote command would be the only lever left, but it is parsed by
  sshd's default shell: `cmd.exe` by default, PowerShell if the server sets `DefaultShell`.
  `cd /d X & set K=V & python ...` and `Set-Location X; $env:K='V'; python ...` are mutually
  incompatible. There is no portable shell-level injection point.

## Alternatives considered

### A. cmd-style prefix — rejected: non-portable

`cd /d X & set K=V & python ...` breaks entirely when sshd's `DefaultShell` is PowerShell,
and the two shells disagree on quoting and escaping rules. Which shell you get is a
**server-side configuration** the client cannot reliably detect.

### B. PowerShell wrapper around python — rejected: hot-path cost + encoding traps

Set location/env in a PS wrapper, then invoke python with the script forwarded through
stdin. Windows PowerShell 5.1 pipes data to native processes as re-encoded strings — slow,
and it corrupts non-ASCII/binary data. Worse, *every* python exec would pay for a second
interpreter layer, not just the ones using workdir/env.

### C. Python preamble injection — rejected: half-semantics is a trap

Prepend `import os; os.chdir(...); os.environ[K]=V` lines to the payload. The payload
channel is fully controlled, so this *works mechanically*, but:

- env set after interpreter startup cannot touch startup-time variables (`PYTHONPATH`,
  `PYTHONUTF8`, `PYTHONHASHSEED`) — silently weaker than the POSIX `env` prefix;
- every traceback in `<stdin>` shifts by the preamble's line count;
- new edge questions pile up (should the preamble count into `content_sha1`/audit? ASCII
  rules for workdir?) — each trivial alone, but the sum is a feature whose semantics diverge
  per platform. Users would build on it and hit the gaps later.

### D. Explicit error + user-side workaround — chosen

`os.chdir()` / `os.environ` at the top of the script is two lines, fully visible, and has
exactly the semantics Python gives you. The cost to the user is trivial; the cost of A–C is
hidden failure modes.

POSIX+python keeps the `cd`/`env` prefix (stronger: pre-startup semantics). The dual
mechanism is deliberate — user-visible behavior is the same, only the implementation
differs.

## Notes

- Versions ≤ 0.2.1 said "not supported **yet**"; the "yet" was removed because it signaled
  a planned feature and kept getting re-raised in reviews.
- Tangential: pass-through args on Windows+python are quoted with POSIX `shlex` — harmless
  for the ASCII-only simple args rrun allows, and one more reason to keep the Windows python
  command line minimal.

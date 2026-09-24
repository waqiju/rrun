# rrun push / pull design (v0.3.0)

**[中文](push-pull-design.zh-CN.md)**

Design record for file transfer. Scoped as "deserves its own design" during v0.2.0 planning;
implemented in v0.3.0. This document also captures hard-won **OpenSSH-on-Windows stdio
pitfalls** — read before touching the transfer protocol.

## Principles (derived from rrun's existing DNA)

1. **Pipes are the protocol**: data flows over ssh stdin/stdout, never over the command line.
   No scp/sftp subprocesses (scp is deprecated by OpenSSH; sftp puts paths back on the command
   line and the encoding hell returns).
2. **Static scripts + out-of-band parameters**: remote scripts never contain unencoded user
   data; paths/hashes travel via argv (POSIX) or embedded base64 (Windows). *Injection and
   encoding issues are impossible by construction.*
3. **Atomic replacement**: always write `<dest>.rrun-tmp-<token>` → verify → rename.
   Overwriting is safe, therefore **no `--force` flag exists**.
4. **Hash as receipt**: both ends compute sha256; the remote verifies before rename and
   deletes the temp file with exit 3 on mismatch.
5. **stdout carries payload only**: `rrun pull host path - | tar xz` must be lossless;
   all status goes to stderr.

## CLI shape

```
rrun push <host> <local> <remote>      # file → file
rrun push <host> - <remote> < data     # stream from stdin
rrun pull <host> <remote> <local>      # file → file
rrun pull <host> <remote> - > data     # stream to stdout
```

- **Host first**: consistent with exec/setup/pip/doctor; the scp `user@host:path` syntax is
  deliberately rejected — rrun has exactly one addressing scheme.
- **cp-style directory semantics**: a target that is an existing directory or ends with `/`
  receives the source's basename.
- **Flags aligned with exec**: `--timeout`, `--no-mux`, `-q`. Nothing else.
- **Overwrite by default** (atomic rename makes it safe); **parent directories are created
  automatically** (ansible copy semantics).
- `~` is expanded inside the remote script (paths passed via argv are never tilde-expanded
  by a shell).

## Transfer protocol (as implemented)

### POSIX

```
push: file --raw binary--> ssh stdin --> static bash receiver (argv params) --> tmp --> sha256 --> mv
pull: static bash sender --raw binary--> ssh stdout --> local tmp --> sha256 --> os.replace
```

EOF-terminated; receipts (`RRUN <size> <sha256>`) and errors (`RRUN-ERROR <msg>`) on stderr.

### Windows (revised after real-machine debugging)

```
push: file --chunked, base64-embedded--> N single-line `-Command -` calls --> tmp append --> sha256 --> rename
pull: one `-EncodedCommand` --> raw binary stdout stream --> local tmp --> sha256 --> os.replace
```

- **push ≤512KB: a single call** (resolve → write → verify → rename → receipt); larger files
  use init (resolve + clean stale tmp + return resolved path base64-encoded) → append×N →
  finalize.
- All paths and payloads are base64-embedded into single-line PS commands — **pure ASCII on
  the wire, CJK/special-character immune**.
- Measured throughput ~2MB/s (~0.3s per call over a muxed connection) — plenty for bot-scale
  files.

### Why this seemingly roundabout design — OpenSSH-Windows stdio pitfalls (tested on 9.5p1)

Three "obviously better" approaches were tried and rejected during debugging; recorded for
posterity:

1. **`-EncodedCommand` + streamed stdin (ReadLine or raw `OpenStandardInput().Read`)**:
   intermittent hangs. Root cause: sshd-win32's stdin pump is unreliable for *data arriving
   after process start, once flow control kicks in (>~64KB in flight)* — bytes sit in the
   pipe while the remote PowerShell blocks forever on read. Payloads ≤32KB (arriving within
   the startup window, no flow control) are stable.
2. **RDY handshake (send only after the remote signals readiness)**: *worse* — guarantees the
   "after startup" branch, 100% hang.
3. **Delayed EOF / small slow chunks**: no improvement.

Meanwhile the **`-Command -` channel (same as exec) survives 1MB-class single-line payloads
reliably** (PowerShell's host reads stdin via a different code path, and exec uses it daily in
production). Hence Windows push converged to chunked appends over the exec channel.

**Bonus finding — mux master wedge**: an ssh client killed on timeout leaves that host's
ControlMaster in a wedged state; *all* subsequent operations reusing the connection (including
exec) hang until ControlPersist (600s) expires. Mitigation: transfers self-heal by closing the
mux master after any timeout (`_heal_mux`). This also explains a mid-debugging phantom where
"everything hung" — `rrun close <host>` restored it instantly.

### Windows pull is not affected by the stdin pump bug

The stdout direction (remote → local) is reliable: `[Console]::OpenStandardOutput()` raw
binary, 1MB random data 10/10 sha256-identical. `$ProgressPreference='SilentlyContinue'` is
mandatory, otherwise CLIXML progress noise pollutes the stderr protocol channel.

## Integrity & exit codes

- Exit codes: **0** ok / **1** remote failure / **2** local usage error / **3** integrity
  mismatch / **255** transport / **124** local timeout.
- Audit: same `~/.rrun/log/remote-exec.jsonl`, with `op: push|pull`, size, sha256, verified,
  duration.

## Explicit non-goals (not in v0.3.0)

| Not doing | Why |
|---|---|
| Directory recursion `-r` | **Confirmed postponed.** Extension point ready: a directory is a tar stream through the same pipe (POSIX direct; Windows reuses the chunked channel to land a tarball, then remote `tar xzf` — bsdtar ships with Windows 10+). Build when real demand shows up |
| rsync-style delta sync / resume | rsync's job; rrun does not reinvent it |
| Permission/timestamp preservation | v1 transfers content only |
| Progress bar | bot tool; one stderr summary line is enough |
| Multi-host fan-out / `--stream` | candidates A4/A5, explicitly excluded this round |

## Change surface & verification

- New `src/rrun/transfer.py`, wiring in `__main__.py`; reuses `_ssh_args`/`_ssh_run`/registry;
  zero new dependencies.
- Unit tests: command builders (single-line/ASCII/injection-safety), POSIX script argv
  round-trips, path/`~` semantics, receipt/CLIXML-noise parsing.
- CI e2e (sshd container, POSIX paths): text, CJK filenames, random binary (sha256 compared),
  auto-created parents, atomic overwrite, stdin/stdout piping.
- Windows/Mac real-machine smoke: 1MB×10 stability, 5MB chunked, CJK paths, directory
  targets, error paths.

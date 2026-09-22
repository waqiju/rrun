# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] - 2026-09-22

### Added

- **`rrun --version`**: prints the installed version (e.g. `rrun 0.2.2`) and exits 0 —
  the de-facto CLI standard, useful for issue reports and version/feature detection
  in automation. Reads the installed distribution metadata (`importlib.metadata`),
  with a source-tree fallback.

## [0.2.1] - 2026-09-22

### Added

- **Linux support for the standalone python fallback** (x86_64 / aarch64 gnu builds). Previously
  the fallback assets only covered Windows/macOS — a bare Linux machine would even have pulled
  the wrong (macOS) tarball.
- **Base qualification**: `rrun setup` now requires a base python to actually be able to create
  venvs (`ensurepip` importable). A system python 3.12 without it — the classic Debian/Ubuntu
  split into the separate `python3.12-venv` package — is skipped in favor of the standalone
  base, so bare Debian/Ubuntu machines provision with zero manual steps.
- **Half-broken venv self-heal**: a venv whose creation died midway (python runs but pip is
  missing, e.g. after an ensurepip failure) is detected and recreated by `rrun setup` —
  previously it shadowed the working pythons and made every rerun fail.
- `rrun doctor` reports both states: a broken unified venv (pip missing), and — when the venv
  is absent — whether `setup` will use an existing base or install a standalone python
  (with the `apt install python3.12-venv` alternative for Debian/Ubuntu).
- CI e2e now exercises `rrun setup` for real: the ubuntu:24.04 container ships python3.12
  without `python3.12-venv`, i.e. exactly the standalone-fallback scenario.

## [0.2.0] - 2026-09-22

### Added

- **Key-based SSH auth**: leave `password` empty in machines.json to use the default ssh key
  chain / agent / `~/.ssh/config` (optional `identity_file` field); runs with `BatchMode=yes`
  so auth failure is fast instead of prompting. `sshpass` is only required for password auth.
- **Custom SSH port**: per-machine `port` field (default 22), honored by exec/setup/pip/close/doctor.
- **`rrun doctor <host|--all>`**: health check (ssh connectivity + auth + remote python probe,
  reports whether the unified venv is in use). Single machine by default — `--all` opens real
  connections to every machine.
- Unit test suite (pytest, 49 tests covering the registry merge chain, PS wrapper, ssh arg
  assembly, setup script generation, CLI helpers) and ruff lint config; `pip install -e ".[dev]"`.
- CI: `ci.yml` runs ruff + unit tests + an end-to-end suite against a real sshd container
  (ubuntu 24.04, password + key auth, custom port, CJK content, exit-code passthrough, doctor),
  and gates the PyPI publish workflow.
- `machines --json` output gains `port` and `auth` fields.

### Changed

- Password auth now passes `NumberOfPasswordPrompts=1` (fail fast on wrong password).

## [0.1.2] - 2026-09-22

### Changed

- All user-facing CLI messages — `--help` text, error messages, status lines — are now
  English, matching the English-first docs. Internal code comments remain in Chinese for now.

## [0.1.1] - 2026-09-22

### Changed

- Documentation overhaul: English-primary README with a working Install → Configure →
  Quickstart flow, plus a full Chinese translation (`README.zh-CN.md`).
- Conventions doc is now bilingual (`docs/remote-exec-conventions.md` /
  `*.zh-CN.md`) and restructured into rules + internals; usage reference stays in the README.
- License metadata modernized to PEP 639 (SPDX expression, requires setuptools ≥ 77 at build time).

### Added

- Security section (plaintext-password caveat, password-only auth limitation, audit redaction).
- Exit-code reference table, PyPI/license/python-version/CI badges, macOS sshpass install notes,
  upgrade/uninstall instructions, contributing notes.
- This changelog.

### Removed

- Team-specific notes (Perforce/Unity ops rules) from the public docs; they live in the
  internal wiki where they belong.

## [0.1.0] - 2026-09-22

### Added

- Initial release: `exec` / `machines` / `config` / `setup` / `pip` / `close` subcommands.
- Script execution over ssh stdin pipes: PowerShell single-line pure-ASCII wrapper,
  `python -X utf8 -`, `bash -s --`; exit-code pass-through (255 transport / 124 timeout).
- `machines.json` multi-source chain with redacted listing and chain diagnostics.
- Unified remote Python 3.12 venv provisioning (python-build-standalone streamed over ssh stdin).
- Zero runtime dependencies; dual CLI entry points `rrun` and `remote-machine`.

[0.2.2]: https://github.com/waqiju/rrun/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/waqiju/rrun/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/waqiju/rrun/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/waqiju/rrun/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/waqiju/rrun/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/waqiju/rrun/releases/tag/v0.1.0

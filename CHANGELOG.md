# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.2]: https://github.com/waqiju/rrun/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/waqiju/rrun/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/waqiju/rrun/releases/tag/v0.1.0

# Roadmap

**[中文](ROADMAP.zh-CN.md)**

> rrun is feature-complete for its current scope. This file records designs that have
> been **discussed and agreed** but are **not scheduled** — written down so that future
> iterations start from the decisions, not from scratch. (Recorded 2026-09.)

## Secrets management (agreed design)

### Why

- **Get secrets out of inventory files that may live inside repo trees.** The
  `machines.d` symlink pattern lets projects carry their own inventories — but with
  inline passwords that means credentials physically sit in a working tree (accidental
  commits, cloud sync). After migration, an inventory drops from credential-grade to
  topology-grade sensitivity.
- **Deepen the core promise** — credentials never enter agent context — from ssh
  machine passwords to *all* service credentials (P4, Jenkins, MySQL, Redis, ...),
  instead of leaving service passwords as plaintext scattered across shell rc files
  and per-tool configs.
- One store, one command family, one discipline — not a separate tool.

### Agreed decisions

1. **`password_ref` field → built-in store** at `~/.rrun/secrets.json` (mode 600).
   - The store has exactly **one home** (`RRUN_SECRETS` env override for tests) —
     deliberately *no* multi-source chain like machines: there should be no path
     from secrets into repos.
   - Explicit references only; no convention-based fallback.
   - Both `password` and `password_ref` present on one machine → fail loud, no
     silent precedence.
   - Namespace convention: `machine/<name>` for machine passwords; service secrets
     namespaced likewise (`mysql/prod-ro`, `p4/depot`, `jenkins/ci`, `redis/m101`).

2. **`password_command` helper** — git credential-helper style delegation: an argv
   array (never a shell string) whose stdout is the password.
   - One mechanism covers `pass`, 1Password CLI, `sops`, `age`, OS keychains, ...
   - **No built-in encryption/decryption.** At-rest crypto is outsourced to the
     helper ecosystem. Rationale: the hard part of encryption is key management —
     a passphrase breaks the non-interactive agent/CI flows that are rrun's raison
     d'être; a key file next to the store adds little over mode 600; a key in an
     env var is readable by same-user processes. ssh's unencrypted private key +
     filesystem permissions is the accepted precedent, and rrun stays
     zero-dependency.
   - **No `password_file` field** — subsumed by the helper (`["cat", path]`).

3. **`rrun secret` command family**
   - `set` / `rm` — `set` reads interactively (getpass), never from argv, keeping
     values out of shell history.
   - `list` — names and descriptions only, never values: the **agent-safe**
     discovery path (an agent may learn *which* secrets exist, never their values).
   - `show` — prints plaintext for humans; **TTY-gated**, so it stays a human
     escape hatch instead of an accidental door into agent context.
   - `export` — eval-able output for human shells
     (`eval "$(rrun secret export mysql/prod-ro)"`).
   - `run --env VAR=ref [--env ...] -- cmd` — resolves refs into the child process
     environment: values never touch argv (invisible to `ps`), context, or logs.
     Service conventions (`MYSQL_PWD`, `REDISCLI_AUTH`, `P4PASSWD`,
     `JENKINS_API_TOKEN`, ...) are covered with zero per-service code.

4. **`rrun secret migrate`** — one-shot move of inline passwords into the store.
   - Dry-run by default; `--apply` to execute.
   - Scans all inventory sources; symlink-aware — rewrites the *real* file (the
     desired effect for repo-resident inventories) and says so in its report.
   - Idempotent, `.bak` backups, skips key-auth machines, refuses broken JSON.

5. **Compatibility**: inline `password` stays supported indefinitely. Docs framing
   evolves from "plaintext is an honest trade-off" to "plaintext works; the store
   is the step up" — no deprecation warnings.

### Phases

- **v0.4.0 — core**: store + `password_ref` + the `secret` command family +
  `secret migrate` + SKILL.md credential-discipline update + README Security/FAQ
  evolution.
- **v0.5.0 — ecosystem**: `password_command` helper + `exec --secret-env VAR=ref`
  (inject store values into the *remote* process env over the ssh channel — closes
  the local-argv exposure of today's `--env KEY=VAL`).
- **v0.6.0+ — hardening, only if real demand appears**: built-in at-rest
  encryption, secret-use auditing (names only), rotation helpers.

### Details deliberately deferred to implementation

- Exact `secrets.json` schema (baseline: `value` + `description` + `rotated_at`).
- Error-message wording, migration report format.

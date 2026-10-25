# Naming and Ports

> **Navigation**: [Decisions](./README.md) | [Documentation Root](../README.md)

**Decided 2026-10-25 by the operator.** This record keeps the analysis the names rest on.

## The progression

Dromon is written three times.

1. Python, this repository, to be retired.
2. Rust, to be retired.
3. Keleusma, entirely or almost entirely.

## Repositories

| Stage | Repository | Local path |
|---|---|---|
| Python | `sgeos/pydromon` | `~/projects/python/pydromon` |
| Rust | `dromon-rs`, no remote yet | `~/projects/rust/dromon-rs` |
| Keleusma and the specification | `dromon`, no remote yet | `~/projects/keleusma/dromon` |

The paths follow the operator's layout of one folder per language. All three repositories
were created on 2026-10-25. The other two hold no files yet.

**Reason.** The bare name `dromon` belongs to the version that lasts. The two versions
that will be retired get qualified names, so retiring them never forces a rename. On
2026-10-25, PyPI returned 404 for `dromon` and `pydromon`, crates.io returned 404 for
`dromon`, and GitHub had no `sgeos/dromon`. That suggests the names were free and does
not prove it.

## Python names

The distribution is `pydromon` and the import package is `dromon`, following
`pykeleusma`, which imports as `keleusma`. The operator chose this. The Keleusma version
will never be a Python package, so the import name collides with nothing.

## Commands

`dromond` is the daemon and `dromon` is the client, with subcommands such as
`dromon attach` for the interactive client and `dromon questions` for pending operator
questions. This pairing follows `dockerd` and `docker`. The operator chose it over a
separate `dromonc` client binary. A standalone graphical client, if one is built, would
get its own application name.

## What stays the same across ports

- The command names `dromond` and `dromon`.
- The per-project configuration, proposed as `.dromon/dromon.toml`.
- The client protocol, the message envelope and agent-facing tool names such as
  `ask_operator`.

These are versioned by the specification, not by any implementation, so each port is
checked against one source rather than against the previous implementation.

## Open

- **The Rust repository's name.** Keleusma programs run inside a host. If a thin Rust host
  outlives the Rust port, a role name such as `dromon-host` may suit it better than
  `dromon-rs`.

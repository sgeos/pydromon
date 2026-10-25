# pydromon

The Python prototype of **Dromon**, a harness that runs several AI agent session lines at
once inside one daemon, lets them message and wake each other, and tracks the questions
they raise for the human operator as a first-class feature.

The distribution is `pydromon` and the import package is `dromon`. The commands are
`dromond`, the daemon, and `dromon`, the client, which takes subcommands.

## Status

**Seed only.** The repository was cut on 2026-10-25. The package installs, both commands
parse their arguments, and every subcommand reports that it is not implemented. No
daemon, session line, message or question exists yet. The first milestone is
[`docs/roadmap/V0_1_0.md`](docs/roadmap/V0_1_0.md) and the planned system is
[`docs/architecture/DESIGN.md`](docs/architecture/DESIGN.md). What Dromon needs from
the Keleusma Python bindings is in
[`docs/architecture/KELEUSMA_INTEGRATION.md`](docs/architecture/KELEUSMA_INTEGRATION.md).

## Why Dromon

Work on Keleusma and other projects has run several agent session lines side by side.
Two observations motivate a dedicated harness.

- Agents that can talk to each other often do better work than one agent alone.
- A shared mailbox such as MCP Agent Mail delivers messages, but it cannot wake an idle
  session, because nothing outside a harness can start that harness's next turn. A
  harness that owns the agent loops can deliver a message and wake its recipient in one
  step.

Questions for the operator also get lost in long replies and tracking files. Dromon makes
them a tracked object with a lifecycle.

## The plan across implementations

| Stage | Repository | Lifetime |
|---|---|---|
| Python prototype | `sgeos/pydromon`, this repository | Retired after the Rust port |
| Rust port | `dromon-rs` | Retired after the Keleusma version, unless a thin host survives |
| Keleusma version | `dromon` | The lasting implementation |

The command names, the per-project configuration and the protocol stay the same across
all three stages. See [`docs/decisions/NAMING_AND_PORTS.md`](docs/decisions/NAMING_AND_PORTS.md).

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

## License

[0BSD](LICENSE).

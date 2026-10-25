# pydromon

The Python prototype of **Dromon**, a harness that runs several AI agent session lines at
once inside one daemon, lets them message and wake each other, and tracks the questions
they raise for the human operator as a first-class feature.

The distribution is `pydromon` and the import package is `dromon`. The commands are
`dromond`, the daemon, and `dromon`, the client, which takes subcommands.

## Status

**Early prototype.** The repository was cut on 2026-10-25. The data model and its
append-only SQLite event log are implemented and specified in
[`docs/protocol/EVENT_LOG.md`](docs/protocol/EVENT_LOG.md). `dromon start`, `dromon status`
and `dromon stop` manage a per-project `dromond` over a private Unix domain socket, as
specified in [`docs/protocol/CLIENT.md`](docs/protocol/CLIENT.md). `dromon probe` runs one
short turn of Claude Opus 5.5 on Claude Code, on the operator's Claude subscription,
through a Keleusma driver, and reports the engine's capabilities. `dromon create` and
`dromon send` create lines and message them, and the daemon wakes an idle line when a
message arrives. Lines message each other through a `send_message` tool. Lines ask operators
questions through an `ask_operator` tool, and `dromon questions` and `dromon answer`
work the queue. Every party has a mailbox, which lines search with `list_mail` and
`read_mail` and operators with `dromon mail` and `dromon read`. `dromon attach LINE` opens a
curses client attached to one line, or a Tk window with `--gui`, and any number may be
attached at once. A project declares its lines, limits and wake policy in
`.dromon/dromon.toml`, as
[`docs/protocol/PROJECT_CONFIG.md`](docs/protocol/PROJECT_CONFIG.md) specifies, and
`dromon config` validates it. Lines run on Claude Code or, for OpenAI models such as GPT-6-Luna, on Codex, as
[`docs/protocol/CODEX.md`](docs/protocol/CODEX.md) specifies, and nominally on Grok
Build for Grok 4.7, as [`docs/protocol/GROK.md`](docs/protocol/GROK.md) specifies. A line's shell commands
can run in Claude Code's sandbox,
configured per project and per line as
[`docs/protocol/SANDBOX.md`](docs/protocol/SANDBOX.md) specifies, and its permission
rules, hooks and environment as
[`docs/protocol/SETTINGS.md`](docs/protocol/SETTINGS.md) specifies. A project's local
teacher model, served by Ollama, is brought up with `dromon teacher up`, as
[`docs/protocol/TEACHER.md`](docs/protocol/TEACHER.md) specifies. Agents outside the
daemon, such as another Claude Code session, reach it and its lines over MCP through
`dromon mcp`, as [`docs/protocol/AGENTS.md`](docs/protocol/AGENTS.md) specifies. The first milestone is
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

The build needs a Rust toolchain, because `pykeleusma` is built from its repository at a
pinned commit. Running a Claude line needs a Claude subscription signed in to Claude
Code, and no `ANTHROPIC_API_KEY` in the environment. Running a GPT-6-Luna line needs
Codex signed in to a ChatGPT plan, and Dromon starts Codex without any OpenAI API key
in its environment.

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

## License

[0BSD](LICENSE).

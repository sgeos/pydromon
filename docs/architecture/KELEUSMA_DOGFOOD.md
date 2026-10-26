# Dogfooding Dromon on Keleusma

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

Keleusma runs four session lines, kaizen, proofs, V0.2.X and V0.3.X, each a Claude
Code session in its own git worktree. This page records what Dromon needed before
those lines could run as Dromon lines, what was built, how it was tried, and the
project file that results. Keleusma itself was not changed, as this repository's
`CLAUDE.md` requires.

## How the work was done

**Fact, 2026-10-26.** pydromon's own lines did the work under Dromon, directed by the
operator's session. The Opus line `worker` inspected Keleusma read only and ranked the
gaps, then implemented three stages. The Haiku line `reviewer` reviewed each stage,
and its findings were fixed before merging. The operator's session verified each stage
live and merged pull requests 35 to 38.

## The gaps and their status

| Gap | Status |
|---|---|
| Each line works in its own worktree | Done. `workdir`, verified live, with session adoption and a guard on git hooks and config |
| Commits from a linked worktree | Done for Claude Code lines, through an `allowWrite` on the common git directory, verified live |
| Long gates and continuation | Done. `wake_after`, verified live. A command started with `nohup` outlives its turn, observed live |
| A mandatory sandbox | Done. `[daemon] require_sandbox`, verified live: a line without a sandbox was refused |
| A disclosure guard on messages | Done. `[daemon] disclosure_terms`, verified live: a line's message holding a term was refused without naming it, and the term was stored only in the operator's exempt brief |
| Engine changes that keep a line's identity | Done. `rebind_line`, operator only, verified live: a Haiku line rebound to Opus answered as Opus in a fresh session |
| Project settings and skills | Done, under allowlists, verified live: a project skill loaded and ran, and a project hook refused the turn before it started, and never ran |
| The required user plugin | Done. `plugins` loads one plugin by path, under allowlists, verified live: a line saw `typesafe:typesafe-ai` and the plugin's files were not written |
| One full gate at a time | Done. `[leases]`, verified in both trials |
| Turn budgets for real work | Done. Subscription drivers allow 200 engine turns and 400 tool calls |
| The proofs line's model | Fable 5.1 drivers, verified live by one probe turn on the subscription. The project file runs proofs on Opus |
| Pushing over HTTPS and pull requests | Keleusma's configuration, for the operator |

## The trial

**Fact, observed on 2026-10-26.** A scratch project ran the four lines on Claude Haiku
5.5, each in its real worktree, with the sandbox denying every write to Keleusma and
its `secret/` directory. Each line named its line, branch and task correctly from its
own handoff, acquired or was refused the `full-gate` lease as another line held it,
and reported what it would need. No line wrote anything. One worktree showed changes
during the trial, and their times and the trial line's tool calls showed they came from
a live session working there.

**Fact, observed on 2026-10-26.** A writing trial ran the kaizen line on Haiku in a
private clone of Keleusma, with a local bare repository as its remote, so neither the
real worktrees nor GitHub were touched. The line read Appendix B, acquired the
`full-gate` lease, started `cargo build -p keleusma-arena` in the background with
`nohup`, scheduled `wake_after` twice while it ran, wrote one file, committed it in its
linked worktree, pushed the branch, and released the lease. The first build failed
because the sandbox refuses `sccache`, and succeeded with `RUSTC_WRAPPER` unset, which
the project file below therefore sets.

What the lines reported a real deployment needs, beyond the read-only trial's rules:

- Reading Appendix B before any tracked write. The project file below leaves it
  readable and closes it to writes.
- The `typesafe-ai` skill, which Keleusma's instructions require. It is a user plugin,
  which the `plugins` key now loads by path.
- Network to the crate registry and to GitHub, and a push path, which needs Keleusma's
  remote on HTTPS with the GitHub command-line helper.
- Writes to the worktree, the common git directory, the interline store and the cargo
  registry.

## Operating notes

- **Start timeout.** On a loaded host Claude Code missed the Agent SDK's 60 second start
  timeout, which failed a turn. Start the daemon with
  `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=300000` to allow five minutes.
- **Cost holds.** An Opus line doing a stage of real work ran up an estimated $20 to $35.
  On a subscription this is an estimate, not a charge. A limit below one stage holds
  the line after every stage, and work stops until the operator answers.
- **Live sessions.** A Dromon line and a live session in the same worktree share one
  tree. Stop the live session before the line starts, and adopt its session with
  `session` only after it has stopped.

## Installation

**Fact, 2026-10-26.** On the operator's decision, the project file below, cut to the
kaizen line, is installed as Keleusma's `.dromon/dromon.toml`, with `.dromon/` in
`.git/info/exclude`, and `dromon config` accepts it there. Keleusma's
`secret/disclosure-terms.txt` exists, untracked. No daemon was started, because the
kaizen line's own live session was still running, and the operator starts it once that
session ends:

```sh
cd /PATH/TO/keleusma
CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=300000 uv run --project /PATH/TO/pydromon dromon start
uv run --project /PATH/TO/pydromon dromon attach kaizen
```

The remote stays on SSH by the operator's decision, so the kaizen line can commit but
not push. The other three lines join by adding their sections from the file below.

## The project file

Validated with `dromon config` against the real worktrees on 2026-10-26, with the paths
below as placeholders. Install it as `.dromon/dromon.toml` in Keleusma, with `.dromon/`
in `.git/info/exclude`, and create `secret/disclosure-terms.txt`, only on the
operator's decision. It assumes Keleusma's lines keep their authority to commit, push
and merge, which is the operator's decision.

```toml
# Dromon project file for Keleusma, drafted by pydromon. Install it as
# KELEUSMA/.dromon/dromon.toml, with .dromon/ in .git/info/exclude, only on the
# operator's decision, and after the live sessions of these lines have stopped.
version = 1

[daemon]
require_sandbox = true
disclosure_terms = "secret/disclosure-terms.txt"

[leases]
names = ["full-gate"]
ttl_seconds = 10800

[limits]
max_autonomous_turns = 12
max_cost_usd = 60.0

[profiles.keleusma]
model = "claude-opus-5-5"
builtin_tools = ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "Skill"]
claude_code_prompt = true
# Keleusma's instructions require this user plugin's skill.
plugins = ["~/.claude/plugins/cache/typesafe-ai/typesafe/VERSION"]

[profiles.keleusma.sandbox]
enabled = true

[profiles.keleusma.sandbox.filesystem]
allowWrite = ["/PATH/TO/keleusma/.git", "/PATH/TO/keleusma-worktrees", "/PATH/TO/keleusma/.worktrees", "/PATH/TO/keleusma/secret/interline", "~/.cargo/registry", "~/.cargo/git"]
denyWrite = ["/PATH/TO/keleusma/secret/notes"]
denyRead = ["~/.ssh", "~/.aws"]

[profiles.keleusma.sandbox.network]
allowedDomains = ["github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com", "index.crates.io", "static.crates.io", "crates.io"]

# The sandbox refuses sccache, which a build otherwise runs through.
[profiles.keleusma.settings.env]
RUSTC_WRAPPER = ""

[profiles.keleusma.settings.permissions]
deny = ["Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.codex/auth.json)", "Read(~/.grok/auth.json)"]

[lines."kaizen"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma/.worktrees/kaizen"
instructions = ["CLAUDE.md", "AGENTS.md"]

[lines."kaizen".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma/.worktrees/kaizen/target"

[[lines."kaizen".settings.hooks.SessionStart]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line kaizen", timeout = 10 }]

[[lines."kaizen".settings.hooks.UserPromptSubmit]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line kaizen", timeout = 10 }]

[lines."v0.2.3"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma-worktrees/v0.2.x"
instructions = ["CLAUDE.md", "AGENTS.md"]

[lines."v0.2.3".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma-worktrees/v0.2.x/target"

[[lines."v0.2.3".settings.hooks.SessionStart]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line v0.2.3", timeout = 10 }]

[[lines."v0.2.3".settings.hooks.UserPromptSubmit]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line v0.2.3", timeout = 10 }]

[lines."v0.3.0"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma-worktrees/arena-composites"
instructions = ["CLAUDE.md", "AGENTS.md"]

[lines."v0.3.0".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma-worktrees/arena-composites/target"

[[lines."v0.3.0".settings.hooks.SessionStart]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line v0.3.0", timeout = 10 }]

[[lines."v0.3.0".settings.hooks.UserPromptSubmit]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line v0.3.0", timeout = 10 }]

[lines."proofs"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma-worktrees/proofs"
instructions = ["CLAUDE.md", "AGENTS.md"]

[lines."proofs".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma-worktrees/proofs/target"

[[lines."proofs".settings.hooks.SessionStart]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line proofs", timeout = 10 }]

[[lines."proofs".settings.hooks.UserPromptSubmit]]
hooks = [{ type = "command", command = "/PATH/TO/python3 /PATH/TO/keleusma/secret/interline/interline.py --store /PATH/TO/keleusma/secret/interline hook --line proofs", timeout = 10 }]

[lines.proofs.wake]
every_ticks = 10
```

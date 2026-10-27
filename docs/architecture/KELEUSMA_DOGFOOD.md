# Dogfooding Dromon on Keleusma

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

Keleusma runs four session lines, kaizen, proofs, V0.2.X and V0.3.X, each a Claude
Code session in its own git worktree. This page records what Dromon needed before
those lines could run as Dromon lines, what was built, how it was tried, and the
project file that results. Keleusma itself was not changed, as this repository's
`CLAUDE.md` requires.

The operator will time the migration. Installing this project file does not start the
lines or imply an immediate cutover.

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
| A disclosure guard | Done for messages, questions and scheduled wake notes sent through Dromon's tools. It does not inspect commits, pull requests or commands sent through `gh`. |
| Engine changes that keep a line's identity | Done. `rebind_line`, operator only, verified live: a Haiku line rebound to Opus answered as Opus in a fresh session |
| Project settings and skills | Done, under allowlists. Live checks are listed below. |
| The required user plugin | Done. `plugins` loads one plugin by path, under allowlists, verified live: a line saw `typesafe:typesafe-ai` and the plugin's files were not written |
| One full gate at a time | Proposal. Keep `[leases]` with `full-gate` as the only lease, pending the operator's decision. |
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

## Established facts and remaining live checks

The statements below distinguish facts from inferences and recommendations. They do not
establish that the Keleusma host currently has working credentials, network access,
filesystem permissions or installed tools.

- **Fact.** Dromon implements `send_message`, `list_mail`, `read_mail` and `list_sent`,
  and stores mailbox state in its event log. **Recommendation.** Relay messages still
  pending in the retired mailbox through operator `dromon send` calls to their intended
  lines.
- **Fact.** The documented remote uses SSH, and the sandbox profile denies `~/.ssh`.
  **Inference.** HTTPS fetch and push through the `gh` credential helper can provide a
  path that avoids SSH credentials. The operator's check below confirms this only in a
  Claude Code sandbox against pydromon, not in a Dromon line's sandbox.
- **Fact.** `setting_sources = []` prevents Claude Code user settings from loading.
  **Fact.** `AGENTS.md` only points to `CLAUDE.md`. **Recommendation.** List
  `~/.claude/CLAUDE.md` explicitly and omit the auto-memory index, which is not needed
  as project instruction and may contain unrelated session context.
- **Fact.** Dromon expands `~` in configured instruction paths with
  `Path.expanduser()` in `src/dromon/engine.py:294`. Configuration validation also
  expands explicit home paths in `src/dromon/lineconfig.py:242`.
- **Fact.** The template denies reading the Codex and Grok authentication files and
  Cargo credentials through both the sandbox and Claude Code permission rules.
  **Recommendation.** Verify shell reads fail in a live line sandbox using the check
  below.
- **Fact.** Each line's write grant names its own worktree and the shared Git directory.
  **Inference.** This confines worktree writes to the owning line, subject to confirming
  the resolved paths and sandbox enforcement on the host.
- **Fact.** Dromon's engine table and adapters identify the engines implemented in this
  version. **Inference.** Claude Code is the suitable engine for these configured
  writes and network access. The Codex sandbox in this deployment has no network, so it
  cannot fetch crates or use GitHub. Other engines need separate sandbox review.
- **Fact.** This profile's built-in tool list does not offer Agent. **Inference.** Whether
  Claude Code subagents inherit a parent's sandbox is not established by this code or
  documentation. **Recommendation.** Check that behavior before offering Agent. A
  shadow evaluation pilot would need write access to a directory under Keleusma's
  `secret` tree and network access to `api.typesafe.ai`, neither of which this template
  grants.
- **Fact.** The template configures a USD 60 per-line cost hold. **Proposal, pending the
  operator's decision.** With four simultaneous lines, review and release each hold as
  capacity permits. This does not guarantee that four long stages fit within the host's
  time or budget.
- **Fact.** No live proof-target build has been run in the sandbox. **Inference.** Setting
  `RUSTC_WRAPPER = ""` avoids the observed `sccache` denial, but only a live probe can
  establish that the proofs toolchain and targets work.
- **Proposal, pending the operator's decision.** Use the `full-gate` lease alone to
  serialize full gates. A second build lease remains an option if full-gate proves
  insufficient.

The operator must run these checks on the actual host. Use disposable branches and pull
requests for remote write checks.

**Fact, observed 2026-10-27 by the operator's session.** In a Claude Code sandbox whose
network allowlist contained only `github.com`, `api.github.com`, `codeload.github.com`
and `objects.githubusercontent.com`, against the pydromon repository, public HTTPS
`git fetch` succeeded without credentials, `gh auth status` read its token from the
keyring, and `git push --dry-run` to an HTTPS URL through the `gh` credential helper
succeeded for a new branch. This does not establish the same behavior under a Dromon
line's own sandbox profile. `gh pr view` works with `github.com` and `api.github.com`
allowed. `gh pr create` and `gh pr merge` have not been observed.

**Fact, observed 2026-10-27 by the operator's session.** In a Claude Code sandbox,
`gh run view --log` was first refused for
`results-receiver.actions.githubusercontent.com`. Adding that host and
`*.blob.core.windows.net` to the network allowlist got past the network refusal, but the
command could not write its cache under `~/.cache/gh`, which the sandbox denies. With
`XDG_CACHE_HOME` pointed inside `TMPDIR`, it printed the workflow log. This observation
was in a Claude Code sandbox, not a Dromon line sandbox.

The next check is an operator-run scratch-project trial line with the Keleusma profile's
network allowlist. That trial should check fetch and push, pull request viewing and log
retrieval in the Dromon line sandbox. Pull request creation should use a disposable draft
on pydromon. A merge check requires the operator's confirmation in session.

1. From each line's worktree, run `git -c http.followRedirects=true fetch
   https://github.com/sgeos/keleusma.git <branch>` and confirm it fetches the intended
   ref without reading SSH credentials. This needs `github.com`,
   `objects.githubusercontent.com` and `codeload.github.com` if GitHub redirects.
2. From a disposable local commit, run `git push --dry-run
   https://github.com/sgeos/keleusma.git HEAD:refs/heads/dromon-sandbox-check`.
   This checks the configured `gh` credential helper without changing the remote. It
   needs `github.com` and `api.github.com` for helper authentication.
3. On pydromon's disposable remote, run `gh pr create --draft --title "sandbox check"
   --body "temporary check"` from a disposable pushed branch, then `gh pr view --json
   url,state` and close the draft with `gh pr close <number>`. Do not use Keleusma for
   this check. This line cannot run `gh`, so the operator's session must perform it.
   Any merge check requires the operator's confirmation in session. If confirmed, run
   `gh pr merge <number> --squash --delete-branch` only on a separate disposable pull
   request. This merges that pull request and deletes its branch. These commands need
   `github.com` and `api.github.com`.
4. Run `gh run view <run-id> --log` for a disposable completed workflow. This downloads
   logs and needs `github.com`, `api.github.com`, `objects.githubusercontent.com`,
   `results-receiver.actions.githubusercontent.com` and `*.blob.core.windows.net`.
   The template sets `XDG_CACHE_HOME` to a cache directory inside each line's own
   worktree, avoiding a write grant to `~/.cache/gh`.
5. In the Claude Code sandbox, run `test -w "$TMPDIR" && p=$(mktemp "$TMPDIR/dromon-check.XXXXXX") && rm "$p"`.
   This establishes whether the per-process temporary directory is writable.
6. Outside the sandbox, first confirm that each credential file exists with `test -f`
   and do not print or open its contents. Then, inside a Dromon line with this sandbox
   profile, run `for p in ~/.codex/auth.json
   ~/.grok/auth.json ~/.cargo/credentials.toml; do if cat "$p" >/dev/null 2>&1; then
   echo "read succeeded: $p"; exit 1; fi; done`. It must exit successfully without
   printing credential contents, showing that shell reads fail for all three existing
   files.
7. Start a fresh Dromon line from its handoff file after stopping its existing live
   session. Confirm it reads the expanded `~/.claude/CLAUDE.md`, does not load `AGENTS.md`
   or an auto-memory index implicitly, and can list, read and report its own in-flight
   mail.
8. On a throwaway copy of the project file, test Agent availability and the subagent's
   sandbox inheritance with a harmless read-only subagent task. Do not enable the shadow
   evaluation pilot's write or network grants until this behavior is confirmed.
9. In a throwaway proof worktree, run the project's documented proof-target build
   command inside the sandbox with `RUSTC_WRAPPER` unset. Record the exact command,
   target and result.

The first four checks require outbound HTTPS to the hosts named beside each command.
Checks 5 and 6 exercise local filesystem behavior. Checks 7 to 9 exercise engine or
build behavior; any model or service calls must
use only the operator's normal approved credentials.

What the lines reported a real deployment needs, beyond the read-only trial's rules:

- Reading Appendix B before any tracked write. The project file below leaves it
  readable and closes it to writes.
- The `typesafe-ai` skill, which Keleusma's instructions require. It is a user plugin,
  which the `plugins` key now loads by path.
- Network to the crate registry and to GitHub, and a push path, which needs Keleusma's
  remote on HTTPS with the GitHub command-line helper.
- Writes to each line's own worktree, the common git directory and the Cargo registry.

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

The remote currently stays on SSH. Before enabling push from a line, the operator must
choose and configure the HTTPS remote and confirm that the `gh` credential helper works
in the sandbox. The other three lines join by adding their sections from the file below.

## The project file

Validated with `dromon config` on 2026-10-27 against a scratch project in this worktree,
using placeholder worktree and repository paths. This checks the project-file schema
and configuration, not the actual Keleusma paths, credentials or live sandbox behavior.
Install it as `.dromon/dromon.toml` in Keleusma, with `.dromon/`
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
allowWrite = ["/PATH/TO/keleusma/.git", "/PATH/TO/keleusma/.worktrees/kaizen", "/PATH/TO/keleusma-worktrees/v0.2.x", "/PATH/TO/keleusma-worktrees/arena-composites", "/PATH/TO/keleusma-worktrees/proofs", "~/.cargo/registry", "~/.cargo/git"]
denyWrite = ["/PATH/TO/keleusma/secret/notes"]
denyRead = ["~/.ssh", "~/.aws", "~/.codex/auth.json", "~/.grok/auth.json", "~/.cargo/credentials.toml"]

[profiles.keleusma.sandbox.network]
allowedDomains = ["github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com", "results-receiver.actions.githubusercontent.com", "*.blob.core.windows.net", "index.crates.io", "static.crates.io", "crates.io"]

# The sandbox refuses sccache, which a build otherwise runs through.
[profiles.keleusma.settings.env]
RUSTC_WRAPPER = ""

[profiles.keleusma.settings.permissions]
deny = ["Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.codex/auth.json)", "Read(~/.grok/auth.json)", "Read(~/.cargo/credentials.toml)"]

[lines."kaizen"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma/.worktrees/kaizen"
instructions = ["CLAUDE.md", "~/.claude/CLAUDE.md"]

[lines."kaizen".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma/.worktrees/kaizen/target"
XDG_CACHE_HOME = "/PATH/TO/keleusma/.worktrees/kaizen/.cache"

[lines."v0.2.3"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma-worktrees/v0.2.x"
instructions = ["CLAUDE.md", "~/.claude/CLAUDE.md"]

[lines."v0.2.3".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma-worktrees/v0.2.x/target"
XDG_CACHE_HOME = "/PATH/TO/keleusma-worktrees/v0.2.x/.cache"

[lines."v0.3.0"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma-worktrees/arena-composites"
instructions = ["CLAUDE.md", "~/.claude/CLAUDE.md"]

[lines."v0.3.0".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma-worktrees/arena-composites/target"
XDG_CACHE_HOME = "/PATH/TO/keleusma-worktrees/arena-composites/.cache"

[lines."proofs"]
profile = "keleusma"
workdir = "/PATH/TO/keleusma-worktrees/proofs"
instructions = ["CLAUDE.md", "~/.claude/CLAUDE.md"]

[lines."proofs".settings.env]
CARGO_TARGET_DIR = "/PATH/TO/keleusma-worktrees/proofs/target"
XDG_CACHE_HOME = "/PATH/TO/keleusma-worktrees/proofs/.cache"

[lines.proofs.wake]
every_ticks = 10
```

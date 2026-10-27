# Engines as data, and probe evidence

> **Navigation**: [Protocol](README.md)

**Status.** Draft, written on 2026-10-27, from rows 7 and 10 of the T3 Code review.
Normative for Dromon's side. The Python modules are `dromon.capabilities` and
`dromon.evidence`.

## Capability tables

Each engine has one `EngineTable` in `dromon.capabilities`, and a port carries the
tables across as data. A capability is `yes`, `no` or `unknown`. Unknown is refused
wherever Dromon needs the capability, as `no` is, but it is reported as unknown, so an
operator can tell a limit of the engine from a gap in what Dromon has observed.

| Capability | Claude Code | Pi | Cursor Agent | Meaning |
|---|---|---|---|---|
| `resume` | yes | yes | no | A line's engine session continues in a later turn |
| `kept_session` | yes | no | no | A connection can be kept between turns |
| `own_tools` | yes | unknown | yes | Dromon's own tools reach the model, through MCP or an extension |
| `deny_before_run` | yes | unknown | no | Every built-in tool is denied before it runs |
| `cost_report` | yes | yes | no | The engine reports a cost for each turn |
| `max_turns` | yes | yes | no | Model calls in a turn are bounded, by the engine or by Dromon's count |
| `credential_visible` | yes | yes | yes | Dromon can tell which credential a turn uses |
| `sign_in_check` | unknown | unknown | unknown | The engine can check its sign-in without a turn |

**Pi was never run live.** Review of 2026-10-27, finding C1, by the operator's
decision. Its table once said `yes` for `own_tools`, `deny_before_run` and
`sign_in_check` from its documentation and source alone, so a Pi line's isolation was
reported as full. They are `unknown` until a live run shows them, and a Pi line's
isolation is reported as unknown. Its other capabilities are as the adapter's code
states them, which no live run has confirmed either.

**Grok, Codex and Antigravity.** Their adapters were being changed on another branch
when the tables were written, so every capability of theirs is `unknown`, and each
table says it is to be filled once that branch is merged.

**Derived from the tables.**
- The refusal of `keep_session` for Pi and Cursor lines.
- `isolation` in each `recent_turns` entry, `full`, `partial` or `unknown` from
  `deny_before_run`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies.
- The engines `dromon probe --accept-partial-isolation` lets run, which are Cursor by
  its table and Antigravity by name until its table is filled.
- The probe's report, which names each engine's table, its tested minimum version or
  that it was never run live, and each capability.

**Unknown is not empty.** A Claude Code `init` message without a list of tools was
read as an empty list, which passed the check that the engine offers no tool the line
was not given. Such a report is now unknown, and refuses the turn.

## Minimum versions

Each engine has a table in `dromon.capabilities`, and the table names the lowest engine
version Dromon was run against live, with the date of that run. An adapter refuses an
engine that reports a lower version, at the start of each turn, so a probe, which runs a
turn, refuses it too. A version is the one run of dot-separated numbers, after
an optional `v`, that stands alone in the text the engine names for its version, and
missing parts are taken as zero, so `2.1` equals `2.1.0`, and `2026.10.01-e373342`
compares as `2026.10.1`. Text that holds two different versions, such as
`node 20, agy 1.0.0`, is unreadable. Review finding C3 of 2026-10-27 found that such
text was read by its first number. An engine whose table names a minimum,
but whose report holds no version Dromon can read, is refused, since nothing shows it
was tested.

| Engine | Minimum | Run live on | Where the version is read |
|---|---|---|---|
| Claude Code | `2.1.310` | 2026-10-26 | `claude_code_version` in the engine's `init` message, before its output is used |
| Pi | none | never | Not read. Pi was never run live, so no version can be named as tested, and none is enforced |
| Cursor Agent | none | never | Not read. Cursor was never run live, so no version can be named as tested, and none is enforced |
| Grok, Codex, Antigravity | pending | pending | Their adapters were being changed on another branch, and their rows are filled once it is merged |

**No minimum for Pi and Cursor Agent.** Review of 2026-10-27, finding C2. Any version
of either engine runs, and `dromon probe` says so in its report, as `never run live,
so no minimum version is enforced and any version the engine reports runs`.

**Fact.** The Antigravity adapter already requires `antigravity-acp` 1.3.0 or later at
`initialize`, which [`ANTIGRAVITY.md`](ANTIGRAVITY.md) records. That version was taken
from a second implementation and was not run live, so it is not yet a tested minimum.

## Sign-in check without a turn

**Row 16 of the T3 Code review, 2026-10-27.** `dromon probe --no-turn` checks the
engine's sign-in for the given model and strategy, and runs no turn. It runs for an
engine whose table says `sign_in_check` is `yes`, and `dromon.cli.SIGN_IN_CHECKS`
holds a check for exactly those engines, which a test keeps in step with the tables.
No table says `yes` yet. Any other engine is refused, naming what its table says, and
nothing is checked. `--no-turn` takes no `--record`, since no frames are exchanged.

**A check never run live.** Review of 2026-10-27, finding C1, by the operator's
decision. `dromon.cli.UNVERIFIED_SIGN_IN_CHECKS` holds checks written from an engine's
documentation, for engines whose table says `unknown`. Such a check runs only with
`--no-turn --accept-unverified-check`, and its report says that its answer is
unverified. Without the flag the refusal names it. The flag is refused without
`--no-turn`.

- **Pi, unverified.** `pi auth check --provider PROVIDER --json --no-refresh`, in the
  environment a turn would have, with Dromon's agent directory, offline and without
  telemetry, and for a subscription without any variable that could carry an API key.
  Pi reads its own credential store and prints no credential. The driver must accept
  the credential Pi reports, as before a turn. The check is the one the Pi adapter
  runs before each turn, factored out.
- **Claude Code and Cursor Agent.** `unknown`. Neither has been observed to offer a
  check whose answer Dromon could read without an account address or a credential in
  it, and Dromon may not start an engine to find out. `CURSOR.md` names `cursor-agent
  status` as a step to run by hand.
- **Grok, Codex and Antigravity.** Pending, with their tables.

**Never a credential store.** No check opens a credential file itself. A test runs
Pi's check with its `auth.json` made unreadable, and the check still succeeds.

## Probe evidence

`dromon probe --record` writes every frame the adapter sent and received during its
one turn to a new file under the project's `tmp/probes`, named for the engine and the
time in Coordinated Universal Time. The file is created with owner-only permissions,
and `tmp/` is ignored by git. `--record-stderr`, accepted only with `--record`, also
keeps the engine's standard error, at most 64 KiB, masked by `dromon.redact`.

**What a recording holds.** Frames are recorded as they were exchanged, with prompts
and replies, so a recording is read by a person before it is kept as a test fixture.
Masking applies only to standard error, and it is by pattern, as `dromon.redact`
states. A recording holds at most 20,000 frames and 32 MiB of them, then marks itself
truncated and records nothing more.

**Which engines record.** Claude Code records through a transport that wraps the Agent
SDK's own. Pi, Cursor Agent and Antigravity record in `dromon.jsonlines`, which starts
their engines. Codex and Grok frame their own traffic, and `--record` refuses them
until their adapters record. **Porting hazard.** The Claude Code recorder implements
the Agent SDK's internal `Transport` interface, and gives the SDK's process transport
the options the SDK's own connect would give it, which an SDK upgrade must check.

**File format, version 1.** Each line is one JSON object.

| `kind` | Fields | Where |
|---|---|---|
| `header` | `v` of 1, `engine`, `model`, `recorded_at` | first line, once |
| `frame` | `dir` of `out` for what the adapter sent or `in` for what the engine sent, and `frame`, the object | any number |
| `truncated` | `frames`, how many were recorded | once, after the last frame, when a bound was reached |
| `stderr` | `text`, masked, and `cut`, whether the bound cut it | last, only with `--record-stderr` |

A reader refuses a file whose first line is not a version 1 header, a malformed frame,
an unknown record, a frame after truncation and any record after standard error, and
names the line.

## Replay

`dromon.evidence.Replay` plays a recording back as the engine's side of the exchange.
It hands the adapter each recorded `in` frame only once the adapter has sent as many
frames as preceded it in the recording, so the order of the exchange is kept. An
adapter that waits for a frame it has not earned, or that sends more frames than the
recording holds, is said to diverge within 5 seconds, and a test never hangs. The
frames the adapter sent are kept for the test to compare.

- **Pi, Cursor Agent and Antigravity.** `replay_factory` is passed as the adapter's
  connection factory, so the real adapter runs against the recording.
- **Claude Code.** `replay_session` runs the real adapter and the real Agent SDK client
  over a replayed transport. The SDK names each control request it sends at random, so
  the answer to the recording's `n`-th control request is renamed for the `n`-th the
  SDK sent.

**Tested.** A Claude Code turn recorded through a scripted engine and replayed through
the real adapter and SDK client gives the same report. A Cursor recording replays
through the real adapter, and one cut before its result fails as a live stream would.

## Scrubbing a recording into a fixture

A raw recording is never tracked. The first live Claude Code recording, of 2026-10-27,
held the account's email address and organization in the `initialize` response, the
operator's home directory, session identifiers and message universally unique
identifiers. `dromon probe --scrub FILE` runs no turn. It writes a copy to
`--scrub-to PATH`, or beside `FILE` with `.scrubbed.jsonl` in place of `.jsonl`, and
never overwrites. `dromon.evidence.scrub_file` does the same for a test or a script.

| Value | Placeholder |
|---|---|
| each field of an `account` block, other than `type` | `scrubbed-account-<field>` |
| that field's value anywhere else it stands as a whole word, whatever its length | the same placeholder |
| the home directory, and its dashed form in Claude Code's project directories | `/home/operator` and `-home-operator` |
| each universally unique identifier | `00000000-0000-4000-8000-<n>`, numbered by first appearance |
| a `session_id` that is not one | `session-<n>` |
| in `rate_limit_info`, through each window and list, every `utilization` | `0.5` |
| there, every `resetsAt` and `overageResetsAt` | `1767225600`, which is 2026-01-01T00:00:00Z |
| there, every `status` and `overageStatus`, which the Agent SDK requires to be a known value | `allowed` |
| there, every `overageDisabledReason` | `scrubbed` |
| there, a `rateLimitType` that is not a window the Agent SDK names | `scrubbed` |
| there, every other field, by its type | `0`, `false` or `scrubbed` |

The same value always becomes the same placeholder, so the frames still replay. `type`
is kept because it names a kind of account, which the Codex adapter reads. Standard
error is scrubbed as text.

**Rate limits by a list of what is read.** Review of 2026-10-27, finding E3. The fields
of `rate_limit_info` that the Agent SDK `0.2.174` reads, which are the six named above
and `rateLimitType`, keep a placeholder valid for that reader. Dromon's adapter reads
none of them. Every other field is replaced by a fixed value of its type, so a field
that no one listed, such as a count of remaining requests, cannot pass through. Field
and window names are kept, and an absent value, `null`, stays absent. An upgrade of the
Agent SDK checks the list against its `RateLimitInfo`.

**Account values as words.** Review of 2026-10-27, finding E2. Every account value is
replaced wherever it stands as a whole word, however short, the longest first. A value
inside a longer word is not replaced. **Failure mode.** A short value that is also a
common word, such as a plan name, is replaced in unrelated text too, which may keep a
frame from replaying, and then the fixture is made from another recording. A value that
appears only outside the account block, such as a name in a message body, is not known
to the scrub, and only the reading by a person and the disclosure scan can catch it.

**Refusal.** The copy is refused, and nothing is written, when its text still holds
the home directory, the user name as a word in any case and of any length, an email
address, or an account value as a word, or inside a longer word once it is at least
four characters long. The text is also searched in its NFKC normal form, so a
full-width `@` or letter does not hide an address or a name. The refusal names the kind
of value, never the value. A short user name that is also a common word refuses every
recording, which fails safe. The check is by pattern, so a person still reads a fixture
before committing it, and the repository's disclosure scan covers it once it is
tracked.

**Not scrubbed.** Prompts and replies, model names, the engine's version, its slash
commands, skills, agents and plugins, the `rateLimitType` of each rate limit, process
identifiers, and the request and message identifiers the API assigns. A fixture shows
which commands and skills the operator's installation offered.

**Token counts and cost are kept.** Review of 2026-10-27, finding E1, by the operator's
decision. The `usage` of each message and the `total_cost_usd` of the result describe
the probe's own turn, not the account, and the adapter reads them on replay as it does
live. The account's use across turns is in `rate_limit_info`, which is scrubbed.

**The first fixture.** `tests/fixtures/probes/claude-code-haiku-2026-10-27.jsonl` is
that recording scrubbed. A test replays it through the real adapter and Agent SDK
client, and checks that the turn succeeds on the subscription with every recorded
frame exchanged in order.

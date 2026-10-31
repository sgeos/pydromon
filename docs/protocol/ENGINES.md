# Engines as data, and probe evidence

> **Navigation**: [Protocol](README.md)

**Status.** Draft, written on 2026-10-27, from rows 7 and 10 of the T3 Code review.
Normative for Dromon's side. The Python modules are `dromon.capabilities` and
`dromon.evidence`.

## Project read-only paths

The project setting `read_only_paths` names existing files or directories relative to
each line's resolved workdir. The daemon resolves real paths and refuses a missing
target or one that leaves the workdir. Claude Code adds sandbox write denials and file
tool permission denials. Codex marks each path `read` in its permission profile. Grok
requires each path in the profile's `read_only` array before starting its line. Pi,
Cursor Agent and Antigravity refuse a non-empty setting because their adapters cannot
confine writes to selected paths. The list defaults to empty.

**Inference for Grok.** The adapter checks that Grok's profile contains each path in
`read_only`, but no live run has verified that Grok prevents writes through those
entries. The operator should verify this before relying on path-level protection for a
Grok line.

## Capability tables

Each engine has one `EngineTable` in `dromon.capabilities`, and a port carries the
tables across as data. A capability is `yes`, `no` or `unknown`. Unknown is refused
wherever Dromon needs the capability, as `no` is, but it is reported as unknown, so an
operator can tell a limit of the engine from a gap in what Dromon has observed.

| Capability | Claude Code | Pi | Cursor Agent | Codex | Grok | Antigravity | Meaning |
|---|---|---|---|---|---|---|---|
| `resume` | yes | yes | no | yes | yes | yes | A line's engine session continues in a later turn |
| `kept_session` | yes | no | no | yes | no | no | A connection can be kept between turns |
| `own_tools` | yes | unknown | yes | yes | unknown | unknown | Dromon's own tools reach the model, through MCP or an extension |
| `deny_before_run` | yes | unknown | no | yes | unknown | no | Every built-in tool is denied before it runs |
| `read_only_workdir` | yes | no | no | yes | unknown | no | The adapter confines a read-only workdir while preserving the scratch exception |
| `cost_report` | yes | yes | no | no | no | unknown | The engine reports a cost for each turn |
| `max_turns` | yes | yes | no | no | no | no | Model calls in a turn are bounded, by the engine or by Dromon's count |
| `credential_visible` | yes | yes | yes | yes | unknown | no | Dromon can tell which credential a turn uses |
| `sign_in_check` | unknown | unknown | unknown | unknown | unknown | unknown | The engine can check its sign-in without a turn |
| `mcp_http_sse_headers` | yes | no | no | no | no | no | Dromon's adapter accepts HTTP or SSE MCP servers with headers |

**Pi was never run live.** Review of 2026-10-27, finding C1, by the operator's
decision. Its table once said `yes` for `own_tools`, `deny_before_run` and
`sign_in_check` from its documentation and source alone, so a Pi line's isolation was
reported as full. They are `unknown` until a live run shows them, and a Pi line's
isolation is reported as unknown. Its other capabilities are as the adapter's code
states them, which no live run has confirmed either.

**Grok, Codex and Antigravity.** Their adapters were being changed on another branch
when the tables were first written, and their columns were filled on 2026-10-27, after
it was merged, under the rule of finding C1. `own_tools`, `deny_before_run` and
`sign_in_check` are `yes` only where a live run showed them. The other capabilities are
as the adapter's code states them, and `unknown` where the code names its reading an
inference. A test checks each column's `kept_session` against what its adapter refuses.

**Grok's read-only workdir is unknown.** Review of read-only seats, finding 3. The
`dromon-reader` profile rests on Grok's built-in `read-only` base, which Dromon names but
cannot inspect, and no live run has shown that the profile denies writes to the
worktree. It stays `unknown` until an operator probe shows it, and the adapter refuses a
read-only line while it is anything but `yes`. Second round, findings 1 and 2. Codex's
`yes` rests on its adapter's code and its profile check, with no live observation of a
refused write recorded in `CODEX.md`.

- **Codex** was run live with Codex 0.164.0 and GPT-6-Luna on 2026-10-25, as
  [`CODEX.md`](CODEX.md) records. A resumed thread, Dromon's tools as dynamic tools and
  the account type were observed.
  - **Kept sessions, observed live.** By the operator's decision of 2026-10-27, after
    the review's finding T1, the evidence is named. The luna line has run with
    `keep_session` on throughout since round 3, decided on 2026-10-26, and the
    `codexkept` line in the operator's scratch project ran the live probes of round 7,
    of the hold and of compaction, on 2026-10-26, each on a kept app-server and thread.
  - **Denial before running, observed for some tool classes only.** By the operator's
    decision of 2026-10-27, after the review's finding T2, `deny_before_run` stays
    `yes`, and a Codex line's isolation is reported as full, on these observations of
    2026-10-25 with Codex 0.164.0 on macOS. Every tool the line was not given is
    switched off before the thread starts, which the live configuration and server
    listings showed. Under the permission profile, edits to `.dromon` and `.git` were
    rejected before they were written, a shell read of `.dromon` failed, and writing
    the home directory, reaching the network, and renaming, linking or changing the
    mode of `.dromon` failed through `codex sandbox`. Every other tool class, such as a
    read outside the workspace or a web search, was not observed, and its denial rests
    on the permission profile's documented behaviour. Codex emits no item for a call
    it refuses, so Dromon cannot count such a call. An item of a kind no one expected
    still interrupts the turn, as a second guard. A later build that adds a tool would
    need the check again.
- **Grok** was never run for a turn. An `initialize` answer from Grok Build 1.0.70
  advertised `loadSession`, which the adapter uses to resume. Whether Dromon's tools
  arrive under the `toolName` the adapter requires is the first step of the verify-first
  list in [`GROK.md`](GROK.md), and whether every built-in tool asks before it runs is
  not documented, so both are `unknown`, and a Grok line's isolation is reported as
  unknown. That its cached sign-in is a subscription is an inference, so
  `credential_visible` is `unknown`.
- **Antigravity** was never run live. A built-in tool that its runtime runs without
  asking is stopped only after it starts, as [`ANTIGRAVITY.md`](ANTIGRAVITY.md)
  records, so `deny_before_run` is `no`. Its cost is counted from `usage_update` but was
  never observed, so `cost_report` is `unknown`. The runtime does not say which
  credential it uses, so `credential_visible` is `no`.

**Derived from the tables.**
- The refusal of `keep_session` for Pi and Cursor lines.
- `isolation` in each `recent_turns` entry, `full`, `partial` or `unknown` from
  `deny_before_run`, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies.
- The engines `dromon probe --accept-partial-isolation` lets run, which are Cursor and
  Antigravity, both by their tables. Antigravity was named in the code until its table
  was filled.
- The probe's report, which names each engine's table, its tested minimum version or
  that it was never run live, and each capability.

**Unknown is not empty.** A Claude Code `init` message without a list of tools was
read as an empty list, which passed the check that the engine offers no tool the line
was not given. Such a report is now unknown, and refuses the turn.

## Engine process environments

Each external engine process starts with an explicit allowlist. The shared
operating-system entries are `PATH` for command lookup, `HOME` for the engine's
designated home, `LANG` for locale, `TERM` for terminal behavior, and `TMPDIR` for
temporary files. Missing entries remain missing. Each adapter adds only the
engine-specific names below. Adapter-created Dromon values such as a line token,
process mark, profile path or temporary home are added at the point that needs them.

Claude Code runs through the Agent SDK inside the daemon process. The SDK merges its
host environment into the launcher's initial process. Before starting Claude Code, that
launcher replaces itself with the same interpreter and arguments under a filtered
environment. The initial launcher process briefly has the inherited environment; after
the replacement, neither the watching launcher nor Claude Code receives unrelated
daemon variables. The filtered launcher retains the common operating-system entries,
Claude's allowlisted engine entries, its process mark and the launch control entries it
needs. It removes launch controls from Claude Code's environment. This sequencing means
the filtering occurs before any engine child starts. The SDK's separate version-only
probe starts no watcher; the launcher executes the resolved Claude Code command with
only the common entries and pinned `ENABLE_TOOL_SEARCH`.

| Adapter | Additional inherited entries | Scope |
|---|---|---|
| Claude Code | `SHELL`, `CLAUDE_CODE_SHELL`, `USER`, `LOGNAME`, `ENABLE_TOOL_SEARCH`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_AGENT_SDK_VERSION`, `CLAUDE_CODE_SDK_READS_SESSION_STATE`, `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING`, `PWD`, `TRACEPARENT`, `TRACESTATE`, `BAGGAGE`, `DROMON_LINE_MARK` | Claude Code uses the account names to find its macOS Keychain sign-in, confirmed by the operator's live check. Tool search is pinned on. The Agent SDK sets the entry point, its version and session-state behavior. Checkpointing and trace values are present only when the SDK sets them. The process mark is for tracking. Launcher target, parent, report, stop-policy and filter entries do not reach Claude Code. |
| Codex | `SHELL`, `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `XDG_CACHE_HOME` | Credentials are allowed only for `api` and `flexible` strategies. Subscription lines omit them. Dromon uses a supplied cache path or sets its private cache directory. |
| Pi | `OPENAI_API_KEY` | Only for `pi:openai/...` lines with `api` or `flexible` strategy. Subscription lines omit it. Other provider variables and endpoint selectors are not inherited. Dromon's Pi directory, bridge and tool values are set separately. |
| Cursor Agent | `CURSOR_API_KEY`, `CURSOR_AUTH_TOKEN`, `CURSOR_AUTH_TOKEN_EXPIRES_AT` | Only for metered strategies. Subscription lines omit them. Dromon sets the config and data directories separately. |
| Grok Build | `GROK_CLAUDE_MCPS_ENABLED`, `GROK_CURSOR_MCPS_ENABLED`, `GROK_CLAUDE_HOOKS_ENABLED`, `GROK_CURSOR_HOOKS_ENABLED` | Dromon sets these compatibility switches to disable scans of unrelated harness configuration. Its Grok home and sandbox are set separately. |
| Antigravity | none | Runtime path, isolated home, profile and temporary directory are set separately by Dromon. |

The adapter tables intentionally exclude every other daemon variable, including
unrecognized credential variables and endpoint overrides. A command relay receives
only the common operating-system entries. A teacher's Ollama server receives those
entries and Dromon's `OLLAMA_HOST`. MCP `${NAME}` expansion is a separate path. The
project file grants names per line, and Dromon resolves only those names into that
line's MCP configuration, never into the engine environment.

The adapter accepts HTTP or Server-Sent Events MCP headers only for Claude Code, which
is reflected by `mcp_http_sse_headers` in the capability report. Other adapters refuse
custom MCP server configuration.

## Shell selection

When a line is given a shell tool, Dromon requires the daemon's `SHELL` value to be an
absolute path to an executable file. A missing value, relative path or non-executable
file refuses the turn before the engine starts. Claude Code additionally accepts only
paths named `bash` or `zsh`, as required by its override. `line_status` reports whether
the line requires a shell, the path its adapter will use, or the validation reason.

The pinned Agent SDK source merges `ClaudeAgentOptions.env` into the CLI subprocess
environment. **Fact.** The bundled Claude Code executable contains the strings
`Using shell override:`, `CLAUDE_CODE_SHELL=`, `is not a valid bash/zsh path, falling
back to detection`, and `No suitable shell found. Claude CLI requires a Posix shell
environment. Please ensure you have a valid shell installed and the SHELL environment
variable set.` **Inference.** Claude Code reads `CLAUDE_CODE_SHELL` as an override and
accepts only a Bash or Zsh path. Dromon sets both `SHELL` and `CLAUDE_CODE_SHELL` to the
same validated path. The launcher relays only those adapter-selected values and drops
the daemon's own values, including for Claude Code lines without shell tools. No live
Claude Code turn was used to confirm selection.

Codex receives the validated `SHELL` in its app-server environment and in
`shell_environment_policy.set`, which the adapter uses for shell command variables.
**Inference.** The app-server selects its shell from `SHELL`, since the adapter has no
separate shell setting and previously omitted the variable. No live Codex turn was used
to confirm that selection. The Codex adapter supports `Bash`, `Edit` and `Write` only,
so it has neither Claude Code's `Monitor` nor `PowerShell` tool. If a later Codex
interface provides a documented shell executable setting, it should replace this
inference.

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
| Codex | none | 2026-10-25 | Not read. Codex 0.164.0 was run live, but the adapter reads no version from the app-server, so none is enforced |
| Grok | none | never | Not read. Grok was never run for a turn, so no version can be named as tested, and none is enforced |
| Antigravity | none | never | Not read as a tested minimum. The runtime was never run live, so none is enforced from this table |

**No minimum for Pi, Cursor Agent, Grok and Antigravity.** Review of 2026-10-27,
finding C2. Any version of these engines runs, and `dromon probe` says so in its
report, as `never run live, so no minimum version is enforced and any version the
engine reports runs`.

**No minimum for Codex yet.** Codex was run live, so its table names the date of that
run, but its adapter reads no version, so its report says `run live on 2026-10-25, but
no version is read, so no minimum version is enforced and any version the engine
reports runs`. **Next step.** A probe recording of a Codex turn shows where the
app-server reports its version, and the adapter then reads it from that field and
enforces 0.164.0. A table may name a minimum only with the date of its run, which the
table itself checks.

**Fact.** The Antigravity adapter already requires `antigravity-acp` 1.3.0 or later at
`initialize`, which [`ANTIGRAVITY.md`](ANTIGRAVITY.md) records. That version was taken
from a second implementation and was not run live, so it is not yet a tested minimum.

## Sign-in check without a turn

**Claude Code's turn result.** When a result contains both `Not logged in` and
`Please run /login`, Dromon refuses the turn even if Claude Code labels it `success`.
The result is not sent to the driver's success event. The scheduler records the
exception as a failed turn, holds the line and asks the operator to sign it in before
resuming.

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
- **Codex, Grok and Antigravity.** `unknown`, for the same reason. Codex reads its
  account with `account/read`, but only on an app-server Dromon has started, which the
  probe does for a turn. Grok's and Antigravity's sign-ins are read only by the engines
  themselves.

**Never a credential store.** No check opens a credential file itself. A test runs
Pi's check with its `auth.json` made unreadable, and the check still succeeds.

## The longest Claude Code message

The Agent SDK reads Claude Code's output one line of JSON at a time, and refuses a line
longer than its buffer size, whose default is 1 MiB. **Fact.** It counts the
characters of the line, not its bytes, although its error says bytes. A refusal stops
the SDK reading the engine, so the turn ends, and a kept connection is closed.

**What was seen, 2026-10-31.** A Claude Code line of another project ended its turn on
that refusal, inferred to be on a tool result that carried a file of about 1 MB, and
again after a two-line Edit of a 2.5 MB file. Each turn then said only that the
engine's process ended.

**Why an Edit is large.** Read in Claude Code 2.1.310, as bundled with Agent SDK
0.2.174, without running it. **Fact.** Its schema for a user message in the stream
describes `tool_use_result` as the tool's full output object, not the text the model
is sent, and its stream writer copies the tool's output there. Edit's output holds
`originalFile`, the file's contents before the edit, beside the patch, and a Write
that replaces a file holds both the old and the new content. The code that empties
those fields was found only where Claude Code stores copies of results on disk, under
its `toolResultCopiesOnDisk` setting. **Inference.** An Edit's message is at least as
long as the file it edits, however small the edit. No live turn confirmed it.

**The bound.** `dromon.engine.MAX_MESSAGE_CHARACTERS`, 32 MiB of characters, is
passed as `ClaudeAgentOptions.max_buffer_size` on every Claude Code turn. It is over
ten times the largest file a line was seen to edit, which leaves room for a result
that carries a file twice and for the escapes of its JSON form. **Inference.** While
it reads one message the SDK holds about four copies of it, its chunks, the joined
text, the split line and the parsed object, so one message at the bound costs some
130 MB of memory for ASCII text, and up to four times that for text whose characters
take four bytes each. Each line reads its own engine, so the worst case grows with the
number of lines that receive such a message at once. A larger bound would only move
the failure to a larger file.

**A message past the bound.** The turn ends with `EngineRefused` and the text of
`MESSAGE_TOO_LONG`, which names the limit and gives a tool result that carries a large
file, such as an Edit, a Write or a Read of a file of several megabytes, as the likely
cause. That holds for a one-shot turn and for a kept connection's turn, whose reader
records the cause before the connection is marked dead, which tests cover. A
self-started turn Dromon admitted is given the same text, which no test covers yet.
The next turn starts a new engine, and its prompt says the old one
ended. Any other error the SDK raises ends the turn as before. **Porting hazard.** The
SDK gives the refusal no type of its own, so `dromon.engine.TOO_LONG_PREFIX` matches
the start of its text, and a test takes that text from the SDK's own reader, so an SDK
upgrade that changes it fails there.

## Probe evidence

`dromon probe --record` writes every frame the adapter sent and received during its
one turn to a new file under the daemon's state directory, in `.dromon/probes`, named
for the engine and the time in Coordinated Universal Time. The file is created with
owner-only permissions, in a directory of mode 0700, and is never tracked.
`--record-stderr`, accepted only with `--record`, also keeps the engine's standard
error, at most 64 KiB, masked by `dromon.redact`.

**Where, and why.** Review of 2026-10-27, finding R1, by the operator's decision. A raw
recording holds the account's email address and organization, and the shared `tmp/`
can be read by every line, so the raw file is kept where the sandbox closes the
directory to every line. Only a scrubbed copy, which `--scrub` writes to `tmp/probes`
by default, is put where lines and tests can read it. A recording made before this
change may still lie under `tmp/probes` and is moved by the operator, since no line
may write the state directory.

**Standard error alike, recording or not.** Review of 2026-10-27, finding R2. The
engines whose adapters record, every one but Claude Code, send their standard error to
a pipe that Dromon reads to its end whether or not a probe records, so a probe's engine
and a daemon line's engine run alike, and only what is kept differs. **Failure mode.**
A descendant left running that holds standard error open delays the end of a
connection by at most 10 seconds, the bound on that read, where the null device once
cost nothing.

**A failed write.** Review of 2026-10-27, finding R3. A write to the recording that
fails, such as on a full disk, ends the recording and is noted, and the turn goes on.
The probe still prints its report, says the recording is incomplete, and exits with a
failure.

**What a recording holds.** Frames are recorded as they were exchanged, with prompts
and replies, so a recording is read by a person before it is kept as a test fixture.
Masking applies only to standard error, and it is by pattern, as `dromon.redact`
states. A recording holds at most 20,000 frames and 32 MiB of them, then marks itself
truncated and records nothing more.

**Which engines record.** Every engine. Claude Code records through a transport that
wraps the Agent SDK's own. Pi, Cursor Agent and Antigravity record in
`dromon.jsonlines`, which starts their engines. Codex and Grok frame their own traffic,
and from 2026-10-27 their own process wrappers record it in the same way, with the
same handling of standard error, which `dromon.evidence` holds for all three. The
`codex mcp list` that Codex runs before the turn is a separate command, not part of the
exchange, and is not recorded. `--record` still refuses an engine added later whose
adapter does not record. **Porting hazard.** The Claude Code recorder implements
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

- **Pi, Cursor Agent, Antigravity, Codex and Grok.** `replay_factory` is passed as the
  adapter's connection factory, so the real adapter runs against the recording. Codex
  also takes a server lister in place of `codex mcp list`.
- **Claude Code.** `replay_session` runs the real adapter and the real Agent SDK client
  over a replayed transport. The SDK names each control request it sends at random, so
  the answer to the recording's `n`-th control request is renamed for the `n`-th the
  SDK sent.

**Tested.** A Claude Code turn recorded through a scripted engine and replayed through
the real adapter and SDK client gives the same report. A Cursor recording replays
through the real adapter, and one cut before its result fails as a live stream would.
A Codex turn and a Grok turn, each with one of Dromon's tools, recorded from a scripted
engine, replay through the real adapter with the same report, every frame exchanged
and the same frames sent. Codex's and Grok's process wrappers record a scripted engine
started as `codex` and as `grok`, with its standard error masked. A recorded Codex
turn also replays through the real process wrapper, from a stand-in `codex` that plays
the recording and stops at the first frame that differs, so the wrapper, its standard
error drain and the launcher run, and the wrapper records the same frames again, at
the review's finding P1. An engine that writes more standard error than a pipe holds
still answers through every wrapper without a recording. **Not yet tested.**
No live Codex or Grok recording exists, so neither engine's real frames have been
replayed.

## Scrubbing a recording into a fixture

A raw recording is never tracked. The first live Claude Code recording, of 2026-10-27,
held the account's email address and organization in the `initialize` response, the
operator's home directory, session identifiers and message universally unique
identifiers. `dromon probe --scrub FILE` runs no turn. A relative `FILE` is read from
`.dromon/probes`, where `--record` writes. It writes a copy to `--scrub-to PATH`, or
by default to the project's `tmp/probes` with `.scrubbed.jsonl` in place of `.jsonl`,
and never overwrites. `dromon.evidence.scrub_file` does the same for a test or a script.

| Value | Placeholder |
|---|---|
| each field of an `account` block, other than `type` | `scrubbed-account-<field>`, or `0`, `false` or `null` by its type |
| anywhere, the value of a key with `account`, `org` or `organization` as a word of its name, through each object and list, other than a `type` | the same, by the innermost key |
| anywhere, a string, number or boolean under a key with `email` or `plan` as a word of its name | the same |
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
| in Codex's `rateLimits`, every value as in `rate_limit_info` | the same placeholders |

The same value always becomes the same placeholder, so the frames still replay. `type`
is kept because it names a kind of account, which the Codex adapter reads. Standard
error is scrubbed as text.

**Keys by the words of their names.** Review of 2026-10-27, finding S2, by the
operator's decision. A key's name is split into words in camel case, snake case or
kebab case, so `planType`, `orgId` and `user_email` are covered and `explanation` is
not. A list or object under an `email` or `plan` key is scrubbed as any other value,
because Codex's `turn/plan/updated` holds the agent's plan steps, whose status words
such as `completed` would otherwise be replaced in every frame, including in method
names, and the frames would no longer replay. An email address under any other key is
not replaced, and the copy is refused, as below.

**Codex's plan usage.** From 2026-10-27. Codex reports its account in `account/read`,
whose `account` block is scrubbed as any other, and its plan's use under `rateLimits`.
The Codex adapter reads `credits.hasCredits` and `credits.unlimited` on a notice only
when both `primary` and `secondary` windows are present and null, and it requires both
credit values to be false. The scrubber preserves that shape and the null windows, but
replaces other values by type. It cannot preserve a positive balance or usage
percentage. **Inference, not yet seen in a recording.** That Codex names the key
`rateLimits` is taken from its app-server protocol. A person reads the first Codex
recording before it becomes a fixture, as for any engine.

**Rate limits by a list of what is read.** Review of 2026-10-27, finding E3. The fields
of `rate_limit_info` that the Agent SDK `0.2.174` reads, which are the six named above
and `rateLimitType`, keep a placeholder valid for that reader. Dromon reads status,
overage status, reset time and window name to classify a rejected window, and reads
utilization to record a positive remaining percentage for that window. It records
each window separately and exposes the lowest unexpired remaining percentage, with
that window's reset. Percentage conversion uses integer arithmetic rounded toward
less remaining. A positive snapshot cannot replace a stored zero before its reset.
Every other field is replaced by a fixed value of its type,
so a field that no one listed, such as a count of remaining requests, cannot pass
through. Field and window names are kept, and an absent value, `null`, stays absent. An
upgrade of the Agent SDK checks the list against its `RateLimitInfo`.

**Account values as words.** Review of 2026-10-27, finding E2. Every account value is
replaced wherever it stands as a whole word, however short, the longest first. A value
inside a longer word is not replaced. **Failure mode.** A short value that is also a
common word, such as a plan name, is replaced in unrelated text too, which may keep a
frame from replaying, and then the fixture is made from another recording. A value that
appears only outside the keys above, such as a name in a message body, is not known to
the scrub, and only the reading by a person and the disclosure scan can catch it.

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
live. The account's use across turns is in `rate_limit_info`, which is scrubbed. The
same decision covers Codex, by the operator's instruction of 2026-10-27 after the
review's finding S1. The `total` and `last` blocks of `thread/tokenUsage/updated`
describe the probe's own thread and are kept, and the plan's use across turns is in
`rateLimits`, which is scrubbed.

**The first fixture.** `tests/fixtures/probes/claude-code-haiku-2026-10-27.jsonl` is
that recording scrubbed. A test replays it through the real adapter and Agent SDK
client, and checks that the turn succeeds on the subscription with every recorded
frame exchanged in order.

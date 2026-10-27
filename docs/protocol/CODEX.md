# Codex Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative for Dromon's side. The Python module
`dromon.codex` implements it against Codex 0.164.0. The app-server protocol is OpenAI's,
documented as experimental, and Dromon also relies on its experimental dynamic tools.

## Purpose

**Directed by the operator on 2026-10-25.** OpenAI models run on Dromon, tested with
GPT-6-Luna. A line whose driver names the `codex` engine runs its turns through
`codex app-server`, signed in by Codex itself on the operator's ChatGPT plan or with an
API key.

## Transport

Each turn starts `codex app-server` with standard input and output as the transport.
Messages are JSON-RPC requests, responses and notifications without a `jsonrpc` field,
one JSON object per line. Dromon reads one message at a time and answers every request
from the engine before reading on. The process is stopped when the turn ends.

## A turn

1. `initialize`, with `capabilities.experimentalApi` set, then `initialized`.
2. `account/read`. The account type `chatgpt` is a subscription and `apiKey` is API
   usage. Any other type is refused. The driver accepts or refuses the credential
   before any thread exists.
3. `thread/start` for a new line, or `thread/resume` with the stored thread
   identifier, with the driver's model, the line's working directory as `cwd`, which is
   its `workdir` as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies or the project root,
   `permissions` set to
   `dromon-line`, `approvalPolicy` set to `never` and `approvalsReviewer` set to `user`.
   A new thread also receives Dromon's own tools as `dynamicTools`. A thread is resumed
   only when the tools it started with are today's, as the tools section below
   specifies, and a new thread is started otherwise.
4. The response's `modelProvider` must be `openai`, its `activePermissionProfile` must
   be `dromon-line`, and its `model` is handed to the driver, which refuses any model
   but its own.
5. `mcpServerStatus/list` for the thread must report every server as `disabled` with
   no tools, and `app/list` must list nothing.
6. `turn/start` with the wake prompt as text and the line's effort, or the driver's.
7. Notifications become driver events until `turn/completed`. A started
   `dynamicToolCall` of one of Dromon's tools, a `commandExecution` for a line given
   `Bash`, or a `fileChange` for a line given `Edit` or `Write`, is tool use. A `reasoning` item is thinking, and a completed `agentMessage`
   is text. Any other tool item, and a `model/rerouted` to a model the driver refuses,
   interrupts the turn with `turn/interrupt` and refuses it.
8. `item/tool/call` requests run Dromon's tool and answer with `success` and text
   `contentItems`. Approval requests are declined, and any other request is answered
   with an error.
9. A subscription limit with a recognized structured error code is classified
   separately. Dromon includes a reset time when Codex reports one and holds the line
   for operator release, so the next tick does not retry against the same limit.

## Plan limits

**Inference, not observed in a live app-server error.** The structured codes
`plan_limit_reached`, `plan_limit_exceeded`, `usage_limit_reached` and
`usage_limit_exceeded` identify subscription limits. When Codex supplies no recognized
structured code, Dromon uses the specific phrases `plan limit`, `usage limit`,
`reached your limit`, `reached the usage limit` and `quota exceeded`. The generic word
`quota` does not classify an error. A live app-server error is needed to confirm this
vocabulary. A reported reset time is kept in the operator question that holds the line.

## Tools across threads

For `workdir_access: read`, the `dromon-line` permission profile marks the working
directory read-only and grants writes only to its `tmp` subdirectory. This setting is
part of the stored thread fingerprint, so a resumed thread with different access is
replaced before a turn starts.

**Decided by the operator on 2026-10-26.** **Fact, observed live on 2026-10-26.** A
resumed thread saw none of the tools Dromon added after it started.

**Inference, read on 2026-10-26 in the embedded strings of the installed Codex 0.164.0
binary, not tested live.** Codex gates its experimental request fields per method, and
its list names `thread/start.dynamicTools` but no `dynamicTools` for `thread/resume`,
whose own experimental fields are `history`, `path`, `runtimeWorkspaceRoots`,
`permissions`, `initialTurnsPage`, `activePermissionProfile` and `multiAgentMode`. So a
thread keeps the tools it was started with.

Dromon therefore stores a Codex line's engine session as the thread identifier, a
colon, and the first 16 hexadecimal digits of the SHA-256 of the sorted names of the
line's Dromon tools. At each turn, a stored session whose fingerprint differs from
today's tools, or that has none, as one stored before fingerprints existed or adopted
from elsewhere, is not resumed. A new thread is started with today's tools, and the
turn's text opens with a note telling the line that its earlier thread's context is
not available and that its mailbox, files and reports are unchanged.

## Process mark

**Decided by the operator on 2026-10-26.** The app-server is started with
`-c shell_environment_policy.set.DROMON_LINE_MARK="..."`, so the commands Codex runs
carry the line's mark and Codex's own processes do not. **Fact, read on 2026-10-26** in
the installed Codex 0.164.0 binary. Its help names `-c shell_environment_policy.inherit=all`
as an example override, and its configuration errors name `shell_environment_policy.set`.
That a `set` entry reaches every shell command is an inference from that, not yet
observed live.

## Isolation

Codex loads the operator's user configuration, which cannot be skipped by the
app-server. Dromon starts it with these arguments.

- `-c mcp_servers.NAME.enabled=false` for every server `codex mcp list --json` names.
  A name that is not a bare key is refused, because Codex rejects a quoted one.
- `--disable` for `apps`, `browser_use`, `browser_use_external`, `computer_use`,
  `goals`, `hooks`, `image_generation`, `in_app_browser`, `memories`, `multi_agent`,
  `plugins`, `remote_plugin`, `skill_mcp_dependency_install`, `skill_search`,
  `sleep_tool`, `tool_suggest` and `view_image`.
- `shell_tool` and `unified_exec` enabled only for a line given `Bash`, and disabled
  otherwise.
- `-c web_search="disabled"`.

**Fact, observed on 2026-10-25.** Without these arguments a GPT-6-Luna thread on the
operator's machine connected to the operator's mail, calendar, document, chat and task
servers, one of them with 47 tools. With them every server reported `disabled`, and
`config/read` reported web search and the disabled features as off. A
`-c mcp_servers={}` override did not remove any server.

## Permission profile

Every thread runs under `dromon-line`, a Codex permission profile that Dromon defines
on the app-server's command line as one inline TOML value, because Codex's `-c` parser
takes only bare keys and the profile's own keys need quoting. Codex's built-in
read-only sandbox would let a line read the event log and its stored credentials, and
its writable sandbox would let a line write them, so neither is used.

```toml
[permissions.dromon-line]
extends = ":workspace"

[permissions.dromon-line.filesystem]
":workspace_roots" = { "." = "write", ".git" = "read" }
"/real/path/of/project/.dromon" = "deny"
"/real/path/of/runtime" = "deny"
```

- The workspace root `.` is the line's working directory. Its relative `instructions`
  paths resolve there too.
- The workspace is `write` for a line given `Edit` or `Write`, and `read` otherwise.
- `.git` is always `read`, so a Codex line never commits. Codex documents that a
  profile extending `:workspace` keeps `.git` read-only, and a reported issue says it
  does not, so Dromon states it.
- Each protected directory that exists, the state directory and the runtime
  directory, is denied as its real path.
- Network access is off, as `:workspace` leaves it, so a Codex line cannot reach a
  local teacher.
- Codex's `:workspace` also leaves `$TMPDIR` and `/tmp` writable.

**Facts, verified on 2026-10-25 with Codex 0.164.0 on macOS.** Under the profile through
`codex sandbox -P`, a write in the workspace succeeded. Reading or writing `.dromon`,
writing `.git`, writing the home directory and reaching the network failed with
`Operation not permitted` or no connection. So did renaming `.dromon` or `.git` away,
replacing `.dromon` with another directory, a symbolic or hard link into it, and
changing its mode. An absolute deny on a runtime directory outside the workspace
refused reads and writes there. Through Dromon, a GPT-6-Luna line given `Edit` wrote a
file in the workspace, Codex rejected its edits to `.dromon` and `.git` with "patch
rejected: writing outside of the project", its shell read of `.dromon` failed, and in
the epagoge project the line ran the project's full gate, which passed.

**Fact, observed the same day.** Codex emits no item for an edit it rejects or for that
shell read, so Dromon's driver cannot count a call that Codex refuses before running
it.

## Credentials

Codex keeps its own login, and Dromon never reads, stores or forwards it. For a
subscription driver, Codex is started without `OPENAI_API_KEY`, `CODEX_API_KEY` and
`CODEX_ACCESS_TOKEN`, since Dromon starts the process and so controls its environment,
and the account must still read `chatgpt`. `OPENAI_BASE_URL` is refused for every
driver. **Fact, observed on 2026-10-25.** The operator's environment sets
`OPENAI_API_KEY`, and with it removed Codex reported a ChatGPT account.

## What a Codex line may have

| Configuration | On a Codex line |
|---|---|
| `builtin_tools` | `Bash`, which enables Codex's shell, and `Edit` or `Write`, which make the workspace writable for Codex's file changes and its shell |
| `mcp_servers`, `inherit_account_mcp` | Refused, not yet supported |
| `sandbox`, `settings` | Refused, including the project's own tables, which `dromond` checks at start |
| `tool_policy`, `allowed_tools` | Accepted, with no effect beyond Dromon's own tools |
| `effort` | Passed with each turn |
| `instructions` | Their text becomes the thread's developer instructions |
| `claude_code_prompt` | Refused, since Codex uses its own instructions |
| `project_settings` | Refused, since it loads Claude Code's settings |
| `plugins` | Refused, since they are Claude Code plugins |

A configuration a Codex line cannot run is refused when the line is created or
configured.

## Verification

**Fact, verified on 2026-10-25 with Codex 0.164.0 and GPT-6-Luna on the operator's
ChatGPT plan.** The live tests of `tests/test_codex.py` and
`tests/test_scheduler.py` showed that a line resumes its thread with its dynamic tools,
calling `record` in its second turn with a word given in its first, and that a
GPT-6-Luna line sends a message to a Claude Haiku 5.5 line, which is woken and runs its
turn. `dromon probe --model gpt-6-luna` reported the subscription credential.

## Not at parity with Claude Code lines

- No MCP servers, no sandbox or settings documents, and no way to commit, since `.git`
  is always read-only. In a linked worktree `.git` is a file, and the common git
  directory, with its hooks and configuration, lies outside the workspace. It is not
  writable either, because the profile leaves everything outside the workspace
  read-only apart from the temporary directories. That rule comes from Codex's
  `:workspace` profile, which `dromon-line` extends. Dromon's own entries grant writes
  only to the workspace root and add denies, as `permission_profile` in
  `dromon.codex` shows. **Fact, observed on 2026-10-25** under the profile, as recorded
  above. A write in the home directory failed. Two exceptions follow from the same
  rule. A common git directory under `$TMPDIR` or `/tmp` would be writable. So would one
  inside the workspace, when a line's `workdir` contains another checkout's git
  directory, since only the workspace's own `.git` is held read-only.
- **A cache by default.** **Decided by the operator on 2026-10-26.** Codex starts with
  `XDG_CACHE_HOME` set to `dromon-cache-` and the user's number under the system
  temporary directory, which the profile leaves writable, unless the operator's
  environment sets it. The daemon creates it with mode 0700, and refuses the turn when
  it is a link, not a directory, owned by another user, or writable by its group or
  others, since on a shared temporary directory another user could make it first and
  plant cache entries. **Inference, not tested live.** Tools that honour it, uv among
  them, then have a writable cache. A cold cache still needs the network to fill, and a
  Codex line has none, so a tool that must download still fails until the cache holds
  what it needs.
- No extra writable paths. **Fact, observed on 2026-10-26 in pydromon.** uv could not
  write its cache in the home directory, so a Codex line runs the gate with
  `UV_CACHE_DIR` under `$TMPDIR` and `UV_OFFLINE=1`, which works while the environment
  already matches the lock. A tool that stats the state directory, as pytest's
  collection did, fails with `Operation not permitted`, because the profile denies its
  metadata too, so pydromon's pytest configuration ignores `.dromon`.
- A call Codex refuses before running it is invisible to the driver's tool budget.
- Codex reports no cost, so cost limits do not count a Codex line's turns, and the
  driver's `max_turns` is not enforced. The tool budget still interrupts a turn.
- The app-server and dynamic tools are experimental, so a Codex update may break the
  adapter.
- **Inference, not checked.** Codex loads its usual `AGENTS.md` instruction files,
  while a Claude Code line loads no settings sources and so no `CLAUDE.md`.
- **A kept session.** **Decided by the operator on 2026-10-26** in round 3, at the
  request of a Codex line. A Codex line with `keep_session` keeps its app-server and
  thread between turns, as [`LINE_CONFIG.md`](LINE_CONFIG.md) specifies for kept
  sessions. Every Codex line runs under the `dromon-line` permission profile, so the
  rule that a kept line with a shell needs an enabled sandbox is met by construction.
  **From 2026-10-26** the ceiling, `keep_session_at_max` and the hard bound apply to
  the background terminals Dromon last listed, as to a Claude Code line's tasks, and an
  operator's stop closes a kept app-server that still lists the line's terminals once
  the stopped turn has ended. The app-server runs under Dromon's launcher, which is
  given the line's mark in a variable of its own and removes it, so Codex itself still
  does not carry the mark.
  - **Facts, read from the installed Codex 0.164.0 binary on 2026-10-26, not run.** The
    app-server has `thread/backgroundTerminals/list`, `thread/backgroundTerminals/terminate`
    and `thread/backgroundTerminals/clean`. A listed terminal has seven fields, of which
    `itemId`, `processId`, `osPid`, `cpuPercent` and `rssKb` were read. The list takes
    three parameters, termination two and cleaning one. Termination reports an invalid
    background terminal process identifier as an error. Resuming a thread that is
    already loaded logs that its overrides are ignored. The interactive client reports
    background terminals still running between turns, with commands to view and stop
    them.
  - **Inferences, for the live probe.** The parameters are `threadId` for the list and
    `threadId` with `processId` for termination. A terminal outlives the turn that
    started it while the app-server runs. Resuming the loaded thread returns its
    model, provider and active permission profile, so the checks can be made on it.
  - **Every check at every turn.** Before a kept app-server is used, the environment,
    the configuration, the MCP server listing, the instructions and Dromon's tools are
    checked and digested as at a new start, and any difference opens a new
    app-server. On the kept connection the account is read again, the loaded thread is
    resumed and its provider, model and permission profile checked, and the MCP server
    and app listings checked, all before the turn starts. Any refusal, and any failed
    request, closes the connection.
  - **A message router.** A pump reads every message. A response to a request made
    outside the turn's own loop resolves that request, so a Dromon tool can list
    terminals during a turn without a deadlock. Writes come from the turn, the pump, a
    tool's request and an interrupt, so each message is written whole under a lock.
    Between turns a request from the engine is refused, an approval declined, and a
    turn the engine begins by itself interrupted and noted. An interrupt is never sent
    without the turn's identifier, so a turn begun without one closes the app-server
    instead.
  - **The line's own terminals.** Dromon does not rely on the app-server scoping a
    listing to the thread. It records the process and item identifiers of every
    command execution the line's own turns report on the connection, and a listed
    terminal is the line's only if one of its identifiers matches.
  - **`list_background` and `stop_background`.** Only a Codex line with `keep_session`
    has them. The first lists the line's own background terminals, at most 64, with
    `process_id`, `os_pid` and `command`, where the command field's name was not read
    and is looked for as `command`, `commandLine` or `cmd`. The second takes a
    `process_id` of 1 to 128 characters, which must be in a fresh listing of the same
    thread, and terminates it. Both refuse when no Codex session is kept.
    Listings follow `nextCursor` for at most 16 pages. A malformed cursor, a walk that
    remains unfinished, or a result over 64 terminals is an error and the terminal
    state is treated as unknown. A failed listing preserves the last known terminal
    set and cannot trigger the idle deadline. The connection stays open until its hard
    bound or a later successful listing resolves the state.
  - **Closed output.** If the app-server closes standard output while its process stays
    alive, Dromon waits at most two seconds for process exit, then marks the connection
    dead and fails pending requests.
  - **Inherited usage.** On a resumed thread the first usage notice may include totals
    from earlier turns. Dromon subtracts that notice's latest call to establish the
    inherited baseline, so the current turn reports only its own usage.
  - **One lifecycle.** **Decided by the operator on 2026-10-26** in round 4. The last
    listing of the line's own terminals is the one source of both `list_background`
    and the status's task list, which also gives how many seconds ago it was listed and
    how many terminals the engine listed in all. A listing is made at the end of each
    turn, by `list_background`, and between turns by polling. Identifiers are matched
    as text whether the engine sends text or a whole number. Commands are named from
    the line's own command execution items when a listing gives none.
  - **Exit code and output.** **Decided by the operator on 2026-10-26** in round 5. When
    the engine reports a `commandExecution` item completed, in a turn or between
    turns, its `exitCode` and `aggregatedOutput` are kept for the line's own command,
    by its process and item identifiers. A completion, in a turn or between turns,
    counts only when its item identifier is already the line's, since a process
    identifier names a process and not the item, and Dromon does not rely on the
    engine scoping items to the thread. At the cap of 1,024 identifiers the oldest are
    forgotten one at a time, with their commands and completions, but never one that
    names a terminal still listed or an end still waiting to be noted. A terminal's end found by a listing
    waits up to one polling interval for that report, and the note then gives the exit
    code and the first lines of the output, quoted as data, or says that the engine
    reported neither. **Inference, not run.** The field names are those the
    app-server's items use for a finished command.
  - **Why the two views disagreed.** **Not established.** The luna line saw a task in
    the status that `list_background` did not list. In round 3 the status showed the
    listing taken at the end of the previous turn, while the tool listed afresh, so the
    two were taken at different times. Round 3 also matched identifiers as text only,
    and the task's identifier, 59780, looks like a number. Either could explain it, and
    the raw listing was not kept to tell which. The status now gives the listing's age
    and the engine's count, which will show the difference if it happens again.
  - **A note when a terminal ends.** Codex was not found to report a terminal's end, so
    while the line has a listed terminal, a kept connection lists its terminals again
    between turns, at most once every 15 seconds, and stops after 3 failed listings
    until the next turn ends. A terminal that a listing no longer holds, other than one
    the line stopped with `stop_background` or saw end in a listing of its own turn,
    wakes the line with the same task-end note, with the status `ended`, since the
    listing gives no exit code. An end found by the listing after a turn is noted too,
    since the engine does not tell the model that a terminal ended.

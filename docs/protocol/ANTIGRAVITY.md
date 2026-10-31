# Antigravity Engine

Antigravity refuses a line configured with `workdir_access: read`, because its
adapter cannot confine writes to the working directory.

It also refuses a project with non-empty `read_only_paths`, because it cannot confine
writes to selected paths under a line's workdir.

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, rewritten on 2026-10-26. Normative for Dromon's side. The Python
module `dromon.antigravity` implements it. **The operator decided on 2026-10-26** to
drive Google's Antigravity ACP runtime, `agy_acp_server`, in place of `agy --print`.
**The runtime is not installed on the operator's machine, so nothing here was observed
by Dromon.** A daemon refuses Antigravity lines. `dromon probe
--accept-partial-isolation --accept-unverified-credential` runs one turn.

## Purpose

A prospective adapter, ready for one `dromon probe` turn once the operator has
installed the runtime, signed it in and confirmed by hand that the sign-in is a plan. A
line whose driver names the `antigravity` engine runs each turn through the runtime
over the Agent Client Protocol, ACP, through the client it shares with
[`GROK.md`](GROK.md) in `dromon.acp`.

## Evidence

Each claim below is labelled. **Second implementation** means that T3 Code, an
open-source client that runs this runtime for real, shows it in its source or
documentation, as the review of T3 Code made for the operator on 2026-10-26 recorded,
read at the commit then current and not copied. That is stronger
than a guess and weaker than a live test by Dromon, since T3 Code may be wrong or out
of date. **ACP schema** means the protocol's published version 1 schema, as T3 Code's
generated copy holds it.

- **Second implementation.** The runtime is a program separate from Antigravity's app
  and its `agy` command, with its own sign-in. It is `agy_acp_server.par` on macOS and
  Linux, with a `localharness_external` helper that must sit beside it at the same
  version. T3 Code sets `ANTIGRAVITY_HARNESS_PATH` to that helper.
- **Second implementation.** The runtime keeps its profile, sign-in and settings under
  `GEMINI_HOME`. A private `GEMINI_HOME` keeps the user's `~/.gemini` hooks, MCP
  servers and rules out. Global hooks live in `config/hooks.json` and MCP servers in
  `config/mcp_config.json` under the profile, and either can run before a client
  refuses a tool. The runtime's own `antigravity-acp/settings.json` names the sign-in
  method in `auth.type` and never holds a credential. On macOS the sign-in is kept in
  the keychain unless `AGY_ACP_FORCE_FILE_STORAGE` is set, and then it is the file
  `antigravity-acp/acp_token.json` under `GEMINI_HOME`. T3 Code sets that variable.
  It finds the user's home directory as Python's `os.path.expanduser` does, from
  `HOME`, which suggests that the runtime does too.
- **Second implementation.** `initialize` answers with `agentInfo` naming
  `antigravity-acp` and a version, and with `authMethods`. The client chooses a method
  with `authenticate`. The methods are `oauth-personal`, a Google account sign-in,
  `oauth-business` for Gemini Enterprise, `gemini-api-key` and `agent-platform`. The
  runtime accepts methods it does not list. An API key in `GOOGLE_API_KEY` takes
  precedence in the environment.
- **Second implementation.** When it is not signed in, the runtime writes a plain-text
  line beginning `Open the following link to authenticate the ACP server: ` with a
  Google sign-in address, and answers with error code `-32000`. It opens a browser
  through `BROWSER`, with a loopback callback on its own machine.
- **Second implementation.** The runtime accepts `mcpServers` in `session/new`. Its
  permission modes are `default`, `auto_edit` and `yolo`. It sends
  `session/request_permission` with `allow_once`, `allow_always` and `reject_once`
  options, also in `yolo`. Its own questions use the same request with a tool call
  identifier beginning `interaction_`. It reports a subagent as a tool titled
  `start_subagent`. It resumes with `session/resume`, which replays no history, and
  cannot roll back a conversation. A cancel is followed by the prompt's own answer.
- **Second implementation.** In T3 Code's scripted runtime, a call of an MCP tool is a
  `tool_call` titled `Running html_render`, of the kind `other`, with no `toolName`.
  T3 Code tells its own tool's calls apart on its MCP server's side, not from the
  title. If the real runtime reports MCP calls so, Dromon refuses every use of its
  own tools, which is safe, as Tools under A probe turn says.
- **Second implementation.** The runtime unpacks about 1 GB on each launch, so T3 Code
  never starts it for a health check and gives each process its own temporary
  directory.
- **ACP schema.** The update kinds of version 1, the tool kinds, the stop reasons
  `end_turn`, `max_tokens`, `max_turn_requests`, `refusal` and `cancelled`, and
  `session/set_config_option` with `configId` and `value`.
- **ACP schema.** An agent must answer a cancelled turn's pending permission requests
  as cancelled, and a cancelled outcome grants nothing.
- **ACP schema.** A `config_option_update` carries the full set of `configOptions`. A
  `usage_update` carries `used`, the tokens in context, `size`, the context window,
  and an optional `cost` with an `amount` and a `currency`, the session's running
  total. A `compaction_update` and a `compaction_summary_chunk` report a compaction.
  The schema does not define `toolName` on a tool call.

## How the operator obtains the runtime

Dromon downloads and installs nothing. **Antigravity review, F10.** Everything this
section says about where the runtime is released, which version is current and what
its checksums are comes from T3 Code's code, which is unverified third-party
material. Google's own release has not confirmed any of it. **Each item below is a
hypothesis, not a pin**, until verify-first step 1 confirms it against what Google
publishes, and the procedure has no download step until then.

- **Hypothesis.** T3 Code downloads version 1.3.0 from
  `dl.google.com/agy-extensions/releases/` as `agy-acp-server-1.3.0-PLATFORM.zip`, and
  checks each archive's SHA-256 against the values below before unpacking it.
- **Hypothesis.** T3 Code's user guide points to the runtime's entry in the ACP
  registry, `agentclientprotocol/registry`, `antigravity-acp/agent.json`.
- **Hypothesis.** T3 Code's compatibility manifest lists runtime 1.1.1 as supported
  while it downloads 1.3.0, so its own sources disagree on the version. Dromon's
  minimum of 1.3.0 rests on the same material.

| Platform | Archive, hypothesis | SHA-256 in T3 Code, unverified |
|---|---|---|
| macOS, ARM64 | `macos/agy-acp-server-1.3.0-darwin-arm64.zip` | `7cd97045f7b4fe81175a107cdf16f9c51484e3c78a5162cae415338bb6aa5b88` |
| macOS, Intel | `macos/agy-acp-server-1.3.0-darwin-x86_64.zip` | `bb23956b89984bf5d354af2c3725e6c57f0cc1b7228e77a0e91c9c2bc1d47646` |
| Linux, x86-64 | `linux/agy-acp-server-1.3.0-linux-x86_64.zip` | `9fb60956af0a9d76220a4db91ca9ac88e2a2372ad68f985ab5fceace6b825b96` |
| Linux, ARM64 | `linux/agy-acp-server-1.3.0-linux-arm64.zip` | `500b0bc0fb858e88f4df404d4cedf80bf9298c178291e39e383d6c50b111cbdf` |

Once the runtime is obtained from a source verify-first step 1 has confirmed, the
operator places `agy_acp_server.par` and `localharness_external` in one directory,
makes both executable, and either uses Dromon's default location or names the runtime
with `DROMON_ANTIGRAVITY_RUNTIME`.

## A probe turn

1. **Probe only.** Without `--accept-partial-isolation` the turn is refused, for the
   reason under Isolation below. The line may have no built-in tools and none of the
   configurations listed under Configurations below, and its effort must be its
   driver's, since Antigravity names the effort in the model.
2. **Strategy and credential.** Only the subscription strategy. The driver is given the
   credential `unverified` before anything is located or started, or `subscription`
   with `--accept-unverified-credential`, and a refusal ends the turn there.
3. **Runtime.** The executable is `DROMON_ANTIGRAVITY_RUNTIME`, an absolute path, or
   else `~/.local/share/agy-acp-server/agy_acp_server.par`, Dromon's own default since
   Google documents none. It must be executable, with an executable
   `localharness_external` beside it and a `profile` directory beside it. A missing
   runtime is reported as the ACP runtime missing, not Antigravity, since the app and
   `agy` do not provide it. A missing profile is reported as not signed in.
4. **Workspace.** **Antigravity review, F3.** A `.gemini`, `.agy` or `.antigravity` in
   the working directory or a directory above it, other than the home directory,
   refuses the turn. **Inference.** A project's `.agents` or `.agent` skills are not
   refused, since they are the project's own text and every tool but Dromon's is
   refused.
5. **Profile.** A `.env` in the profile or in `~/.gemini` refuses the turn by its
   existence, **re-review N2**. `config/hooks.json` and `config/mcp_config.json` must be
   absent or hold only their `hooks` or `mcpServers` key, empty. **Antigravity review,
   F6.** `settings.json` and `antigravity-cli/settings.json` may hold only `$schema`
   and `permissions`, and `permissions` only an empty `allow` and a `deny`, so any key
   Dromon has not reviewed refuses the turn. `antigravity-acp/settings.json` must exist
   and hold only `auth.type`, set to `oauth-personal`, since the method the runtime
   chooses without it is not shown. Dromon never opens the runtime's sign-in.
6. **Environment.** Every variable beginning `GEMINI_`, `AGY_` or `ANTIGRAVITY_`, every
   cloud and credential variable as for the other engines, the `XDG_CONFIG_HOME`,
   `XDG_DATA_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` variables, and
   `DROMON_LINE_TOKEN` are removed. Dromon sets `GEMINI_HOME` to the profile,
   `ANTIGRAVITY_HARNESS_PATH` to the helper, `BROWSER` to the `true` command so that a
   sign-in never opens a browser, `PYTHONUNBUFFERED` and `ELECTRON_RUN_AS_NODE` as T3
   Code does, and `HOME` and `TMPDIR` to two empty directories of the turn's own,
   removed with it. **Antigravity review, F7.** It also sets
   `AGY_ACP_FORCE_FILE_STORAGE`, so that the sign-in is the file in the profile and
   does not depend on the home directory or the keychain.
7. **Start.** The runtime starts with no arguments in the line's working directory, in a
   process group of its own, which every stop signals whole.
8. **`initialize`**, with ACP version 1, no file system and no terminal. The answer must
   speak version 1, name itself `antigravity-acp` at version 1.3.0 or later, and list
   `oauth-personal` among its methods.
9. **`authenticate`** with `oauth-personal`. The sign-in notice, which Dromon recognises
   without quoting it, or error `-32000` refuses the turn as not signed in.
10. **Session.** `session/new` with the working directory and Dromon's MCP server, or
    `session/resume` for a stored session when the runtime offers it. The session
    identifier must be plain text of at most 128 characters.
11. **Mode.** The session must be in `default`, and is switched to it when it reports
    another mode and offers `default`.
12. **Model.** When the session runs another model and offers the driver's, Dromon
    selects the driver's with `session/set_config_option`. The driver then judges the
    model the session reports.
13. **Prompt.** One `session/prompt`, with the line's instructions ahead of the prompt in
    a new session. Text and thinking become driver events.
14. **Tools.** A call is one of Dromon's own tools only when its structured
    `toolName` is `dromon__TOOL`. **Antigravity review, F4.** A title never names a
    tool, since a file name or the model can make it equal to one. A permission
    request for one of Dromon's own tools is allowed once. Any other is refused with a
    single rejection, and a tool call that is not Dromon's cancels and refuses the
    turn, saying whether it came before the tool ran or after it started. **F1.** No
    kind, `think` included, exempts a call. **F2 and F3.** Every `tool_call_update` is
    checked as well, and a recorded call whose `toolName`, `title` or `kind` changes,
    or gains one it lacked, refuses the turn, in an update or in a permission request,
    which is then rejected. A call without an identifier refuses the turn. A question
    from the runtime is answered as cancelled and refuses the turn. Once a turn is
    cancelled, every permission request is answered as cancelled, as ACP requires.
15. **Strictness.** An update kind outside ACP version 1, a request other than a
    permission request, an unknown notification, or a switch away from `default`
    refuses the turn. **F5.** So does any `config_option_update` that gives the model
    a value other than the driver's, read from its `configOptions`, from a `models`
    field, or from its own `configId` and `value`, including a value that cannot be
    read. A request Dromon does not serve is answered with an error.
16. **End.** `end_turn` is success, `cancelled` interrupted, and the other ACP stop
    reasons an error. Any other reason refuses the turn. A cancelled turn waits at most
    60 seconds for the prompt's answer before the runtime is stopped. A turn that has
    before its first activity has a 180 second deadline and its error names the stage,
    including sign-in and first model output. After activity, the sliding inactivity
    deadline is `TURN_SECONDS`, 3600 seconds, reset by model or tool activity. The
    absolute `ABSOLUTE_TURN_SECONDS` cap is four hours. Reaching either bound cancels,
    stops and refuses the turn. Its credential is `unverified` whatever the flags.
    Before the first agent message, each wait has a 180 second deadline and its error
    names the stage, including sign-in and first model output. Once output arrives, the
    ordinary one hour turn deadline applies.
17. **Usage.** **Antigravity review, F9.** A `usage_update` is counted where its shape
    allows. A whole, non-negative `used` becomes the context, and a finite,
    non-negative `cost.amount` in `USD` becomes the session's running total, from
    which the turn's cost is the difference from the total the line last recorded for
    that session, or the whole total for another session. The report labels both as
    unverified. Anything else is left uncounted. A turn in which the runtime compacts
    its session is refused unless a counted cost arrives after the compaction, and a
    refused turn leaves the recorded total unmoved, so that the next turn's difference
    still covers it. **Re-review, N5.** A turn with no counted cost is accepted, since
    that is a subscription's expected case, and its usage note begins "no cost was
    seen" and says that a turn billed elsewhere would look the same.

## The three refusals, re-derived

| Refusal before this change | On the ACP runtime | Basis |
|---|---|---|
| No way to give Antigravity Dromon's tools | **Lifted in the adapter.** Dromon's tools reach the session as its MCP server, through `dromon mcp --line` on `lines.sock` | ACP schema, which defines `mcpServers` on `session/new`, and the second implementation, which gives the runtime its tools so. **Not observed.** An ACP agent can accept `mcpServers` and not show the tools to its model, verify-first step 6. **Antigravity review, F4.** Dromon recognises its tools only by `toolName`, which the second implementation's scripted runtime does not send, so the lift may refuse every use until step 6 shows a structured field |
| Tools stopped only after they start | **Partly lifted, the refusal kept.** A tool that asks is denied before it runs. A tool that does not ask is stopped only after it starts | Second implementation, which shows the permission requests. **Unknown.** Which built-in tools run without asking in `default`, verify-first step 5. A daemon therefore still refuses Antigravity lines, and only the probe flag passes |
| Unverified credential | **Kept.** Dromon now chooses `oauth-personal` and refuses a profile set to another method, but the runtime does not report which credential it then uses | No source shows the runtime naming its credential. The probe flag remains, verify-first step 3 |

Daemon lines stay refused, for the isolation reason and, through the drivers, the
unverified credential. `TOOLS_REFUSAL` no longer exists, since the adapter can give a
line its tools.

## Model names

Dromon names an Antigravity model `antigravity:ID`, as [`DRIVER.md`](DRIVER.md)
records. The packaged model is `antigravity:gemini-3.8-flash-high`, with the effort
`high` in the identifier, so its drivers declare `high` and a line may not set another
effort.

- **Second implementation.** T3 Code's model manifest lists
  `gemini-3.8-flash-high`, `-medium` and `-low` as current for this runtime, and
  `gemini-3.1-pro-low` and `gemini-pro-agent` as legacy.
- **User report.** T3 Code issue 19150 quotes `agy models` from the CLI at 1.3.4,
  listing `gemini-4-argon-high`, `-medium` and `-low` and `gemini-3.8-flash-high`,
  `-medium` and `-low`. It also reports that runtime 1.3.0 lists no `gemini-4-argon`,
  so the runtime may list fewer models than the CLI.
- **Hypothesis.** The context window of 1,000,000 tokens.

The earlier guess `gemini-3.1-pro` is contradicted by both sources and was removed.

## Configurations an Antigravity line refuses

Any built-in tool, MCP servers of its own and `inherit_account_mcp`,
`claude_code_prompt`, `project_settings`, `plugins`, a sandbox or settings document,
`allow_compact`, `keep_session`, a strategy other than subscription, and an effort
other than its driver's. A project with non-empty `read_only_paths` is also refused.

## Not verified

Everything above that is not marked as observed, and in particular these.

- **Inference.** That the runtime's default `GEMINI_HOME` is irrelevant once Dromon sets
  it, and that it reads no settings from `~/.gemini` then.
- **Inference.** That `oauth-personal` draws on the account's plan and never bills per
  token.
- **Hypothesis.** That the runtime names Dromon's MCP tools as `dromon__TOOL` in a
  structured `toolName` in tool calls and permission requests. If it names them only
  in titles, as the second implementation's scripted runtime does, every use of them
  is refused, which is safe.
- **Hypothesis.** That a tool call keeps its title and kind for its whole life. If
  the runtime refines a title as a call proceeds, every such turn is refused, which
  is safe.
- **Hypothesis.** That the runtime sends no `config_option_update` naming another
  model while a session is opened and before Dromon selects the driver's model. If it
  does, every turn that switches the model is refused, which is safe.
- **Hypothesis.** That with `HOME` replaced and `AGY_ACP_FORCE_FILE_STORAGE` set, the
  runtime finds its sign-in in the profile and reads nothing under the operator's
  home directory. A runtime that finds the home directory from the account rather
  than from `HOME` would still reach it.
- **Hypothesis.** That `usage_update` reports a running total for the session in
  `USD`, and that the total covers the runtime's compactions.
- **Inference.** That `BROWSER` set to `true` keeps the runtime from opening a browser,
  and that `ELECTRON_RUN_AS_NODE` and `PYTHONUNBUFFERED` are needed, as T3 Code sets
  them without saying why.
- **Hypothesis.** That the runtime's update kinds stay within ACP version 1. An
  extension kind refuses every turn until it is added.

## Removed with print mode

`agy --print` and its stream-json reading, its `--effort` and `--print-timeout`
arguments, the `init`, `step_update` and `result` record handling, the status table,
the usage note, and their tests. Nothing else used them. The workspace check, the
deadline, the process group, the probe flags and the environment filter carry over.

## Verify first

Run these by hand, in a throwaway directory, before the probe.

1. **Antigravity review, F10.** Before downloading anything, confirm from what Google
   itself publishes where the runtime is released, which version is current, and the
   SHA-256 of each archive, and compare them with the hypotheses under How the
   operator obtains the runtime. Record what Google publishes in this document. Only
   then add a download step here, obtain the runtime from that source, check its
   checksum, place it and its helper as that section says, and create the `profile`
   directory beside them, with `antigravity-acp/settings.json` holding
   `{"auth": {"type": "oauth-personal"}}`.
2. Sign the runtime in by hand. Start it with `GEMINI_HOME` set to the profile,
   `AGY_ACP_FORCE_FILE_STORAGE` set to `1`, `HOME` set to an empty directory and
   `BROWSER` unset, send `initialize` and then `authenticate` with `oauth-personal` as
   JSON lines, and complete the Google sign-in in the browser it opens. Confirm that
   the sign-in is then a file under `antigravity-acp/` in the profile, without opening
   it. Record the whole `initialize` answer, in particular `protocolVersion`,
   `agentInfo`, `authMethods`, `agentCapabilities.mcpCapabilities` and
   `agentCapabilities.sessionCapabilities`. Record which files the runtime wrote into
   the profile, and any key it wrote into a settings file, since a key outside
   Dromon's list refuses the turn.
3. Confirm by hand that the account is on a plan and that the sign-in cannot bill per
   token, that the profile holds no `.env` and that its `antigravity-acp/settings.json`
   names `oauth-personal`. Record whether `authenticate`, `session/new` or any update
   names the credential in use. If one does, the credential can be read and the probe
   flag retired. **F6.** Record which method the runtime uses when that file is
   absent, by its `authenticate` answer in a scratch profile with no sign-in.
4. Send `session/new` with no MCP servers. Record `modes`, `models` and
   `configOptions`, and correct the packaged identifier if `gemini-3.8-flash-high` is
   not offered. **F5.** Send `session/set_config_option` for another model and record
   the whole `config_option_update` that follows, if any, and whether one arrives
   while the session is opened.
5. In `default`, ask the model to read `/etc/hosts`, to list a directory and to run
   `ls`. Record for each whether `session/request_permission` comes before the tool
   runs, and the `toolCall` fields, `toolCallId`, `title`, `kind` and `status`. This
   decides the isolation refusal. **F2 and F3.** Record every `tool_call_update` of
   each call, and whether any changes its `title` or `kind`.
6. Give the session a harmless stdio MCP server and record whether its tools reach the
   model, and how they are named in tool calls and permission requests. **F4.**
   Record whether any structured field, such as `toolName` or a field under `_meta`,
   names the tool, as opposed to the title alone. Until one does, Dromon refuses
   every use of its own tools.
7. With an empty profile, confirm the sign-in notice and error `-32000`, and that
   `BROWSER` set to `true` opens nothing.
8. Confirm that `session/resume` continues a session and reports the same identifier.
9. Run `dromon probe --model antigravity:gemini-3.8-flash-high
   --accept-partial-isolation --accept-unverified-credential` and confirm a reply, the
   credential `unverified`, and with `ps` that no process of the turn remains and that
   its temporary directory is gone.
10. Record which `session/update` kinds a plain turn and a tool turn carry, and whether
    any lies outside ACP version 1.
11. **F7.** With `HOME` set to an empty directory, confirm that a turn still signs in
    and runs, and record with a file-access trace, such as `fs_usage` on macOS or
    `strace -e trace=file` on Linux, whether the runtime opens anything under the
    operator's real home directory, in particular `~/.config/gcloud` and `~/.gemini`.
12. **F9.** Record every `usage_update` of a plain turn and of a turn long enough to
    compact, whether `cost` is present and in which currency, whether it is a running
    total for the session, and whether a compaction is followed by one.

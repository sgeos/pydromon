# Antigravity Engine

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-26. Normative for Dromon's side. The Python module
`dromon.antigravity` implements it from documentation alone. **The command-line tool
`agy` is not installed on the operator's machine, so nothing here was observed.**
A line in a daemon is refused when it is created, since Dromon's tools cannot reach it.
`dromon probe --accept-partial-isolation` reaches the adapter, as the operator decided
on 2026-10-26, but a turn is then refused before `agy` starts, because its
credential cannot be verified and no packaged driver accepts an unverified one, unless
the probe is also given `--accept-unverified-credential`.

## Purpose

**Directed by the operator on 2026-10-26.** A prospective adapter, ready for one
`dromon probe` turn once `agy` is installed and its credential is shown by hand to be
a plan. A
line whose driver names the `antigravity` engine runs each turn as `agy` in print mode
with `--output-format stream-json`.

## Model names

Dromon names an Antigravity model `antigravity:ID`, as [`DRIVER.md`](DRIVER.md)
records. The packaged model is `antigravity:gemini-3.1-pro`. **Hypothesis.** The
identifier is a guess, since `agy models` could not be run, and the context window of
1,000,000 tokens is not verified. Antigravity documents that an unknown model fails with
the status `ERROR` rather than falling back, so a wrong guess fails safely.

## Evidence

**Facts, from the operator's summary** of `antigravity.google/docs/cli/headless`,
fetched on 2026-10-26, a summary rather than the page.

- `-p`, `--print` or `--prompt` runs one prompt, prints to standard output and exits,
  with diagnostics on standard error. An unauthenticated run exits with an
  authentication error, and the sign-in is cached by an earlier interactive run.
- `--output-format stream-json` writes `init`, with `cwd`, `tools`, `permission_mode`
  and, when overridden, `model` and `agent`. `step_update` has `step_index`, `state`,
  `step_type` such as `user_input`, `agent_response`, `tool` or `checkpoint`, and
  optional `text_delta`, `tool_name`, `tool_info`, `subagent_info`, `duration_seconds`
  and `usage`. `result` has `conversation_id`, `status`, `response`, `error`,
  `duration_seconds`, `num_turns` and `usage`.
- `--conversation ID` resumes a conversation. `--model`, `--effort low|medium|high`.
- Without an interactive prompt, a tool needing approval is denied and the run
  continues. Allow rules live in `permissions.allow` of
  `~/.gemini/antigravity-cli/settings.json`. `--dangerously-skip-permissions` approves
  everything, and `--sandbox` restricts the terminal.
- `--print-timeout` defaults to five minutes. The statuses are `SUCCESS`, `ERROR`,
  `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING` and `RUNNING`.
- The page does not describe MCP configuration.

**Reported, not verified.** The issue tracker and release notes say that before 1.1.8
print mode dropped its output or hung without a terminal, and that stream-json arrived
in 1.1.8.

## Why a turn is refused

- **Isolation.** The documentation does not say which built-in tools run without
  approval. **Inference, with no source.** Tools of this kind commonly read files
  without approval, so Dromon cannot show that a line's reads stay away from Dromon's
  state and the operator's credentials. **Decided by the operator on 2026-10-26**, as
  for [`CURSOR.md`](CURSOR.md). A daemon refuses Antigravity lines, and only `dromon
  probe --accept-partial-isolation`, which sets the adapter's `partial_isolation`,
  passes this refusal. Every tool is stopped only after it starts.
- **Credential. Antigravity review, F2.** The stream names no credential source, so
  the credential is `unverified`, put to the driver before `agy` starts. No packaged
  driver accepts it, so the probe's turn is refused here, before anything can bill.
  **Decided by the operator on 2026-10-26.** `dromon probe
  --accept-unverified-credential`, accepted only for an Antigravity model and only
  together with `--accept-partial-isolation`, puts `subscription` to the driver in its
  place, which sets the adapter's `unverified_credential`. The flag stands for the
  operator's check by hand, step 2 of the list below, that `agy` is signed in to a plan
  and does not bill per token. Dromon itself still sees no credential.
- **Dromon's tools.** The documentation does not say how to give Antigravity an MCP
  server, so Dromon's tools cannot reach a line. A daemon refuses an Antigravity line
  when it is created or configured, and an adapter given Dromon's tools refuses its
  turn. `dromon probe` has no tools.

## A turn, once permitted

1. **Configuration.** No built-in tools, and none of the configurations below.
2. **Strategy and environment.** Only the subscription strategy, since the stream names
   no credential. Every variable beginning `GEMINI_`, every variable of a cloud
   provider by its prefix, among them `GOOGLE_`, `CLOUDSDK_`, `AWS_` and `AZURE_`, and
   every variable whose name ends in a suffix such as `_TOKEN`, `_PASSWORD` or
   `_ACCESS_KEY` are removed.
3. **Credential.** The driver is given the credential `unverified` before `agy`
   starts, or `subscription` with `--accept-unverified-credential`, and a refusal ends
   the turn there.
4. **Workspace and settings. Antigravity review, F3.** A `.gemini`, `.agy` or
   `.antigravity` in the working directory or a directory above it, other than the
   home directory, refuses the turn, since the documentation does not say which of
   them Antigravity reads. `~/.gemini/antigravity-cli/settings.json` and, **re-review
   N2**, `~/.gemini/settings.json` must approve no tool in `permissions.allow`. A
   `~/.gemini/.env` refuses the turn by its existence alone, since a key there would
   bill per token unseen by the environment filter. Dromon never reads it. Whether
   Antigravity reads either file is unverified.
5. **Start.** `agy --output-format stream-json --model ID --effort EFFORT --sandbox
   --print-timeout 60m`, with `--conversation ID` for a later turn and the prompt
   attached as `--prompt=TEXT`. Never `--dangerously-skip-permissions`. Dromon's
   efforts `xhigh` and `max` become `high`. The line's instructions open a new
   conversation ahead of the prompt. `agy` starts in a process group of its own, which
   every stop signals whole.
6. **Init.** The first event must be `init`. The driver judges the reported model,
   `permission_mode` must be `default`, and, **Antigravity review, F4**, `tools` must
   be an empty list, since the line was given no tool, and `cwd` must be the line's
   working directory.
7. **Records and steps. Antigravity review, F1.** Only `init`, `step_update` and
   `result` records, and only the step types `user_input`, `agent_response` and
   `checkpoint`, pass. An `agent_response` delta is text. A `tool` step, any other step
   type or record type, and any step carrying `tool_name`, `tool_info` or
   `subagent_info`, whatever its value, ends the process group and refuses the turn,
   which waits at most 60 seconds for the stream to close. So does the driver's tool
   budget, which no tool can reach while every tool refuses the turn.
8. **Deadline.** A turn that has not ended an hour after it started is stopped and
   refused, the same hour Dromon passes as `--print-timeout`.
9. **Result.** `SUCCESS` is success, `CANCELED` and `INTERRUPTED` are interrupted, and
   any other status an error. A resumed turn must report the conversation it asked
   for. The result's whole-number `usage` fields are reported under Antigravity's own
   names, since the names are not documented. **Inference.** No cost is reported,
   since the summary names no cost field. The turn's credential is reported as
   `unverified` with or without the operator's confirmation, since Dromon saw none.

## Configurations an Antigravity line refuses

Any built-in tool, Dromon's own tools, MCP servers, `claude_code_prompt`,
`project_settings`, `plugins`, a sandbox or settings document, `allow_compact`,
`keep_session`, and a strategy other than subscription.

## Not verified

Everything above that is not a fact from the summary, and in particular these.

- **Inference.** That `--prompt=TEXT` is accepted with print mode implied, and that
  `--print-timeout` takes `60m`.
- **Inference.** That `init` reports `model` as the identifier given,
  `permission_mode` as `default`, `cwd` as the working directory, and `tools` as an
  empty list when no tool is approved. If `tools` lists the built-in tools whatever
  their approval, every turn is refused until the check is revisited.
- **Inference.** That the step types listed above are all a turn without tools
  carries. An undocumented type, such as one for thinking, refuses the turn.
- **Inference.** That an unapproved read is denied rather than run. If reads run
  without approval, the isolation refusal stands.
- **Hypothesis.** That the result's `usage` is an object of counts.

## Verify first

1. Install `agy` at 1.1.8 or later, sign in interactively, and run `agy --version`.
2. Run `agy models` and correct the packaged identifier, the drivers' `engine_model`
   and their context window. Then find whether the sign-in is a plan or a key, and
   whether `agy` reads a key from a file such as `.env`, which the environment filter
   cannot see. Confirm that no `~/.gemini/.env` or `~/.gemini/settings.json` exists, or
   that the latter approves nothing. Only a plan, with no key that `agy` could read,
   supports `dromon probe --accept-partial-isolation --accept-unverified-credential`.
   Neither flag changes the strategy.
3. Read the installed help for print mode, `--prompt`, `--print-timeout` and any MCP or
   configuration-directory option. An MCP option would let Dromon's tools reach a line.
4. Run one print turn by hand with stream-json, and record the exact `init`,
   `step_update` and `result` events, every step type, and what `init` lists in
   `tools`.
5. Ask the model to read `/etc/hosts` and to list a directory, and record whether
   either runs without approval, which decides the isolation question.
6. Confirm that no Google or Gemini API key is in the environment, and find whether
   Antigravity reports the credential it used anywhere.
7. Confirm that `--conversation ID` continues the conversation and reports the same
   `conversation_id`.
8. Find which workspace and user directories `agy` reads settings, permissions and MCP
   servers from, and narrow the workspace check to them.
9. Confirm with `ps` that no process of a stopped turn remains.

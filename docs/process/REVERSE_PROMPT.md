# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-26, Antigravity on its ACP runtime

**Done.** On the operator's decision of 2026-10-26, after a review of T3 Code, a second
implementation that runs these engines for real, the Antigravity adapter moved from
`agy --print` to Google's ACP runtime, `agy_acp_server`.

- **Work.** The Opus `worker` line wrote `acp.py`, an engine-neutral ACP client, moved
  `grok.py` onto it, and rewrote `antigravity.py`. The packaged model became
  `gemini-3.8-flash-high`, which a user report shows exists. The Haiku `reviewer` line
  reviewed it twice, and three fix rounds followed.
- **Fail closed.** No tool kind is exempt from the name check, on either engine. Each
  call's identity is recorded at first sight, and any later change refuses the turn. A
  tool is named only by its structured `toolName`, never its title. A model change, an
  unknown profile setting or a missing sign-in file refuses. The runtime gets an empty
  home directory of its own. Grok's cancel is now bounded.
- **Refusals.** MCP control is lifted. Deny-before-run is partial, since tools the
  runtime runs without asking are stopped only after they start, so daemon lines stay
  refused. The credential stays unverified, so the probe still needs both flags.

**Not verified.** Nothing ran against a real runtime. `ANTIGRAVITY.md` and `GROK.md`
each list what to verify first. If either runtime names Dromon's tools only by title,
every use of them is refused. The checksums, version and download location come from
unverified third-party material.

**For the operator.**
1. Decide whether `keep_session_max_seconds` may end background work still running.
   The recommendation is an operator question at the limit, with a 24-hour hard stop.
2. Install the runtime and sign in by hand per `ANTIGRAVITY.md` before a probe.

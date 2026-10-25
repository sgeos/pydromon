# Process

> **Navigation**: [Documentation Root](../README.md)

| File | Purpose | Kind |
|---|---|---|
| [`HANDOFF.md`](HANDOFF.md) | What is in flight, why, the next step and how to validate it | State file, overwritten whole |
| [`REVERSE_PROMPT.md`](REVERSE_PROMPT.md) | The agent's latest report to the operator | State file, overwritten whole |
| [`PROMPT.md`](PROMPT.md) | Instruction staging from the operator | Read-only for the agent |

There is no task log. Task state lives in the roadmap tables and in `HANDOFF.md`, and
history lives in git. This follows `pykeleusma`, which chose the overwrite-only form after
the Keleusma project's append-only logs grew too large to read at startup.

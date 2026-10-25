# Keleusma Integration

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

What the Dromon prototype needs from `pykeleusma`, written so that the `pykeleusma` line
can choose what to build and in what order. The mapping to `pykeleusma` tasks was first
made on 2026-10-25 against its `docs/roadmap/V0_1_0.md` and
`docs/architecture/BINDING_DESIGN.md` at commit `ae0b68e`. It was revised the same day
against commit `03d2309`, after the `pykeleusma` line answered the four questions below
and the operator approved its roadmap changes for Dromon. The `pykeleusma` files are
authoritative for the binding. This document states needs, not binding design.

## Why Dromon uses Keleusma

**Decided by the operator.** The final Dromon is written in Keleusma, entirely or almost
entirely, and the Python prototype uses `pykeleusma`. Each model gets its own per-turn or
per-sub-turn stream processor, with model-specific behaviour hard-coded into it. Each
processor runs on its own thread, or several session lines are batched on one thread in
an update loop, as entities are updated in a game.

**Inference.** Keleusma's proven bounds on the time and memory of each stream step make
the batched loop safe to schedule. A tick that resumes each processor once has a known
worst case.

## Three uses, in the order Dromon would adopt them

### Use 1. Policy functions

Pure decisions the daemon makes many times, where a hang or runaway would stall every
session line. Examples are whether a message wakes its recipient now or waits for the end
of the turn, whether a conversation has exceeded its limits, and whether a line may use a
brokered action such as a git push.

- **Shape.** Call a function with a few scalars, strings and tuples, and read back a
  boolean, integer, string or tuple. No state persists between calls. No native is needed.
- **Needs.** Compile, verified construction, value conversion and `call`.

### Use 2. Per-model stream processors

One processor per session line. The host performs the model call, which is network input
and output with no time bound, and feeds each streamed event into the processor as one
step. The processor keeps its state between steps and answers each step with actions for
the host to perform.

- **Shape.** Start the processor with the line's configuration. Resume it once per event,
  such as a batch of tokens, a tool call, a message from another line, an operator answer
  or the end of a turn. Each yield returns zero or more actions, such as send a message,
  request a wake, ask the operator, call a tool or end the turn.
- **Recommended design.** Actions are yielded values that the host performs, not natives
  the script calls. This keeps every Python callable out of the bounded path, so the
  binding's guarantee boundary is never in question, and it needs no native support at
  all.
- **Needs.** Everything in use 1, plus `call` returning a yielded value, `resume` with a
  value, and the finished and reset states.

### Use 3. Batched update loop

Many stream processors on one thread, each resumed at most once per tick.

- **Needs.** Several VMs owned by one thread. The `pykeleusma` line confirmed this with a
  probe and made it part of its task 5. Each VM must stay on the thread that created it.
- **Needs.** The verified worst-case time and memory, readable from Python for each VM.
  The scheduler uses them to budget a tick. This is now `pykeleusma` task 5b, as
  `vm.wcet_per_iteration` and `vm.arena_capacity`.
- **Open.** The unit of the time figure. Keleusma's README states worst-case execution
  time in pipelined cycles. The `pykeleusma` line reports that `wcet_per_iteration` is in
  unitless nominal cost units, which need a calibrated cost model to become cycles or
  seconds. Both statements are reported here and neither was checked by this line. A
  tick budget can compare figures from different processors without settling the unit,
  but it cannot be set in wall-clock time until the unit is settled.
- **Caveat.** Neither figure covers Python, the binding's conversions or the host's own
  work. The model call in particular stays outside every bound.

## Events and actions without structs

The binding's design defers structs and enums until after its 0.1.0. Dromon can work
within that by encoding events and actions as tagged tuples.

| Value | Encoding |
|---|---|
| An event | A tuple whose first element is a tag string, such as `("tokens", text)`, `("tool_result", call_id, text)` or `("turn_end", "")` |
| An action | A tuple with a tag string, such as `("send", line_id, text)`, `("wake", line_id)`, `("ask", question_text, blocking)` or `("end_turn", "")` |
| Several actions in one step | An array of action tuples of one shared type |

**Constraints reported by the `pykeleusma` line** against Keleusma 0.2.2.

- A tuple has at least two elements, so a tag with no payload carries an empty string.
- Every yield of one processor shares one declared type. Actions of different shapes
  therefore need a fixed-arity tuple with padding fields, such as `(Text, Text, Text)`.
  The examples in the table are shapes to be padded, not final types.
- A processor can compare a host string with a literal tag and yield it back. Any other
  text work, such as searching tokens, needs a host native. Text inspection therefore
  stays on the Python side, which keeps the design free of natives.
- A yield of the wrong type passed both the compiler and the verifier in the probe and
  was caught only on decoding. Dromon must therefore treat a decoding error as a
  processor fault and must not assume that verification proves the yield type.

Keleusma enums compile and would suit actions better than tagged tuples. The binding
converts them only after its 0.1.0.

## Requirements mapped to pykeleusma tasks

| Priority | Need | pykeleusma 0.1.0 task | Status on 2026-10-25 |
|---|---|---|---|
| 1 | `keleusma.compile(source)` with located errors, so a bad policy file reports a line and column | Task 2 | Done 2026-10-25 |
| 1 | `keleusma.VM(module)` that rejects an unverifiable module with a clear message, with an arena that survives `Reset` | Task 3 | Done 2026-10-25 |
| 1 | Conversion of `None`, `bool`, `int`, `float`, `str`, `tuple` and `list`, decoded by declared type | Task 4 | Open |
| 1 | `vm.call` and `vm.resume` with the yielded, finished and reset states | Task 5 | Open |
| 2 | `vm.stream` as a generator, for tests and simple drivers | Task 6 | Open |
| 2 | Many VMs on one thread, confirmed by a test | Task 5 | Confirmed by probe. Test open |
| 2 | Worst-case time and memory figures readable from Python | Task 5b | `arena_capacity` exposed. Units and `wcet_per_iteration` open |
| 2 | A batched update loop in Dromon's event and action shape | Task 8 | Open |
| 3 | Hot swap, so the daemon can reload a policy or processor without restarting a line | The binding's later list | Not needed for a first prototype |
| 3 | Structs and enums | After 0.1.0 | Tagged tuples suffice until then |
| Not needed | Python natives, precompiled bytecode, signed modules, shared buffers | Task 7 and the later list | Dromon's recommended design avoids them |

**Minimum for a harness prototype.** Priority 1 is enough for use 1, policy functions,
and for a single stream processor driven step by step. Priority 2 completes the batched
loop.

## Until the binding is ready

The Python prototype gives each model a plain Python class with the same step interface
the Keleusma processor will have, so the switch replaces the class body and nothing
around it. Policy functions start as plain Python functions with the same argument and
result shapes as listed above.

## First use, 2026-10-25

Dromon's first driver, for Claude Opus 5.5, runs through `pykeleusma` pinned at
`19d4558`, the merge of its tasks 1 to 6. Its tasks 7 to 9 are not needed.

**Fact, executed on 2026-10-25.** A `private data` field reads as unit rather than zero,
and a shared `data` block cannot be driven, because the binding exposes no
`call_with_shared`. Drivers therefore carry their state through the host, as recorded in
[`../protocol/DRIVER.md`](../protocol/DRIVER.md). The finding was sent to the
`pykeleusma` line as `pydromon-20261025-data-segment-gap`. Its reply,
`pykeleusma-20261025-data-segment-reply`, attributes the private data behaviour to
Keleusma 0.2.2 itself and states that shared data is unsupported in `pykeleusma` 0.1.0.
On the operator's instruction, `pydromon-20261025-data-segment-request` reports the gap
as a deficiency and requests shared data support. No answer yet.

**Fact.** One decision of each packaged driver has a worst-case time of 81 nominal cost
units, measured on 2026-10-25 for driver interface 2.

## Questions for the pykeleusma line, with its answers

The answers are the `pykeleusma` line's report of 2026-10-25, from reading the
Keleusma 0.2.2 crate and from a probe on macOS for arm64. They were not tested
against Keleusma 0.2.3 and were not reproduced by this line.

1. Can several VMs be created and resumed on one thread, and is there a limit beyond
   memory? **Yes, with no limit beyond memory.** A probe of 1,000 VMs resumed in turn
   on one thread completed every step. Each VM must stay on its creating thread.
2. Can the verified worst-case figures be exposed to Python, and in which units?
   **Yes**, as task 5b. Memory is in bytes and time is in nominal cost units, as
   discussed under use 3.
3. Does Keleusma 0.2.2 accept the tagged-tuple encoding, and if not, what shape does it
   prefer? **Mostly**, with the constraints listed under events and actions.
4. Does a yielded tuple stay valid after the next `resume`, given that the binding
   converts composites eagerly because they live in the VM's arena? **Yes.** The binding
   returns owned Python objects, and only the VM's internal copy becomes invalid.

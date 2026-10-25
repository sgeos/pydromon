# Keleusma Integration

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

What the Dromon prototype needs from `pykeleusma`, written so that the `pykeleusma` line
can choose what to build and in what order. The mapping to `pykeleusma` tasks was made
on 2026-10-25 against its `docs/roadmap/V0_1_0.md` and
`docs/architecture/BINDING_DESIGN.md` at commit `ae0b68e`. Those files are authoritative
for the binding. This document states needs, not binding design.

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

- **Needs.** Several VMs owned by one thread. `BINDING_DESIGN.md` says a VM belongs to the
  thread that creates it and tests several worker threads, each with its own VM. Dromon
  needs the converse case confirmed, many VMs created and resumed on one thread.
- **Needs, not in the binding's plan.** The verified worst-case execution time, in
  pipelined cycles, and the worst-case memory in bytes, readable from Python for each
  module or VM. The scheduler uses them to budget a tick.

## Events and actions without structs

The binding's design defers structs and enums until after its 0.1.0. Dromon can work
within that by encoding events and actions as tagged tuples.

| Value | Encoding |
|---|---|
| An event | A tuple whose first element is a tag string, such as `("tokens", text)`, `("tool_result", call_id, text)` or `("turn_end",)` |
| An action | A tuple with a tag string, such as `("send", line_id, text)`, `("wake", line_id)`, `("ask", question_text, blocking)` or `("end_turn",)` |
| Several actions in one step | A tuple or array of action tuples |

**Open for the binding line.** Whether Keleusma 0.2.2 accepts this shape well. A
heterogeneous array may not type-check, in which case a fixed-arity tuple per step, or one
yield per action, is the fallback.

## Requirements mapped to pykeleusma tasks

| Priority | Need | pykeleusma 0.1.0 task | Status on 2026-10-25 |
|---|---|---|---|
| 1 | `keleusma.compile(source)` with located errors, so a bad policy file reports a line and column | Task 2 | Open |
| 1 | `keleusma.VM(module)` that rejects an unverifiable module with a clear message | Task 3 | Open |
| 1 | Conversion of `None`, `bool`, `int`, `float`, `str`, `tuple` and `list` | Task 4 | Open |
| 1 | `vm.call` and `vm.resume` with the yielded, finished and reset states | Task 5 | Open |
| 2 | `vm.stream` as a generator, for tests and simple drivers | Task 6 | Open |
| 2 | Many VMs on one thread, confirmed by a test | Not listed | Asked here |
| 2 | Worst-case time and memory figures readable from Python | Not listed | Asked here |
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

## Questions for the pykeleusma line

1. Can several VMs be created and resumed on one thread, and is there a limit beyond
   memory?
2. Can the verified worst-case figures be exposed to Python, and in which units?
3. Does Keleusma 0.2.2 accept the tagged-tuple encoding, and if not, what shape does it
   prefer?
4. Does a yielded tuple stay valid after the next `resume`, given that the binding
   converts composites eagerly because they live in the VM's arena?

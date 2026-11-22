# Engine stream bounds

## Audit scope

Fact. This audit compares source at `6567167` with `bbf262a`. Tests use local
processes and scripted engines. No model subscription is used. The external
failure has no traceback or failing record available here.

## Codex history responses

Fact. The installed Codex binary contains the `ThreadResumeParams` schema field
`excludeTurns` and guidance to request it for history loaded separately. Dromon
now sends `excludeTurns` as true on every `thread/resume`. It consumes only the
thread identifier and settings from that response. Its other thread operation is
`thread/start`. It makes no `thread/read`, `thread/fork` or rollback request.
The binary also describes `thread/read` with `includeTurns` and the paginated
`thread/turns/list` and `thread/items/list` alternatives. None is needed here.
Residual. Schema field presence was read from the installed binary without
starting Codex. A full exported schema and live compatibility were not tested.

Fact. Codex records are read in 64 KiB chunks. A record past 64 MiB is discarded
through its newline, preserving any following frame. The drain has a total ten
second deadline. Completed drains report the exact byte count before newline.
Timeout or end of file reports only a lower bound. The turn then ends with
`EngineStreamTooLong` and the scheduler does not create a failure hold for it.
Normal conversation limits still apply. No automatic retry is requested.

Fact. If the overrun occurs while awaiting `thread/resume`, Dromon clears the
engine session identifier in memory and in the event log. Its next turn starts a
fresh session and receives a replacement notice. The line identity, mail, files,
role ownership and evidence index remain. The diagnostic names the outstanding
request rather than claiming to identify the method of a discarded response.
The kept connection pump preserves the exception rather than replacing it with
a generic end of stream. A failure during another request keeps the session
identifier, so the next instruction can avoid repeating the oversized operation.

| Stream and reader | Limit at both revisions unless stated | Handling |
| --- | --- | --- |
| Codex app-server stdout in `codex._Process.receive` | 64 MiB before newline | Previously `readline` could raise an unnamed `ValueError`. Now bounded chunk reads discard excess and name the stream and size |
| Grok bridge stdout in `grok._Process.receive` | 64 MiB before newline | Same correction as Codex |
| Pi, Cursor and Antigravity stdout in `jsonlines.Process.receive` | 64 MiB before newline | `readuntil` already converted overruns to engine refusals. Now shares the stream diagnostic |
| Engine stderr in `evidence.drain_stderr` | Same reader limit as stdout, reads 4096 bytes at a time | No line framing and no separator overrun |
| Claude Agent Software Development Kit stdout | Default 1 MiB, overridden by Dromon to 32 Mi characters | SDK frames text chunks and refuses oversized messages. Dromon reports `MESSAGE_TOO_LONG` and closes the session |
| Claude SDK stderr | Same configured SDK buffer bound | SDK frames text chunks and flushes an oversized partial line |
| Launcher input relay | No `StreamReader` | `os.read` uses bounded chunks and a bounded backlog. Output descriptors pass through to the harness |
| Codex `mcp list`, Grok and other synchronous listings | No `StreamReader` | `spawn.run` uses subprocess communication and whole-output parsing, not separator framing |
| Pi authentication check | Subprocess default 65536 | `communicate` reads chunks to end of file, not lines |
| Relay command stderr | Subprocess default 65536 at `6567167` | Old reader uses 65536 byte chunk reads. Current relay uses a descriptor and bounded tail |
| Generic `spawn.subprocess_exec` stdout and stderr | 65536 at current revision | Callers supply 64 MiB for engine records. `communicate` uses `read`, not `readline`. Old revision used asyncio directly with the same default |
| Operator, line and agent daemon sockets | 64 KiB | `readline` overrun returns protocol `TOO_LONG` and closes the connection |
| Synchronous client socket | 64 KiB | `recv` chunks and protocol decoding enforce the bound |
| Federation hello | 64 KiB | `readline` or the Transport Layer Security chunk reader rejects excess and closes |
| Federation authenticated records | 8 MiB | `daemon.LineReader` and `open_tls.OpenTlsStream` use chunk reads and enforce the record bound |
| Forwarder Unix socket answers | 64 KiB | `readline` errors become `LinkError` |
| Federation TLS answers | 64 KiB answer protocol | Chunked TLS reads and an explicit line bound become link errors |
| Model Context Protocol standard input in `mcp.main` | `MAX_INPUT_LINE` plus one byte | Synchronous bounded `readline`, independent of asyncio |
| Client token input | `MAX_PEER_TOKEN_INPUT` | Synchronous bounded `readline`, independent of asyncio |

Fact. A reader limit is a separator bound only when the consumer frames lines.
Increasing every subprocess limit would spend memory without correcting a chunk
consumer. Listing output and `communicate` still accumulate whole output in memory.
That resource concern is separate from separator errors and remains unchanged.

Inference. A Codex app-server record over 64 MiB is the most direct explanation
of the reported error on a Codex turn. The error text alone cannot establish its
source or its configured limit. A 64 MiB limit does not exclude that explanation.
Hypothesis. A large tool result or restored session record could cross that bound.
No such record was available to verify the hypothesis.

## Refusal and recovery

Fact. Retained engine records remain bounded. Oversized output ends the turn and closes
the engine process through the adapter cleanup path. The new refusal has class
`EngineStreamTooLong` and names `ENGINE_STREAM_TOO_LONG`, the stream, its bound and
the number of bytes already observed. For Grok and the shared reader that count
is a lower bound because the newline may not have arrived. The record itself is
never included in the diagnostic.
Discarding a prefix and continuing would lose framing and is unsafe.

Fact. Other failed turns may ask a failure question and hold the line to prevent
repeating the same operation unattended. Answering that question releases it.
The scheduler recovery test exercises an actual reader overrun followed by an
operator answer and a successful next turn. This does not promise that replaying
the same oversized operation will succeed.

At `6567167` an operator can inspect the failure question, attach with
`dromon attach LINE` and send `/clear` while the line is idle to discard its engine
session, then use
`dromon answer QUESTION BODY` to answer that question. The body should instruct
the line to avoid printing large results and to inspect output in bounded pieces.
Clearing keeps files, mail and the brief. It discards engine conversation context.
Answering alone releases the hold but can encounter the same output on resume.
These commands require no upgrade. They are recovery steps, not a correction to
the old adapter diagnostic.

## Verification limits

Fact. Production-bound pipe tests send more than 64 KiB through each affected
adapter and send just over 64 MiB to verify named refusal. A separate test reproduces
the Codex boundary one byte below, exactly at and one byte above the limit, then
reads the following frame after a discard. Another test reproduces
the old error with the production reader limit. The SDK test sends one character
over Dromon's configured production bound through the SDK reader. Standard error
is tested beyond the default subprocess limit without a newline.

Residual. These are local reproductions. They do not identify the external failing
record, run live engines, or prove a maximum whole-process memory footprint.
Socket and federation bounds retain their existing protocol rejection behavior.

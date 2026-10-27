"""A harness process that speaks JSON, one object per line, on its standard streams.

The Pi, Cursor Agent and Antigravity adapters each start their harness this way. Pi and
Antigravity's ACP runtime keep their standard input open for requests, while Cursor
Agent takes its prompt as an argument and only writes. The Grok adapter predates this
module and keeps its own copy, which a later change may fold in.

Framing. A record ends at a line feed and at nothing else, as Pi's documentation
requires, since a JSON string may hold the Unicode line and paragraph separators. A
trailing carriage return is dropped. A line that is not a JSON object refuses the turn,
because the adapters cannot tell what such output meant. ``NotJsonLine`` keeps the
start of such a line apart from its message, for an adapter that recognises a
harness's plain-text notice.

Process group. **Cursor review, F5.** A harness starts under ``dromon.launcher``, in
a session and process group of its own, so a child the harness started, such as Pi's
MCP bridge or a tool's process, cannot outlive the turn, and the launcher also ends
the harness if the daemon dies. The line's stop policy, which its adapter passes in
the environment, decides whether a stop ends everything the harness started, the
default, or leaves its background descendants running and records them. **Porting
hazard.** ``os.killpg`` is POSIX only, and a group identifier could in principle be
reused once the group is empty, a window of at most ``EXIT_SECONDS`` after the harness
exits.

Standard error is discarded. A harness may print diagnostics there that Dromon cannot
parse, and in principle text near a credential, so none of it reaches the event log.

Porting note. Each record is read whole up to ``MAX_LINE`` bytes, a bound a port keeps.
"""

import asyncio
import contextlib
import json
import shutil
import signal
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Final, Protocol, cast

from dromon import evidence, launcher
from dromon.engine import EngineRefused
from dromon.launcher import StopPolicy

MAX_LINE: Final = 64 * 1024 * 1024
"""The longest record accepted, in bytes."""

EXIT_SECONDS: Final = 10
"""How long a harness may take to exit once its input is closed, before it is
terminated, and again before it is killed."""

TURN_SECONDS: Final = 3600
"""**Cursor review, F6.** The longest a turn on these engines may run before Dromon
stops it and refuses the turn, so that a hung or endless run cannot hold a line without
bound. An hour."""

CLOUD_PREFIXES: Final = (
    "ALIBABA_CLOUD_",
    "ALICLOUD_",
    "ARM_",
    "AWS_",
    "AZURE_",
    "CLOUDFLARE_",
    "CLOUDSDK_",
    "DIGITALOCEAN_",
    "GCE_",
    "GCLOUD_",
    "GOOGLE_",
    "IBMCLOUD_",
    "OCI_",
)
"""**Cursor review, F4.** Prefixes of the variables through which cloud providers'
tools find a credential, a credential file, an endpoint or an account. A subscription
line's harness starts without any of them, since it needs none, and a prefix catches
names such as ``AWS_SECRET_ACCESS_KEY`` that no credential suffix matches."""

CREDENTIAL_SUFFIXES: Final = (
    "_ACCESS_KEY",
    "_API_KEY",
    "_CREDENTIALS",
    "_PASSWORD",
    "_SECRET",
    "_SECRET_KEY",
    "_TOKEN",
)
"""Any variable ending so is treated as a credential, so a provider added to a harness
later cannot slip one past a subscription line."""


def is_cloud_or_credential(name: str) -> bool:
    """Whether a subscription line's harness starts without the variable ``name``."""
    return name.startswith(CLOUD_PREFIXES) or name.endswith(CREDENTIAL_SUFFIXES)


HEAD_BYTES: Final = 64
"""How much of a line that is not JSON an adapter may inspect."""


class NotJsonLine(EngineRefused):
    """A harness wrote a line that is not JSON. ``head`` holds at most ``HEAD_BYTES`` of
    it, so that an adapter can recognise a harness's own plain-text notice, such as a
    request to sign in. The head never reaches the message, which goes to the event log,
    since the line may carry a sign-in address or text near a credential."""

    def __init__(self, head: bytes) -> None:
        super().__init__("the engine sent a line that is not JSON")
        self.head = head[:HEAD_BYTES]


class Connection(Protocol):
    """One running harness, as an adapter sees it."""

    async def send(self, message: Mapping[str, object]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...

    async def stop(self) -> None:
        """End the harness at once, for a harness that takes no abort command."""
        ...


type ConnectionFactory = Callable[
    [list[str], Path, Mapping[str, str]], AbstractAsyncContextManager[Connection]
]
"""Starts a harness with its arguments, working directory and environment."""


class Process:
    """A started harness behind the ``Connection`` interface."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        policy: StopPolicy = StopPolicy.KILL_ALL,
        recording: evidence.Recording | None = None,
    ) -> None:
        self._process = process
        self._policy = policy
        self._recording = recording

    async def send(self, message: Mapping[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise EngineRefused("the engine was started without a standard input")
        stdin.write((json.dumps(message) + "\n").encode())
        await stdin.drain()
        if self._recording is not None:
            self._recording.frame(evidence.OUT, message)

    async def receive(self) -> dict[str, Any] | None:
        stdout = self._process.stdout
        if stdout is None:
            raise EngineRefused("the engine was started without a standard output")
        try:
            line = await stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as partial:
            line = partial.partial
        except asyncio.LimitOverrunError as error:
            raise EngineRefused(
                f"the engine sent a record longer than {MAX_LINE} bytes"
            ) from error
        if not line:
            return None
        try:
            decoded: object = json.loads(line.rstrip(b"\r\n"))
        except ValueError as error:
            raise NotJsonLine(line) from error
        if not isinstance(decoded, dict):
            raise EngineRefused("the engine sent a record that is not an object")
        record = cast(dict[str, Any], decoded)
        if self._recording is not None:
            self._recording.frame(evidence.IN, record)
        return record

    async def stop(self) -> None:
        if self._process.returncode is None:
            launcher.signal_launch(self._process.pid, signal.SIGTERM, self._policy)


def json_lines_factory(command: str, *, keep_input: bool) -> ConnectionFactory:
    """A factory that starts ``command``, found on ``PATH``, with standard input open
    when ``keep_input`` is set and closed otherwise."""

    @asynccontextmanager
    async def start(
        arguments: list[str], cwd: Path, environment: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        found = shutil.which(command, path=environment.get("PATH"))
        if found is None:
            raise EngineRefused(
                f"the {command} command is not installed or not on PATH"
            )
        argv, launched, policy = launcher.wrap([found, *arguments], environment)
        recording = evidence.RECORDING.get()
        keep_stderr = recording is not None and recording.wants_stderr
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if keep_input else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
            if keep_stderr
            else asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=launched,
            limit=MAX_LINE,
            start_new_session=True,
        )
        draining = (
            asyncio.create_task(evidence.drain_stderr(process.stderr, recording))
            if recording is not None and process.stderr is not None
            else None
        )
        try:
            yield Process(process, policy, recording)
        finally:
            await launcher.end(
                process, policy, close_input=True, input_seconds=EXIT_SECONDS
            )
            if draining is not None:
                # The harness has ended, so its standard error ends too, unless a
                # descendant left running holds it, which the bound covers.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(draining, EXIT_SECONDS)
                draining.cancel()

    return start

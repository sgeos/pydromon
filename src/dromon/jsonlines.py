"""A harness process that speaks JSON, one object per line, on its standard streams.

The Pi, Cursor Agent and Antigravity adapters each start their harness this way. Pi
keeps its standard input open for commands, while Cursor Agent and Antigravity take
their prompt as an argument and only write. The Grok adapter predates this module and
keeps its own copy, which a later change may fold in.

Framing. A record ends at a line feed and at nothing else, as Pi's documentation
requires, since a JSON string may hold the Unicode line and paragraph separators. A
trailing carriage return is dropped. A line that is not a JSON object refuses the turn,
because the adapters cannot tell what such output meant.

Process group. **Cursor review, F5.** A harness starts in a session and process group
of its own, and every signal Dromon sends reaches the whole group, so a child the
harness started, such as Pi's MCP bridge or a tool's process, cannot outlive the turn.
Once the harness has exited, any member of its group that remains is terminated and
then killed. **Platform limit.** A child that starts a session of its own leaves the
group and is not reached. **Porting hazard.** ``os.killpg`` is POSIX only, and a group
identifier could in principle be reused once the group is empty, a window of at most
``EXIT_SECONDS`` after the harness exits.

Standard error is discarded. A harness may print diagnostics there that Dromon cannot
parse, and in principle text near a credential, so none of it reaches the event log.

Porting note. Each record is read whole up to ``MAX_LINE`` bytes, a bound a port keeps.
"""

import asyncio
import json
import os
import shutil
import signal
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Final, Protocol, cast

from dromon.engine import EngineRefused

MAX_LINE: Final = 64 * 1024 * 1024
"""The longest record accepted, in bytes."""

EXIT_SECONDS: Final = 10
"""How long a harness may take to exit once its input is closed, before it is
terminated, and again before it is killed."""

GROUP_POLL_SECONDS: Final = 0.1
"""How often a harness's remaining process group is checked while it is terminated."""

TURN_SECONDS: Final = 3600
"""**Cursor review, F6.** The longest a turn on these engines may run before Dromon
stops it and refuses the turn, so that a hung or endless run cannot hold a line without
bound. An hour, the same as the Antigravity print timeout Dromon passes."""

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

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def send(self, message: Mapping[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise EngineRefused("the engine was started without a standard input")
        stdin.write((json.dumps(message) + "\n").encode())
        await stdin.drain()

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
            raise EngineRefused("the engine sent a line that is not JSON") from error
        if not isinstance(decoded, dict):
            raise EngineRefused("the engine sent a record that is not an object")
        return cast(dict[str, Any], decoded)

    async def stop(self) -> None:
        if self._process.returncode is None:
            signal_group(self._process.pid, signal.SIGTERM)


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
        process = await asyncio.create_subprocess_exec(
            found,
            *arguments,
            stdin=asyncio.subprocess.PIPE if keep_input else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=dict(environment),
            limit=MAX_LINE,
            start_new_session=True,
        )
        try:
            yield Process(process)
        finally:
            await _finish(process)

    return start


def signal_group(group: int, number: int) -> bool:
    """Send ``number`` to every process in ``group``, or with zero only ask whether any
    remains. Returns whether any remained."""
    try:
        os.killpg(group, number)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A member that changed its credentials cannot be signalled, and macOS also
        # reports a group of zombies so. Neither is a process Dromon can end.
        return False
    return True


async def _finish(process: asyncio.subprocess.Process) -> None:
    """Close the harness's input, which asks Pi for an orderly exit, then terminate
    and at last kill a harness that does not exit, and then the rest of its group.
    A child that keeps the harness's output open keeps the harness from counting as
    exited, so such a group is signalled after ``EXIT_SECONDS``."""
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=EXIT_SECONDS)
        except TimeoutError:
            signal_group(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=EXIT_SECONDS)
            except TimeoutError:
                signal_group(process.pid, signal.SIGKILL)
                await process.wait()
    await sweep_group(process.pid)


async def sweep_group(group: int) -> None:
    """End what remains of a harness's process group once the harness has exited,
    with a termination, a bounded wait and then a kill."""
    if not signal_group(group, signal.SIGTERM):
        return
    for _ in range(int(EXIT_SECONDS / GROUP_POLL_SECONDS)):
        await asyncio.sleep(GROUP_POLL_SECONDS)
        if not signal_group(group, 0):
            return
    signal_group(group, signal.SIGKILL)

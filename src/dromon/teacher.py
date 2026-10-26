"""A project's local teacher model, served by Ollama on the loopback interface.

Some projects generate data with a local model rather than a hosted one, so that
nothing leaves the machine. The project file's ``[teacher]`` table names the model, the
context to load it with and how long it stays resident. ``dromon teacher up`` brings it
up, ``status`` reports it and ``down`` releases it, and ``dromond`` brings it up at
start when the table sets ``autostart``. ``docs/protocol/TEACHER.md`` is normative.

Bringing the teacher up. If nothing answers at the endpoint, Dromon starts ``ollama
serve`` bound to it, records the process in the runtime directory and waits for it to
answer. The model must already be pulled, because a pull downloads many gigabytes and
is the operator's decision. The model is then loaded with the declared context, since a
request that omits it inherits the server's default, which a project measured at eight
times what its prompts needed.

Memory. A teacher can need most of a host's memory, and a project recorded its host
crashing when memory ran out. Before loading, ``up`` compares the memory the operating
system reports as available, on macOS ``kern.memorystatus_level`` of the physical memory
and on Linux ``MemAvailable``, with the model's size plus ``memory_headroom_gb``, and
refuses unless forced. Free pages alone understate what the system can reclaim, so
they are not used. Where neither source exists the check is skipped and said to be.

Releasing it. ``down`` unloads the model, and stops the server only if Dromon started
it, so a server the operator runs, such as the desktop application's, is never stopped.

Trust. The endpoint must be ``http`` on ``127.0.0.1`` or ``localhost``, so the teacher
cannot be pointed at a remote host. Its answers are untrusted text, and Dromon only
loads and unloads the model without reading its output.

Porting note. HTTP to a local server and a recorded process identifier carry over to
the Rust port unchanged. Starting a detached process uses ``subprocess``, which maps
onto ``std::process``.
"""

import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

DEFAULT_ENDPOINT: Final = "http://127.0.0.1:11434"
LOCAL_ENDPOINT: Final = re.compile(r"http://(127\.0\.0\.1|localhost):([0-9]{1,5})")
KEEP_ALIVE: Final = re.compile(r"-1|0|[1-9][0-9]{0,5}[smh]")
MODEL_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
MIN_CONTEXT: Final = 512
MAX_CONTEXT: Final = 1048576
SERVER_WAIT_SECONDS: Final = 60.0
LOAD_TIMEOUT_SECONDS: Final = 900.0
PID_FILE: Final = "teacher.pid"
LOG_FILE: Final = "teacher.log"


class TeacherError(Exception):
    """The teacher could not be configured, reached, started, loaded or released."""


@dataclass(frozen=True, slots=True)
class TeacherConfig:
    model: str
    endpoint: str = DEFAULT_ENDPOINT
    context: int = 4096
    """The context the model is loaded with, in tokens."""
    keep_alive: str = "30m"
    """How long Ollama keeps the model resident after its last request."""
    autostart: bool = False
    """Whether ``dromond`` brings the teacher up when it starts."""
    memory_headroom_gb: float = 4.0
    """Memory that must stay available beyond the model's size when it loads."""


TEACHER_KEYS: Final = frozenset(
    {"model", "endpoint", "context", "keep_alive", "autostart", "memory_headroom_gb"}
)
MAX_HEADROOM_GB: Final = 1024.0


def parse_teacher(value: object) -> TeacherConfig:
    """Validate a ``[teacher]`` table, or raise ``TeacherError`` naming the key."""
    if not isinstance(value, dict):
        raise TeacherError("teacher must be a table")
    table = cast(dict[str, object], value)
    unknown = sorted(set(table) - TEACHER_KEYS)
    if unknown:
        raise TeacherError(
            f"teacher.{unknown[0]} is not a known key."
            f" Known keys are {sorted(TEACHER_KEYS)}"
        )
    model = table.get("model")
    if not isinstance(model, str) or MODEL_NAME.fullmatch(model) is None:
        raise TeacherError("teacher.model must be an Ollama model name")
    endpoint = table.get("endpoint", DEFAULT_ENDPOINT)
    found = LOCAL_ENDPOINT.fullmatch(endpoint) if isinstance(endpoint, str) else None
    if found is None or not 1 <= int(found.group(2)) <= 65535:
        raise TeacherError(
            "teacher.endpoint must be http on 127.0.0.1 or localhost with a port"
        )
    context = table.get("context", 4096)
    if type(context) is not int or not MIN_CONTEXT <= context <= MAX_CONTEXT:
        raise TeacherError(
            f"teacher.context must be an integer in [{MIN_CONTEXT}, {MAX_CONTEXT}]"
        )
    keep_alive = table.get("keep_alive", "30m")
    if not isinstance(keep_alive, str) or KEEP_ALIVE.fullmatch(keep_alive) is None:
        raise TeacherError(
            "teacher.keep_alive must be -1, 0, or a number followed by s, m or h"
        )
    autostart = table.get("autostart", False)
    if type(autostart) is not bool:
        raise TeacherError("teacher.autostart must be a boolean")
    headroom = table.get("memory_headroom_gb", 4.0)
    if (
        type(headroom) not in (int, float)
        or not 0 <= cast(float, headroom) <= MAX_HEADROOM_GB
    ):
        raise TeacherError(
            f"teacher.memory_headroom_gb must be a number in [0, {MAX_HEADROOM_GB:.0f}]"
        )
    return TeacherConfig(
        model,
        cast(str, endpoint),
        context,
        keep_alive,
        autostart,
        float(cast(float, headroom)),
    )


def available_memory() -> int | None:
    """Bytes the operating system reports as available to a new process, or ``None``
    where it reports nothing Dromon reads."""
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                fields = line.split()
                if len(fields) >= 2 and fields[1].isdigit():
                    return int(fields[1]) * 1024
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_level", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    values = completed.stdout.split()
    if (
        completed.returncode != 0
        or len(values) != 2
        or not all(value.isdigit() for value in values)
    ):
        return None
    percent, total = int(values[0]), int(values[1])
    if not 0 <= percent <= 100:
        return None
    return total * percent // 100


@dataclass(frozen=True, slots=True)
class TeacherStatus:
    reachable: bool
    version: str
    pulled: bool
    loaded: bool
    resident_bytes: int
    context: int
    """The context Ollama reports for the loaded model, or 0 when it reports none."""
    started_by_dromon: bool
    available_bytes: int | None = None
    """Memory the operating system reports as available, or ``None`` if unknown."""

    def summary(self) -> str:
        if not self.reachable:
            return "server not reachable"
        state = (
            "loaded"
            if self.loaded
            else "pulled, not loaded"
            if self.pulled
            else "not pulled"
        )
        parts = [f"Ollama {self.version}", state]
        if self.loaded:
            parts.append(f"{self.resident_bytes / 1e9:.1f} GB resident")
            if self.context:
                parts.append(f"context {self.context}")
        parts.append(
            "server started by Dromon"
            if self.started_by_dromon
            else "server not started by Dromon"
        )
        if self.available_bytes is not None:
            parts.append(f"{self.available_bytes / 1e9:.1f} GB available")
        return ", ".join(parts)


type Transport = Callable[[str, str, Mapping[str, object] | None, float], object]
"""``(method, url, body, timeout)`` to the decoded JSON answer, or ``OSError``."""


def _http(
    method: str, url: str, body: Mapping[str, object] | None, timeout: float
) -> object:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = cast(bytes, response.read())
    return json.loads(payload) if payload else {}


type Starter = Callable[[list[str], Mapping[str, str], Path], int]
"""Starts a detached process and returns its identifier."""


def _start_detached(
    command: list[str], environment: Mapping[str, str], log: Path
) -> int:
    with log.open("ab") as output:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            env=dict(environment),
            start_new_session=True,
        )
    return process.pid


def _command_name(pid: int) -> str | None:
    """The executable name of process ``pid``, or ``None`` if there is none."""
    completed = subprocess.run(  # noqa: S603
        ["/bin/ps", "-p", str(pid), "-o", "comm="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    name = completed.stdout.strip()
    return Path(name).name if completed.returncode == 0 and name else None


class Teacher:
    """The teacher of one project, with its runtime directory for the server record."""

    def __init__(
        self,
        config: TeacherConfig,
        runtime_dir: Path,
        *,
        transport: Transport = _http,
        starter: Starter = _start_detached,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        command_of: Callable[[int], str | None] = _command_name,
        kill: Callable[[int, int], None] = os.kill,
        memory: Callable[[], int | None] = available_memory,
    ) -> None:
        self.config = config
        self.runtime_dir = runtime_dir
        self._transport = transport
        self._starter = starter
        self._environment = os.environ if environment is None else environment
        self._clock = clock
        self._sleep = sleep
        self._command_of = command_of
        self._kill = kill
        self._memory = memory

    def _get(self, path: str) -> dict[str, object]:
        answer = self._transport("GET", self.config.endpoint + path, None, 5.0)
        return cast(dict[str, object], answer) if isinstance(answer, dict) else {}

    def _version(self) -> str | None:
        try:
            return str(self._get("/api/version").get("version", ""))
        except (OSError, ValueError):
            return None

    def _names(self, path: str) -> list[dict[str, object]]:
        models = self._get(path).get("models", [])
        return [
            cast(dict[str, object], m)
            for m in cast(list[object], models)
            if isinstance(m, dict)
        ]

    def _ours(self, entries: list[dict[str, object]]) -> dict[str, object] | None:
        for entry in entries:
            if self.config.model in (entry.get("name"), entry.get("model")):
                return entry
        return None

    def _recorded_pid(self) -> int | None:
        try:
            text = (self.runtime_dir / PID_FILE).read_text(encoding="ascii").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def _alive(self, pid: int) -> bool:
        """Whether ``pid`` is still the ``ollama`` process Dromon started. A recorded
        identifier can outlive its process and be reused, so the command is checked
        before any signal is sent."""
        return self._command_of(pid) == "ollama"

    def status(self) -> TeacherStatus:
        pid = self._recorded_pid()
        started = pid is not None and self._alive(pid)
        version = self._version()
        available = self._memory()
        if version is None:
            return TeacherStatus(False, "", False, False, 0, 0, started, available)
        try:
            pulled = self._ours(self._names("/api/tags")) is not None
            loaded = self._ours(self._names("/api/ps"))
        except (OSError, ValueError) as error:
            raise TeacherError(
                f"the teacher answered its version but not its models: {error}"
            ) from error
        size = loaded.get("size_vram", loaded.get("size", 0)) if loaded else 0
        context = loaded.get("context_length", 0) if loaded else 0
        return TeacherStatus(
            True,
            version,
            pulled,
            loaded is not None,
            size if type(size) is int else 0,
            context if type(context) is int else 0,
            started,
            available,
        )

    def _serve(self) -> None:
        """Start ``ollama serve`` on the endpoint and wait until it answers."""
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        host = self.config.endpoint.removeprefix("http://")
        environment = {**self._environment, "OLLAMA_HOST": host}
        try:
            pid = self._starter(
                ["ollama", "serve"], environment, self.runtime_dir / LOG_FILE
            )
        except OSError as error:
            raise TeacherError(f"cannot start ollama serve: {error}") from error
        (self.runtime_dir / PID_FILE).write_text(f"{pid}\n", encoding="ascii")
        deadline = self._clock() + SERVER_WAIT_SECONDS
        while self._clock() < deadline:
            if self._version() is not None:
                return
            if not self._alive(pid):
                raise TeacherError(
                    "ollama serve exited before answering."
                    f" See {self.runtime_dir / LOG_FILE}"
                )
            self._sleep(0.5)
        raise TeacherError(
            f"ollama serve did not answer within {SERVER_WAIT_SECONDS:.0f}s"
        )

    def up(self, *, force: bool = False) -> TeacherStatus:
        """Bring the teacher up and return its status. Raises ``TeacherError``, after
        stopping a server this call started, so a failed attempt leaves none behind.
        ``force`` skips the memory check."""
        started = False
        if self._version() is None:
            self._serve()
            started = True
        try:
            return self._load(force)
        except TeacherError:
            if started:
                self._stop_server()
            raise

    def _load(self, force: bool) -> TeacherStatus:
        current = self.status()
        if not current.pulled:
            raise TeacherError(
                f"the model {self.config.model} is not pulled. Pull it with"
                f" `ollama pull {self.config.model}`, which downloads it"
            )
        if not current.loaded and not force:
            self._check_memory(current.available_bytes)
        body: dict[str, object] = {
            "model": self.config.model,
            "prompt": "",
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "options": {"num_ctx": self.config.context},
        }
        try:
            self._transport(
                "POST",
                self.config.endpoint + "/api/generate",
                body,
                LOAD_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError) as error:
            raise TeacherError(
                f"the teacher did not load {self.config.model}: {error}"
            ) from error
        loaded = self.status()
        if not loaded.loaded:
            raise TeacherError(f"the teacher reports {self.config.model} as not loaded")
        return loaded

    def _check_memory(self, available: int | None) -> None:
        """Refuse to load when available memory is below the model's size plus the
        headroom. An unknown amount is not a refusal, and ``status`` shows it as
        absent."""
        if available is None:
            return
        entry = self._ours(self._names("/api/tags")) or {}
        size = entry.get("size", 0)
        needed = (size if type(size) is int else 0) + int(
            self.config.memory_headroom_gb * 1e9
        )
        if available < needed:
            raise TeacherError(
                f"only {available / 1e9:.1f} GB of memory is available, and"
                f" {self.config.model} needs {needed / 1e9:.1f} GB with"
                f" {self.config.memory_headroom_gb:g} GB of headroom. Free memory, or"
                " pass --force to load it anyway"
            )

    def down(self) -> TeacherStatus:
        """Unload the model, and stop the server if Dromon started it."""
        # Ollama answers 404 to unloading a model it has not loaded, so only a loaded
        # model is unloaded, and the server is stopped either way.
        if self._version() is not None and self.status().loaded:
            body = {
                "model": self.config.model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            }
            try:
                self._transport(
                    "POST", self.config.endpoint + "/api/generate", body, 60.0
                )
            except (OSError, ValueError) as error:
                raise TeacherError(
                    f"the teacher did not unload {self.config.model}: {error}"
                ) from error
        self._stop_server()
        return self.status()

    def _stop_server(self) -> None:
        """Stop the server Dromon started, if it is still running, and forget it."""
        pid = self._recorded_pid()
        if pid is not None and self._alive(pid):
            self._kill(pid, signal.SIGTERM)
            deadline = self._clock() + SERVER_WAIT_SECONDS
            while self._alive(pid) and self._clock() < deadline:
                self._sleep(0.2)
            if self._alive(pid):
                raise TeacherError(f"the teacher server {pid} did not stop")
        (self.runtime_dir / PID_FILE).unlink(missing_ok=True)

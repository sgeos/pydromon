"""Probe evidence. The frames an engine exchanged, recorded to a file, and replayed.

**Row 7 of the T3 Code review, 2026-10-27.** Pi, Cursor and Antigravity were tested
only against hand-written fakes. ``dromon probe --record`` writes every frame the
adapter sent and received during its turn, and optionally a bounded, redacted copy of
the engine's standard error, to a file under ``tmp/probes``. A test replays such a file
through the real adapter, so each live probe becomes regression evidence that costs no
subscription in continuous integration.

File format, version 1, JSON lines.
- The first line is ``{"v": 1, "kind": "header", "engine": ..., "model": ...,
  "recorded_at": ...}``.
- Each frame is ``{"kind": "frame", "dir": "out" | "in", "frame": {...}}``, ``out``
  for what the adapter sent and ``in`` for what the engine sent.
- ``{"kind": "truncated", "frames": N}`` once the bounds were reached, after which no
  frame is recorded.
- ``{"kind": "stderr", "text": ..., "cut": bool}`` last, when standard error was
  asked for.

What it holds. Frames are recorded as exchanged, prompts and replies included, so the
file is written with owner-only permissions under the project's ignored ``tmp/``
directory and is never committed. Standard error is masked by ``dromon.redact`` and cut
to ``MAX_STDERR_BYTES``, since a harness may print text near a credential there.
Masking is by pattern, as that module states, so a reviewer still reads the file
before it is kept as a fixture.

Where it hooks in. The Pi, Cursor and Antigravity adapters start their engine through
``dromon.jsonlines``, which records while ``RECORDING`` holds a recording. The Claude
Code adapter records through ``recording_transport``. Codex and Grok frame their own
traffic and do not record yet. Porting note, ``RECORDING`` is a context variable, an
ambient value a port passes explicitly instead.

Replay. ``Replay`` hands the adapter each recorded ``in`` frame only once the adapter
has sent as many frames as preceded it in the recording, so the order of the exchange
is kept. An adapter that waits for a frame it has not earned, or that sends after the
recording ends, is said to diverge, within ``REPLAY_WAIT_SECONDS``, and never hangs.
The frames the adapter sent are kept for the test to compare.

Scrubbing, at the operator's instruction of 2026-10-27. The first live Claude Code
recording held the account's email address and organization, the operator's home
directory, session ids and message uuids. A raw recording never becomes a tracked
fixture. ``scrub`` replaces the values of every ``account`` block, except the ``type``
an adapter reads, and those values wherever else they occur, the home directory and
its dashed form in Claude Code's project directories, every uuid and every session id,
and the account's usage in ``rate_limit_info``, at the operator's further instruction,
with stable placeholders, the same value always by the same placeholder, so the frames
still replay. ``scrub_file`` then refuses a fixture in which the home directory, the
user name, an email address or an account value is still found. The check is by
pattern, so a person still reads a fixture before it is committed, and the repository's
disclosure scan runs over it once it is tracked.

Review of 2026-10-27. Account values are replaced as whole words whatever their length,
and ``rate_limit_info`` keeps only what a reader needs, so a field nobody listed is
replaced rather than passed through. The probe turn's own token counts and cost are
kept, at the operator's decision, since they describe that one turn and not the
account, and the adapter reads them on replay as it does live.
"""

import asyncio
import json
import os
import re
import unicodedata
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from dromon import redact

FORMAT_VERSION: Final = 1
MAX_FRAMES: Final = 20_000
"""The most frames one recording holds."""
MAX_RECORD_BYTES: Final = 32 * 1024 * 1024
"""The most bytes of frames one recording holds."""
MAX_STDERR_BYTES: Final = 64 * 1024
"""The most bytes of standard error kept, after masking."""
MAX_READ_BYTES: Final = MAX_RECORD_BYTES + MAX_STDERR_BYTES + 1024 * 1024
"""The largest recording ``read_recording`` accepts."""
REPLAY_WAIT_SECONDS: Final = 5.0
"""How long a replay waits for the adapter to send what the recording says it sent
before the next frame, before it says the adapter diverged."""
OUT: Final = "out"
IN: Final = "in"
PROBES_DIRECTORY: Final = Path("tmp") / "probes"


class RecordingError(ValueError):
    """A recording that cannot be read, with where it went wrong."""


class ReplayDiverged(RuntimeError):
    """The adapter did not exchange what the recording holds."""


class Recording:
    """One probe's recording, written as the frames pass."""

    def __init__(
        self, path: Path, engine: str, model: str, *, stderr: bool = False
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.path = path
        self.wants_stderr = stderr
        self._file = os.fdopen(descriptor, "w", encoding="utf-8")
        self._frames = 0
        self._bytes = 0
        self._truncated = False
        self._stderr = bytearray()
        self._stderr_cut = False
        self._write(
            {
                "v": FORMAT_VERSION,
                "kind": "header",
                "engine": engine,
                "model": model,
                "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    def _write(self, document: Mapping[str, object]) -> int:
        line = json.dumps(document, ensure_ascii=False) + "\n"
        self._file.write(line)
        self._file.flush()
        return len(line.encode("utf-8"))

    def frame(self, direction: str, frame: Mapping[str, object]) -> None:
        """Record one frame, or nothing once the bounds were reached."""
        if direction not in (OUT, IN):
            raise ValueError(f"a frame goes out or in, not {direction!r}")
        if self._truncated or self._file.closed:
            return
        line = json.dumps(
            {"kind": "frame", "dir": direction, "frame": dict(frame)},
            ensure_ascii=False,
        )
        size = len(line.encode("utf-8")) + 1
        if self._frames >= MAX_FRAMES or self._bytes + size > MAX_RECORD_BYTES:
            self._truncated = True
            self._write({"kind": "truncated", "frames": self._frames})
            return
        self._file.write(line + "\n")
        self._file.flush()
        self._frames += 1
        self._bytes += size

    def stderr(self, data: bytes) -> None:
        """Keep standard error, up to ``MAX_STDERR_BYTES`` before masking."""
        if not self.wants_stderr:
            return
        room = MAX_STDERR_BYTES - len(self._stderr)
        if len(data) > room:
            self._stderr_cut = True
        self._stderr.extend(data[: max(0, room)])

    def close(self) -> None:
        """Write the masked standard error, if asked for, and close the file."""
        if self._file.closed:
            return
        if self.wants_stderr:
            text = redact.mask(self._stderr.decode("utf-8", "replace"))
            self._write({"kind": "stderr", "text": text, "cut": self._stderr_cut})
        self._file.close()


RECORDING: ContextVar[Recording | None] = ContextVar("dromon_recording", default=None)
"""The recording the frames of this context go to, set only by ``dromon probe``."""


def probe_path(root: Path, engine: str, now: datetime | None = None) -> Path:
    """Where a probe of ``engine`` records, under the project's ``tmp/probes``."""
    moment = now or datetime.now(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%S%fZ")
    return root / PROBES_DIRECTORY / f"{engine}-{stamp}.jsonl"


def record(direction: str, frame: Mapping[str, object]) -> None:
    """Record ``frame`` when a recording is active, and nothing otherwise."""
    recording = RECORDING.get()
    if recording is not None:
        recording.frame(direction, frame)


async def drain_stderr(stream: asyncio.StreamReader, recording: Recording) -> None:
    """Read an engine's standard error to its end, so the engine never blocks on
    it, keeping what the recording's bound allows."""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        recording.stderr(chunk)


@dataclass(slots=True)
class Recorded:
    """A recording, read back."""

    header: dict[str, Any]
    frames: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    truncated: bool = False
    stderr: str | None = None
    stderr_cut: bool = False


def read_recording(path: Path) -> Recorded:
    """Read and check a recording. Raises ``RecordingError`` naming the line."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
    except OSError as error:
        raise RecordingError(f"cannot read the recording {path}: {error}") from error
    if len(raw) > MAX_READ_BYTES:
        raise RecordingError(f"the recording {path} exceeds {MAX_READ_BYTES} bytes")
    lines = raw.decode("utf-8", "replace").splitlines()
    if not lines:
        raise RecordingError(f"the recording {path} is empty")
    documents: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        try:
            document: object = json.loads(line)
        except ValueError as error:
            raise RecordingError(f"{path}:{number}: not JSON") from error
        if not isinstance(document, dict):
            raise RecordingError(f"{path}:{number}: not an object")
        documents.append(cast(dict[str, Any], document))
    header = documents[0]
    if header.get("kind") != "header" or header.get("v") != FORMAT_VERSION:
        raise RecordingError(f"{path}:1: not a version {FORMAT_VERSION} header")
    found = Recorded(header)
    for number, document in enumerate(documents[1:], start=2):
        kind = document.get("kind")
        if found.stderr is not None:
            raise RecordingError(f"{path}:{number}: a record after standard error")
        if kind == "frame":
            direction, frame = document.get("dir"), document.get("frame")
            if direction not in (OUT, IN) or not isinstance(frame, dict):
                raise RecordingError(f"{path}:{number}: a malformed frame")
            if found.truncated:
                raise RecordingError(f"{path}:{number}: a frame after truncation")
            found.frames.append((str(direction), cast(dict[str, Any], frame)))
        elif kind == "truncated":
            found.truncated = True
        elif kind == "stderr" and isinstance(document.get("text"), str):
            found.stderr = str(document["text"])
            found.stderr_cut = document.get("cut") is True
        else:
            raise RecordingError(f"{path}:{number}: an unknown record {kind!r}")
    return found


class Replay:
    """The engine's side of a recorded exchange, played back in order."""

    def __init__(
        self, recorded: Recorded, wait_seconds: float = REPLAY_WAIT_SECONDS
    ) -> None:
        self._incoming: list[tuple[int, dict[str, Any]]] = []
        self.recorded_out: list[dict[str, Any]] = []
        outs = 0
        for direction, frame in recorded.frames:
            if direction == OUT:
                outs += 1
                self.recorded_out.append(frame)
            else:
                self._incoming.append((outs, frame))
        self.expected_out = outs
        self.sent: list[dict[str, Any]] = []
        self._next = 0
        self._wait = wait_seconds
        self._changed = asyncio.Event()

    def send(self, frame: Mapping[str, object]) -> None:
        """Take a frame the adapter sent."""
        if len(self.sent) >= self.expected_out:
            raise ReplayDiverged(
                f"the adapter sent frame {len(self.sent) + 1}, but the recording"
                f" holds {self.expected_out}"
            )
        self.sent.append(dict(frame))
        self._changed.set()

    async def receive(self) -> dict[str, Any] | None:
        """The next frame the engine sent, once the adapter has earned it, or
        ``None`` at the recording's end."""
        if self._next >= len(self._incoming):
            return None
        needed, frame = self._incoming[self._next]
        while len(self.sent) < needed:
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), self._wait)
            except TimeoutError as error:
                raise ReplayDiverged(
                    f"the adapter waits for frame {self._next + 1} from the engine,"
                    f" which the recording gave after {needed} sent frames, but the"
                    f" adapter has sent {len(self.sent)}"
                ) from error
        self._next += 1
        return frame

    def finished(self) -> bool:
        """Whether every recorded frame was exchanged."""
        return self._next >= len(self._incoming) and len(self.sent) == self.expected_out


class ReplayConnection:
    """A ``dromon.jsonlines`` connection played from a recording."""

    def __init__(self, replay: Replay) -> None:
        self.replay = replay
        self.stopped = False

    async def send(self, message: Mapping[str, object]) -> None:
        self.replay.send(message)

    async def receive(self) -> dict[str, Any] | None:
        return await self.replay.receive()

    async def stop(self) -> None:
        self.stopped = True


@dataclass(slots=True)
class ReplayStart:
    """What an adapter asked of a replayed start, for a test to check."""

    arguments: list[str]
    cwd: Path
    environment: dict[str, str]


def replay_factory(replay: Replay, starts: list[ReplayStart] | None = None) -> Any:  # noqa: ANN401
    """A ``dromon.jsonlines.ConnectionFactory`` that plays ``replay`` once per start,
    and notes each start in ``starts``. Typed loosely here, since ``dromon.jsonlines``
    imports this module."""

    @asynccontextmanager
    async def start(
        arguments: list[str], cwd: Path, environment: Mapping[str, str]
    ) -> AsyncGenerator[ReplayConnection]:
        if starts is not None:
            starts.append(ReplayStart(list(arguments), cwd, dict(environment)))
        yield ReplayConnection(replay)

    return start


SCRUBBED_HOME: Final = "/home/operator"
"""What the operator's home directory becomes in a scrubbed recording."""
ACCOUNT_KEPT: Final = frozenset({"type"})
"""Account fields a scrub keeps, which name a kind of account, not a person, and
which an adapter reads, as the Codex adapter reads ``type``."""
SESSION_KEYS: Final = frozenset({"session_id", "sessionId"})
MIN_SECRET_LENGTH: Final = 4
"""Account values at least this long are also refused where they remain inside a
longer word. Every account value is replaced, and refused, as a whole word whatever
its length. **Review of 2026-10-27, E2.** Values shorter than this were once replaced
in the account block alone. Failure mode, a short value such as a plan name is also
replaced where it stands as a word in unrelated text, which may stop a frame from
replaying, and then the fixture is made from another recording."""
MAX_SCRUB_DEPTH: Final = 64
"""The deepest nesting a scrub walks. A deeper frame is refused, not passed over."""
_UUID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_EMAIL: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _word(value: str) -> re.Pattern[str]:
    """``value`` where it stands as a whole word, not inside a longer one."""
    return re.compile(rf"(?<!\w){re.escape(value)}(?!\w)")


def _replace_word(text: str, word: str, placeholder: str) -> str:
    """``text`` with ``word`` replaced by ``placeholder`` wherever it is a whole word.
    The placeholder is inserted as it is, never read as a pattern."""
    return _word(word).sub(lambda _: placeholder, text)


class Scrubber:
    """Stable placeholders for one recording's identifying values."""

    def __init__(self, home: str, secrets: Mapping[str, str]) -> None:
        if not home.startswith("/") or home.rstrip("/") == "":
            raise ValueError(f"the home directory {home!r} is not an absolute path")
        self.home = home.rstrip("/")
        self.secrets = dict(secrets)
        self._uuids: dict[str, str] = {}
        self._sessions: dict[str, str] = {}

    def _uuid(self, found: re.Match[str]) -> str:
        key = found.group(0).lower()
        if key not in self._uuids:
            self._uuids[key] = f"00000000-0000-4000-8000-{len(self._uuids) + 1:012x}"
        return self._uuids[key]

    def text(self, value: str) -> str:
        """``value`` with every account value, home directory and uuid replaced."""
        for secret, placeholder in self.secrets.items():
            value = _replace_word(value, secret, placeholder)
        value = value.replace(self.home, SCRUBBED_HOME)
        value = value.replace(_dashed(self.home), _dashed(SCRUBBED_HOME))
        return _UUID.sub(self._uuid, value)

    def session(self, value: str) -> str:
        """A session id's placeholder, a uuid's where it is one."""
        if _UUID.fullmatch(value):
            return self.text(value)
        if value not in self._sessions:
            self._sessions[value] = f"session-{len(self._sessions) + 1}"
        return self._sessions[value]

    def value(self, value: object, depth: int = 0) -> object:
        """``value``, a decoded JSON value, scrubbed."""
        if depth > MAX_SCRUB_DEPTH:
            raise RecordingError(f"a frame nests deeper than {MAX_SCRUB_DEPTH}")
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item, depth + 1) for item in cast(list[object], value)]
        if not isinstance(value, dict):
            return value
        scrubbed: dict[str, object] = {}
        for key, item in cast(dict[str, object], value).items():
            if key == "account" and isinstance(item, dict):
                scrubbed[key] = _account(cast(dict[str, object], item))
            elif key == "rate_limit_info":
                scrubbed[key] = _rate_limits(item, depth + 1)
            elif key in SESSION_KEYS and isinstance(item, str):
                scrubbed[key] = self.session(item)
            else:
                scrubbed[key] = self.value(item, depth + 1)
        return scrubbed


SCRUBBED_FRACTION: Final = 0.5
"""What a utilization in ``rate_limit_info`` becomes."""
SCRUBBED_EPOCH: Final = 1767225600
"""What a reset time in ``rate_limit_info`` becomes, 2026-01-01T00:00:00Z."""
SCRUBBED_STATUS: Final = "allowed"
"""What a status in ``rate_limit_info`` becomes, a value the Agent SDK's
``RateLimitStatus`` accepts, so the frame still parses."""
SCRUBBED_REASON: Final = "scrubbed"
"""What a reason, and any other text, in ``rate_limit_info`` becomes."""
SCRUBBED_NUMBER: Final = 0
"""What a number in ``rate_limit_info`` that no reader is known to read becomes."""

READ_RATE_LIMIT_FIELDS: Final[Mapping[str, object]] = {
    "status": SCRUBBED_STATUS,
    "resetsAt": SCRUBBED_EPOCH,
    "utilization": SCRUBBED_FRACTION,
    "overageStatus": SCRUBBED_STATUS,
    "overageResetsAt": SCRUBBED_EPOCH,
    "overageDisabledReason": SCRUBBED_REASON,
}
"""The fields of ``rate_limit_info`` the Agent SDK reads, as its ``RateLimitInfo``
of ``claude-agent-sdk`` 0.2.174 names them, each with the placeholder that keeps it
valid there. Dromon's adapter reads none of them. An SDK upgrade checks this list."""
RATE_LIMIT_WINDOWS: Final = frozenset(
    {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"}
)
"""The values of ``rateLimitType`` kept, the windows the SDK names, which say which
limit applies and nothing of its use. Any other value is replaced."""


def _rate_limit_value(key: str, value: object) -> object:
    """One field of ``rate_limit_info`` that holds no object or list. **Review of
    2026-10-27, E3.** A field the SDK reads gets its fixed placeholder, the window
    name is kept, and every other field is replaced by a fixed value of its type, so
    a field no one listed, such as a count of remaining requests, cannot pass
    through. ``None`` says only that a value is absent, and is kept."""
    if value is None:
        return None
    if key == "rateLimitType" and value in RATE_LIMIT_WINDOWS:
        return value
    if key in READ_RATE_LIMIT_FIELDS:
        return READ_RATE_LIMIT_FIELDS[key]
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return SCRUBBED_NUMBER
    return SCRUBBED_REASON


def _rate_limits(value: object, depth: int, key: str = "") -> object:
    """``rate_limit_info``, at the operator's instruction of 2026-10-27, with every
    value replaced as ``_rate_limit_value`` says, through each window and list it
    nests, a list's items under the list's own field name. The names of fields and
    windows are kept, so the shape replays. ``rate_limit_info`` that is itself not
    an object is replaced as one field would be."""
    if depth > MAX_SCRUB_DEPTH:
        raise RecordingError(f"a frame nests deeper than {MAX_SCRUB_DEPTH}")
    if isinstance(value, list):
        return [
            _rate_limits(item, depth + 1, key) for item in cast(list[object], value)
        ]
    if not isinstance(value, dict):
        return _rate_limit_value(key, value)
    return {
        name: _rate_limits(item, depth + 1, name)
        for name, item in cast(dict[str, object], value).items()
    }


def _dashed(path: str) -> str:
    """``path`` as Claude Code names a project's directory, ``/`` turned to ``-``."""
    return path.replace("/", "-")


def _account(block: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value if key in ACCOUNT_KEPT else f"scrubbed-account-{key}"
        for key, value in block.items()
    }


def _account_values(recorded: Recorded) -> dict[str, str]:
    """Every string in an ``account`` block of ``recorded``, by its placeholder,
    found by walking each frame with an explicit, bounded stack."""
    found: dict[str, str] = {}
    stack: list[tuple[object, int]] = [(frame, 0) for _, frame in recorded.frames]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_SCRUB_DEPTH:
            raise RecordingError(f"a frame nests deeper than {MAX_SCRUB_DEPTH}")
        if isinstance(value, list):
            stack.extend((item, depth + 1) for item in cast(list[object], value))
        elif isinstance(value, dict):
            for key, item in cast(dict[str, object], value).items():
                child: object = item
                if key == "account" and isinstance(item, dict):
                    for name, field_value in cast(dict[str, object], item).items():
                        if name not in ACCOUNT_KEPT and isinstance(field_value, str):
                            found[field_value] = f"scrubbed-account-{name}"
                stack.append((child, depth + 1))
    return found


def scrub(recorded: Recorded, home: str) -> tuple[Recorded, frozenset[str]]:
    """``recorded`` with identifying values replaced by stable placeholders, and the
    original account values, every one of which is replaced wherever it stands as a
    word, and which ``leaks`` looks for afterwards. The longest are replaced first,
    so a value inside another is not replaced in part."""
    secrets = {
        secret: placeholder
        for secret, placeholder in sorted(
            _account_values(recorded).items(), key=lambda item: -len(item[0])
        )
        if secret
    }
    scrubber = Scrubber(home, secrets)
    frames = [
        (direction, cast(dict[str, Any], scrubber.value(frame)))
        for direction, frame in recorded.frames
    ]
    header = cast(dict[str, Any], scrubber.value(recorded.header))
    stderr = None if recorded.stderr is None else scrubber.text(recorded.stderr)
    scrubbed = Recorded(header, frames, recorded.truncated, stderr, recorded.stderr_cut)
    return scrubbed, frozenset(secrets)


def leaks(text: str, home: str, secrets: frozenset[str] = frozenset()) -> list[str]:
    """What identifying values ``text`` still holds, by kind, never by value.

    **Review of 2026-10-27, E2 and E6.** The text is also searched in its NFKC form,
    so a full-width ``@`` or letter does not hide an address or a name. The user name
    is looked for as a word whatever its length, and each account value as a word,
    and inside longer words too once it is ``MIN_SECRET_LENGTH`` long. Failure mode,
    a short user name or value that is also a common word refuses a recording that
    holds nothing of the account, which fails safe."""
    home = home.rstrip("/")
    forms = (text, unicodedata.normalize("NFKC", text))
    found: list[str] = []
    if home and any(home in form or _dashed(home) in form for form in forms):
        found.append("the home directory")
    user = home.rsplit("/", 1)[-1]
    named = re.compile(rf"(?<!\w){re.escape(user)}(?!\w)", re.IGNORECASE)
    if user and any(named.search(form) for form in forms):
        found.append("the user name")
    if any(_EMAIL.search(form) for form in forms):
        found.append("an email address")
    if any(
        _word(secret).search(form)
        or (len(secret) >= MIN_SECRET_LENGTH and secret in form)
        for secret in secrets
        if secret
        for form in forms
    ):
        found.append("an account value")
    return found


def _lines(recorded: Recorded) -> str:
    documents: list[Mapping[str, object]] = [recorded.header]
    documents.extend(
        {"kind": "frame", "dir": direction, "frame": frame}
        for direction, frame in recorded.frames
    )
    if recorded.truncated:
        documents.append({"kind": "truncated", "frames": len(recorded.frames)})
    if recorded.stderr is not None:
        documents.append(
            {"kind": "stderr", "text": recorded.stderr, "cut": recorded.stderr_cut}
        )
    return "".join(
        json.dumps(document, ensure_ascii=False) + "\n" for document in documents
    )


def scrub_file(source: Path, destination: Path, home: str | None = None) -> Path:
    """Scrub the recording at ``source`` into a new file at ``destination``, which may
    be committed as a fixture. Raises ``RecordingError`` when the source cannot be
    read or the scrubbed text still holds an identifying value, and
    ``FileExistsError`` when ``destination`` exists. Nothing is written on refusal."""
    where = home if home is not None else str(Path.home())
    recorded, secrets = scrub(read_recording(source), where)
    text = _lines(recorded)
    remaining = leaks(text, where, secrets)
    if remaining:
        raise RecordingError(
            f"the scrubbed recording still holds {', '.join(remaining)}, so no fixture"
            " was written"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(text)
    return destination

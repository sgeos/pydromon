"""Request keys, which make a retried mutating line tool act once.

**Row 11 of the T3 Code review, 2026-10-27.** An engine or a model that retries a call
of ``send_message``, ``ask_operator`` or ``wake_after``, after a timeout or a lost
answer, used to send a second message, ask a second question or move the wake. Each of
those tools now takes an optional ``request_key``. A call whose key the same party used
before on the same tool, with the same arguments, returns the first call's result and
does nothing. The same key with other arguments is refused, since it cannot be told
whether the caller meant the first request or a new one.

Only a call that acted is remembered. A refused call may be retried with its key, and
acts when the cause of its refusal has gone.

Bounds and limits.
- At most ``MAX_KEYS_PER_PARTY`` keys are kept for each party and tool, the oldest
  forgotten first, so a party that cycles through more keys than that may act twice on
  a key it reuses after it was forgotten.
- Keys are kept in memory for the daemon's life, not in the event log, so a retry
  that crosses a daemon restart acts again. The event log's format is unchanged.
- A tool handler checks, acts and remembers with no ``await`` between, so in one
  asyncio loop no other call interleaves. **Porting hazard.** A port that serves tools
  from several threads must hold a lock across the three.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final, cast

REQUEST_KEY: Final = "request_key"
"""The argument each mutating line tool accepts."""
MAX_KEY_LENGTH: Final = 128
MAX_KEYS_PER_PARTY: Final = 256

REQUEST_KEY_PROPERTY: Final[dict[str, object]] = {
    "type": "string",
    "maxLength": MAX_KEY_LENGTH,
    "description": (
        "Optional. A key you choose for this request, at most 128 characters. A"
        " repeated call with the same key and arguments returns the first call's"
        " result and does nothing again, so a retry is safe"
    ),
}

REPEATED: Final = (
    " This repeats an earlier call with the same request_key, so nothing was done"
    " again."
)


class RequestKeyRefused(ValueError):
    """A request key that is malformed, or was used for other arguments."""


def _digest(arguments: Mapping[str, Any]) -> str:
    """A digest of ``arguments`` without the key, independent of their order.
    Raises ``RequestKeyRefused`` for a value JSON cannot hold exactly, such as an
    object or a number that is not finite. **Review of 2026-10-27, K3.** Such values
    were once written as their text, so two different objects with the same text
    would have had the same digest."""
    rest = {name: value for name, value in arguments.items() if name != REQUEST_KEY}
    try:
        text = json.dumps(rest, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RequestKeyRefused(
            f"a call with a request_key must have JSON arguments: {error}"
        ) from error
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key_of(arguments: Mapping[str, Any]) -> str | None:
    """The request key in ``arguments``, or ``None`` when none was given. Raises
    ``RequestKeyRefused`` for a key that is not text of 1 to ``MAX_KEY_LENGTH``
    printable characters."""
    key: object = arguments.get(REQUEST_KEY)
    if key is None:
        return None
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= MAX_KEY_LENGTH
        or not key.isprintable()
    ):
        raise RequestKeyRefused(
            f"request_key must be text of 1 to {MAX_KEY_LENGTH} printable characters"
        )
    return key


class RequestKeys:
    """The results of the keyed calls that acted, by party, tool and key."""

    def __init__(self, limit: int = MAX_KEYS_PER_PARTY) -> None:
        if limit < 1:
            raise ValueError("the limit must be at least 1")
        self._limit = limit
        self._seen: dict[tuple[str, str], dict[str, tuple[str, dict[str, Any]]]] = {}

    def earlier(
        self, party: str, tool: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The result of the earlier call this one repeats, with a note that it is a
        repeat, or ``None`` when it repeats none. Raises ``RequestKeyRefused`` for a
        malformed key, for arguments ``_digest`` refuses, which is checked here
        before the call acts so that ``remember`` cannot fail after it, or for a key
        used before with other arguments."""
        key = key_of(arguments)
        if key is None:
            return None
        current = _digest(arguments)
        found = self._seen.get((party, tool), {}).get(key)
        if found is None:
            return None
        digest, result = found
        if digest != current:
            raise RequestKeyRefused(
                f"request_key {key!r} was used for a different {tool} call. Use a new"
                " key for a new request"
            )
        return _repeated(result)

    def remember(
        self,
        party: str,
        tool: str,
        arguments: Mapping[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Keep ``result`` for the key in ``arguments``, if any, forgetting the oldest
        key of the party and tool beyond the limit."""
        key = key_of(arguments)
        if key is None:
            return
        seen = self._seen.setdefault((party, tool), {})
        seen.pop(key, None)
        seen[key] = (_digest(arguments), result)
        while len(seen) > self._limit:
            del seen[next(iter(seen))]


def _repeated(result: dict[str, Any]) -> dict[str, Any]:
    """``result`` with ``REPEATED`` after its first text block."""
    content: object = result.get("content")
    if not isinstance(content, list):
        return dict(result)
    blocks = [
        dict(cast(dict[str, Any], block))
        for block in cast(list[object], content)
        if isinstance(block, dict)
    ]
    if blocks and isinstance(blocks[0].get("text"), str):
        blocks[0]["text"] = str(blocks[0]["text"]) + REPEATED
    return {**result, "content": blocks}

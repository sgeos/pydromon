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

**Task of 2026-10-29, round 5.** The key is durable. A keyed call records its key and
the digest of its other arguments as a ``RequestStamp`` in the event of its act, a
``message_sent`` or a ``wake_scheduled``, so one append records both and no crash can
leave the act without its key. The state keeps the acts by party, tool and key, and
replay rebuilds them from the events alone, so a retry that crosses a daemon restart
repeats the first result as one within the daemon's life does. Keys were once kept in
memory only, which a restart forgot.

Bounds and limits.
- At most ``MAX_KEYS_PER_PARTY`` keys are kept for each party and tool, the oldest
  forgotten first, after replay as before it, so a party that cycles through more keys
  than that may act twice on a key it reuses after it was forgotten.
- The repeated result is rebuilt from the event of the act, so it does not depend on
  what the daemon held in memory. Where it differs from the first result's text, the
  tool that rebuilds it says so.
- A tool handler checks, acts and records with no ``await`` between, so in one asyncio
  loop no other call interleaves. **Porting hazard.** A port that serves tools from
  several threads must hold a lock across the check and the append.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final, cast

from dromon.model import (
    MAX_REQUEST_KEY_CHARS,
    MAX_REQUEST_KEYS_PER_PARTY,
    RequestedAct,
    RequestStamp,
    State,
)

REQUEST_KEY: Final = "request_key"
"""The argument each mutating line tool accepts."""
MAX_KEY_LENGTH: Final = MAX_REQUEST_KEY_CHARS
MAX_KEYS_PER_PARTY: Final = MAX_REQUEST_KEYS_PER_PARTY

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


def digest_of(arguments: Mapping[str, Any]) -> str:
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


def stamp_of(arguments: Mapping[str, Any]) -> RequestStamp | None:
    """The stamp the act of a call with ``arguments`` records, or ``None`` when the
    call gave no key. Raises ``RequestKeyRefused`` as ``key_of`` and ``digest_of``
    do, before the call acts, so recording the act cannot fail on its key."""
    key = key_of(arguments)
    if key is None:
        return None
    return RequestStamp(key, digest_of(arguments))


def earlier(
    state: State, party: str, tool: str, arguments: Mapping[str, Any]
) -> RequestedAct | None:
    """The event of the earlier act this call repeats, or ``None`` when it repeats
    none. Raises ``RequestKeyRefused`` for a malformed key, for arguments
    ``digest_of`` refuses, or for a key the party used before on the tool with other
    arguments."""
    stamp = stamp_of(arguments)
    if stamp is None:
        return None
    act = state.requests.get((party, tool), {}).get(stamp.key)
    if act is None:
        return None
    if act.request is None or act.request.digest != stamp.digest:
        raise RequestKeyRefused(
            f"request_key {stamp.key!r} was used for a different {tool} call. Use a"
            " new key for a new request"
        )
    return act


def repeated(result: dict[str, Any]) -> dict[str, Any]:
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

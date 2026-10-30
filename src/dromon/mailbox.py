"""Mailboxes: what a party has received, and the views that let it look through them.

Every party, line or operator, has a mailbox holding the messages delivered to it, in
the order sent. Within it a message is unread until its recipient opens it, and
unanswered until the recipient replies to it or acknowledges it. An acknowledgement
marks a message dealt with without a reply. Neither state obliges anything. A recipient
may read, reply, acknowledge, defer or ignore any message, and doing nothing is always
valid.

Every party also sees the mail it has sent, with each message's fate on the
recipient's side, so a sender expecting a reply can tell a reply from a mere
acknowledgement.

These are pure functions of the state, so a client, a line's tools and the wake prompt
all see the same mailbox.
"""

from dataclasses import dataclass
from typing import Final

from dromon.model import Address, HarnessAddress, Message, RemoteAddress, State

MAX_PAGE: Final = 50
"""Upper bound on the messages one listing returns, so a large mailbox is paged."""


@dataclass(frozen=True, slots=True)
class MailSummary:
    new: int
    unread: int
    """Unread messages from every party but the owner."""
    unanswered: int
    own_notes: int = 0
    """The owner's own notes, such as fired wakes, task-end notes and compaction
    requests, that are neither read nor acknowledged, which ``unread`` leaves out and
    this counts apart."""

    def sentence(self) -> str:
        """The summary in the form a wake prompt opens with."""
        plural = "" if self.new == 1 else "s"
        text = (
            f"{self.new} new message{plural}. {self.unread} unread."
            f" {self.unanswered} unanswered."
        )
        if self.own_notes:
            own = "note" if self.own_notes == 1 else "notes"
            verb = "is" if self.own_notes == 1 else "are"
            text += (
                f" {self.own_notes} {own} of your own, such as fired wakes, neither"
                f" read nor acknowledged, {verb} counted apart from unread."
            )
        return text

    def counts(self) -> dict[str, int]:
        """The three standing counts, as every structured view gives them."""
        return {
            "unread": self.unread,
            "unanswered": self.unanswered,
            "own_notes": self.own_notes,
        }


def is_own_note(message: Message, owner: Address) -> bool:
    """Whether ``message`` in ``owner``'s mailbox is a note ``owner`` sent itself,
    such as a fired wake, a task-end note or a compaction request. Mail from an
    operator, another line, an agent, a remote party or the harness never is."""
    return message.sender == owner


def counts_unread(message: Message, owner: Address) -> bool:
    """Whether ``message`` in ``owner``'s mailbox is counted in ``unread``, the rule
    ``summary`` counts by and ``listing`` filters by with ``unread_only``."""
    return not message.read and not is_own_note(message, owner)


def counts_own_note(
    message_id: str, message: Message, owner: Address, replied: set[str]
) -> bool:
    """Whether ``message`` in ``owner``'s mailbox is counted in ``own_notes``, the
    rule ``summary`` counts by and ``listing`` filters by with ``own_notes_only``.
    An own note leaves the count once it is read, acknowledged or replied to."""
    return (
        is_own_note(message, owner)
        and not message.read
        and not is_answered(message_id, message, replied)
    )


def mailbox(state: State, owner: Address) -> list[tuple[str, Message]]:
    """Every message delivered to ``owner``, oldest first."""
    return [
        (message_id, message)
        for message_id, message in state.messages.items()
        if message.recipient == owner and message.delivered
    ]


def replied_ids(state: State, owner: Address) -> set[str]:
    """The messages ``owner`` has sent a reply to."""
    return {
        message.reply_to
        for message in state.messages.values()
        if message.sender == owner and message.reply_to is not None
    }


def is_answered(message_id: str, message: Message, replied: set[str]) -> bool:
    """Whether the recipient replied to the message or acknowledged it."""
    return message_id in replied or message.acknowledged


def summary(state: State, owner: Address, new: int) -> MailSummary:
    """The counts for ``owner``'s mailbox after ``new`` messages arrived, the one
    rule every view uses, the wake prompt, ``line_status``, ``list_mail`` and the
    client alike.

    Unanswered. A reply to a question Dromon asked on the owner's behalf when it held
    the owner, such as an operator's ``resume``, a note from the owner to itself and
    a note from the harness, which nothing can reply to, are not counted, since none
    asks a reply of the owner.

    Own notes. **Task of 2026-10-30, the rule decided by core for review.** A note
    the owner sent itself is never counted in ``unread``, and is counted in
    ``own_notes`` instead, so nothing is hidden. Lines acknowledge such notes without
    opening them, or ignore a wake that fired after its work was done, so counting
    them unread swelled the figure for good. Until this task only the wake prompt
    left them out, and only those shown to an earlier turn, so ``line_status`` and
    the prompt gave different figures for one mailbox.

    **Decided by core on 2026-10-30, for review.** A wake prompt that delivers an
    own note does not mark it read. The prompt shows its header, as it shows every
    message's, and never its body, so the note was not read, and ``read`` stays
    true to what the owner opened. Since ``unread`` no longer counts own notes,
    marking them read would change no figure, and would add a ``message_read``
    event to the log for each one.

    **Review r6, finding 6.** An own note leaves ``own_notes`` once the owner
    acknowledges it, or replies to it, as well as once it opens it. Lines already
    acknowledge fired wakes without opening them, so the count stays near the notes
    still waiting to be dealt with rather than growing with every fired wake. A line
    that never deals with its notes still sees them counted, since they were not
    dealt with."""
    held = mailbox(state, owner)
    replied = replied_ids(state, owner)
    return MailSummary(
        new=new,
        unread=sum(1 for _, message in held if counts_unread(message, owner)),
        unanswered=sum(
            1
            for message_id, message in held
            if not is_answered(message_id, message, replied)
            and message.reply_to not in state.hold_questions
            and not is_own_note(message, owner)
            and not isinstance(message.sender, HarnessAddress)
        ),
        own_notes=sum(
            1
            for message_id, message in held
            if counts_own_note(message_id, message, owner, replied)
        ),
    )


def listing(
    state: State,
    owner: Address,
    *,
    unread_only: bool = False,
    own_notes_only: bool = False,
    sender: Address | None = None,
    offset: int = 0,
    limit: int = MAX_PAGE,
) -> tuple[list[tuple[str, Message]], int]:
    """A page of ``owner``'s mailbox, oldest first, and the number of matches in all.

    **Review r6, finding 1.** ``unread_only`` selects exactly what ``summary`` counts
    in ``unread``, and ``own_notes_only`` exactly what it counts in ``own_notes``, so
    a listing and the counts beside it agree. The two select disjoint messages, so
    giving both is refused rather than answered with an empty page."""
    if offset < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"offset must be at least 0 and limit in [1, {MAX_PAGE}]")
    if unread_only and own_notes_only:
        raise ValueError(
            "unread_only and own_notes_only select different messages, so give one"
        )
    replied = replied_ids(state, owner) if own_notes_only else set[str]()
    matches = [
        (message_id, message)
        for message_id, message in mailbox(state, owner)
        if (not unread_only or counts_unread(message, owner))
        and (not own_notes_only or counts_own_note(message_id, message, owner, replied))
        and (sender is None or message.sender == sender)
    ]
    return matches[offset : offset + limit], len(matches)


def outcome(message_id: str, message: Message, replied: set[str]) -> str:
    """``replied``, ``acknowledged`` or ``unanswered``, from the recipient's side."""
    if message_id in replied:
        return "replied"
    if message.acknowledged:
        return "acknowledged"
    return "unanswered"


def header(message_id: str, message: Message, replied: set[str]) -> str:
    """One line describing a received message without its body."""
    parts = [f"{message_id} from {message.sender}", message.kind.value]
    if message.reply_to is not None:
        parts.append(f"replying to {message.reply_to}")
    parts.append("read" if message.read else "unread")
    answered = outcome(message_id, message, replied)
    if answered != "unanswered":
        parts.append(answered)
    return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class SentStatus:
    """A sent message's fate on the recipient's side."""

    message_id: str
    recipient: str
    kind: str
    delivered: bool
    read: bool
    outcome: str
    """``replied``, ``acknowledged`` or ``unanswered``."""
    delivery: str
    """``waiting``, ``delivered`` or ``read`` for local mail. Mail to a peer is
    ``waiting for peer``, ``delivered to peer``, ``returned`` with its code, or
    ``unconfirmed``, since this daemon never learns whether a remote party read it."""

    def line(self) -> str:
        return (
            f"{self.message_id} to {self.recipient}, {self.kind}, {self.delivery},"
            f" {self.outcome}"
        )


def delivery(message: Message) -> str:
    """How far ``message`` went, as ``SentStatus.delivery`` gives it."""
    if not isinstance(message.recipient, RemoteAddress):
        return (
            "read" if message.read else "delivered" if message.delivered else "waiting"
        )
    if message.returned is not None:
        return f"returned {message.returned.value}"
    if message.unconfirmed:
        return "unconfirmed"
    return "delivered to peer" if message.delivered else "waiting for peer"


def sent(
    state: State,
    owner: Address,
    *,
    unanswered_only: bool = False,
    offset: int = 0,
    limit: int = MAX_PAGE,
) -> tuple[list[SentStatus], int]:
    """A page of the mail ``owner`` has sent, oldest first, and the number in all."""
    if offset < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"offset must be at least 0 and limit in [1, {MAX_PAGE}]")
    statuses: list[SentStatus] = []
    for message_id, message in state.messages.items():
        if message.sender != owner:
            continue
        replied = replied_ids(state, message.recipient)
        status = SentStatus(
            message_id,
            str(message.recipient),
            message.kind.value,
            message.delivered,
            message.read,
            outcome(message_id, message, replied),
            delivery(message),
        )
        if not unanswered_only or status.outcome == "unanswered":
            statuses.append(status)
    return statuses[offset : offset + limit], len(statuses)

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

from dromon.model import (
    Address,
    HarnessAddress,
    LineAddress,
    Message,
    MessageKind,
    RemoteAddress,
    State,
)

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
    requests, that are neither read nor acknowledged and that no turn of the owner
    whose engine reported it has ended since their delivery, which ``unread`` leaves
    out and this counts apart. A note that left this count is in no count, and is
    still listed by ``list_mail`` without a filter."""

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
                f" {self.own_notes} {own} of your own, such as fired wakes, {verb}"
                " counted apart from unread until read, acknowledged, or shown to a"
                " turn that ends. Those past that are still listed by list_mail"
                " without a filter."
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


def shown_to_ended_turn(state: State, message: Message) -> bool:
    """Whether a turn of the line ``message`` was delivered to has ended since its
    delivery with a report from its engine, read from the event order alone, as
    ``State.reported_turn_ends`` and ``Message.reported_ends_at_delivery`` record it.
    Shown means delivered to the line, whether in a prompt, inside a running turn or
    for the record between turns, and never only in a prompt. Always false for mail
    to an operator, an agent or a peer, which has no turns."""
    if message.reported_ends_at_delivery is None or not isinstance(
        message.recipient, LineAddress
    ):
        return False
    ended = state.reported_turn_ends.get(message.recipient.line_id, 0)
    return ended > message.reported_ends_at_delivery


def counts_own_note(
    state: State, message_id: str, message: Message, owner: Address, replied: set[str]
) -> bool:
    """Whether ``message`` in ``owner``'s mailbox is counted in ``own_notes``, the
    rule ``summary`` counts by and ``listing`` filters by with ``own_notes_only``.
    An own note leaves the count once it is read, acknowledged or replied to, or
    once a turn of its line has ended with a report after it was delivered, as
    ``summary`` sets out."""
    return (
        is_own_note(message, owner)
        and not message.read
        and not is_answered(message_id, message, replied)
        and not shown_to_ended_turn(state, message)
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


def asks_reply(message: Message) -> bool:
    """Whether ``message`` asks its recipient for a reply. **Round 14 task of
    2026-11-01, decided by core for review.** A question and an issue always do. A
    note does when its sender said so with ``asks_reply``, and a note whose sender
    said nothing does unless it is itself a reply, since a reply answers something
    and an answer asks nothing by default. Decided over counting every note, which
    made the count grow with every report and every operator's reply, and over a
    default of asking nothing, which would hide an operator's task note that a
    client sent without the flag."""
    if message.kind is not MessageKind.NOTE:
        return True
    if message.asks_reply is not None:
        return message.asks_reply
    return message.reply_to is None


def counts_unanswered(state: State, message: Message) -> bool:
    """Whether ``message`` is counted in its recipient's ``unanswered``, the rule
    ``summary`` counts by and ``listing`` filters by with ``unanswered_only``.

    It reads the message and one set of the state, so the event log can keep the
    time of each unanswered message as events apply. **Round 14 task of
    2026-11-01.** A message leaves the count once its recipient replies to it or
    acknowledges it, as before, and also once its own sender corrects it with a
    later note naming it, since the correction is the one left to answer. A note
    that asks nothing is never counted. As before, a reply to a question Dromon
    asked when it held a line, a note from the owner to itself and a note from the
    harness are not counted."""
    return (
        not message.replied
        and not message.acknowledged
        and message.corrected_by is None
        and asks_reply(message)
        and message.reply_to not in state.hold_questions
        and not is_own_note(message, message.recipient)
        and not isinstance(message.sender, HarnessAddress)
    )


def is_open_issue(message_id: str, message: Message, replied: set[str]) -> bool:
    """Whether ``message`` is an issue its recipient has not yet closed by a reply
    or an acknowledgement."""
    return message.kind is MessageKind.ISSUE and not is_answered(
        message_id, message, replied
    )


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
    dealt with.

    **Round 8 task of 2026-10-30, decided by core for review.** That last sentence no
    longer holds. An own note also leaves ``own_notes`` once a turn of its line has
    ended after the note was delivered, whatever the turn's outcome, since the daemon
    records ``line_turn_ended`` for a failed or held turn too, so long as its engine
    returned a report, as ON1 below says. Lines left fired wakes
    and task notes alone once their work was done, so the count grew without bound
    and told a line nothing. A worker line in another project counted 62, and 89 of
    its 90 task notes were never read or acknowledged.

    **Round 8 review of 2026-10-30, decided by the operator.** Shown means delivered
    to the line, whether in its prompt, inside a running turn or for the record
    between turns. It does not mean that a prompt carried the note. A note retired
    unopened is in no count, and is still listed by ``list_mail`` without a filter.

    - **A turn with no report, ON1.** A turn whose engine returned no report retires
      nothing, since the daemon cannot say that its prompt reached the engine. Its
      prompt may not have been composed, a check before the engine may have failed,
      the engine may have raised, or the turn may have been cancelled. The daemon
      records ``line_turn_ended`` with ``unreported`` for it, in the ordinary turn
      and in a turn the engine started, and its notes count until a later turn of
      the line ends with a report. A cancelled or failed engine may have read the
      prompt, so the rule can keep a note counted one turn too long, and never
      retires one too early. A ``line_turn_ended`` written before the field counts
      as reported, since nothing in it says otherwise.
    - **The current prompt.** A note the current prompt delivered still counts, since
      its turn has not ended, so the turn shown it sees it in the count.
    - **A task note recorded during a running turn.** No prompt delivers it, and it
      counts until that running turn ends, then leaves with it. Decided so because
      the line's engine reported the task's end inside that turn, so it is that
      turn the end was shown to, and because a rule with no exception for it needs
      no record of which deliveries a prompt made. Never counting it would hide it
      from that turn's own ``line_status``.
    - **Delivery between turns without a wake.** The end of a task the line stopped
      is delivered for the record only, and so is a context note withdrawn after its
      compaction, which is reported between turns when the engine compacted outside
      a turn of Dromon's. Each counts until the next turn of its line ends, so that
      turn sees it in the count before it leaves. Decided over counting it until
      dealt with, which would bring back the unbounded count, since neither note
      asks anything. Delivered inside a turn, either leaves when that turn ends, as
      a task note does.
    - **A turn cut short by a restart** records no turn end. Its notes count until
      the next turn of the line ends after the restart.
    - **Prompts still mark nothing read.** The round 6 decision above stands. Since
      leaving the count no longer needs ``read``, marking a prompt's notes read would
      change no figure and would only add events to the log.
    - **Other mailboxes.** Mail from any other sender is unchanged. Mail to an
      operator, an agent or a peer keeps no turn count, so no line's turn retires
      it, and an operator cannot send itself a note, so its ``own_notes`` is 0.

    The rule reads only ``State.reported_turn_ends`` and each message's
    ``reported_ends_at_delivery``, which the event order rebuilds, so it holds
    across a restart and nothing of it is held in the scheduler's memory."""
    held = mailbox(state, owner)
    replied = replied_ids(state, owner)
    return MailSummary(
        new=new,
        unread=sum(1 for _, message in held if counts_unread(message, owner)),
        unanswered=sum(1 for _, message in held if counts_unanswered(state, message)),
        own_notes=sum(
            1
            for message_id, message in held
            if counts_own_note(state, message_id, message, owner, replied)
        ),
    )


def listing(
    state: State,
    owner: Address,
    *,
    unread_only: bool = False,
    own_notes_only: bool = False,
    unanswered_only: bool = False,
    sender: Address | None = None,
    issues_only: bool = False,
    offset: int = 0,
    limit: int = MAX_PAGE,
) -> tuple[list[tuple[str, Message]], int]:
    """A page of ``owner``'s mailbox, oldest first, and the number of matches in all.

    ``issues_only`` selects the open issues, those of kind ``issue`` that the owner
    has neither replied to nor acknowledged, which is how an operator lists the
    issues lines raised and sees one close.

    **Review r6, finding 1.** ``unread_only`` selects exactly what ``summary`` counts
    in ``unread``, and ``own_notes_only`` exactly what it counts in ``own_notes``, so
    a listing and the counts beside it agree. The two select disjoint messages, so
    giving both is refused rather than answered with an empty page. **Round 14 task
    of 2026-11-01.** ``unanswered_only`` selects exactly what it counts in
    ``unanswered``, which never holds an own note, so it is refused beside
    ``own_notes_only`` for the same reason."""
    if offset < 0 or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"offset must be at least 0 and limit in [1, {MAX_PAGE}]")
    if unread_only and own_notes_only:
        raise ValueError(
            "unread_only and own_notes_only select different messages, so give one"
        )
    if unanswered_only and own_notes_only:
        raise ValueError(
            "unanswered_only and own_notes_only select different messages, so give one"
        )
    replied = replied_ids(state, owner) if own_notes_only or issues_only else set[str]()
    matches = [
        (message_id, message)
        for message_id, message in mailbox(state, owner)
        if (not unread_only or counts_unread(message, owner))
        and (
            not own_notes_only
            or counts_own_note(state, message_id, message, owner, replied)
        )
        and (not unanswered_only or counts_unanswered(state, message))
        and (sender is None or message.sender == sender)
        and (not issues_only or is_open_issue(message_id, message, replied))
    ]
    return matches[offset : offset + limit], len(matches)


def outcome(message_id: str, message: Message, replied: set[str]) -> str:
    """``replied``, ``acknowledged``, ``corrected`` or ``unanswered``, from the
    recipient's side. ``corrected`` means the sender named it with a later note
    before the recipient answered."""
    if message_id in replied:
        return "replied"
    if message.acknowledged:
        return "acknowledged"
    if message.corrected_by is not None:
        return "corrected"
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
    if message.corrected_by is not None and answered != "corrected":
        parts.append(f"corrected by {message.corrected_by}")
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
    """``replied``, ``acknowledged``, ``corrected`` or ``unanswered``."""
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

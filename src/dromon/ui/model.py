"""The client's state, and the events and effects that pass between it and a runtime.

Everything here is immutable data. ``dromon.ui.update`` computes the next model, and
``dromon.ui.render`` draws one.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from dromon.ui.commands import DaemonCommand, Pane, Suggestion
from dromon.ui.data import ActivityPage, Entry, LineSummary, Settings, View
from dromon.ui.editor import Editor, History
from dromon.ui.keys import Key

MAX_ENTRIES: Final = 1024
"""Activity entries a client keeps for its line, the oldest dropped first."""
QUIT_WINDOW_SECONDS: Final = 2.0
"""How long a first Control-C waits for a second one to detach."""
RUNNING_POLL_DIVISOR: Final = 4
"""How much faster a client polls while its line runs a turn, to follow it closely."""
MIN_POLL_SECONDS: Final = 0.1
LOST_POLLS: Final = 5
"""How many polls in a row may find no daemon before a client detaches by itself."""
LOST_SECONDS: Final = 10.0
"""How long a client goes on asking a daemon that is gone before it detaches. Both
bounds must be passed, so a short poll interval does not detach it in a moment, and a
long one still retries ``LOST_POLLS`` times. **Round 9, item 6.** A client whose
daemon was gone polled for days after its project was deleted. It applies to polls
that found the socket file missing."""
REFUSED_SECONDS: Final = 60.0
"""How long refused connections must go on before a client detaches, when its socket
file is still there. **Round 9 review, F14, decided by the operator.** On Darwin a
connection to a Unix socket whose listen backlog is full is refused, so a live daemon
whose event loop is busy can refuse several clients at once, while a daemon that
crashed leaves its socket file behind and refuses every connection. A minute is long
past any such stall, and still ends a client whose daemon crashed."""


@dataclass(frozen=True, slots=True)
class ProjectRow:
    """A project of the fleet index as ``/projects`` lists it. Round 13, item 4."""

    name: str
    state: str
    """Its entry's liveness, ``live``, ``stale`` or ``unknown``."""
    root: str
    """Its root, which tells apart two projects that share a name. **Review of
    7ff033f, G4.**"""


class Overlay(StrEnum):
    NONE = "none"
    HELP = "help"
    LINES = "lines"
    SETTINGS = "settings"


@dataclass(frozen=True, slots=True)
class Model:
    line_id: str
    operator: str
    poll_seconds: float
    width: int = 80
    height: int = 24
    view: View | None = None
    activity: tuple[Entry, ...] = ()
    """The line's activity entries, ordered by ``entry_id``."""
    activity_seq: int = 0
    """The highest revision fetched, which the next fetch asks after."""
    activity_gap: bool = False
    """Whether entries older than those held were left out."""
    lines: tuple[LineSummary, ...] = ()
    editor: Editor = field(default_factory=Editor)
    history: History = field(default_factory=History)
    suggestions: tuple[Suggestion, ...] = ()
    selected: int = 0
    """The highlighted suggestion."""
    pane: Pane = Pane.ACTIVITY
    overlay: Overlay = Overlay.NONE
    picked: int = 0
    """The highlighted line in the line list."""
    settings: Settings | None = None
    """The line's and the project's settings, fetched on attach and after changes."""
    setting_row: int = 0
    """The highlighted row of the settings panel, over the line's rows and then the
    project's."""
    scroll: int = 0
    """Transcript rows hidden below the bottom of the view."""
    notice: str = ""
    alarm: bool = False
    """Whether the notice reports a failure."""
    now: float = 0.0
    polled_at: float | None = None
    quit_armed_at: float | None = None
    known_questions: frozenset[str] | None = None
    """Questions already seen, or ``None`` before the first view, so that only a
    question that arrives while attached rings the bell."""
    finished: bool = False
    lost_since: float | None = None
    """When the polls in a row that found no daemon began, or ``None``."""
    missing_since: float | None = None
    """When the polls in a row that found the socket file missing began, or ``None``,
    which a refused connection resets."""
    lost_polls: int = 0
    """How many polls in a row found no daemon."""
    lost_poll_at: float | None = None
    """The poll last counted as lost, so the several fetches of one poll count once."""
    exit_message: str | None = None
    """Why the client detached by itself, or ``None``."""
    project: str | None = None
    """The attached project's name, or ``None`` when the client was given none."""
    projects: tuple[ProjectRow, ...] = ()
    """The projects of the fleet index from the last ``/projects``, which
    ``/project`` completes from."""

    def __post_init__(self) -> None:
        if not self.line_id:
            raise ValueError("a client attaches to a line")
        if not self.poll_seconds > 0:
            raise ValueError("poll_seconds must be positive")

    @property
    def running(self) -> bool:
        return self.view is not None and self.view.line.running

    @property
    def poll_interval(self) -> float:
        if self.running:
            return max(self.poll_seconds / RUNNING_POLL_DIVISOR, MIN_POLL_SECONDS)
        return self.poll_seconds


# Events, which a runtime feeds to ``update``.


@dataclass(frozen=True, slots=True)
class Tick:
    now: float


@dataclass(frozen=True, slots=True)
class Resized:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Pressed:
    key: Key


@dataclass(frozen=True, slots=True)
class Wheel:
    rows: int
    """Rows to scroll, back toward older rows when positive."""


@dataclass(frozen=True, slots=True)
class ViewLoaded:
    line_id: str
    view: View


@dataclass(frozen=True, slots=True)
class ActivityLoaded:
    line_id: str
    page: ActivityPage


@dataclass(frozen=True, slots=True)
class SettingsLoaded:
    line_id: str
    settings: Settings


@dataclass(frozen=True, slots=True)
class LinesLoaded:
    lines: tuple[LineSummary, ...]


@dataclass(frozen=True, slots=True)
class ProjectOpened:
    """The runtime now speaks to ``project``'s daemon, which has ``lines``."""

    project: str
    line_id: str
    lines: tuple[LineSummary, ...]


@dataclass(frozen=True, slots=True)
class ProjectsLoaded:
    projects: tuple[ProjectRow, ...]


@dataclass(frozen=True, slots=True)
class Done:
    text: str


@dataclass(frozen=True, slots=True)
class Failed:
    text: str


@dataclass(frozen=True, slots=True)
class Lost:
    """A request found nothing accepting connections on the daemon's socket.
    ``missing`` says the socket file was absent, and otherwise the connection was
    refused."""

    text: str
    missing: bool = False


type Event = (
    Tick
    | Resized
    | Pressed
    | Wheel
    | ViewLoaded
    | ActivityLoaded
    | LinesLoaded
    | SettingsLoaded
    | ProjectOpened
    | ProjectsLoaded
    | Done
    | Failed
    | Lost
)


# Effects, which ``update`` asks a runtime to perform.


@dataclass(frozen=True, slots=True)
class FetchView:
    line_id: str


@dataclass(frozen=True, slots=True)
class FetchActivity:
    line_id: str
    after: int


@dataclass(frozen=True, slots=True)
class FetchLines:
    pass


@dataclass(frozen=True, slots=True)
class FetchSettings:
    line_id: str


@dataclass(frozen=True, slots=True)
class Request:
    command: DaemonCommand
    line_id: str
    operator: str


@dataclass(frozen=True, slots=True)
class OpenProject:
    """Speak to another project's daemon from now on, once it answers."""

    project: str
    line_id: str | None
    current: str
    """The attached line, kept when the other project has a line of that name."""


@dataclass(frozen=True, slots=True)
class FetchProjects:
    pass


@dataclass(frozen=True, slots=True)
class Bell:
    pass


@dataclass(frozen=True, slots=True)
class Exit:
    pass


type Effect = (
    FetchView
    | FetchActivity
    | FetchLines
    | FetchSettings
    | Request
    | OpenProject
    | FetchProjects
    | Bell
    | Exit
)

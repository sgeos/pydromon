"""The client core: keys, the editor, commands, the daemon's results, update and render.

Everything here is pure, so no test needs a terminal, a window or a daemon.
"""

from dataclasses import replace

import pytest

from dromon.activity import ActivityKind
from dromon.ui import editor as edit
from dromon.ui.commands import (
    MAX_SUGGESTIONS,
    SPECS,
    Acknowledge,
    Answer,
    Choices,
    Help,
    Interrupt,
    Invalid,
    Pane,
    Quit,
    Release,
    Say,
    ShowLines,
    ShowPane,
    Switch,
    parse_input,
    suggest,
)
from dromon.ui.data import (
    ActivityPage,
    Entry,
    LineSummary,
    View,
    activity_of,
    lines_of,
    view_of,
)
from dromon.ui.keys import Char, Named, from_text
from dromon.ui.model import (
    MAX_ENTRIES,
    QUIT_WINDOW_SECONDS,
    ActivityLoaded,
    Bell,
    Done,
    Effect,
    Event,
    Exit,
    Failed,
    FetchActivity,
    FetchLines,
    FetchSettings,
    FetchView,
    LinesLoaded,
    Model,
    Overlay,
    Pressed,
    Request,
    Resized,
    Tick,
    ViewLoaded,
    Wheel,
)
from dromon.ui.render import (
    MAX_INPUT_ROWS,
    Style,
    clean,
    frame,
    scroll_limit,
    text_of,
    transcript,
    wrap,
)
from dromon.ui.update import initial, update

# Keys.


def test_control_characters_become_named_keys() -> None:
    assert from_text("\x01") is Named.CTRL_A
    assert from_text("\x1b") is Named.ESCAPE
    assert from_text("\r") is Named.ENTER
    assert from_text("\n") is Named.NEWLINE
    assert from_text("a") == Char("a")
    assert from_text("\x00") is None
    assert from_text("ab") is None


def test_a_char_is_one_printable_character() -> None:
    with pytest.raises(ValueError, match="printable"):
        Char("\x07")
    with pytest.raises(ValueError, match="printable"):
        Char("ab")


# The editor.


def typed(text: str) -> edit.Editor:
    return edit.insert(edit.Editor(), text)


def test_insertion_and_deletion_follow_the_cursor() -> None:
    editor = edit.left(edit.left(typed("hello")))
    assert edit.insert(editor, "X") == edit.Editor("helXlo", 4)
    assert edit.backspace(editor) == edit.Editor("helo", 2)
    assert edit.delete(editor) == edit.Editor("helo", 3)
    assert edit.backspace(edit.Editor("ab", 0)) == edit.Editor("ab", 0)
    assert edit.delete(typed("ab")) == typed("ab")


def test_the_cursor_stays_inside_the_text() -> None:
    assert edit.left(edit.Editor("a", 0)).cursor == 0
    assert edit.right(typed("a")).cursor == 1
    with pytest.raises(ValueError, match="outside"):
        edit.Editor("a", 2)


def test_insertion_never_grows_past_the_limit() -> None:
    full = typed("x" * edit.MAX_INPUT)
    assert edit.insert(full, "more") == full
    assert len(typed("y" * (edit.MAX_INPUT + 5)).text) == edit.MAX_INPUT


def test_row_keys_act_on_the_cursor_row() -> None:
    editor = edit.Editor("one\ntwo three", 8)
    assert edit.home(editor).cursor == 4
    assert edit.end(editor).cursor == len(editor.text)
    assert edit.cut_before(editor) == edit.Editor("one\nthree", 4)
    assert edit.cut_after(editor) == edit.Editor("one\ntwo ", 8)


def test_control_w_cuts_the_word_and_the_spaces_before_the_cursor() -> None:
    assert edit.cut_word(typed("send it  ")) == edit.Editor("send ", 5)
    assert edit.cut_word(typed("word")) == edit.Editor("", 0)
    assert edit.cut_word(edit.Editor("", 0)) == edit.Editor("", 0)


def test_vertical_moves_keep_the_column_where_they_can() -> None:
    editor = edit.Editor("abcdef\nxy\nlonger", 5)
    down = edit.vertical(editor, 1)
    assert down is not None and down.cursor == len("abcdef\nxy")
    assert edit.vertical(editor, -1) is None
    assert edit.vertical(edit.Editor("abc\nd", 5), 1) is None
    assert edit.row_of(edit.Editor("ab\ncd", 4)) == (1, 1)


def test_history_walks_back_and_returns_to_the_draft() -> None:
    history = edit.remember(edit.remember(edit.History(), "first"), "second")
    assert edit.remember(history, "second").entries == ("first", "second")
    assert edit.remember(history, "   ").entries == ("first", "second")
    step = edit.older(history, "draft")
    assert step is not None and step[1] == "second"
    step = edit.older(step[0], "ignored")
    assert step is not None and step[1] == "first"
    assert edit.older(step[0], "") is None
    forward = edit.newer(step[0])
    assert forward is not None and forward[1] == "second"
    back_to_draft = edit.newer(forward[0])
    assert back_to_draft is not None and back_to_draft[1] == "draft"
    assert back_to_draft[0].index is None
    assert edit.newer(back_to_draft[0]) is None
    assert edit.older(edit.History(), "x") is None


def test_history_is_bounded() -> None:
    history = edit.History()
    for index in range(edit.MAX_HISTORY + 10):
        history = edit.remember(history, str(index))
    assert len(history.entries) == edit.MAX_HISTORY
    assert history.entries[-1] == str(edit.MAX_HISTORY + 9)


# Commands.


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("hello there", Say("hello there")),
        ("  padded  ", Say("padded")),
        ("two\nrows", Say("two\nrows")),
        ("/answer q-1 yes, go", Answer("q-1", "yes, go")),
        ("/ack m-2", Acknowledge("m-2")),
        ("/release", Release()),
        ("/interrupt", Interrupt()),
        ("/line beta", Switch("beta")),
        ("/lines", ShowLines()),
        ("/mail", ShowPane(Pane.MAIL)),
        ("/activity", ShowPane(Pane.ACTIVITY)),
        ("/help", Help()),
        ("/?", Help()),
        ("/quit", Quit()),
        ("/exit", Quit()),
        ("/answer q-1", Invalid("usage: /answer QUESTION TEXT")),
        ("/ack", Invalid("usage: /ack MESSAGE")),
        ("/ack a b", Invalid("usage: /ack MESSAGE")),
        ("/line", Invalid("usage: /line LINE")),
        ("/release now", Invalid("usage: /release")),
        ("/mail please", Invalid("usage: /mail")),
    ],
)
def test_input_parses_into_commands(text: str, command: object) -> None:
    assert parse_input(text) == command


def test_empty_input_is_no_command_and_unknown_ones_point_to_help() -> None:
    assert parse_input("   ") is None
    parsed = parse_input("/frobnicate")
    assert isinstance(parsed, Invalid) and "/help" in parsed.reason


def test_every_command_in_the_table_parses() -> None:
    for spec in SPECS:
        assert parse_input(spec.usage.replace("QUESTION TEXT", "q-1 yes")) is not None
        sample = spec.usage.replace("QUESTION TEXT", "q-1 yes")
        sample = sample.replace("MESSAGE", "m-1").replace("LINE", "alpha")
        assert not isinstance(parse_input(sample), Invalid), spec.name


CHOICES = Choices(
    lines=(("alpha", "idle"), ("beta", "running")),
    questions=(("q-1", "Resume?"),),
    values=(("/answer", "q-1", ("yes", "no", "yes, later")),),
    messages=(("m-1", "a note"),),
)


def test_command_names_complete_from_a_slash() -> None:
    names = [s.label for s in suggest("/", CHOICES)]
    assert len(names) == min(len(SPECS), MAX_SUGGESTIONS)
    assert [s.text for s in suggest("/rele", CHOICES)] == ["/release"]
    assert [s.text for s in suggest("/ans", CHOICES)] == ["/answer "]
    assert suggest("hello", CHOICES) == ()
    assert suggest("/release\nmore", CHOICES) == ()


def test_arguments_complete_from_what_was_fetched() -> None:
    assert [s.text for s in suggest("/line b", CHOICES)] == ["/line beta"]
    assert [s.text for s in suggest("/answer ", CHOICES)] == ["/answer q-1 "]
    assert [s.text for s in suggest("/ack m", CHOICES)] == ["/ack m-1"]
    assert [s.label for s in suggest("/answer q-1 yes", CHOICES)] == [
        "yes",
        "yes, later",
    ]
    assert suggest("/answer q-404 y", CHOICES) == ()
    assert suggest("/release x", CHOICES) == ()
    assert suggest("/unknown x", CHOICES) == ()
    assert suggest("/line beta extra", CHOICES) == ()


# The daemon's results.

LINE = {
    "line_id": "alpha",
    "engine": "claude-haiku-5-5",
    "strategy": "subscription",
    "state": "idle",
    "turns": 3,
    "pending": 1,
    "cost_estimate_usd": "0.0123",
    "held_by": None,
}

VIEW = {
    "line": LINE,
    "tick": 42,
    "total": 3,
    "messages": [
        {
            "message_id": "m-1",
            "sender": "@ana",
            "recipient": "alpha",
            "kind": "note",
            "reply_to": None,
            "status": None,
            "read": True,
            "body": "first line\nsecond line",
            "truncated": False,
        },
        {
            "message_id": "q-9",
            "sender": "alpha",
            "recipient": "@ana",
            "kind": "question",
            "reply_to": None,
            "status": "open",
            "body": "x" * 30,
            "truncated": True,
        },
    ],
    "open_questions": [
        {
            "question_id": "q-9",
            "operator": "@ana",
            "text": "Resume alpha?",
            "choices": ["yes", "no", 7],
        }
    ],
}


def test_a_view_reads_into_typed_values() -> None:
    view = view_of(VIEW)
    assert view.line.line_id == "alpha" and view.line.cost == "0.0123"
    assert view.line.held_by is None and not view.line.running
    assert [mail.message_id for mail in view.mail] == ["m-1", "q-9"]
    assert view.mail[1].truncated and not view.mail[1].read
    assert view.questions[0].choices == ("yes", "no")


def test_malformed_results_read_as_neutral_values() -> None:
    view = view_of({"line": "nonsense", "messages": [3, {"body": 4}], "tick": "x"})
    assert view.line.line_id == "" and view.tick == 0
    assert view.mail[0].body == "" and view.questions == ()
    assert lines_of({"lines": "no"}) == ()
    page = activity_of(
        {
            "seq": 4,
            "entries": [
                {"entry_id": 1, "seq": 2, "kind": "text", "text": "hi"},
                {"entry_id": 2, "seq": 3, "kind": "future", "text": "?"},
                {"entry_id": 3, "seq": 4, "kind": "tool", "text": None},
            ],
        }
    )
    assert page == ActivityPage(4, (Entry(1, 2, ActivityKind.TEXT, "hi"),), True)
    assert activity_of({"complete": False}).complete is False


# Update and render.


def attached(**changes: object) -> Model:
    model = replace(initial("alpha", "ana", 1.0), width=60, height=16)
    return replace(model, **changes)  # pyright: ignore[reportArgumentType]


def run(model: Model, *events: Event) -> tuple[Model, list[Effect]]:
    effects: list[Effect] = []
    for event in events:
        model, produced = update(model, event)
        effects.extend(produced)
    return model, effects


def keys(text: str) -> list[Event]:
    return [Pressed(Char(character)) for character in text]


def entry(entry_id: int, kind: ActivityKind, text: str) -> Entry:
    return Entry(entry_id, entry_id, kind, text)


def running_view() -> View:
    view = view_of(VIEW)
    return replace(view, line=replace(view.line, state="running"))


POLL = [
    FetchView("alpha"),
    FetchActivity("alpha", 0),
    FetchLines(),
    FetchSettings("alpha"),
]


def test_the_first_tick_polls_and_later_ones_wait_for_the_interval() -> None:
    model, effects = run(attached(), Tick(10.0))
    assert effects == POLL
    model, effects = run(model, Tick(10.5))
    assert effects == []
    model, effects = run(model, Tick(11.0))
    assert effects == POLL


def test_a_running_line_is_polled_faster() -> None:
    model = attached(view=running_view())
    assert model.poll_interval == pytest.approx(0.25)
    assert attached(poll_seconds=0.2, view=running_view()).poll_interval == 0.1


def test_the_model_checks_its_own_invariants() -> None:
    with pytest.raises(ValueError, match="attaches"):
        Model("", "ana", 1.0)
    with pytest.raises(ValueError, match="positive"):
        Model("alpha", "ana", 0.0)


def test_typing_and_enter_send_a_message_and_remember_it() -> None:
    model, effects = run(attached(), *keys("hi!"), Pressed(Named.BACKSPACE))
    assert model.editor.text == "hi" and effects == []
    model, effects = run(model, Pressed(Named.ENTER))
    assert effects == [Request(Say("hi"), "alpha", "ana")]
    assert model.editor.text == "" and model.history.entries == ("hi",)
    model, effects = run(model, Done("sent to alpha"))
    assert model.notice == "sent to alpha" and not model.alarm
    assert effects == POLL


def test_enter_on_empty_input_does_nothing() -> None:
    model = attached()
    assert run(model, Pressed(Named.ENTER)) == (model, [])


def test_a_trailing_backslash_or_a_newline_key_breaks_the_row() -> None:
    model, effects = run(attached(), *keys("one\\"), Pressed(Named.ENTER))
    assert model.editor.text == "one\n" and effects == []
    model, _ = run(model, *keys("two"), Pressed(Named.NEWLINE), *keys("3"))
    assert model.editor.text == "one\ntwo\n3"
    _, effects = run(model, Pressed(Named.ENTER))
    assert effects == [Request(Say("one\ntwo\n3"), "alpha", "ana")]


def test_up_and_down_walk_the_history_on_a_single_row() -> None:
    model, _ = run(attached(), *keys("first"), Pressed(Named.ENTER))
    model, _ = run(model, *keys("draft"), Pressed(Named.UP))
    assert model.editor.text == "first"
    model, _ = run(model, Pressed(Named.DOWN))
    assert model.editor.text == "draft"
    model, _ = run(model, Pressed(Named.DOWN))
    assert model.editor.text == "draft"


def test_up_and_down_move_between_rows_of_a_multi_row_input() -> None:
    model, _ = run(attached(), *keys("ab"), Pressed(Named.NEWLINE), *keys("cd"))
    model, _ = run(model, Pressed(Named.UP))
    assert model.editor.cursor == 2 and model.editor.text == "ab\ncd"


def test_editing_keys_reach_the_editor() -> None:
    model, _ = run(attached(), *keys("one two"), Pressed(Named.CTRL_W))
    assert model.editor.text == "one "
    model, _ = run(model, Pressed(Named.CTRL_A), *keys(">"), Pressed(Named.CTRL_E))
    assert model.editor == edit.Editor(">one ", 5)
    model, _ = run(model, Pressed(Named.CTRL_U))
    assert model.editor.text == ""


def test_a_slash_shows_suggestions_that_tab_and_enter_take() -> None:
    model, _ = run(attached(), *keys("/rele"))
    assert [s.text for s in model.suggestions] == ["/release"]
    _, effects = run(model, Pressed(Named.ENTER))
    assert effects == [Request(Release(), "alpha", "ana")]
    model, _ = run(attached(), *keys("/"), Pressed(Named.TAB))
    assert model.selected == 1
    model, _ = run(model, Pressed(Named.BACK_TAB), Pressed(Named.BACK_TAB))
    assert model.selected == len(model.suggestions) - 1
    model, _ = run(model, Pressed(Named.ESCAPE))
    assert model.suggestions == () and model.editor.text == "/"


def test_taking_a_suggestion_that_needs_an_argument_keeps_editing() -> None:
    view = view_of(VIEW)
    model, effects = run(attached(view=view), *keys("/ans"), Pressed(Named.ENTER))
    assert model.editor.text == "/answer " and effects == []
    assert [s.text for s in model.suggestions] == ["/answer q-9 "]
    model, _ = run(model, Pressed(Named.ENTER), *keys("y"))
    assert [s.label for s in model.suggestions] == ["yes"]
    _, effects = run(model, Pressed(Named.ENTER))
    assert effects == [Request(Answer("q-9", "yes"), "alpha", "ana")]


def test_tab_completes_a_single_match_at_once() -> None:
    model, _ = run(attached(), *keys("/inter"), Pressed(Named.ESCAPE))
    model, _ = run(model, Pressed(Named.TAB))
    assert model.editor.text == "/interrupt"


def test_a_question_mark_on_empty_input_opens_help_and_escape_closes_it() -> None:
    model, _ = run(attached(), *keys("?"))
    assert model.overlay is Overlay.HELP and model.editor.text == ""
    model, _ = run(model, *keys("x"))
    assert model.overlay is Overlay.HELP
    model, _ = run(model, Pressed(Named.ESCAPE))
    assert model.overlay is Overlay.NONE
    model, _ = run(model, *keys("a?"))
    assert model.editor.text == "a?"


def test_escape_interrupts_only_a_running_line() -> None:
    assert run(attached(), Pressed(Named.ESCAPE))[1] == []
    _, effects = run(attached(view=running_view()), Pressed(Named.ESCAPE))
    assert effects == [Request(Interrupt(), "alpha", "ana")]


def test_control_c_clears_then_needs_a_second_press_to_detach() -> None:
    model, effects = run(attached(), *keys("draft"), Pressed(Named.CTRL_C))
    assert model.editor.text == "" and effects == []
    model, effects = run(model, Pressed(Named.CTRL_C))
    assert "again" in model.notice and effects == [] and not model.finished
    model, effects = run(model, Pressed(Named.CTRL_C))
    assert model.finished and effects == [Exit()]


def test_a_first_control_c_expires() -> None:
    model, _ = run(attached(), Tick(1.0), Pressed(Named.CTRL_C))
    model, _ = run(model, Tick(1.0 + QUIT_WINDOW_SECONDS + 0.1))
    assert model.quit_armed_at is None
    model, effects = run(model, Pressed(Named.CTRL_C))
    assert not model.finished and effects == []


def test_control_d_detaches_only_from_empty_input() -> None:
    model, effects = run(attached(), *keys("ab"), Pressed(Named.LEFT))
    model, effects = run(model, Pressed(Named.CTRL_D))
    assert model.editor.text == "a" and effects == []
    _, effects = run(attached(), Pressed(Named.CTRL_D))
    assert effects == [Exit()]


def test_quit_finishes_and_invalid_input_raises_an_alarm() -> None:
    model, effects = run(attached(), *keys("/quit"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert model.finished and effects == [Exit()]
    model, _ = run(attached(), *keys("/line"), Pressed(Named.ESCAPE))
    model, _ = run(model, Pressed(Named.ENTER))
    assert model.alarm and model.notice == "usage: /line LINE"


LINES = (
    LineSummary("alpha", "m", "subscription", "idle", 0, 0, "", None),
    LineSummary("beta", "m", "subscription", "idle", 0, 0, "", None),
)


def test_switching_lines_resets_the_line_state_and_polls() -> None:
    before = attached(
        lines=LINES,
        view=view_of(VIEW),
        activity=(entry(1, ActivityKind.TEXT, "old"),),
        activity_seq=1,
        scroll=2,
        pane=Pane.MAIL,
    )
    model, effects = run(before, *keys("/line beta"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert model.line_id == "beta" and model.view is None and model.activity == ()
    assert model.pane is Pane.MAIL and model.lines == LINES and model.scroll == 0
    assert effects == [
        FetchView("beta"),
        FetchActivity("beta", 0),
        FetchLines(),
        FetchSettings("beta"),
    ]


def test_switching_to_an_unknown_or_the_same_line_stays_put() -> None:
    model, _ = run(attached(lines=LINES), *keys("/line gamma"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert model.line_id == "alpha" and model.alarm and effects == []
    model, _ = run(attached(), *keys("/line alpha"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert model.line_id == "alpha" and "already" in model.notice


def test_control_n_and_p_cycle_through_the_lines() -> None:
    model, _ = run(attached(lines=LINES), Pressed(Named.CTRL_N))
    assert model.line_id == "beta"
    model, _ = run(model, Pressed(Named.CTRL_N))
    assert model.line_id == "alpha"
    model, _ = run(model, Pressed(Named.CTRL_P))
    assert model.line_id == "beta"
    model, effects = run(attached(), Pressed(Named.CTRL_N))
    assert model.line_id == "alpha" and effects == []


def test_the_line_list_picks_a_line() -> None:
    model, _ = run(attached(), *keys("/lines"), Pressed(Named.ESCAPE))
    model, effects = run(model, Pressed(Named.ENTER))
    assert model.overlay is Overlay.LINES and effects == [FetchLines()]
    model, _ = run(model, LinesLoaded(LINES), Pressed(Named.DOWN), Pressed(Named.DOWN))
    assert model.picked == 1
    model, _ = run(model, Pressed(Named.UP), Pressed(Named.DOWN), Pressed(Named.ENTER))
    assert model.line_id == "beta" and model.overlay is Overlay.NONE
    model, _ = run(attached(overlay=Overlay.LINES), Pressed(Named.ENTER))
    assert model.overlay is Overlay.NONE and model.line_id == "alpha"


def test_control_o_and_the_pane_commands_switch_panes() -> None:
    model, _ = run(attached(), Pressed(Named.CTRL_O))
    assert model.pane is Pane.MAIL
    model, _ = run(model, Pressed(Named.CTRL_O))
    assert model.pane is Pane.ACTIVITY


def test_results_for_another_line_are_dropped() -> None:
    model = attached()
    page = ActivityPage(3, (entry(1, ActivityKind.TEXT, "x"),), True)
    assert run(model, ViewLoaded("beta", view_of(VIEW))) == (model, [])
    assert run(model, ActivityLoaded("beta", page)) == (model, [])


def test_activity_merges_by_entry_and_keeps_the_newest_revision() -> None:
    first = ActivityPage(
        2,
        (entry(1, ActivityKind.TURN_STARTED, "p"), entry(2, ActivityKind.TEXT, "he")),
        True,
    )
    later = ActivityPage(3, (Entry(2, 3, ActivityKind.TEXT, "hello"),), True)
    model, _ = run(
        attached(), ActivityLoaded("alpha", first), ActivityLoaded("alpha", later)
    )
    assert [e.text for e in model.activity] == ["p", "hello"]
    assert model.activity_seq == 3 and not model.activity_gap
    _, effects = run(model, Tick(0.0))
    assert FetchActivity("alpha", 3) in effects


def test_an_incomplete_page_or_an_overflow_marks_a_gap() -> None:
    page = ActivityPage(1, (entry(1, ActivityKind.TEXT, "x"),), False)
    assert run(attached(), ActivityLoaded("alpha", page))[0].activity_gap
    many = tuple(entry(i, ActivityKind.TOOL, "t") for i in range(1, MAX_ENTRIES + 3))
    model, _ = run(attached(), ActivityLoaded("alpha", ActivityPage(9999, many, True)))
    assert len(model.activity) == MAX_ENTRIES and model.activity_gap
    assert model.activity[0].entry_id == 3


def test_a_restarted_daemon_resets_the_activity() -> None:
    model = attached(activity=(entry(1, ActivityKind.TEXT, "x"),), activity_seq=50)
    model, effects = run(model, ActivityLoaded("alpha", ActivityPage(2, (), True)))
    assert model.activity == () and model.activity_seq == 0
    assert effects == [FetchActivity("alpha", 0)]


def test_a_question_that_arrives_while_attached_rings_once() -> None:
    quiet = replace(view_of(VIEW), questions=())
    model, effects = run(attached(), ViewLoaded("alpha", view_of(VIEW)))
    assert effects == [] and model.known_questions == frozenset({"q-9"})
    model, effects = run(attached(), ViewLoaded("alpha", quiet))
    assert effects == []
    model, effects = run(model, ViewLoaded("alpha", view_of(VIEW)))
    assert effects == [Bell()]
    model, effects = run(model, ViewLoaded("alpha", view_of(VIEW)))
    assert effects == []


def test_failures_raise_an_alarm_until_a_success() -> None:
    model, _ = run(attached(), Failed("the daemon is not running"))
    assert model.alarm and model.notice == "the daemon is not running"
    model, _ = run(model, Done("ok"))
    assert not model.alarm


def tall_activity(rows: int) -> tuple[Entry, ...]:
    return (entry(1, ActivityKind.TEXT, "\n".join(f"row {i}" for i in range(rows))),)


def test_page_keys_and_the_wheel_scroll_within_the_transcript() -> None:
    model = attached(activity=tall_activity(100))
    limit = scroll_limit(model)
    assert limit > 0
    model, _ = run(model, Pressed(Named.PAGE_UP))
    assert 0 < model.scroll <= limit
    model, _ = run(model, *[Pressed(Named.PAGE_UP)] * 50)
    assert model.scroll == limit
    model, _ = run(model, Wheel(-3))
    assert model.scroll == limit - 3
    model, _ = run(model, *[Pressed(Named.PAGE_DOWN)] * 50)
    assert model.scroll == 0
    assert run(attached(), Pressed(Named.PAGE_UP))[0].scroll == 0


def test_new_rows_keep_a_scrolled_reader_in_place() -> None:
    model, _ = run(attached(activity=tall_activity(100)), Pressed(Named.PAGE_UP))
    before = model.scroll
    page = ActivityPage(9, (entry(2, ActivityKind.TOOL, "Bash"),), True)
    model, _ = run(model, ActivityLoaded("alpha", page))
    assert model.scroll == before + 1


def test_resizing_clamps_the_scroll() -> None:
    model, _ = run(attached(activity=tall_activity(30)), *[Pressed(Named.PAGE_UP)] * 5)
    model, _ = run(model, Resized(60, 200))
    assert model.scroll == 0 and model.height == 200
    model, _ = run(model, Resized(0, 0))
    assert (model.width, model.height) == (1, 1)


# Render.


def check_invariants(model: Model) -> None:
    drawn = frame(model)
    assert len(drawn.rows) == model.height
    assert all(len(text_of(row)) <= model.width for row in drawn.rows)
    if drawn.cursor is not None:
        row, column = drawn.cursor
        assert 0 <= row < model.height and 0 <= column < model.width


def busy_model(width: int, height: int) -> Model:
    activity = (
        entry(1, ActivityKind.TURN_STARTED, "prompt\n" * 20),
        entry(2, ActivityKind.THINKING, ""),
        entry(3, ActivityKind.TEXT, "# Plan\n```\ncode\n```\n" + "word " * 80),
        entry(4, ActivityKind.TOOL, "mcp__dromon__send"),
        entry(5, ActivityKind.NOTICE, "the engine's start timed out, so once more"),
        entry(6, ActivityKind.TURN_ENDED, "failed: broke"),
    )
    return attached(
        width=width,
        height=height,
        view=running_view(),
        activity=activity,
        editor=edit.Editor("x" * 300, 150),
        notice="hello",
    )


@pytest.mark.parametrize("width", [1, 5, 20, 80])
@pytest.mark.parametrize("height", [1, 2, 5, 16, 40])
def test_every_frame_fills_its_size_and_keeps_the_cursor_inside(
    width: int, height: int
) -> None:
    model = busy_model(width, height)
    for changes in (
        {},
        {"pane": Pane.MAIL},
        {"overlay": Overlay.HELP},
        {"overlay": Overlay.LINES, "lines": LINES, "picked": 1},
        {"suggestions": suggest("/", Choices())},
        {"view": None, "editor": edit.Editor()},
    ):
        check_invariants(replace(model, **changes))  # pyright: ignore[reportArgumentType]


def frame_text(model: Model) -> str:
    return "\n".join(text_of(row) for row in frame(model).rows)


def test_the_activity_pane_shows_a_turn_as_it_runs() -> None:
    text = frame_text(replace(busy_model(80, 60), editor=edit.Editor()))
    assert "> turn started" in text and "more rows of prompt" in text
    assert "~ thinking" in text and "* # Plan" in text
    assert "- tool mcp__dromon__send" in text
    assert "turn ended, failed: broke" in text
    styles = {span.style for row in frame(busy_model(80, 60)).rows for span in row}
    assert {Style.CODE, Style.ERROR, Style.TOOL, Style.BAR}.issubset(styles)


def test_a_running_line_shows_a_spinner_until_its_turn_ends() -> None:
    model = attached(view=running_view(), activity=(entry(1, ActivityKind.TEXT, "x"),))
    assert "Escape interrupts" in frame_text(model)
    ended = replace(model, activity=(entry(1, ActivityKind.TURN_ENDED, "success"),))
    assert "Escape interrupts" not in frame_text(ended)


def test_the_mail_pane_and_the_status_bar_show_the_view() -> None:
    model = attached(view=view_of(VIEW), pane=Pane.MAIL, height=30, width=120)
    text = frame_text(model)
    assert "1 earlier messages not shown" in text
    assert "m-1 @ana -> alpha note" in text and "  second line" in text
    assert "q-9 alpha -> @ana question [open]" in text and "x" * 30 + " ..." in text
    assert "? q-9 to @ana: Resume alpha?  [yes / no]" in text
    bar = next(row for row in frame(model).rows if row and row[0].style is Style.BAR)
    assert "alpha | idle | 1 open questions | claude-haiku-5-5 | turns 3" in text_of(
        bar
    )
    assert "$0.0123 | pending 1 | mail" in text_of(bar)


@pytest.mark.parametrize(
    ("cost", "unknown", "shown", "absent"),
    [
        ("0.0123", 0, "$0.0123 | pending", "unknown"),
        ("0.0123", 2, "$0.0123 + 2 unknown | pending", "cost unknown"),
        (None, 2, "cost unknown | pending", "$"),
        (None, 0, "turns 3 | pending", "$"),
    ],
)
def test_the_status_bar_keeps_an_unknown_cost_apart_from_zero(
    cost: str | None, unknown: int, shown: str, absent: str
) -> None:
    """Row 8 of the T3 Code review."""
    line = {**LINE, "cost_estimate_usd": cost, "cost_unknown_turns": unknown}
    model = attached(view=view_of({**VIEW, "line": line}), width=200)
    bar = next(row for row in frame(model).rows if row and row[0].style is Style.BAR)
    assert shown in text_of(bar)
    assert absent not in text_of(bar)


def test_a_held_line_says_how_to_release_it() -> None:
    view = view_of(VIEW)
    held = replace(view, line=replace(view.line, held_by="q-9"))
    assert "HELD, /release" in frame_text(attached(view=held))


def test_the_help_and_line_panels_list_what_they_offer() -> None:
    text = frame_text(attached(overlay=Overlay.HELP, height=64))
    assert all(spec.usage in text for spec in SPECS)
    assert "Ctrl-O" in text and "Escape or Enter closes" in text
    listed = frame_text(attached(overlay=Overlay.LINES, lines=LINES))
    assert "* alpha" in listed and "  beta" in listed
    assert frame(attached(overlay=Overlay.HELP)).cursor is None


def test_the_cursor_follows_the_input() -> None:
    model = attached(width=20)
    assert frame(model).cursor == (model.height - 2, 2)
    model, _ = run(model, *keys("abc"))
    assert frame(model).cursor == (model.height - 2, 5)
    wide, _ = run(attached(width=20), *keys("x" * 18))
    row, column = frame(wide).cursor or (0, 0)
    assert column == 2 and row == wide.height - 2
    tall = attached(editor=edit.Editor("\n" * 20, 20))
    rows = [text_of(r) for r in frame(tall).rows]
    assert (
        sum(1 for r in rows if r.startswith(("> ", "  ")) and not r.strip())
        <= MAX_INPUT_ROWS
    )


def test_untrusted_text_cannot_drive_the_terminal() -> None:
    hostile = "ok\x1b[2J\x1b]0;title\x07\tend\r"
    assert "\x1b" not in clean(hostile) and "\x07" not in clean(hostile)
    assert "\r" not in clean(hostile) and "    end" in clean(hostile)
    model = attached(activity=(entry(1, ActivityKind.TEXT, hostile),))
    assert all(
        character.isprintable()
        for row in frame(model).rows
        for character in text_of(row)
    )


def test_wrapping_keeps_line_breaks_and_prefixes() -> None:
    assert wrap("one two three", 7, "* ", "  ") == ["* one", "  two", "  three"]
    assert wrap("a\n\nb", 10) == ["a", "", "b"]
    assert all(len(row) <= 3 for row in wrap("abcdefgh", 3, "> ", "> "))


def test_transcript_is_the_chosen_pane() -> None:
    model = attached(view=view_of(VIEW))
    assert "no activity yet" in text_of(transcript(model)[0])
    assert any(
        "m-1" in text_of(row) for row in transcript(replace(model, pane=Pane.MAIL))
    )
    assert "waiting" in text_of(transcript(attached(pane=Pane.MAIL))[0])


def test_a_notice_is_drawn_as_dromons_note() -> None:
    model = busy_model(80, 40)
    shown = "\n".join(text_of(row) for row in frame(model).rows)
    assert "note: the engine's start timed out, so once more" in shown

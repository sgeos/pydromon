"""The client's state machine: an event and a model in, a model and effects out.

``update`` is total. Every event has a meaning in every state, an event that does not
apply leaves the model as it was, and nothing here raises on what the daemon sent.

Polling. A ``Tick`` that finds the last poll older than the model's interval asks for
the line's view, its activity after the newest revision held, and the line list. The
interval shortens while the line runs a turn, so that a client follows a turn closely
without pushed updates. A result for another line than the one attached, which a
switch can leave in flight, is dropped.
"""

from dataclasses import replace
from typing import Final

from dromon.ui import editor as edit
from dromon.ui import render
from dromon.ui.commands import (
    Acknowledge,
    Answer,
    Choices,
    ClearSession,
    Command,
    CreateLine,
    DaemonCommand,
    Help,
    Interrupt,
    Invalid,
    Pane,
    Quit,
    ReloadProject,
    Release,
    Say,
    Set,
    ShowLines,
    ShowPane,
    ShowSettings,
    Switch,
    parse_input,
    suggest,
)
from dromon.ui.data import View
from dromon.ui.settings import command_key, prefill, values_for
from dromon.ui.settings import request_of as settings_request
from dromon.ui.settings import rows as setting_rows
from dromon.ui.keys import Char, Key, Named
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
    SettingsLoaded,
    Tick,
    ViewLoaded,
    Wheel,
)

type Result = tuple[Model, tuple[Effect, ...]]

NO_EFFECTS: Final[tuple[Effect, ...]] = ()


def initial(line_id: str, operator: str, poll_seconds: float) -> Model:
    return Model(
        line_id,
        operator,
        poll_seconds,
        notice=f"attached to {line_id} as @{operator}. ? for shortcuts",
    )


def update(model: Model, event: Event) -> Result:
    match event:
        case Tick(now):
            return _tick(model, now)
        case Resized(width, height):
            resized = replace(model, width=max(width, 1), height=max(height, 1))
            return _scrolled(resized, 0), NO_EFFECTS
        case Pressed(key):
            return _pressed(model, key)
        case Wheel(rows):
            return _scrolled(model, rows), NO_EFFECTS
        case ViewLoaded(line_id, view):
            if line_id != model.line_id:
                return model, NO_EFFECTS
            return _keep_place(model, replace(model, view=view)), _bells(model, view)
        case ActivityLoaded(line_id, page):
            if line_id != model.line_id:
                return model, NO_EFFECTS
            if page.seq < model.activity_seq:
                # The daemon restarted and its revisions began again.
                fresh = replace(model, activity=(), activity_seq=0, activity_gap=False)
                return fresh, (FetchActivity(model.line_id, 0),)
            merged = {entry.entry_id: entry for entry in model.activity}
            merged.update((entry.entry_id, entry) for entry in page.entries)
            ordered = tuple(merged[key] for key in sorted(merged))
            kept = ordered[-MAX_ENTRIES:]
            loaded = replace(
                model,
                activity=kept,
                activity_seq=page.seq,
                activity_gap=model.activity_gap
                or not page.complete
                or len(kept) < len(ordered),
            )
            return _keep_place(model, loaded), NO_EFFECTS
        case LinesLoaded(lines):
            picked = min(model.picked, max(len(lines) - 1, 0))
            return replace(model, lines=lines, picked=picked), NO_EFFECTS
        case SettingsLoaded(line_id, settings):
            if line_id != model.line_id:
                return model, NO_EFFECTS
            count = len(setting_rows(settings))
            row = min(model.setting_row, max(count - 1, 0))
            return replace(model, settings=settings, setting_row=row), NO_EFFECTS
        case Done(text):
            polled, effects = _poll(replace(model, notice=text, alarm=False))
            # Any request may change a setting, so the settings are fetched again.
            if model.settings is not None:
                effects = (*effects, FetchSettings(model.line_id))
            return polled, effects
        case Failed(text):
            return replace(model, notice=text, alarm=True), NO_EFFECTS


def _tick(model: Model, now: float) -> Result:
    model = replace(model, now=now)
    if (
        model.quit_armed_at is not None
        and now - model.quit_armed_at > QUIT_WINDOW_SECONDS
    ):
        model = replace(model, quit_armed_at=None, notice="")
    if model.polled_at is None or now - model.polled_at >= model.poll_interval:
        return _poll(model)
    return model, NO_EFFECTS


def _poll(model: Model) -> Result:
    """The polls due now. Settings are fetched only until they first arrive, because
    they change only through requests, after which ``Done`` fetches them again."""
    effects: tuple[Effect, ...] = (
        FetchView(model.line_id),
        FetchActivity(model.line_id, model.activity_seq),
        FetchLines(),
    )
    if model.settings is None:
        effects = (*effects, FetchSettings(model.line_id))
    return replace(model, polled_at=model.now), effects


def _bells(before: Model, view: View) -> tuple[Effect, ...]:
    """One bell when any question arrived while attached."""
    if before.known_questions is None:
        return NO_EFFECTS
    arrived = {q.question_id for q in view.questions} - before.known_questions
    return (Bell(),) if arrived else NO_EFFECTS


def _keep_place(before: Model, after: Model) -> Model:
    """``after``, with its scroll moved by the rows that arrived, so that an operator
    reading back in the transcript keeps their place, and its questions noted."""
    if after.view is not None:
        known = frozenset(q.question_id for q in after.view.questions)
        if before.known_questions is not None:
            known |= before.known_questions
        after = replace(after, known_questions=known)
    if before.scroll == 0:
        return after
    grown = len(render.transcript(after)) - len(render.transcript(before))
    return _scrolled(after, max(grown, 0))


def _scrolled(model: Model, rows: int) -> Model:
    limit = render.scroll_limit(model)
    return replace(model, scroll=min(max(model.scroll + rows, 0), limit))


def _choices(model: Model) -> Choices:
    view = model.view
    lines = tuple((line.line_id, f"{line.state}, {line.model}") for line in model.lines)
    values: list[tuple[str, str, tuple[str, ...]]] = []
    settings_pool: tuple[tuple[str, str], ...] = ()
    line_models: tuple[tuple[str, str], ...] = ()
    efforts: tuple[tuple[str, str], ...] = ()
    models: tuple[tuple[str, str], ...] = ()
    settings = model.settings
    if settings is not None:
        editable = [(p, e) for p, e in setting_rows(settings) if e.editable]
        settings_pool = tuple((command_key(p, e), e.summary) for p, e in editable)
        values.extend(("/set", command_key(p, e), values_for(e)) for p, e in editable)
        for entry in settings.line:
            if entry.key == "model":
                line_models = tuple((choice, "") for choice in entry.choices)
            if entry.key == "effort":
                efforts = tuple((choice, "") for choice in entry.choices)
        models = tuple((name, "") for name in settings.models)
    questions: tuple[tuple[str, str], ...] = ()
    messages: tuple[tuple[str, str], ...] = ()
    if view is not None:
        questions = tuple((q.question_id, q.text) for q in view.questions)
        values.extend(("/answer", q.question_id, q.choices) for q in view.questions)
        messages = tuple(
            (m.message_id, m.body)
            for m in view.mail
            if m.sender == view.line.line_id and m.recipient.startswith("@")
        )
    return Choices(
        lines=lines,
        questions=questions,
        messages=messages,
        settings=settings_pool,
        line_models=line_models,
        models=models,
        efforts=efforts,
        values=tuple(values),
    )


def _edited(model: Model, editor: edit.Editor) -> Model:
    """``model`` with ``editor``, and suggestions for it while it is a command."""
    suggestions = suggest(editor.text, _choices(model))
    return replace(
        model,
        editor=editor,
        suggestions=suggestions,
        selected=min(model.selected, max(len(suggestions) - 1, 0)),
    )


EDITS: Final = {
    Named.BACKSPACE: edit.backspace,
    Named.DELETE: edit.delete,
    Named.LEFT: edit.left,
    Named.CTRL_B: edit.left,
    Named.RIGHT: edit.right,
    Named.CTRL_F: edit.right,
    Named.HOME: edit.home,
    Named.CTRL_A: edit.home,
    Named.END: edit.end,
    Named.CTRL_E: edit.end,
    Named.CTRL_U: edit.cut_before,
    Named.CTRL_K: edit.cut_after,
    Named.CTRL_W: edit.cut_word,
}


def _pressed(model: Model, key: Key) -> Result:
    match model.overlay:
        case Overlay.HELP:
            if key in (Named.ESCAPE, Named.ENTER, Named.CTRL_C) or key == Char("q"):
                return replace(model, overlay=Overlay.NONE), NO_EFFECTS
            return model, NO_EFFECTS
        case Overlay.LINES:
            return _picking(model, key)
        case Overlay.SETTINGS:
            return _setting(model, key)
        case Overlay.NONE:
            pass
    if model.suggestions:
        chosen = _suggesting(model, key)
        if chosen is not None:
            return chosen
    if isinstance(key, Char):
        if key.text == "?" and not model.editor.text:
            return replace(model, overlay=Overlay.HELP), NO_EFFECTS
        return _edited(model, edit.insert(model.editor, key.text)), NO_EFFECTS
    if key in EDITS:
        return _edited(model, EDITS[key](model.editor)), NO_EFFECTS
    match key:
        case Named.ENTER:
            return _enter(model)
        case Named.NEWLINE:
            return _edited(model, edit.insert(model.editor, "\n")), NO_EFFECTS
        case Named.TAB:
            return _tab(model), NO_EFFECTS
        case Named.UP | Named.DOWN:
            return _vertical(model, -1 if key is Named.UP else 1), NO_EFFECTS
        case Named.PAGE_UP:
            return _scrolled(model, render.page(model)), NO_EFFECTS
        case Named.PAGE_DOWN:
            return _scrolled(model, -render.page(model)), NO_EFFECTS
        case Named.ESCAPE:
            if model.running:
                return _run(model, Interrupt())
            return model, NO_EFFECTS
        case Named.CTRL_C:
            return _control_c(model)
        case Named.CTRL_D:
            if model.editor.text:
                return _edited(model, edit.delete(model.editor)), NO_EFFECTS
            return replace(model, finished=True), (Exit(),)
        case Named.CTRL_L:
            return _poll(model)
        case Named.CTRL_O:
            pane = Pane.MAIL if model.pane is Pane.ACTIVITY else Pane.ACTIVITY
            return replace(model, pane=pane, scroll=0), NO_EFFECTS
        case Named.CTRL_N | Named.CTRL_P:
            return _cycle(model, 1 if key is Named.CTRL_N else -1)
        case _:
            return model, NO_EFFECTS


def _suggesting(model: Model, key: Key) -> Result | None:
    """The keys that act on the suggestions while they show, or ``None`` for a key
    that edits the input instead."""
    count = len(model.suggestions)
    match key:
        case Named.TAB | Named.DOWN:
            return replace(model, selected=(model.selected + 1) % count), NO_EFFECTS
        case Named.BACK_TAB | Named.UP:
            return replace(model, selected=(model.selected - 1) % count), NO_EFFECTS
        case Named.ESCAPE:
            return replace(model, suggestions=(), selected=0), NO_EFFECTS
        case Named.ENTER:
            chosen = model.suggestions[model.selected].text
            taken = _edited(model, edit.Editor(chosen, len(chosen)))
            taken = replace(taken, selected=0)
            command = parse_input(chosen)
            if chosen.endswith(" ") or isinstance(command, Invalid):
                return replace(
                    taken, suggestions=suggest(chosen, _choices(model))
                ), NO_EFFECTS
            return _enter(taken)
        case _:
            return None


def _tab(model: Model) -> Model:
    suggestions = suggest(model.editor.text, _choices(model))
    if len(suggestions) == 1:
        text = suggestions[0].text
        return _edited(model, edit.Editor(text, len(text)))
    return replace(model, suggestions=suggestions, selected=0)


def _vertical(model: Model, rows: int) -> Model:
    moved = edit.vertical(model.editor, rows)
    if moved is not None:
        return replace(model, editor=moved)
    if rows < 0:
        step = edit.older(model.history, model.editor.text)
    else:
        step = edit.newer(model.history)
    if step is None:
        return model
    history, text = step
    return replace(_edited(model, edit.Editor(text, len(text))), history=history)


def _control_c(model: Model) -> Result:
    if model.editor.text:
        cleared = replace(model, history=edit.History(model.history.entries))
        return _edited(cleared, edit.Editor()), NO_EFFECTS
    if model.quit_armed_at is not None:
        return replace(model, finished=True), (Exit(),)
    armed = replace(
        model,
        quit_armed_at=model.now,
        notice="press Ctrl-C again to detach",
        alarm=False,
    )
    return armed, NO_EFFECTS


def _enter(model: Model) -> Result:
    text = model.editor.text
    if text.endswith("\\") and model.editor.cursor == len(text):
        # A trailing backslash breaks the row, as in Claude Code.
        return _edited(model, edit.Editor(text[:-1] + "\n", len(text))), NO_EFFECTS
    command = parse_input(text)
    if command is None:
        return model, NO_EFFECTS
    model = replace(
        _edited(model, edit.Editor()),
        history=edit.remember(model.history, text),
        scroll=0,
    )
    return _command(model, command)


def _command(model: Model, command: Command) -> Result:
    match command:
        case (
            Say()
            | Answer()
            | Acknowledge()
            | Release()
            | Interrupt()
            | ClearSession()
            | CreateLine()
            | ReloadProject()
        ):
            return _run(model, command)
        case Set():
            checked = settings_request(model.settings, command)
            if isinstance(checked, str):
                effects: tuple[Effect, ...] = ()
                if model.settings is None:
                    effects = (FetchSettings(model.line_id),)
                return replace(model, notice=checked, alarm=True), effects
            return _run(model, checked)
        case ShowSettings(focus):
            row = model.setting_row
            if model.settings is not None and focus is not None:
                row = next(
                    (
                        index
                        for index, (project, entry) in enumerate(
                            setting_rows(model.settings)
                        )
                        if not project and entry.key == focus
                    ),
                    row,
                )
            opened = replace(model, overlay=Overlay.SETTINGS, setting_row=row)
            return opened, (FetchSettings(model.line_id),)
        case Switch(line_id):
            return _switch(model, line_id)
        case ShowLines():
            picked = next(
                (
                    i
                    for i, line in enumerate(model.lines)
                    if line.line_id == model.line_id
                ),
                0,
            )
            return replace(model, overlay=Overlay.LINES, picked=picked), (FetchLines(),)
        case ShowPane(pane):
            return replace(model, pane=pane, scroll=0), NO_EFFECTS
        case Help():
            return replace(model, overlay=Overlay.HELP), NO_EFFECTS
        case Quit():
            return replace(model, finished=True), (Exit(),)
        case Invalid(reason):
            return replace(model, notice=reason, alarm=True), NO_EFFECTS


def _run(model: Model, command: DaemonCommand) -> Result:
    request = Request(command, model.line_id, model.operator)
    return replace(model, notice="sending", alarm=False), (request,)


def _switch(model: Model, line_id: str) -> Result:
    if line_id == model.line_id:
        return replace(
            model, notice=f"already attached to {line_id}", alarm=False
        ), NO_EFFECTS
    if model.lines and line_id not in {line.line_id for line in model.lines}:
        return replace(model, notice=f"no line {line_id}", alarm=True), NO_EFFECTS
    switched = Model(
        line_id,
        model.operator,
        model.poll_seconds,
        width=model.width,
        height=model.height,
        lines=model.lines,
        history=model.history,
        pane=model.pane,
        now=model.now,
        notice=f"attached to {line_id} as @{model.operator}",
    )
    return _poll(switched)


def _cycle(model: Model, step: int) -> Result:
    if not model.lines:
        return replace(
            model, notice="no other line is known yet", alarm=False
        ), NO_EFFECTS
    ids = [line.line_id for line in model.lines]
    index = ids.index(model.line_id) if model.line_id in ids else -step
    target = ids[(index + step) % len(ids)]
    return _switch(model, target)


def _setting(model: Model, key: Key) -> Result:
    """The keys of the settings panel. Enter on a row a client may change fills the
    input with the ``/set`` that edits it, as Claude Code's ``/config`` edits in
    place, and the panel closes so the operator can type."""
    found = setting_rows(model.settings) if model.settings is not None else []
    count = len(found)
    match key:
        case Named.UP | Named.CTRL_P | Named.BACK_TAB:
            return replace(model, setting_row=max(model.setting_row - 1, 0)), NO_EFFECTS
        case Named.DOWN | Named.CTRL_N | Named.TAB:
            row = min(model.setting_row + 1, max(count - 1, 0))
            return replace(model, setting_row=row), NO_EFFECTS
        case Named.PAGE_UP:
            row = max(model.setting_row - render.page(model), 0)
            return replace(model, setting_row=row), NO_EFFECTS
        case Named.PAGE_DOWN:
            row = min(model.setting_row + render.page(model), max(count - 1, 0))
            return replace(model, setting_row=row), NO_EFFECTS
        case Named.ENTER:
            if not count:
                return replace(model, overlay=Overlay.NONE), NO_EFFECTS
            project, entry = found[model.setting_row]
            if not entry.editable:
                notice = f"{entry.key} cannot be changed from a client. {entry.summary}"
                return replace(model, notice=notice, alarm=False), NO_EFFECTS
            text = prefill(project, entry)
            closed = replace(model, overlay=Overlay.NONE)
            return _edited(closed, edit.Editor(text, len(text))), NO_EFFECTS
        case Named.ESCAPE | Named.CTRL_C:
            return replace(model, overlay=Overlay.NONE), NO_EFFECTS
        case _:
            if key == Char("q"):
                return replace(model, overlay=Overlay.NONE), NO_EFFECTS
            return model, NO_EFFECTS


def _picking(model: Model, key: Key) -> Result:
    count = len(model.lines)
    match key:
        case Named.UP | Named.CTRL_P | Named.BACK_TAB:
            return replace(model, picked=max(model.picked - 1, 0)), NO_EFFECTS
        case Named.DOWN | Named.CTRL_N | Named.TAB:
            return replace(
                model, picked=min(model.picked + 1, max(count - 1, 0))
            ), NO_EFFECTS
        case Named.ENTER:
            closed = replace(model, overlay=Overlay.NONE)
            if not count:
                return closed, NO_EFFECTS
            return _switch(closed, model.lines[model.picked].line_id)
        case Named.ESCAPE | Named.CTRL_C:
            return replace(model, overlay=Overlay.NONE), NO_EFFECTS
        case _:
            if key == Char("q"):
                return replace(model, overlay=Overlay.NONE), NO_EFFECTS
            return model, NO_EFFECTS

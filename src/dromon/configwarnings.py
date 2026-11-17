"""Warnings about a project file that loads but will not do what it says.

**Round 17, items 6 and 7.** ``dromon config`` printed these alone, so a daemon that
started or reloaded from the same file said nothing, and the operator learned of a
rule that matches nothing or of a line whose every turn is refused only from a turn.
The checks live here, and the client and the daemon both call ``config_warnings``, so
the two cannot differ. A warning never refuses the file. It is advice, and the
daemon's own refusals, which stay in their places, are the authority.

Porting note. Each check reads the loaded configuration and the filesystem once and
returns plain sentences, so it maps onto a pure function over a configuration and a
directory listing.
"""

from pathlib import Path

from dromon import settings as engine_settings
from dromon.driver import ENGINE_OF
from dromon.lineconfig import WorkdirAccess
from dromon.projectconfig import ProjectConfig
from dromon.roles import CONFINING_ENGINES
from dromon.scheduler import read_only_refusal


def _workdir(root: Path, raw: str | None) -> Path:
    """A line's working directory as ``dromon config`` resolves it."""
    place = Path(raw or ".").expanduser()
    return (place if place.is_absolute() else root / place).resolve()


def slash_warnings(config: ProjectConfig, root: Path) -> list[str]:
    """One sentence for each rule of the project's settings or a line's that names an
    absolute path with one leading slash, as ``engine_settings.single_slash_rules``
    finds them. A project rule may be relative to the project, and a line's also to
    its working directory, so both count as the directories the rule may name."""
    found: list[str] = []
    places = [("the project's settings", config.settings, ())]
    places += [
        (f"line {line_id}'s settings", line.config.settings, (line.config.workdir,))
        for line_id, line in sorted(config.lines.items())
    ]
    for place, layer, workdirs in places:
        if layer is None:
            continue
        projects = [str(root), *(str(_workdir(root, raw)) for raw in workdirs)]
        for kind, rule, corrected in engine_settings.single_slash_rules(
            layer.document(), projects=projects
        ):
            found.append(
                f"{place} {kind} rule {rule} has an absolute path with one leading"
                " slash, which Claude Code reads as relative to the project, so it"
                f" matches nothing there. For an absolute path write {corrected}"
                + (
                    ". A deny rule that matches nothing protects nothing"
                    if kind == "deny"
                    else ""
                )
            )
    return found


def read_only_warnings(config: ProjectConfig) -> list[str]:
    """One sentence for each line that asks for read access, or is a seat of a role
    and so has it whenever another seat owns the role, and that its engine refuses at
    every turn, and for each seat whose engine cannot confine it at all.

    **Round 17, item 7.** Round 16 covered the Claude Code lines that asked for read
    access. The same refusal is now taken for every engine, through
    ``read_only_refusal``, and for the seats of a role, whose access the daemon sets
    to read as a turn starts and which failed then and not before. A seat on an engine
    that cannot confine it is not refused, and its mail waits while another seat owns
    the role, which is said too. **Residual.** Ownership moves at run time, so a seat
    is judged as one that may not own the role, and a line the file does not declare
    is not seen."""
    found: list[str] = []
    for line_id, line in sorted(config.lines.items()):
        engine = ENGINE_OF.get(line.model)
        if line.config.workdir_access is not WorkdirAccess.READ:
            continue
        refusal = read_only_refusal(
            line.config, engine, config.settings, config.sandbox
        )
        if refusal is not None:
            found.append(
                f"line {line_id} has workdir_access read, and every turn of it will be"
                f" refused, since {refusal}. Remove what that names, or change the"
                " line's workdir_access"
            )
    for role_id, role in sorted(config.roles.items()):
        for seat in role.seats:
            member = config.lines.get(seat)
            if member is None:
                continue
            engine = ENGINE_OF.get(member.model)
            if engine not in CONFINING_ENGINES:
                found.append(
                    f"line {seat} is a seat of role {role_id} on {engine}, which"
                    " cannot confine a seat to reading, so its mail waits while"
                    f" another seat owns {role_id}"
                )
                continue
            if member.config.workdir_access is WorkdirAccess.READ:
                continue
            refusal = read_only_refusal(
                member.config, engine, config.settings, config.sandbox
            )
            if refusal is not None:
                found.append(
                    f"line {seat} is a seat of role {role_id}, and every turn it takes"
                    f" while it does not own {role_id} will be refused, since"
                    f" {refusal}. Remove what that names, or take the line out of the"
                    " role"
                )
    return found


def config_warnings(config: ProjectConfig, root: Path) -> list[str]:
    """Every warning for ``config``, whose project is ``root``, in a fixed order, so
    that a client and a daemon give the same list."""
    return [*slash_warnings(config, root), *read_only_warnings(config)]

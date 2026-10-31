"""Shared credential and project secret paths used by engine isolation."""

import os
from pathlib import Path
from typing import Final

CREDENTIAL_PATHS: Final = (
    Path(".ssh"),
    Path(".aws"),
    Path(".codex/auth.json"),
    Path(".cargo/credentials.toml"),
    Path(".pi/agent/auth.json"),
    Path(".grok/auth.json"),
    Path(".claude.json"),
    Path(".claude/.credentials.json"),
    Path(".netrc"),
    Path(".git-credentials"),
    Path(".gnupg"),
    Path(".npmrc"),
    Path(".pypirc"),
    Path(".docker/config.json"),
    Path(".kube/config"),
    Path(".config/gh/hosts.yml"),
)
"""Known operator-home credential paths, not an exhaustive list."""


def existing_real_paths(paths: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """Return unique real absolute paths for entries that exist, preserving order."""
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if os.path.lexists(path):
            real = (
                Path(os.path.realpath(path))
                if os.path.exists(path)
                else Path(os.path.abspath(path))
            )
            if real not in seen:
                seen.add(real)
                found.append(real)
    return tuple(found)


def project_secret_paths(project_root: Path, workdir: Path) -> tuple[Path, ...]:
    """Existing project secret directories, by real path, from both line locations."""
    return existing_real_paths((project_root / "secret", workdir / "secret"))

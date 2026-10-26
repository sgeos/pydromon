#!/usr/bin/env bash
# Isolated git worktrees for parallel development.
#
# Each concurrent agent line, or person, gets its own working directory on its own
# feature branch, so parallel work never shares one dirty tree, one index or one
# running gate.
#
# Worktrees are created as siblings of the repository, under
# ../<repository>-worktrees/<branch>, with each slash in the branch name replaced by a
# dash, so that `feat/x` and `fix/x` do not collide. Override the parent directory with
# WORKTREE_BASE and the branch new work starts from with WORKTREE_TRUNK, which
# defaults to main. If the trunk has no local branch, as in a fresh clone, new work
# starts from origin/<trunk>, and the new branch does not track it.
#
# Usage:
#   scripts/worktree.sh new  feat/some-thing   # new branch off the trunk, in a new tree
#   scripts/worktree.sh list                   # every worktree of this repository
#   scripts/worktree.sh path feat/some-thing   # print the tree's path, for cd
#   scripts/worktree.sh rm   feat/some-thing   # remove the tree, then delete the branch
#
# Failure modes. `rm` refuses a tree with uncommitted or untracked changes, and then
# refuses to delete a branch that is not merged, because both use git's own safety
# checks rather than forcing. A new tree has the tracked tmp/ marker but no secret/,
# which is never in git, so copy whatever it needs by hand.

set -euo pipefail

usage() {
    sed -n '/^# Usage:/,/^#$/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

[[ $# -ge 1 ]] || usage
command="$1"

root="$(git rev-parse --show-toplevel)"
# A linked worktree reports its own top level, so resolve the main checkout through
# the common git directory, so that every tree uses the same parent directory.
common="$(cd "$root" && cd "$(git rev-parse --git-common-dir)" && pwd)"
main_root="$(dirname "$common")"
name="$(basename "$main_root")"
base="${WORKTREE_BASE:-$(dirname "$main_root")/${name}-worktrees}"
trunk="${WORKTREE_TRUNK:-main}"

tree_path() {
    printf '%s/%s\n' "$base" "${1//\//-}"
}

require_branch() {
    [[ -n "${1:-}" ]] || usage
    git check-ref-format --branch "$1" >/dev/null ||
        { printf 'worktree.sh: not a valid branch name: %s\n' "$1" >&2; exit 2; }
}

case "$command" in
    new)
        require_branch "${2:-}"
        path="$(tree_path "$2")"
        if [[ -e "$path" ]]; then
            printf 'worktree.sh: %s already exists\n' "$path" >&2
            exit 1
        fi
        # A fresh clone may have no local trunk branch, only its remote-tracking one.
        start="$trunk"
        if ! git -C "$main_root" rev-parse --verify --quiet "refs/heads/$trunk" >/dev/null; then
            start="origin/$trunk"
            git -C "$main_root" rev-parse --verify --quiet "refs/remotes/$start" >/dev/null ||
                { printf 'worktree.sh: no branch %s or %s\n' "$trunk" "$start" >&2; exit 1; }
        fi
        mkdir -p "$base"
        git -C "$main_root" worktree add --no-track -b "$2" "$path" "$start"
        printf '%s\n' "$path"
        ;;
    list)
        git -C "$main_root" worktree list
        ;;
    path)
        require_branch "${2:-}"
        tree_path "$2"
        ;;
    rm)
        require_branch "${2:-}"
        path="$(tree_path "$2")"
        git -C "$main_root" worktree remove "$path"
        git -C "$main_root" branch -d "$2"
        ;;
    *)
        usage
        ;;
esac

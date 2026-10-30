# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-30, own notes, a stable shell and a truthful quiet notice

**Done, merged in pull requests 82 to 85.**

- **Own notes.** A line's own notes, such as fired wakes and compaction requests, are
  counted as `own_notes` and never as unread, by one rule in every view. Asked for by
  the review line in round 5 and by a worker line in another project.
- **Stale brief.** A turn that leads with the standing brief says when operator mail
  arrived after it was set, and `line_status` shows the same.
- **Shell.** A Claude Code or Codex line with a shell tool gets the daemon's validated
  `SHELL` on every turn, and Claude Code requires bash or zsh. A worker line in another
  project saw its shell change from bash to zsh between turns.
- **Quiet notice.** The silent-turn reminder is dropped once the line has sent mail.
  It had told the core line that it sent no mail after it mailed the operator from a
  turn its engine started by itself.
- **Disclosure refusal.** Without a list, `check_disclosure` says none is configured and
  where the operator sets one.
- **Test timing.** The self-start grace test ends its grace on release, not on the
  clock.

**Found by review.** The unread filter of `list_mail` still listed own notes, and own
notes were never dropped from their count. The daemon's own shell values could reach a
line unvalidated. All were fixed before merge.

**Observed.** The bundled Claude Code executable names `CLAUDE_CODE_SHELL` as a shell
override that must be a bash or zsh path, and falls back to detection otherwise. No
turn was run to confirm it.

**Round 6 reports.** core was satisfied, and its top request, the false quiet notice,
is fixed. engines and review were not satisfied. engines asked for the stale brief fix
and a compaction wake that says why it fired. review asked for the unread filter fix,
and for review briefs that name the commit under review. The first of each is merged,
but the daemon did not run it during the round.

**Decided by the operator on 2026-10-30.** A review line may cite live probes the
operator runs. The Keleusma decisions and budgets are deferred. SSH gets a minimal
push test, then lines return to sandboxed work over HTTPS. Line numbers from
`check_disclosure` were declined, since they would let one check reveal listed terms.

**Not verified.** The daemon was restarted onto `59ea9b0` after the last merge, so no
turn has yet run the round's changes. A push over SSH, which awaits the operator's test repository. The
shell selection in a live turn. Grok has not run a turn and awaits the sign-in.

**For the operator.**
1. Create the private test repository and add the deploy key with the command given
   in session, then the probe in `tmp/r6/ssh-probe.sh` runs the push.
2. Sign in to Dromon's Grok home with the command in `GROK.md`.

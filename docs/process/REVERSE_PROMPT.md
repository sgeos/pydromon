# Reverse Prompt

> **Navigation**: [Process](./README.md)

A state file, overwritten whole after each task.

## 2026-10-29, state that survives a restart, and git over SSH

**Done, merged in pull requests 76 to 80.**

- **Request keys across a restart.** A keyed `send_message`, `ask_operator` or
  `wake_after` records its key in the event of its act, so a retry after a restart
  repeats the first result. Asked for by the review line in round 4.
- **Background task reminders.** A silent-turn reminder checks its tasks against the
  live listing and the task records, and says unknown where it cannot tell. Asked for
  by the core line in round 4.
- **Git over SSH.** An option the operator enables per line, with an agent socket only,
  host keys pinned in a file the operator names, and the files checked before each
  turn. Required by the operator on 2026-10-29.
- **A fresh engine session on every turn.** Asked for by the Keleusma project for a
  blind labeller line.
- **Test fixes.** Test schedulers no longer reach the real process table, which hung
  two gates, and a timing grace was widened.
- **Keleusma template.** Worktree lines name their own `CLAUDE.md` by absolute path,
  and the dogfood page describes a labeller line, per the Kaizen line's third review.

**Found by review.** The SSH review found that a line with its sandbox disabled could
rewrite the pinned host keys, and that any decodable text passed as a host key. Both
are addressed, the first by detection at each turn's start and not by prevention.

**Observed.** Outside a sandbox, the SSH command reached GitHub, verified its host key
and stopped at authentication for a throwaway key. Inside a Claude Code sandbox, ssh
could not resolve GitHub and a proxy tunnel was refused, so the option is inferred to
work only for a line whose sandbox is disabled.

**Round 5 reports.** core and engines were satisfied. review was not, asking that its
own wake notes not count as unread mail. core asked for line numbers from
`check_disclosure`, since bisecting for one term spent its hourly budget.

**Not verified.** Signing with a real key and a push over SSH. A fresh session live.
Whether the background task fix ends the false reminders, since the daemon ran the
old code all round. Grok has not run a turn. The TLS transport has run only between
daemons on one machine.

**For the operator.**
1. To verify a push over SSH, start an agent holding only a deploy key for a test
   repository and name its socket.
2. Decide whether any line should run with its sandbox disabled to use SSH, or use
   HTTPS.
3. Sign in to Dromon's Grok home with the command in `GROK.md`.
4. Decide whether a review line may cite live probes the operator runs.
5. The Keleusma decisions listed in `KELEUSMA_DOGFOOD.md`, and provider budgets with
   `dromon budget set`.

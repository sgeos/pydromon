Type: Fixed

- Codex, Grok and shared JavaScript Object Notation line readers name the stream,
  record bound and observed lower bound when an engine record exceeds its limit.
  The failure question remains answerable and the next turn can run.
- Document the stream limits and recovery procedure for a separator overrun.
- Codex resumes exclude unused turn history. Oversized frames are drained with
  bounded chunks and end the turn without a failure hold. An overrun while
  a correlated resume response replaces the engine session on its next turn and
  says so explicitly.
- Recoverable failures remain eligible after replay. Mail not submitted to Codex
  is requeued by its turn-end event. Submitted mail is not replayed and the next
  notice names its identifiers and uncertain outcome.
- The scheduler restart test closes its original event log before replacing it,
  avoiding an unclosed SQLite connection in each parameter case.

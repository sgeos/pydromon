Type: Added

- `dromon check-engines --account LINE` asks the running daemon to check one Codex
  account with the effective line launch settings and without a thread or model
  request. It uses a fresh process and preserves the cached session. Account type
  and plan use fixed vocabulary, and private account fields are never returned.
  Unrecognized provider messages are withheld, and recognized messages are screened
  against the disclosure list. Claude Code has no supported equivalent account read.
- Cancellation joins the server listing worker and separately bounded launcher
  cleanup before releasing the diagnostic slot. Repeated cancellation does not
  interrupt termination, kill, reaping or the separately bounded output drains.
  Draining also runs when launcher cleanup fails.

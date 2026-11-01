Type: Fixed

- Grok lines without `WebFetch` no longer fail at launch. Grok Build 1.0.70's `grok agent`
  refuses the `--deny` argument with status 2, so Dromon no longer passes it, and withholds
  `WebFetch` by `GROK_WEB_FETCH=0` and per-call refusal instead.

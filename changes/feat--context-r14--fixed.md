Type: Fixed

- A Claude Code line with `ViewImage` and without `Read` could read any file in its
  working directory, not only images. The operator's live run of 2026-11-01 showed a
  renamed text file answered by the engine, and a png of 6,000,287 bytes over the
  5 MiB limit viewed, although `image_refusal` refuses both. The rule ran in the
  permission callback, which Claude Code does not call for a `Read` inside its
  working directory. It now also runs in a `PreToolUse` hook that the engine runs
  before every `Read`, and a refusal there is final. `LINE_CONFIG.md` and `ENGINES.md`
  record the defect, the inference about its cause and what is left to verify live.

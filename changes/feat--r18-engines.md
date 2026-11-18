Type: Added

- Codex drivers for `gpt-6.2-sol` and `gpt-6.2-astra`, which OpenAI released on
  2026-11-17, for every strategy. Each copies its predecessor's settings, `gpt-6.1-sol`
  and `gpt-6-astra`, with the context windows of 500,000 and 272,000 tokens that Codex's
  model cache gives, and a test derives them. Their prices are the highest Codex
  placeholder, since no published price was read. Neither has run a turn through
  Dromon, and `DRIVER.md` lists the checks to make first.

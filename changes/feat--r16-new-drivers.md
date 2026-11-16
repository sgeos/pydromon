Type: Added

- Packaged subscription, application programming interface usage and flexible drivers
  for Claude Fable 5.2, GPT-6-Astra and GPT-6-Sol on Codex, Grok 4.7 Build Fast,
  and GPT-6-Astra, GPT-6-Sol, GPT-6.1-Sol and GPT-6.1 Terra on Pi. All new
  bindings remain unverified through Dromon. Catalogue windows and inherited
  settings are documented and checked against their predecessor drivers.
- Placeholder prices for GPT-6-Sol and GPT-6-Astra on Codex, at the highest Codex
  placeholder and unverified as the rest of the table is, so their metered turns
  count toward `max_cost_usd`. A test checks that every model of a token-priced
  engine with a metered driver has a price.

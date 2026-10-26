---
name: Bug report
about: Report a defect in the daemon, the client, an engine adapter or a driver
title: ''
labels: bug
assignees: ''
---

<!--
A security issue, such as a line reaching Dromon's state or a token, belongs in a
private advisory rather than here. See SECURITY.md.
-->

## Summary

<!-- One or two sentences, stating the expected and the observed behaviour. -->

## Where it happened

- Commit (`git rev-parse HEAD`):
- Command (`dromon ...` or `dromond ...`):
- Engine and model, if a line was involved (Claude Code, Codex or Grok Build):
- Driver strategy (subscription, api or flexible):
- Engine version (`claude --version`, `codex --version` or `grok --version`):
- Platform (operating system and version):

## Steps to reproduce

1.
2.
3.

## Expected behaviour

## Observed behaviour

<!--
Include the client's output and the relevant lines of `dromond.log` from the runtime
directory. Remove tokens, credentials and message bodies you would not publish.
-->

## Additional context

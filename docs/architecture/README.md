# Architecture

> **Navigation**: [Documentation Root](../README.md)

- [`DESIGN.md`](DESIGN.md), the planned daemon, session lines, three communication layers, wakes, operator questions and stream processors.
- [`KELEUSMA_INTEGRATION.md`](KELEUSMA_INTEGRATION.md), what Dromon needs from `pykeleusma`, mapped to that binding's tasks, for the `pykeleusma` line.
- [`KELEUSMA_DOGFOOD.md`](KELEUSMA_DOGFOOD.md), what Dromon needed to run Keleusma's lines, how it was built and tried, and the project file that results.
- [`FEDERATION.md`](FEDERATION.md), messages and wakes between Dromon daemons, first on one machine and then across machines. Proposed on 2026-10-28, for the operator's review.
- [`ROLES.md`](ROLES.md), roles with seats on several providers, one write owner at a time, planned handoff and recovery. Approved on 2026-10-27, and its first step is built.

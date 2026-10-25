# Protocol

> **Navigation**: [Documentation Root](../README.md)

The specification shared by every implementation of Dromon. It is drafted here in the
Python prototype and its mature parts are later ported to the lasting `dromon`
repository, as recorded in [`../decisions/NAMING_AND_PORTS.md`](../decisions/NAMING_AND_PORTS.md).
Documents here describe behaviour and formats, never Python details.

- [`EVENT_LOG.md`](EVENT_LOG.md), the data model as events, their JSON encoding and their SQLite storage. Draft, version 1.
- [`CLIENT.md`](CLIENT.md), the requests between `dromon` and `dromond` over the project's Unix domain socket, with the command exit statuses. Draft, version 1.
- [`RELAY.md`](RELAY.md), forwarding messages to operators through adapters for channels such as email or chat. Draft, version 1.

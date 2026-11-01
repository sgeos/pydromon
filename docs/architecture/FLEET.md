# The fleet

**Asked by the operator on 2026-10-31, round 13.** The clients let the operator manage
every Dromon daemon of this user, rather than one project at a time. This document is
normative for the fleet index, the `dromon fleet` commands, the attached client's
`/project` command and the hosts file. The commands' exact rows are in
[`CLIENT.md`](../protocol/CLIENT.md).

## The constraint

Operator authority never crosses federation, as the trust section of
[`FEDERATION.md`](FEDERATION.md) holds. The fleet reaches each daemon through that
daemon's own operator socket, with the operator's own access to it, and never through
a peer, a peer token or a peer socket. Every fleet command sends the operation the
single-project command sends, so the fleet adds no operation and no authority. The
index only tells the client where each socket is.

## The index

- **Location.** **Decided by core, for the operator's review.** The index is the
  directory `index` inside the registry of open local federation, by default
  `.local/state/dromon/registry/index` under the home directory the password database
  gives the user. `DROMON_REGISTRY_DIR` moves both, so every test, which sets it to a
  temporary directory, never touches the real index, and the registry's trust rules
  hold unchanged. **Reason for a directory of its own.** A registry entry says that a
  daemon is open to local federation, and other daemons act on it, while an index
  entry says only where a daemon's operator socket is. Mixing them would let a daemon
  that is not open be taken for one that is.
- **Every daemon.** A daemon writes its entry at start and removes it at stop,
  whatever its federation mode. A restarted daemon's successor writes its own.
- **The entry.** A file of mode 0600, named by a digest of the project root so that
  the root's name never becomes a path, holding the project's name, which is the last
  step of its root, the root, the operator socket, the pid, the start time the process
  table gives, and the commit the daemon loaded its code from. The directory must be
  the user's with mode 0700, and is refused if wider, never narrowed. An entry of
  another mode, owner or file name is ignored. At most 256 entries are read.
- **The name is the root's.** **Review of 7ff033f, G3.** An entry whose name is not
  the last step of its root is refused when it is made, and so is never written or
  read, and `verify` refuses it again. Otherwise a writer of the index could publish a
  live daemon under another project's name, and the operator's command for that
  project would reach it.
- **One root, one entry.** **Decided by core.** The daemon that starts last for a root
  replaces its entry, since one project runs one daemon under its lock, and an entry a
  dead daemon left is stale anyway. A daemon removes only an entry that is still
  exactly its own, so it never removes its successor's.
- **A daemon that cannot be listed.** **Decided by core, for the operator's review.** A
  failure to write the entry, such as a process table that cannot give the daemon's
  start time, is written to standard error, and the daemon starts unlisted rather than
  refusing to start. The index grants nothing, so a daemon missing from it loses only
  the fleet's sight of it.
- **Lines are denied the index.** The index in use and the default one join every
  line's protected directories in every project, whatever its federation mode, so no
  line can write an entry that would direct the operator's commands to a socket of its
  choosing. **Inference.** The index lies inside the registry, so an open project's
  lines, which are denied the registry, were denied it already.
- **The daemon makes the index at start.** **Review of 7ff033f, G5.** Codex's profile
  denies a protected path only when it exists, and Grok's check requires only those
  that exist, so a line of either could otherwise make the index before any daemon
  had. The daemon therefore makes the index in use, with mode 0700, before any engine
  is built. A failure is written to standard error and the daemon starts, as one that
  cannot be listed does. **Decided by core, for the operator's review.**
  **Consequence.** A Grok line's turns fail, with the entry to add, until its profile
  denies the index. **Limit.** A daemon given `DROMON_REGISTRY_DIR` does not make the
  default index, since that would reach outside the override, so a Codex or Grok line
  of such a daemon could make the default one, which a client without the override
  reads. Claude Code's sandbox denies the path whether or not it exists.

## Staleness and trust

- **Stale.** An entry whose pid now has another start time, or none, is stale. It is
  shown as stale and is never asked.
- **Unknown.** An entry whose process table cannot be read is unknown, shown so, and
  never asked either. A sandbox that denies `ps` therefore sees no live daemon.
- **Verified before use.** A live entry is reached only after the daemon answering its
  socket gives, in `status`, the pid and the project root the entry names. A socket
  that another process now holds is so refused before anything else is sent to it.
  **Limit.** The check and the operation are separate connections, so a process of
  the user that replaces the socket between them is not caught, which is within the
  exposure the next item accepts.
- **Who can write it.** An entry is a statement by a process of the user, never proof.
  An unconfined process of the user could write one, as it could read the operator
  socket itself, which is the exposure the trust section of `CLIENT.md` already
  accepts for the socket.

## The commands

`dromon fleet list`, `questions`, `mail`, `issues`, `answer`, `send`, `read`, `status`,
`restart` and `hosts`, as [`CLIENT.md`](../protocol/CLIENT.md) sets out.

- **Naming.** A project is named by the last step of its root or by its root. An item
  is named `PROJECT:ID` or `PROJECT:LINE`, split at the last colon, since no identifier
  holds one, so a root that holds a colon still works. A name two live daemons share
  is refused, and the root then tells them apart.
- **A daemon that does not answer** is reported, on standard output for `list` and on
  standard error for the others, and the command goes on with the rest. The exit
  status is then 1, so a script can tell.
- **Bounded waits.** Each request a fleet command makes itself waits at most 2
  seconds, so a daemon that hangs delays the rest by at most that for each of them. A
  routed command's own operation waits as its single-project command does, at most 5
  seconds. **Review of 7ff033f, G2.**
- **Side effect.** `fleet list` and `fleet mail` send `mail`, and listing delivers
  waiting operator mail, as `dromon mail` does.
- **Restart.** `fleet restart` runs `dromon restart`, whose check that the installed
  daemon imports reads this client's installation. **Limit.** The successor runs the
  interpreter the daemon itself runs, so the check speaks for it only when both run
  from the same installation.
- **The successor.** **Review of 7ff033f, G1.** `fleet restart` checks the daemon's
  status against its entry again before it asks for the restart. It then takes as the
  successor only the daemon the index lists for the same root and socket under
  another pid, live by its start time, whose status gives that pid and root. Any
  other process that answers the socket under another pid is not taken for it.
  **Consequence.** A successor that could not be listed is never confirmed, and the
  command reports that no listed successor answered.
- **What a hang costs.** **Review of 7ff033f, G2.** The wait for the successor ends
  at a deadline, the drain wait plus 70 seconds for the shutdown and startup bounds,
  and each status waits at most 2 seconds and never past the deadline. A daemon that
  accepts and never answers therefore holds `fleet restart` for at most that plus one
  poll, after the preflight's 60 seconds at most and the two requests before the
  wait. `dromon restart` waits the same way, with 5 seconds for each status.

## The attached client

`/projects` lists the index's projects with their states, and `/project
PROJECT[:LINE]` attaches to a line of another project without restarting the client.
**Review of 7ff033f, G4.** A name two projects share is listed with each root beside
it, and completes to the roots, since the name alone is refused. The target is the
rest of the input whole, so a root that holds a space stays one root, and the line
follows the last colon, as the fleet commands take it. A root that holds a colon is
given with its line, or with a final colon and no line. The client moves
only once the other daemon answers through its own socket, passes the verification
above, and has the line, so a project that cannot be reached leaves it where it was.
Without a line it keeps the line's name when the other project has it, and takes that
project's first line otherwise. Everything fetched from the first daemon is dropped,
and the input history stays.

## Other machines

**Decided by core, for the operator's review.** Only the configuration is implemented.
The reach itself is designed here and is not implemented, because it needs a remote
form of the index, a forwarding whose lifetime the client manages, and a real SSH
server to test against, none of which is small.

### The hosts file

A per-user file, `.config/dromon/hosts.toml` under the home directory the password
database gives, or the absolute path `DROMON_HOSTS_FILE` names. Every test sets that
variable to a temporary path. `dromon fleet hosts` lists the hosts.

```toml
[hosts.studio]
destination = "studio.example"      # a host or an alias of the operator's SSH configuration
# remote_dromon = "/opt/dromon/bin/dromon"   # the dromon command there, default dromon
# remote_registry = "/srv/registry"          # its DROMON_REGISTRY_DIR, default its own
```

- **No credential.** The file holds no credential, and any key it does not know,
  `password` or `identity_file` among them, is refused. SSH finds the key, the agent,
  the known hosts and every option in the operator's own configuration, as it always
  does. Dromon never stores a credential and never uses a peer token for this.
- **Checks.** The file must be a regular file of the user that group and others cannot
  write, at most 64 KiB, naming at most 64 hosts. A host's name is an identifier. The
  destination and `remote_dromon` must each be one word without spaces or control
  characters that does not begin with `-`, so neither can be read by `ssh` as an
  option, and `remote_registry` must be an absolute path.

### The design of the reach

1. **The remote index.** `ssh -o BatchMode=yes -- DESTINATION REMOTE_DROMON fleet list
   --json` would print the remote index as the remote client sees it, live entries
   verified there. The `--json` form does not exist yet. The command and its words are
   passed as arguments, never through a local shell, and the remote side runs them
   under the operator's own login, which already holds operator authority over that
   machine's daemons.
2. **One forwarding for each request.** For a routed command the client would run
   `ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes -N
   -L LOCAL:REMOTE -- DESTINATION`, where `REMOTE` is the daemon's operator socket from
   the remote index and `LOCAL` a socket in a fresh directory of mode 0700 that the
   client makes. It would wait, bounded, for `LOCAL` to appear, send the same
   verification and then the same operation through it, and end the `ssh` process.
3. **No new authority.** Whoever can log in as the operator on that machine can reach
   its sockets already. The forwarding carries the operator's requests over the
   operator's own SSH, and never through federation.
4. **Host keys.** Checking stays as the operator's SSH configuration sets it, and the
   client never passes an option that weakens it.

**Failure modes of the design.** A server that refuses Unix socket forwarding, as
`AllowStreamLocalForwarding no` does, fails the forwarding, which `ExitOnForwardFailure`
turns into an exit the client reports. A host that asks for a password fails at once
under `BatchMode`, rather than prompting in a fleet command. A `LOCAL` path longer than
the 103 bytes a socket path allows is avoided by the short private directory.

## Failure modes

| Situation | What happens |
|---|---|
| A daemon crashed and left its entry | Its pid has another start time or none, so the entry is stale, shown so, and never asked |
| The process table cannot be read | Every entry is unknown and never asked, so nothing is sent |
| Another process holds an indexed socket | `status` gives another pid or root, so nothing else is sent |
| A daemon hangs | Each request waits at most 2 seconds, the daemon is reported, and the rest go on |
| Two projects share a name | The name is refused and the root tells them apart |
| A line writes an entry | Every line is denied the index, in every project, which the daemon makes at start so that every engine's sandbox denies it |
| An entry names a live daemon under another project's name | The name must be the root's last step, so the entry is refused |
| Another process answers a restarting daemon's socket | `fleet restart` takes only the successor the index lists, so it waits on and reports |
| A daemon accepts and never answers a restart's polls | The wait ends at its deadline |
| The index cannot be written at start | The daemon starts unlisted, and says so on standard error |
| The index is wider than 0700 | It is refused, and the commands report the reason |
| A test reaches the real index or hosts file | Every test sets `DROMON_REGISTRY_DIR` and `DROMON_HOSTS_FILE`, and a test checks both |
| A hosts file names a credential or an option | It is refused |

## Not verified

- No test runs two real daemons through real sockets in the fleet. The tests stand in
  for every socket and for the process table, since a sandbox denies both. The
  verification step is tested against daemons that give another pid.
- The terminal and the window were not driven by hand through `/project`. The runtime
  and the state machine are tested.
- The Codex profile and the Grok profile check are tested to deny the index the
  daemon made. No real Codex or Grok turn has been run against it.

**Porting note.** Plain files, the registry's lock and bounded reads, and one request
per daemon at a time, which map onto Rust and onto Keleusma's bounded stream
processors unchanged.

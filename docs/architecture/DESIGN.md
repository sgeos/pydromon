# Design

> **Navigation**: [Architecture](./README.md) | [Documentation Root](../README.md)

The planned shape of Dromon, seeded on 2026-10-25 from a design conversation with the
operator. Each point is marked **Decided** when the operator stated it, **Proposed** when
it is the seeding session's suggestion, and **Open** when it still needs a decision.
Nothing here is implemented.

## Purpose

**Decided.** Dromon is a harness that holds several agent session lines at once. It runs
as a daemon, has non-trivial per-project configuration, and lets its session lines
communicate. It grew out of multi-session-line development on Keleusma and other
projects, where agents that could talk to each other did better work. It is built to
solve the communication problem directly, including waking a session that has a new
message, which a shared mailbox alone cannot do. The harness also tracks questions for
the operator as a first-class feature.

## Terms

| Term | Meaning |
|---|---|
| Session line | One long-lived agent working on a line of work, together with its history and state |
| Engine | The model and agent loop that a session line runs on, either an external harness or Dromon's own loop |
| Daemon | `dromond`, the process that owns the session lines, the event log and the client socket |
| Client | A curses or graphical program, or the `dromon` command, that connects to the daemon |
| Wake | Starting a session line's next turn because something arrived for it |
| Operator | A person with authority over a project's lines, addressed as `@name`. A project may have several |
| Operator question | A question a session line sends to one operator, tracked until it is answered, withdrawn or superseded |
| Stream processor | In the Keleusma version, the per-model program that handles one turn or sub-turn step |

## Process model

**Decided.** `dromond` holds the session lines. Clients connect to the daemon. The
operator can also use sessions held inside the daemon. The client command is `dromon`,
whose subcommands cover starting and stopping the daemon, status, attaching an
interactive client, sending messages and handling operator questions.

**Implemented 2026-10-25, roadmap task 3.** Clients reach the daemon through a Unix
domain socket in a runtime directory readable only by the operator's user account, with
no network listener. One daemon serves one project. See
[`../protocol/CLIENT.md`](../protocol/CLIENT.md).

**Proposed.** The client protocol is versioned independently of any implementation, so
a client written against one version keeps working after a port.

**Decided 2026-10-25.** The protocol specification is drafted in this repository and its
mature parts are later ported to the lasting `dromon` repository. See
[`../decisions/NAMING_AND_PORTS.md`](../decisions/NAMING_AND_PORTS.md).

### Starting a child process

**Implemented 2026-11-05, round 15.** Every child the daemon or its commands start goes
through `dromon.spawn`, which bounds the start as well as the run. `subprocess.run` and
`subprocess.Popen` wait for the child to reach `exec` with no bound, and on macOS that
wait stalled for five minutes in several tests and in continuous integration, which
caused most of the flaky runs. **Fact.** That read was where the stalled threads sat.
**Not found.** Why the child did not start, and it does not reproduce in isolation. Two
hypotheses are recorded in the module, and a traceback of every thread at the next hang
would tell them apart.

**Stall diagnostics, 2026-11-05.** The stall also occurred on a Linux runner, so it is
not peculiar to one macOS host. A start that stalls now raises `StartStalled` with a
report, which is written to the daemon's standard error as well. It gives where the
start's thread is in Python, how many starts were in flight together, the number of
threads, and each child of the daemon with its state, its program name, whether it has
executed and what it waits on, from `/proc` on Linux and from `ps` elsewhere. It names
no argument. **Inference.** A child that is stuck before its program runs holds a copy of
the exec-failure pipe of every start that overlaps it, so the report's overlap and
children tell a stuck child from a missing fork. The module records the full reading and
three further hypotheses. No cause is fixed.

**Watchdog and worker threads, 2026-11-05.** The faulthandler dump of a hung Linux run
shows 100 threads, its limit, of which 99 are idle process workers that tests never
stopped, so the main thread is missing. The waiting thread had already returned from its
timed wait, so it was waiting for the interpreter lock. **Hypothesis.** A thread in
`fork_exec` holds the lock while a child stalls before its program, which freezes every
Python thread, the stall report included. `spawn.watch_starts`, which the daemon turns
on while it serves, sets `faulthandler`'s timer, which needs no lock, to dump every
thread a margin past a start's bound. The tests now stop each scheduler's process
workers, and fail if one outlives its test.

**Fork lock, 2026-11-05.** Main failed three pushes running with the stall. **Inference.**
Starting children from more threads at once made it frequent. Every fork in the daemon
process is now made under one lock, `spawn.forking`, which `spawn.install_fork_lock`
puts around `subprocess._fork_exec`, `os.posix_spawn` and `os.posix_spawnp` when the
module is imported, so it covers the engine drivers and the launcher as well as
`spawn.start`. A start that waits 30 seconds for it is refused with `StartStalled`.
Whether this ends the stall is not known, and the watchdog, which times each fork, shows
a stall that remains. A port holds one mutex around its spawn call.

**A stall with the lock in place.** A sandbox gate on macOS stalled a real `git` start at
the exec-pipe read with one start in flight and two threads alive, so overlapping forks
did not cause that one. **Inference.** The child was stuck before its program ran, or the
system was slow to execute it. The report reads children from `libproc` on macOS, which
needs no process, and shows a child still being created as state `I`.

**A second stall with the lock in place, and one hypothesis tested.** A full gate on macOS
stalled again with one start in flight, two threads alive, and one child that had the
daemon's own program name, so it had not yet executed. The report printed its state as
`R`, which round 16 found to say nothing, see below, so whether the child ran or waited
is not known. A hypothesis was that the child closes every descriptor up to the soft limit
`RLIMIT_NOFILE`, which is 1,048,576 here with an unlimited hard limit, and that a million
closes pass 30 seconds under load. **Measured, and not supported.** A start took about 20
milliseconds with the soft limit at 1,048,576 and at 256, for plain starts, with pipes,
with `start_new_session` and `cwd`, with `preexec_fn` and with `pass_fds`, idle and with
twice as many busy processes as cores and thirty idle threads. A million closes of
unused descriptors take 0.34 seconds when called directly. So CPython's child lists the
open descriptors here and does not close each number, and even the worst case is far
below 30 seconds. **Not read.** The `_posixsubprocess` source, which is not on this
machine. The stall report now gives the soft limit, so a stall that does show a large
limit, or a small one, settles it. No limit was lowered, since nothing showed it helps.

**A Linux hang inside the report, and an experiment.** The second CI run on Linux with
Python 3.12 hung five minutes with two threads left, so the thread leak is fixed. **Fact.**
The main thread was inside the stall report itself, in the read of a `/proc` file in
`_proc_children`, called from a start of `git` that had stalled. **Inference.** A read
of a child's `/proc` file waited for ever while the child was in the middle of `vfork`
or `exec`, and Linux CPython starts children with `vfork`. This is the same stall seen
from the parent, and the first evidence of where the child is. **Not known.** Which file
it was, since the dump names only the line that read `stat` of some process. The report
now reads `/proc` on a thread of its own and gives up after three seconds, naming the
file it was reading and the children it had already found, reads only `stat`, `exe` and
`wchan`, never `cmdline`, `environ` or `maps`, and the daemon goes on. **Experiment.**
`spawn.disable_vfork` turns off `subprocess._USE_VFORK` and, since Python 3.15 and
`glibc` make the same kind of child through `posix_spawn`, `subprocess._USE_POSIX_SPAWN`,
on Linux only, so every start of the `subprocess` module forks. If Linux CI then stops
stalling, `vfork` is the cause. If it does not, the change is taken out, since a fork
copies the daemon's page tables and is slower. Direct calls of `os.posix_spawn` in this
repository are not changed, and the fork lock still covers them. **Result, recorded
after the review of pull request 149.** Linux CI went on timing out with the switch, so
it is off by default again and applies only with `DROMON_NO_VFORK=1`.

**Round 16, the root cause of the stall is not found, and what is known.**
*Ownership.* `src/dromon/spawn.py`, `tests/test_spawn.py`, `tests/test_spawn_stress.py`
and the spawn fixtures of `tests/conftest.py` belong to the runtime line. Core owns the
callers, the scheduler and its tests, and asks for a change in the helper by mail.
*A correction.* On macOS the state that `libproc` reports for a child is `R` for any
process that exists, a plain `sleep` child showed it, so the earlier reading that the
stalled child was running was unfounded. The report now prints `live` for it, and gives
the processor time of each child and how many of its threads run, from `proc_taskinfo`
on macOS and from `utime` and `stime` on Linux, which do tell a child that waits from
one that spins. A test starts a spinning and a sleeping child and checks both.
*What the Linux logs of 2026-11-08 and 2026-11-11 show.* **Facts.** Each of the two
Linux logs that show the timing, one on Python 3.12 and one on 3.15, spent about two and
a half minutes in `tests/test_sandbox_facts.py`, twelve tests that take half a second on
macOS. The timeout of
five minutes then fell in `tests/test_stale_brief.py` after thirteen tests had passed on
3.15 and six on 3.12. The dump on 3.15 shows the one live worker, `asyncio_0`, at the
start of `dirty_state` in `posixpath.realpath`, before any `git` start, and ninety-nine
idle `dromon-processe` threads that hide the main thread behind the hundred that
`faulthandler` prints. A dump on 3.12 shows the worker inside the stall report, in a
read of `/proc`, the main thread running the test's `settle` loop. **Inferences.** The
worker at the start of `dirty_state` shows that the test was making calls and not
waiting in one start, so a loop that never ends, or a run so slow that each step takes
long, fits it and a single hung start does not. The same two and a half minutes on two
interpreters shows a slowness that is systematic on the runner and not a chance stall.
**Hypotheses, none confirmed.** Starts of `git` or `ps` on the Linux runner are slow
for a reason that is not in this repository, the runner is starved of processor or
input and output while the tests run, or `vfork` holds every Python thread frozen while
a child loads its program, which `DROMON_NO_VFORK=1` removes on Linux.
*What now records the answer.* Each start that takes two seconds or more is written to
the daemon's standard error and, in a test run, straight to the terminal past pytest's
capture, and is listed at the end of the run, with where the time went, how long it
waited for the fork lock, how long it was in the call that forks, how many starts
overlapped, and the load of the machine from `os.getloadavg` and, on Linux, the pressure
stall figures of `/proc/pressure` for processor, input and output and memory. A stall
report says whether the fork call has returned, which is the difference between a
thread stuck in `vfork` and one waiting for the program, and gives the same figures.
*The stress test.* `tests/test_spawn_stress.py` starts children from several threads at
once by `spawn.start`, `spawn.run` with pipes, `os.posix_spawn` and `asyncio`, with a
ticker thread that measures how long every thread was frozen, with the fork lock and
without it. On macOS, 24,000 starts at a load of 60 to 70, from 8 and from 16 threads,
gave no stall with the lock and none without it, so the stress test did not reproduce
it and no rate before and after can be given. A count from the environment makes a larger
run possible. The run writes one line for each variant to the summary of the run.
*A stall reproduced on macOS by the sandbox gate.* **Facts.** One run of the whole
sandbox gate on 2026-11-11, with Python 3.15.1 at a load of 24 to 34 and swap about
four fifths used, ended in `StartStalled` for `git config core.hooksPath` in
`tests/test_land.py`. The same file passed at once when run alone, 57 passed. This is
the first stall seen off continuous integration, about one in the 2,700 starts that
core counted for the suite, and the stress test, which gave none in 24,000, is a poorer
reproduction than the suite. The report shows the start thread in `_execute_child` at
the read of the pipe that waits for the program, so the fork call had returned, and
shows the fork lock asked for, taken and released within a hundredth of a second. Two
threads were alive, the main thread and the start thread, so the daemon was not
multithreaded at that moment apart from native threads. The one child still ran the
daemon's own program, with no processor time recorded and none of its threads
running. **Inferences.** The parent was waiting and not stuck, so `vfork` holding the
interpreter lock does not apply, since macOS has no `vfork` path here and the report
shows the call returned. The child had not reached `exec` and was not running, so
it was blocked between the fork and the program, which the report cannot place. A lock
held by another thread at the fork is unlikely with two Python threads and is not ruled
out for native ones. **Hypotheses.** The child waited on memory, since it needs pages
of a large parent to be read in or copied while the machine swaps, which fits the load
and the swap, or it waited on a lock taken in the child by code that runs after a
fork. Neither is confirmed. The report now adds the resident size, the page faults,
the pages read in, the copy-on-write faults, the Unix system calls and the context
switches of each child, so the next stall shows whether the child was paging and how
far it got. **Not done.** A fix. Replacing the fork with `posix_spawn` for the daemon's
starts is the candidate, because it does not run the parent's code in the child, but it
needs an absolute program path, `close_fds` off and a replacement for the `Popen`
conditions, and was not built without a way to measure its effect.
*What the newest Linux logs add.* **Facts.** Four failing Linux logs on Python 3.12 and
3.15, of 2026-11-05, 2026-11-08 and 2026-11-11, all show `tests/test_sandbox_facts.py`
taking between 90 and 180 seconds, and the green run of 2026-11-05 shows it taking five
seconds. The file started no child at all when run on macOS with a counter on
`subprocess.Popen`, so its slowness is not in a child start. The run of PR 145 on
2026-11-11 spent 969 seconds in `tests/test_stale_brief.py`, and its failing assertion
was a `ReadinessUnknown` for `git could not start`. PR 144's re-run at its head showed a
timeout with the start thread in the read loop of `communicate` for `git status`, that
is a child that had started and had not finished. Other files around these two took
their usual seconds in the same runs. **Inferences.** Two files that run a scheduler
turn with `asyncio.run` are slow on the runner and the rest are not, so the cause is more
likely in what a turn does on Linux than in the start of a child. At 30 seconds a
stall, 969 seconds is about thirty stalls, which fits a state where every start of `git`
in those tests stalls and not an occasional one. **Hypotheses, none confirmed.** A
step of a turn waits on a timeout on Linux only, or the runner is starved at those
minutes. **What now records the answer.** The tests write one line for each test that
took five seconds or more, past capture, with its name and the load figures, so the next
slow run names the test and does not leave a file's line unwritten until the next file
begins.
*The logs of pull request 149, the first with the ledger.* **Facts.** Both Linux jobs, on 3.12 and 3.15, were killed by the five minute timeout. On 3.12, `test_a_brief_still_current_heads_no_turn` took 244 seconds and recorded two starts of `git` of 19.6 and 16.6 seconds, with 8.8 and 4.1 seconds inside the fork call, none waiting for the fork lock, one start overlapping, three threads, load about 1.1, and processor, input and output and memory pressure at zero. Slow tests of `test_sandbox_facts` took 6.6 to 45.7 seconds on 3.12 and 9.4 and 77.1 seconds on 3.15, with no slow start recorded at all. The 3.12 timeout dump shows the note's own thread inside a `/proc` read, and a worker waiting in the start. The 3.15 dump shows a worker in `os.path.realpath`. Pull requests 147 and 148 passed both Linux jobs in the same hour, and neither has the switch to fork. **What this refutes.** The switch to fork on Linux did not stop the timeouts, which the pull request 144 re-run had already shown, so `vfork` is not the whole cause. **What it does not show.** That fork makes it worse, which is the reviewer's inference. It is consistent with forks of seconds at no pressure, and it is also consistent with a runner that did not run the process for seconds, since a read of `/proc` and a call of `realpath`, which copy nothing, were slow in the same runs. **Changes.** The switch is off by default, as above. The ledger missed the starts of `test_sandbox_facts` because it saw only `spawn.start`, so every call that forks is now timed where it passes, in `forking`, and a call of `SLOW_FORK_SECONDS` or more is recorded with the processor time the thread itself used in it, which tells the kernel copying a large process from a thread or a machine that was not running, the size of this process, the steal time that a virtual machine's host took, and how long the control group's processor quota stopped it. **Hypotheses, to be tested by the next Linux log.** A fork call of seconds with processor time near the whole is a copy of a large address space. One with processor time near zero is a host or a quota that did not run the runner, and the steal and throttle figures should then show it, and `vfork` would not help. **Not done.** `posix_spawn` with an absolute program path, which avoids both a fork and the exec pipe, since it cannot be tested here for Linux.

*More macOS stalls, on 2026-11-11.* **Facts.** (1) Pull request 147's macOS job on Python 3.15.2 failed twice, at 19:24Z with one setup error and at 19:40Z with one failed test, each `StartStalled` for `git` after 30 seconds, in a `test_land` fixture and in a commit run test, among 6663 passed tests each time. The operator also reports one 30 minute cancellation of that branch, whose log was not examined. (2) Another line's full gate on this Mac met the same stall in a `test_code_staleness` fixture earlier the same day. (3) This line's sandbox gate on this Mac, with the fork lock, stopped at the five minute timeout in `test_a_timed_out_ps_at_stop_is_reported_and_exits_unsuccessfully`, in the wait on the exec pipe inside `_execute_child`, called from the tests' own `Popen` in `tests/test_launcher.py`, which does not go through `spawn.start`, so nothing bounded it and no stall report was written. The exec pipe wait is after the fork call has returned, so the fork lock was not held. The machine was at memory pressure warning and load above 13 at the time, which is one observation. (4) **Pull request 147 does not have the fork lock, the ledger or the vfork change.** Those are on this branch only, which a search of `origin/main` and of 147's `spawn.py` shows, so the lock is not what 147 changes and is not shown to be absent from the cause. (5) What 147 does change is fewer starts, since it answers `git status` from the tests without starting git, and it stalled twice in 15 minutes with the fewest starts of any branch in the hour. Pull requests 148 and 149 passed macOS 3.15 in the same hours. (6) **A stuck child, read from outside.** A second sandbox gate of this line stopped at the same place, the wait on the exec pipe, in a direct `subprocess.run` of a fresh interpreter in `tests/test_spawn.py`. A `python3.15` process with parent 1 and a start time within seconds of the last output was found with the module's `libproc` reader and `lsof`. It had used 0 milliseconds of processor time, had 0 of 2 threads running, a resident size of 0 megabytes, 222 faults, 80 of them copy on write, and 28 system calls. Its mapped libraries were the extensions that pytest had loaded, and its working directory was the worktree, so it was a copy of the test process that had not yet run the new program. Its descriptors showed the capture pipes already installed as standard output and error, the exec pipe, and the directory `/dev/fd` open for reading, which is how the child side of `_fork_exec` closes every other descriptor. **Inference.** The child stalled in or just before the close of descriptors, after `dup2` and before `exec`. It is the first observation of the stuck child's own state, and the identification by start time is not certain. `sample` of it was refused for want of privileges, and the process had ended when I looked again, so no stack was read. **Hypothesis, unproven.** The child blocks on the enumeration of `/dev/fd` or on a lock in the C library just before it. **Next evidence.** Run `sample PID` outside the sandbox on the next stuck child before anything ends it. The notes are in this line's scratch directory. **A test of the descriptor step.** Not made, since a child cannot be made to stall there on demand. **Inference.** These are the stall of rounds 15 and 16, a child that was made and did not reach its program, on the macOS fork and exec path. A stall rate per start that is the same on every branch cannot explain two on the branch with the fewest starts, so the rate is not per start alone and may depend on the runner or the hour, which three observations cannot settle. The fork lock was present in only one of the three, and that one stalled after the lock was released, so the lock does not cover the stall seen on macOS. **Not shown.** The rate, the state of the stuck child in these, and whether load raises it. The stress test saw none in 24,000 starts, so a quiet machine is not the condition. **Hypothesis, unproven.** A forked child of a process with other threads waits on a lock that a thread held at the fork, which fits a child with no processor time and is the classic hazard. The fork lock serializes starts and cannot cover threads that are not starting children. **For the owner of 147.** Look for what the branch changes in threads that run while tests start children, such as the scheduler workers that the conftest fixture stops, and in the number of descriptors or the size of the process, rather than in the number of starts. **Consequence for tests.** Starts that bypass `spawn.start`, 18 in `tests/test_launcher.py` among them, are unbounded and take the whole five minutes, so moving a harness onto `spawn.start` turns the stall into a bounded failure with a child report. **Done at the operator's instruction.** The eight in-process starts of `tests/test_launcher.py` now go through `spawn.start` and `spawn.run`, and a test fails on any direct call to `subprocess.Popen`, `run`, `call`, `check_call` or `check_output` in that file, in code and not in the scripts held in strings. **Not done.** Other test files that start children directly, `tests/test_operator_relay.py`, `tests/test_check_script.py`, `tests/test_runs.py` and `tests/test_changelog_fragments.py` among them, whose starts remain unbounded. The list in `BOUNDED_TEST_FILES` is where to add a file once it is moved.

*The child's descriptor step, and a change on macOS, 2026-11-11.* The operator proposed that the stuck child of fact 6 waited in the closing of descriptors, which the following checks bear on. **Facts, read from the interpreter builds on this Mac, not from `_posixsubprocess.c`, which is not on this machine.** The `libpython3.15.dylib` of Python 3.15.1 holds the function `_close_open_fds_maybe_unsafe`, and its disassembly calls `opendir`, `dirfd`, `readdir`, `close` and the module's own `_close_range_except` and no other library function that allocates or locks, and does not call `proc_pidinfo`, whose three callers are elsewhere in the library. The 3.12.12 build imports `opendir`, `readdir` and `closedir` and not `proc_pidinfo`. The name says that CPython itself marks the function as maybe unsafe after a fork. **Inference.** With `close_fds` set, which is the default, the macOS child opens `/dev/fd` and reads it before it runs its program, which is what the stuck child showed. **Hypothesis, unproven.** It waited on a lock that another thread of the daemon held at the fork, in `opendir` or in the library's own locks, which a copy of one thread cannot release. This line does not know which lock, and macOS's allocator takes its own locks around a fork, so the allocator alone may not be it. The stuck child had already opened the directory, so the stall may equally be in `readdir` or in a `close`. A stack of a stuck child, from `sample`, would settle it, and none has been read. **Change.** `dromon.spawn` now starts a child with `close_fds` off on macOS and does the equivalent in the parent. Inside the fork lock, immediately before the call that forks, and for the thread making the start, it lists `/dev/fd` in the parent, makes non-inheritable every descriptor above 2 that is inheritable and not to be passed, and makes the descriptors the start passes inheritable, then calls `Popen` with `close_fds=False`. It makes the passed descriptors non-inheritable again when the call returns or fails. The child then runs no enumeration, and `subprocess` may use `posix_spawn`, which runs none of the parent's code in the child. **Review of audit, findings 1 to 3.** The first version swept before the wait for the fork lock and released after the call, so another start that forked in between gave its child a passed descriptor, a start that stalled before its fork kept the descriptor inheritable until it ended, and a descriptor made inheritable during the wait reached the child. Doing both inside the lock removes the three, since no other fork can run while the lock is held, a start that has not forked has marked nothing, and the sweep runs after the wait. If the directory cannot be listed before the start, it falls back to the usual child-side closing, and if it cannot be listed inside the lock the start fails with an `OSError` and makes no child. `DROMON_CHILD_CLOSES_FDS` set to `1` or `0` forces either setting on any platform, and the slow-start ledger now says which setting a slow start had and how many descriptors the sweep cleared. Linux keeps the usual setting, because its closing uses `close_range` or a read of the directory by system call, and its stalls have a different symptom. **Tests.** A descriptor made inheritable in the parent does not reach the child and is made non-inheritable, a passed descriptor reaches the child and no other does, a start that has not forked leaves its passed descriptor closed to another start's child, a descriptor made inheritable while a start waits for the lock is cleared, a failed fork leaves the passed descriptor non-inheritable, a passed descriptor that is not open fails the start, an unlistable directory falls back, and a listing that fails inside the lock makes no child. **Residual.** (1) It is not known that this removes the stall, and the stress test cannot tell, as it never reproduced the stall. (2) A descriptor made inheritable by another thread in the few instructions between the sweep and the fork would reach the child, and nothing in this repository makes one inheritable except this module. (3) Starts that do not go through `spawn.start`, the engine drivers and the relay on the event loop, the launcher and the tests that start children directly, still close descriptors in the child. (4) The sweep changes a flag of the daemon's own inherited descriptors for good, which nothing here relies on. (5) A Rust port sets close-on-exec on every descriptor it opens, so it needs no sweep.

*The Linux logs of pull request 149 at `c7151e1`, read on 2026-11-11 at the operator's request.* **Facts.** On Python 3.12 and 3.15 the starts of `git` in `tests/test_stale_brief.py` took 8 to 99 seconds, with fork calls of 0.4 to 5.7 seconds in which the starting thread used 0.0 to 0.1 seconds of processor time, at a load of 1.0 to 1.3 with every pressure figure at zero and no steal or throttling, so the thread was not running while the call lasted. The only dump of all threads, the 3.12 timeout, shows the main thread running Python in `Scheduler._due_wakes` under `step`, the `dirty_state` worker and a diagnostic thread each inside `read_text` at the call of `open`, and no other thread. The 3.15 stall reports show the start thread inside `Thread.start` waiting in `_say`, and inside `open` of a `/proc` or cgroup file. Each is a call that returns from the operating system. In `tests/test_stale_brief.py` the scheduler is built with `tick_seconds=0`, and `Scheduler.run` then ends each step with `await asyncio.sleep(0)` for as long as a turn is running, so the main thread steps flat out, and each step copies the state several times through `_state`. **Inference.** A load of one with idle pressure fits one thread of the test process running without rest. A thread that is waiting to take the interpreter lock back after a call appears in a dump at that call, so the threads at `open`, at `Thread.start` and in the fork may be waiting for the lock and not for the operating system, which would fit no processor time. **Hypothesis, not tested.** The spinning main thread starves threads that make many short calls, such as a start. It is not explained why a switch interval of five milliseconds allows waits of a minute, and nothing here reproduces it, since a spinning event loop beside a worker that starts forty children slowed the starts on macOS by nothing measurable. **This is a different symptom from the macOS stuck child**, which had no processor time and was not running its program, so there may be two causes. **Change.** A start that runs longer than `SNAPSHOT_SECONDS`, five seconds, now records one line with every thread's name, processor time on Linux, and innermost three frames, the busiest first, to the log and to the slow-start ledger. The next slow start in continuous integration then shows which thread used the processor and what the others waited in. **Not done.** Any change to `Scheduler.run` or to the tests' `tick_seconds=0`, which is the owner's, core's, to decide, and a test of the starvation, which needs Linux.

*A leaked matcher child, found by the sandbox gate.* **Fact.** The macOS sandbox gate on a loaded machine (load above 14) failed `test_the_child_is_killed_when_the_start_is_cancelled` with the child's return code unset, and a still-running subprocess at teardown. A probe that holds `spawn.start` for 0.5 seconds after the child exists, and cancels `runparams.match` inside that window, left the child running before the change and killed it after. **Cause.** `match` awaited the worker thread's start with no handle to the child, so a cancellation that arrived after the child was made and before the call returned dropped the only reference. **Change.** The start is shielded, and on cancellation `_end_late_start` waits for the worker, bounded by `START_SECONDS`, and kills and reaps the child and closes its pipes, which the gate showed as a resource warning when the first version did not. A start that failed made no child. **Review of round 16, finding 2.** A second cancellation during that wait used to end it early and leave the child running with its pipes open. `_until_done` now absorbs cancellations while the start and the reap finish, and the first is raised again afterwards, so the delay is the start's own bound. Removing the pipe closing, or letting a second cancellation end the wait, each fails a test. **Residual.** The same shape exists wherever a call on a worker thread returns a process to the loop, and only `runparams.match` was changed. **Inference, not shown.** This is a different defect from the stall, and it does not explain the Linux timeouts, whose children were all started by the start thread with a handle held. A Rust port needs the child owned by the task that starts it, so that a cancelled task drops it and the drop kills it.


*Review of round 16, by the audit line.* **Facts, each shown by a test that a mutation fails.** (1) An allow entry that is a link, or sits under one, was given the link's target, so a writer line that re-linked its own reports directory could grant itself the target at its next engine build. Only the deny lists and the working directory now gain a real form, and `docs/protocol/SANDBOX.md` records the residual that remains. (2) is in the paragraph on the leaked matcher child above. (3) The slow-start line and the stall message were written on the start's thread before the outcome was handed over, so a standard error that blocks turned a good start of 2.2 seconds into `StartStalled` after 16. The slow-start line is now written after the handoff, and both lines go through `_say`, which uses one thread of its own, waits `LOG_WAIT_SECONDS` for it, and writes a slow-start line at most once in `LOG_INTERVAL_SECONDS`. **Follow-up 3, from the re-review.** It first dropped any line that arrived while another was being written, and a stall report was dropped behind a slow-start line. Lines are now queued, at most `MAX_LOG_PENDING`, and written in order by the one writer thread. When the queue is full the oldest rate-limited line goes first and an unlimited line, such as a stall report, only when every queued line is unlimited, and every drop is counted. A standard error that never accepts a line therefore costs one thread and sixteen lines, and a start still waits at most the bound. The ledger still keeps every slow start up to its bound. (4) A start on an event loop's own thread, as `asyncio.create_subprocess_exec` makes, took the fork lock and could wait the whole `FORK_LOCK_SECONDS` for another thread's stuck fork. It now waits `FORK_LOCK_LOOP_SECONDS` and ends in the same `StartStalled`. The lock is not reentrant, so a start made by the thread that holds it is refused at once as a `RuntimeError`, and a test runs every start path to show none nests. (5) The stall message and the refusal past the stranded cap name the program by its base name. **Follow-ups of the re-review.** (1) `test_the_fork_call_records_the_threads_own_processor_time` failed once at a load average near 9, since it needed 0.2 seconds of processor time in a 0.3 second spin. The spin now runs until the thread's own clock has advanced a fixed amount, with a sixty second bound against a hang, and the sleeper is compared with the spinner. (2) Nothing pinned the Linux `vfork` default, since a run on macOS never sees it change. The import-time rule is now `apply_vfork_setting(environment, platform)`, and tests name the platform, so a change that turns `vfork` off for everyone fails on any machine, and a source check shows that the import goes through it. (3) As above.
**Residual.** The loop can still wait `FORK_LOCK_LOOP_SECONDS`, a mutex cannot be made not to delay it, and a Rust port avoids it by making the start the task's own. **Inference.** None of these explains the Linux timeouts.

The start bound, `START_SECONDS`, is named once. A start past it fails at once with
`StartStalled`, a `TimeoutError` and so an `OSError`, whose message names the program and
the bound. The child is killed and reaped should it start later, and at most four stuck
starts are tolerated before no further child is started. The run keeps the semantics of
`subprocess.run`, with a required timeout. **Known residual.** The `asyncio` starts of
the engine drivers run `Popen` on the event loop's own thread and are not covered, so a
stall there stops the daemon's loop, and the launcher's start is exempt because a parent
death signal on Linux belongs to the thread that forked. A test pins both lists. See
`src/dromon/spawn.py`.

**Review of 151cbcd.** The count of stuck starts frees a slot as soon as the thread of
its start ends, and the scheduler tells the default operator once, in a note that holds
no line, when the count reaches the bound. A start that never returns holds its slot
until the daemon restarts, and the note says so. The reads of the process table that a
stop or a hold depends on are started as `essential`, which the bound does not refuse,
up to a ceiling of 32 stuck starts that keeps the threads bounded. A late child is
killed with its process group when it leads one, never the daemon's own group, and its
reap waits at most ten seconds. The group kill covers only a child that leads its own
group, as one started with a new session or its own process group does. A child that
shares the daemon's group is killed alone, and its descendants are not reached, since
signalling the group would kill the daemon. **Past the ceiling.** At 32 stuck starts
the `essential` reads are refused too, so the process table cannot be read and a stop
by pid, which needs it, is refused with the reason. The operator's note at the bound of
four is the warning, and a restart is the only recovery. A command past its timeout
keeps the last 64 KiB of what it wrote on the `TimeoutExpired`. **Exempt and unbounded.** `client.start_daemon`
starts the daemon with `os.posix_spawn`, which has no pipe to wait on, and it is the
call the module's first hypothesis names, so that candidate is neither bounded nor
excluded.

## Session lines and engines

**Proposed.** Coordinate existing harnesses rather than replace them, so that each model
runs in the harness its vendor tuned for it. Dromon then owns the loop around each
engine, which is what makes wakes possible. Candidate interfaces, with how far each was
checked.

| Engine | Interface | Evidence on 2026-10-25 |
|---|---|---|
| Grok Build | Agent Client Protocol | Implemented 2026-10-25 for Grok 4.7 from Grok Build 1.0.70's documentation, not verified against a live turn |
| Pi | Its RPC mode, JSON lines over standard streams | Implemented 2026-10-26 for GPT-6-Luna from Pi 1.4.0's documentation and installed source, not verified against a live turn. Dromon's tools only, since Pi has no sandbox |
| Antigravity | The Agent Client Protocol through Google's ACP runtime, `agy_acp_server`, sharing `dromon.acp` with Grok Build | Moved from print mode on 2026-10-26 by the operator's decision, from the protocol's schema and a second implementation, not installed. Dromon's tools reach it as an MCP server and a tool that asks is refused before it runs. Refused in a daemon, since a tool that does not ask is stopped only after it starts, and every probe turn refused, since the runtime names no credential, unless the operator confirms the sign-in by hand with `--accept-unverified-credential` |
| Cursor Agent | Print mode with `--output-format stream-json` | Implemented 2026-10-26 from Cursor Agent 2026.10.01's help and installed code, whose help lists no `acp` command. Refused in a daemon, and run only by `dromon probe --accept-partial-isolation`, by the operator's decision |
| Claude Code | Claude Agent SDK | Implemented 2026-10-25 with Agent SDK 0.2.174 and Claude Code 2.1.310, on the subscription |
| Codex | Its app-server | Implemented 2026-10-25 with Codex 0.164.0 and GPT-6-Luna, on the ChatGPT plan |
| Dromon's own loop | Direct model calls | Needed where full control of each step is required |

**Decided 2026-10-25.** The first adapter targets the model Claude Opus 5.5. The operator
prefers Dromon's own loop calling the model directly, on the condition that it can run on
a Claude subscription as well as on separately billed application programming interface
usage.

**Fact, read on 2026-10-25** in Anthropic's Claude Code "Legal and compliance" page and
the Agent SDK overview. Subscription sign-in is "intended exclusively" for subscribers'
"ordinary use of Claude Code and other native Anthropic applications". Developers,
including those using the Agent SDK, "should use API key authentication", and "may not
collect, store, or intermediate Claude.ai credentials or session tokens". Signing in to
the unmodified Claude Code binary with one's own subscription is permitted.

**Inference.** Dromon's own loop calling the Messages API can run only on an API key with
usage billing, so the operator's condition is not met for it. A subscription can drive a
line only through the unmodified Claude Code binary, through its headless mode or the
Agent SDK, with Dromon never reading or storing the sign-in token. Whether the Agent SDK
itself may run on a personal subscription is not stated plainly. The same page says Pro
and Max limits "assume ordinary, individual usage of Claude Code and the Agent SDK",
while the Agent SDK overview points developers to API keys.

**Decided 2026-10-25 by the operator.** The operator's only credential is the
subscription, so lines run on Claude Code through the Agent SDK, driven from outside by
Dromon. The per-model drivers are written in Keleusma and run through `pykeleusma`.
**Later the same day,** the operator directed that OpenAI models run on Dromon. Codex on
the operator's machine is signed in to a ChatGPT plan, which Codex lines use, as the
Codex section below records.

**Implemented 2026-10-25, roadmap task 4.** `dromon.engine` runs each turn through
`ClaudeSDKClient` with no user or project settings and no tools unless configured. It
refuses an environment that selects an API key, an API profile or a cloud provider, and
refuses a turn whose engine reports any `apiKeySource` other than `"none"`. Each engine
message becomes an event for the line's driver, whose actions the adapter performs.
`dromon probe` runs one short turn and reports the engine's capabilities. The driver
interface is [`../protocol/DRIVER.md`](../protocol/DRIVER.md).

**Fact, observed live on 2026-10-25.** With a claude.ai login, a Claude Code session
offers the account's connectors, such as mail sending and file sharing, even with
`setting_sources=[]` and no built-in tools. Lines therefore run with
`strict_mcp_config=True`, a permission gate that allows only the line's own tools, and a
check that the engine reports no tool beyond them.

**Decided 2026-10-25 by the operator.** Arbitrary MCP servers can be exposed to a line,
under an allow-all or a whitelist model, with the settings held per line. This is
[`../protocol/LINE_CONFIG.md`](../protocol/LINE_CONFIG.md), stored in the event log as
`line_configured`. The default remains no tools beyond Dromon's own.

**Decided 2026-10-25 by the operator.** Drivers are concrete. Subscription use, API use
and flexible use are separate drivers that bake their strategy in, rather than one
driver choosing behaviour from parameters. Claude Haiku 5.5, the least expensive
subscription model, is used for testing.

Each adapter should probe the engine's capabilities at startup, because support varies by
engine version.

### Codex and Grok Build against the Claude Code conclusions

**Reviewed 2026-10-25 at the operator's request**, which asked whether the conclusions
above for Claude Code hold for Codex and Grok Build. Vendor documents were read on that
day. xAI's consumer terms refused the request, so nothing here rests on them.

| Conclusion for Claude Code | Codex | Grok Build |
|---|---|---|
| Dromon's own loop cannot use the subscription | Does not hold, see below | Unknown |
| Dromon never reads, stores or forwards the sign-in token | Holds when Dromon drives the Codex binary | Holds, as the conservative default |
| Unattended subscription use is unclear | Similar, API keys are the recommended default | Documents invite automation |
| Per-token billing must never start silently | Holds, with OpenAI's variables | Holds, with xAI's variables |
| Account tools must be kept out of a line | Holds, without an equivalent isolation switch | Holds, without an equivalent isolation switch |

- **Fact.** OpenAI's Sign in with ChatGPT documents ChatGPT plan usage "for open-source
  and locally hosted apps", which may request "permission to use the user's ChatGPT
  plan for eligible Responses API requests". The host app then holds and renews the
  OAuth tokens itself. Source <https://developers.openai.com/siwc/token-sharing-open-source>
  and <https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server>.
- **Inference.** Dromon is open source and runs locally, so its own loop could run
  OpenAI models on a ChatGPT plan, which Anthropic's terms rule out for Claude. Doing so
  would make Dromon hold tokens, the reverse of the Claude rule, and needs the
  operator's decision. The program and the app-server it pairs with are described as
  preview and "experimental".
- **Fact.** OpenAI's Codex authentication page says "API keys are still the recommended
  default for automation", and its app-server page says to use the Codex SDK for jobs
  and CI. Codex keeps its login in `~/.codex/auth.json` or the system credential store,
  and says to "treat ~/.codex/auth.json like a password". Source
  <https://learn.chatgpt.com/docs/auth> and <https://learn.chatgpt.com/docs/app-server>.
- **Fact.** xAI's Grok Build overview calls headless use "ideal for scripts,
  automations, or integration into other apps" and names the Agent Client Protocol for
  use "in other apps". xAI's own posts endorse subscription sign-in inside OpenCode and
  Kilo Code. No xAI program for other apps to use the subscription was found. Source
  <https://docs.x.ai/build/overview> and <https://x.ai/news/grok-opencode>.
- **Fact.** On the operator's machine, the user configurations of both Codex and Grok
  Build declare mail, document and calendar MCP servers.
- **Inference.** A line on either engine started on the operator's configuration would
  inherit those servers. A separate configuration directory would also leave the login
  behind, and copying the login would breach the token rule, so isolating a line is an
  open design problem for both. Codex's `-c` overrides may disable servers by name.
- **Owed before a Codex or Grok line runs.** The environment check and the reserved
  variables of `SETTINGS.md` name only Anthropic's credentials, and must add at least
  `OPENAI_API_KEY` and `XAI_API_KEY`, and each adapter needs a way to read the
  credential the engine actually used, as `apiKeySource` does for Claude Code. A
  third-party report, not verified, says a Codex session on a ChatGPT plan may fall back
  to a smaller model at a limit, which the driver's model check exists to catch.
- **Concrete drivers carry over.** Each model and credential strategy keeps its own
  driver. Dromon's own loop on OpenAI's program would add a credential the current
  strategies do not name.

**Implemented 2026-10-25, on the operator's direction.** Drivers for GPT-6-Luna on
Codex are packaged in all three strategies, under driver interface 3, which adds the
engine each driver is written for.

**Implemented 2026-10-25, on the operator's direction to run OpenAI models.**
`dromon.codex` runs a line through `codex app-server`, as
[`../protocol/CODEX.md`](../protocol/CODEX.md) specifies. The items owed above are met
as follows. The account Codex reports is the credential handed to the driver. A
subscription line's Codex starts without API variables, and the reserved variables name
OpenAI, Codex and xAI prefixes. Every MCP server Codex lists is disabled by name, and
connector features are disabled, then verified. Dromon's tools reach the model as
dynamic tools. The app-server was chosen over `codex exec`, which OpenAI recommends for
automation, because only the app-server lets Dromon serve its own tools in process and
see each item as it starts. **Porting hazard.** The app-server and dynamic tools are
experimental. **Later the same day,** Codex lines gained write access under a Dromon
permission profile that denies Dromon's state and runtime directories and keeps `.git`
read-only, as `CODEX.md` records.

**Implemented 2026-10-25, on the operator's direction for nominal Grok support while
Grok usage was exhausted.** `dromon.grok` runs a line through `grok agent stdio` over the
Agent Client Protocol, as [`../protocol/GROK.md`](../protocol/GROK.md) specifies. Grok's
custom sandbox profiles live only in its own files, so the operator defines Dromon's two
profiles and Dromon checks them before each turn. ACP has no client-hosted tools, so
Dromon's tools reach a Grok line through `dromon mcp --line` and a per-line token on a
line socket. Every behaviour that could not be confirmed refuses the turn.

## Communication

**Decided.** Communication has three layers. In the operator's words, they are
inter-harness, across harnesses on the same machine, and across harnesses on different
machines. The first layer is between session lines inside one Dromon daemon.

| Layer | Proposed transport | Trust | Wakes |
|---|---|---|---|
| 1. Inside one daemon | In-process queues | Full | Guaranteed, because the daemon owns every loop |
| 2. Across harnesses on one machine | Unix domain socket, MCP or the Agent Client Protocol | The local user, checked by the socket's peer credentials | Only for harnesses Dromon drives. A harness it does not drive still cannot be woken |
| 3. Across machines | A link between daemons | Needs real authentication, such as mutual TLS, and replay protection | Delivered to the remote daemon, which wakes its own line |

**Proposed.** One message envelope serves all three layers. Each message carries an
identifier that makes a resend harmless, so a message can cross from layer 1 to layer 3
without being rewritten. Each layer adds only its routing and its delivery guarantee.

## Wakes

**Decided.** The harness issues wakes.

**Decided 2026-10-25 by the operator.** Delivery is tick based, and messages are not
interrupts. A message sent in one turn is received no sooner than the next. Mid-turn
injection is therefore not used.

**Decided 2026-10-25 by the operator, refining the above.** The scheduler is a game
loop. Every tick iterates over all lines and updates each once. Messages sent during a
tick are collected in a sent queue, and at the end of the tick they move to the post
office, which makes them valid for delivery from the next tick on. A busy line is
skipped, and that counts as its update for the tick.

**Implemented 2026-10-25.** Each tick visits the lines in a fixed order. An idle,
line under its cap with post office mail starts a turn, which runs as a task
without the tick waiting for it, so a turn may span many ticks and a slow line never
holds the others back. Ticks follow every `--tick-seconds`, one second by default,
while anything is in flight, and the loop rests when nothing is. After a restart every
undelivered message starts in the post office. Lines send through an in-process
`send_message` tool whose sender the daemon fixes, and operators through `dromon send`.
A per-line turn cap stops a runaway exchange until the conversation limits of task 7.

**Round 15, item 5. No turn that only repeats an answer.** A line can reply to a
message by its identifier before any turn has shown it, and the message stays
undelivered. A tick used to start a turn to show it, and a release of a held line did
the same, so the line was woken only to answer what it had answered, and the turn
counted against its limits. A tick now delivers such mail without a turn when every
message waiting for the line has been replied to or acknowledged, and the release of a
held line takes the same path. One unanswered message among them starts the turn as
before and the answered ones are shown with it. A compaction wake and a restart note
always start their turn, because the turn is how they take effect. **Limit.** The rule
reads the line's own reply, so it cannot tell a reply made by mistake from one made
with understanding. Reading a message delivers it, so a message the line read never
waits.

**Inference.** The tick is the unit a batched update loop of Keleusma stream processors
schedules, so the Keleusma version can keep these semantics exactly.

**Proposed.** A wake policy in per-project configuration, for example every tick or
every few ticks.

## Operator questions

**Decided.** The harness tracks questions for the operator as a first-class feature, so
that questions are no longer lost in responses and tracking files.

**Decided 2026-10-25 by the operator.**

- `ask_operator` sends a message to an operator's mailbox, and the operator replies as
  another session line would. The reply reaches the asking line as an ordinary message
  and wakes it.
- The operator is not a singleton. A project may have several people and many session
  lines, and every operator is addressed separately.
- Questions can be forwarded to channels such as email or Slack through a general
  interface that adapters implement for each use case.

**Implemented 2026-10-25.** Operators are parties with addresses such as `@ana`. A
question is a message of kind question from a line to one operator, and the answer is
that operator's reply, which must reverse the question's route. The lifecycle states
open, answered, withdrawn and superseded are tracked on the message. Forwarding goes through relays, with a command relay
as the general adapter. See [`../protocol/EVENT_LOG.md`](../protocol/EVENT_LOG.md) and
[`../protocol/RELAY.md`](../protocol/RELAY.md).

**Decided 2026-10-25 by the operator.** Nobody is compelled to answer mail or questions.
Deferring is always an option, and doing nothing is valid. A human or an agent with a
hundred messages needs tools to look through them. The wake message reads along the
lines of "X new message(s). Y unread. Z unanswered."

**Implemented 2026-10-25.** Every party has a mailbox of the messages delivered to it.
A message is unread until its recipient opens it, recorded as `message_read`, and
unanswered until the recipient replies to it. A line's wake prompt opens with that
summary sentence and lists the new mail's headers, without bodies, and the line's
`list_mail` and `read_mail` tools page through its mailbox and open messages. Operators
use `dromon mail` and `dromon read`. Nothing marks mail as needing an answer, and the
prompt does not ask for one.

**Decided 2026-10-25 by the operator.** A message can be acknowledged, which marks it
answered without sending a reply. Senders see which messages were acknowledged as
opposed to replied to, because they may be expecting a reply.

**Implemented 2026-10-25.** `message_acknowledged` records an acknowledgement. Lines use
`acknowledge_mail` and see their sent mail's fate with `list_sent`, and operators use
`dromon ack` and `dromon sent`. Each sent message reads as waiting, delivered or read,
and as replied, acknowledged or unanswered. Acknowledging a question does not close it,
so a sender can tell that its question was seen but not yet answered.

**Decided 2026-10-25 by the operator.** There is no such thing as a blocking question.
After asking, a line may end its turn or keep working. That is up to the line-agent.

**Implemented 2026-10-25, roadmap task 6.** `ask_operator` is an in-process tool beside
`send_message`. It records a question to one operator, by default the operator who last
messaged the line, and returns at once, so no tool call waits on a person. The daemon
keeps the queue in the event log. `dromon questions` lists an operator's open questions
and `dromon answer` replies. The answer is a reply message that wakes the asking line in
a later tick and closes the question. Each line's engine
session is recorded as `line_session`, so after a restart the answer reaches a line that
still remembers its question. Live, a Haiku line asked a question, the daemon
restarted, the question was listed and answered, and the line woke and recalled a
codeword it had been given before the restart.

**Proposed.**
- Which operator a line asks by default is per-project configuration.
- Clients show each operator their own queue. `dromon questions` lists it and
  `dromon answer` replies from the command line.

**Decided 2026-10-25 by the operator.** The daemon maintains the `ask_operator` queue.
Interacting with it is a `dromon` client concern in the immediate term. A command-line
client alone is not enough. The client wants a graphical interface, or a way to launch
one, so that the operator can start the daemon and then attach a dozen interfaces to
different session lines in different terminals at once.

**Decided 2026-10-25 by the operator, as direction.** The front end and the back end are
split. The graphical client is the front end and the daemon is the back end, unlike
harnesses such as Claude Code and Codex, which unify the two in one program. The
operator's comparison is a multiplayer online game, whose clients attach to a shared
server, against a single-player game that is client and world at once.

**Decided 2026-10-25 by the operator.** The terminal client is probably curses, chosen
after looking at what Claude Code, Codex and Grok Build use.

**Fact, from secondary sources read on 2026-10-25 and not checked against the official
repositories.** Claude Code renders its terminal interface with a heavily customized
Ink, the React renderer for terminals, on Node.js. Codex's interactive mode is the
`codex-tui` Rust crate, built on Ratatui and Crossterm. Grok Build is Rust, with its
interface in an `xai-grok-pager` crate whose underlying library the sources do not
name. All three are full-screen terminal programs rather than separate graphical
applications.

**Implemented 2026-10-25, roadmap task 8.** `dromon attach LINE` opens a curses client,
the standard library's counterpart to Ratatui and Crossterm and so a direct map onto
the Rust port, and `dromon attach LINE --gui` opens a Tk window, which also ships with
Python. Both were thin shells over `dromon.attach`, since replaced as recorded below,
which polled the new `line_view` operation and turned typed input into commands. Every client is an independent
connection, so any number may attach to any lines at once.

**Decided 2026-10-25 by the operator, as direction.** The terminal and graphical clients
are taken near parity with Claude Code, Codex and Grok Build, on a shared core, in the
shape of the operator's Rust projects. Those have a `no_std` core, a platform layer
that implements the core's traits, and an entry point that wires the two together.

**Implemented 2026-10-25.** The clients live in `dromon.ui`, in three layers.

- **The core**, the counterpart of the `no_std` crate. Its modules perform no input or
  output, and a test checks their imports. It is an Elm-style state machine. `update`
  takes the model and an event, a key, a tick, a size or a daemon result, and returns
  the next model and the effects to perform, a fetch, a request, a bell or an exit.
  `frame` draws a model as rows of styled text with a cursor. The input editor, the
  history, the commands with their completion, polling, scrolling, the panes, the
  line list and the help panel are all here, and are tested without a terminal.
- **The ports and the shared runtime.** The `Daemon` port is one `call` method. The
  runtime owns the model, performs effects through the port, and feeds results back
  as events within a fixed bound of steps. Both platforms use the same runtime.
- **The platforms.** `terminal` translates curses keys and paints frames with curses
  attributes, and `window` does the same with Tk events and text tags, adding the
  mouse wheel. Neither holds client behaviour of its own. The entry point in
  `dromon.cli` wires a platform, the runtime and the socket adapter together.

For a turn to be followed as it runs, the daemon gained a bounded, in-memory activity
tail per line, fed by an observer that each engine adapter calls, and two operations,
`line_activity` and `interrupt`, as [`../protocol/CLIENT.md`](../protocol/CLIENT.md)
specifies. A client polls four times as often while its line runs a turn, which
stands in for pushed updates.

**Not at parity, recorded as limitations.** The clients show no token or context
meter, because the engines report none to Dromon, offer no permission prompts,
because Dromon decides tool use by policy rather than by asking, render Markdown only
to the extent of fenced code and headings, and assume each character fills one column.
Codex text appears only when a message completes, and Claude Code text a message at a
time, because only Grok's adapter receives streamed chunks.

**Porting note.** The core maps onto a Rust crate whose events and effects are `enum`s
and whose `update` is a total function, and from there onto a Keleusma stream
processor. The platforms map onto Ratatui with Crossterm and onto a native toolkit.

**Decided 2026-10-25 by the operator, as direction.** The daemon and client pair is
taken to near parity with Claude Code, Codex and Grok Build, with settings for session
lines and for projects, keeping the core, platform and entry point layering.

**Implemented 2026-10-25.** Settings have two scopes, the project and a line, and
each value has layers, as [`../protocol/CLIENT.md`](../protocol/CLIENT.md) specifies.

- **The project file stays the shared layer.** What an operator changes from a client
  is an override, recorded in the event log as `line_overridden` or
  `project_overridden`, and layered over the file, as Claude Code layers its local
  settings over its shared project settings. The daemon never writes the file. The
  alternative, a daemon that rewrites a committed, reviewed TOML file, would lose its
  comments and blur who changed what.
- **The command line wins**, as Claude Code's does, and a value it shadows is
  reported as such rather than silently ignored.
- **Layering in code.** `dromon.overrides` and `dromon.configview` are pure, and hold
  the keys, their kinds, validation and the layers. The scheduler and the daemon apply
  them. The client core reads typed text by the kinds the daemon reports, so it holds
  no list of settings and learns of a new one without a release.
- **What a client cannot change.** A line's strategy, because it decides per-token
  billing, and the long documents, MCP servers, sandbox, engine settings, the teacher
  and the agent policy, which belong in a reviewed file. MCP servers are shown by name
  only, so that no credential in their environment reaches a client.
- **Also added.** `/clear` starts a line's next turn in a new engine session, as
  Claude Code's `/clear` and Codex's `/new` do. `/create` makes a line from the
  client. `/reload` applies the project file without a restart, and refuses a change
  that only a restart can apply, applying nothing.

**Inference, not verified live.** Switching a Claude Code line's model resumes its
engine session on the new model, as Claude Code's own `/model` does. Codex is given
the new model when its thread is started or resumed, which the scripted tests cover
but no live turn has exercised.

**Inference.** The client protocol of [`../protocol/CLIENT.md`](../protocol/CLIENT.md) is
then the game's network protocol. It needs pushed updates for attached clients, and
clients on other machines once communication layer 3 exists, so both belong in its
later versions.
- **Decided 2026-10-25, design deferred.** Every relay adapter supports a response path,
  so an operator can reply through the channel that carried the question. The required
  properties are in [`../protocol/RELAY.md`](../protocol/RELAY.md), chief among them
  that a reply is attributed to an operator only after the channel authenticates them.
  Replies arrive through inbound adapters registered with the daemon and configured per
  channel, so replying by Slack and by email needs two registered adapters.

## Standard harness features

**Decided 2026-10-25.** Standard features of an agent harness, such as compaction of long
conversations, are added as the work reaches them rather than designed up front. Each
lands with the roadmap task that first needs it, together with its events in the
protocol specification.

## Conversation control

**Proposed.** Two agents can exchange messages without end. The daemon enforces
per-conversation turn limits, cost budgets and cooldowns, and turns a long exchange into
an operator question rather than letting it run.

**Decided 2026-10-25 by the operator.** Pick sensible limits, to be tuned later.

**Implemented 2026-10-25, roadmap task 7.** `dromon.limits` counts, for each line, its
turns in a row without mail from an operator, by default at most 12, and the estimated
cost of its turns since its last release, by default at most $2. An optional cooldown
sets the least time between a line's turns. A line past either limit is held with
`line_held`: it takes no turns and its mail waits, and the daemon asks, on the line's
behalf, the operator who last messaged it, or else the daemon's default operator,
whether to resume it. Any reply to that question, or `dromon release`, releases the line
with `line_released` and resets its counts. A runaway ping-pong between two lines is
held within the limit, and both lines raise questions.

**Inference.** Counting turns without operator mail, rather than turns per pair of
lines, catches a runaway among any number of lines, and a single operator message
resets it, which matches the intent of an operator supervising the work.

**Implemented 2026-10-26, from rows 1, 2, 4, 5 and 12 of the T3 Code review and the
operator's decisions A and B.** Daemon guarantees, each specified in
[`../protocol/LINE_CONFIG.md`](../protocol/LINE_CONFIG.md) and
[`../protocol/CLIENT.md`](../protocol/CLIENT.md).

- **A stop stops.** An operator's interrupt withdraws the line's pending wake and holds
  the line until released, and a kept engine connection that still runs work after
  its interrupt is closed.
- **Nothing outlives its owner.** Every harness runs under `dromon.launcher`, a watcher
  that ends it when the daemon stops it or dies. `stop_policy` decides whether
  everything the harness started ends with it, the default, which fails closed, or is
  left and recorded with its pids as `processes_left`. Processes found by the line's
  mark after its engine ended are recorded as detached. **Platform limits.** The
  launcher polls its parent, and its walk of the process table needs `ps`, without
  which a `kill_all` turn is refused. On Linux the launcher is a child subreaper, so
  it adopts a descendant that lost its parent. Elsewhere such a descendant, in a
  session of its own, is found only by the mark, which macOS 27 does not let the
  daemon read, and the daemon records and shows that gap.
- **A bounded ceiling.** `keep_session_at_max` decides whether a kept connection closes,
  asks an operator or keeps going at `keep_session_max_seconds`, with
  `keep_session_hard_seconds` as a bound on every choice. The default is `close`.
- **A stable prompt cache.** Every Claude Code line runs with tool search pinned on.
- **Honest outcomes.** A result with no turn behind it is not a turn, a system message
  Dromon does not handle is surfaced, a reported fallback to a model the driver was
  not written for refuses the turn, a consent fallback is put to an operator, a
  request for a human decision is refused with the way to ask an operator, and a
  line's condition and each turn's outcome are named. A failed turn that the line's
  own mail during it does not accompany holds the line with an operator question, so
  no failure stays only in an activity log.
- **Evidence from probes.** A probe can record the frames it exchanged, and tests
  replay such a recording through the real adapter. Each engine's capabilities and
  minimum version are data, one table per engine, and an adapter refuses an engine
  older than the one it was tested against, as [`ENGINES.md`](../protocol/ENGINES.md)
  specifies. A recording is scrubbed of identifying values before it becomes a
  fixture.
- **Mail is durable and a retry acts once.** Mail that arrives during a turn waits for
  the next, in order and once, and only the line settles it. `send_message`,
  `ask_operator` and `wake_after` take a request key, so a retried call answers as the
  first did and records nothing.
- **A new session is handed its context.** After a rebind or a cleared session, the
  first turn in the new engine session begins with a deterministic, budgeted selection
  of the line's mail, recorded as `context_handed_off`, unless the line opted out.
- **Tokens per engine process.** A line token is renewed as a turn starts a new engine
  process and revoked when that process ends, never expires while a turn runs, and
  expires when idle between turns.

**Porting note.** The launcher is a Python process today. A port replaces it with a
native watcher, using `kqueue` on macOS and a pidfd or `PR_SET_PDEATHSIG` on Linux in
place of the poll, `prctl` for the subreaper, and `waitid` or `kqueue` to see the
harness exit without reaping it.

## Stream processors and scheduling

**Decided.** In the Keleusma version, each model has its own per-turn or per-sub-turn
stream processor. Model-specific behaviour, such as available functionality and context
window size, is hard-coded into that model's processor, so the concrete behaviour is
written for each model. Each processor runs on its own thread, or several session lines
are batched on one thread with an update loop, as entities are updated in a game.

**Inference.** Keleusma proves an upper bound on the time and memory of each stream step.
A batched update loop can therefore schedule many session lines on one thread with a
known worst-case tick, which most harnesses cannot guarantee.

**Proposed.**

- The model call is network input and output with no time bound. The host performs it
  and feeds each streamed event, such as a batch of tokens, a tool call or the end of a
  turn, into the processor as one step. Keleusma's yield and resume model fits that
  shape.
- Each processor declares the model version it targets, so a mismatch is caught when a
  line starts rather than in the middle of a session.
- In this prototype, each model gets a class with one shared interface, for example
  `step(event) -> actions`, so the later Keleusma port is close to a one-for-one
  translation.

## Configuration

**Decided.** Per-project configuration is non-trivial.

**Decided 2026-10-25 by the operator, as direction.** Dromon's configuration for a
multi-agent project may grow as large and as detailed as the project needs, with
Kubernetes as a loose point of comparison. Everything about a session line is
configurable per line, and per project when one harness serves several projects.
Building that machinery is expected to take sustained work. The per-line tool
configuration of [`../protocol/LINE_CONFIG.md`](../protocol/LINE_CONFIG.md) is its first
piece.

**Decided 2026-10-25, task 9.** It lives in `.dromon/dromon.toml`, specified by
[`../protocol/PROJECT_CONFIG.md`](../protocol/PROJECT_CONFIG.md) as file version 1. It
declares the default operator, the tick cadence, the conversation limits, the wake
policy, reusable line profiles, and each line's engine, strategy, tools, limits and wake
policy. Unknown keys are errors, and every error is located in the file. Flags override
the file, and the file overrides the defaults.

**Fact.** The format follows a review of Claude Code, Codex and Grok Build settings on
2026-10-25, recorded in the specification with its sources. **Open.** User and managed
layers, credentials policy and references to secrets held outside the file.

**Porting hazard.** The prototype reads TOML with Python's `tomllib` and locates syntax
errors by parsing its message text. The Rust port should use a parser that reports
spans, such as the `toml` crate, and keep the same messages.

## Persistence

**Implemented 2026-10-25, roadmap task 2.** An append-only event log in SQLite holds
messages, wakes, questions and line state. It makes recovery after a crash and replay in
a newly attached client straightforward, and the event list doubles as the specification
of the data model. The specification is [`../protocol/EVENT_LOG.md`](../protocol/EVENT_LOG.md).

### Workspace capture at turn boundaries, declined for now

**Decided 2026-10-27, row 15 of the T3 Code review.** T3 Code captures each turn's
working tree under hidden Git refs, through a temporary index, `write-tree`,
`commit-tree` and `update-ref`, and refuses to restore a shared checkout. The operator
asked whether capture alone, off by default, fits Dromon now that lines may share a
working tree. It does not yet, for the following reasons.

- **Attribution would be false in the shared tree.** **Fact.** The lines that develop
  Dromon share one working tree, in which the operator's own session also works.
  **Fact.** Dromon learns which tools a turn used, but not which files they changed, so
  it cannot separate one writer's changes from another's. **Inference.** A capture at
  one line's turn boundary would record the other lines' and the operator's uncommitted
  work as that line's turn, so the audit trail it promises would mislead in exactly the
  configuration Dromon runs in.
- **Captured content becomes hard to remove.** **Fact.** A capture writes every changed
  or untracked file that is not ignored into the repository's object store, and the
  ref keeps it reachable. **Fact.** The repository's disclosure scan reads tracked
  files, not hidden refs, and `git push --mirror` pushes every ref under `refs/`.
  **Inference.** Scratch text or a draft holding a disclosure term that a line never
  commits would still be kept, and could be published by a push the operator did not
  intend to carry it. That is the irreversible failure mode Appendix B guards against.
- **The daemon would become a Git writer beside the lines and the operator.** **Fact.**
  Dromon's lines may not commit, and the operator's session does all writes to the
  repository. **Inference.** Even with a temporary index, `update-ref` takes ref locks
  and Git may start an automatic garbage collection, so a capture could contend with
  the operator's own commits, in ways that fail rarely and are hard to see.
- **Porting.** **Inference.** The Rust and Keleusma implementations would need Git, as
  a library or a subprocess, in the daemon's core, for a feature whose output cannot
  yet be trusted.

**What would make it fit.** Each condition below is required, and none is met today.
1. The line runs in a worktree of its own, which `scripts/worktree.sh` makes, and the
   daemon refuses capture for a line whose working directory resolves to the same
   worktree as another line's. The daemon cannot see an operator's session, so the
   operator's configuration states that no one else writes there.
2. Captures go to a private bare repository under the daemon's state directory, which
   borrows the project's objects through Git's `alternates` file, and never to the
   project's own refs, so no push of the project can carry them.
3. The setting is per line and off by default, a capture is recorded in the event log
   by its commit identifier, and restore stays refused.

Until then, the event log's record of each turn, its tools, its outcome and the mail it
carried, together with the line's own commits through the operator, is the audit
trail.

## Security and trust

**Proposed.**

- The client socket is readable only by the operator's user account.
- The daemon runs outside every session line's sandbox, so it can act as a credential
  broker under policy, for example pushing to git on a line's request. That also makes it
  a high-value target. Every brokered action needs an explicit per-project allowance.
- Each session line keeps its own sandbox profile. **Decided 2026-10-25, by the
  operator's direction to reach parity with Claude Code.** A line's shell commands run
  in Claude Code's own sandbox, configured per project and per line in Claude Code's
  vocabulary, with unattended defaults and with Dromon's state and runtime directories
  protected, as [`../protocol/SANDBOX.md`](../protocol/SANDBOX.md) specifies. **Porting
  hazard.** The Rust port inherits the dependency on Claude Code's sandbox runtime for
  the Claude Code engine, and another engine needs a sandbox of its own.
- Each session line carries engine settings. **Decided 2026-10-25, by the operator's
  direction to reach parity with the settings actually used in Claude Code, Codex and
  Grok Build.** Permission rules, command hooks, environment variables and attribution
  use Claude Code's vocabulary, layer per project, profile and line, and reach Claude
  Code through its settings flag, as [`../protocol/SETTINGS.md`](../protocol/SETTINGS.md)
  specifies. An `ask` rule denies, since nothing pauses a line, and every line's file
  tools are denied Dromon's state and runtime directories. MCP servers may refer to
  secrets in the daemon's environment. **Porting hazard.** Claude Code parses the rules
  and runs the hooks, so another engine needs its own rule matcher and hook runner to
  honour the same documents.
- Messages from other session lines, harnesses and machines are untrusted data. Only the
  operator, through a client, holds operator authority.
- External agents reach the daemon over MCP. **Decided 2026-10-25, by the operator's
  direction.** Another harness, such as a Claude Code session, starts `dromon mcp` as an
  MCP server and becomes the party `&name` with its own mailbox. The project file sets
  whether an agent socket exists, the access of an anonymous connection and a ceiling,
  from off to every connection trusted as an operator, and registered agents carry
  tokens stored as digests, as [`../protocol/AGENTS.md`](../protocol/AGENTS.md)
  specifies. An agent's messages carry no operator authority unless its level is
  `operator`.

## Keleusma

**Decided.** The prototype uses the `pykeleusma` wrapper, pinned to its PyPI release
`0.2.2.0`, which binds Keleusma 0.2.2. The final implementation is in Keleusma,
entirely or almost entirely.

**Proposed.** Keleusma's bounded execution suits the harness's policy layer, such as
message routing, wake policy and permission checks, where a hook must never hang. Use it
there, since `pykeleusma` runs scripts, as the per-model drivers show. The needs,
mapped to the binding's tasks, are in [`KELEUSMA_INTEGRATION.md`](KELEUSMA_INTEGRATION.md).

## Teacher models

**Open.** Before the operator clarified that Dromon is its own project, they mentioned
supporting teacher models that help train new language models. Its place in Dromon's
scope is not settled.

**Decided 2026-10-25, by the operator's direction to dogfood Dromon in the epagoge
project.** Dromon can spin up a project's local teacher, a model the project's own
generators call through Ollama on the loopback interface, as
[`../protocol/TEACHER.md`](../protocol/TEACHER.md) specifies. A teacher is a served model,
not a session line. Lines reach it through the project's tools, and Dromon only loads
and releases it. Lines also gained `claude_code_prompt` and `instructions`, because a
Claude Code line had an empty system prompt and read no `CLAUDE.md`, so it could not
follow a project's own rules.

**Fact, observed on 2026-10-25 in epagoge.** With the teacher loaded by `dromon
teacher up` and `dromond` running there, a Claude Haiku 5.5 line in Claude Code's
sandbox queried the teacher over loopback and answered a question from the project's
`CLAUDE.md`, and a GPT-6-Luna line answered one from its instructions. Both replied to
the operator by `send_message`. Dromon's state there is excluded from git through the
repository's local exclude file, so the project's own checks never see it.

**Fact, observed later on 2026-10-25 in epagoge, after its line reviewed the setup.** The
project's full gate passed from a sandboxed Claude Haiku 5.5 line, with 678 tests and a
clean disclosure scan. With the sandbox denying writes to `.git`, the same line could not
write there, so no spelling of a commit succeeds from its shell. `dromon teacher up` now
refuses to load a model that would leave less than its headroom of available memory. **Unverified, from the seeding session's memory.** Several model
vendors' terms forbid using their outputs to train competing models, and subscription
plans tend to be stricter than paid interface use. Check each vendor's current terms
before any trace capture, and record the source model and licence of every trace.

## Open questions

1. The home of the protocol specification. Decided 2026-10-25, drafted here and ported
   later.
2. The engine for the first adapter. Decided 2026-10-25, Claude Opus 5.5 on Claude Code
   through the Agent SDK, on the subscription, with a Keleusma driver.
3. The contents of `.dromon/dromon.toml`.
4. Mid-turn injection. Decided 2026-10-25, not used, because delivery is tick based.
5. Teacher-model scope and licensing. A local teacher served by Ollama is decided.
   Training on traces from hosted models remains open.
6. Whether Dromon retires the interline store, or keeps it for sessions Dromon does not
   drive. Moved here from the handoff on 2026-10-25. Since the same day, a session
   Dromon does not drive can reach Dromon's lines as an external agent, which removes
   one reason to keep a separate store.

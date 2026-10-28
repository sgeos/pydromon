# Teacher

> **Navigation**: [Protocol](./README.md) | [Documentation Root](../README.md)

**Status.** Draft, written on 2026-10-25. Normative. The Python module `dromon.teacher`
implements it against Ollama 0.40.10. It is part of project file version 1, as an
optional table.

## Purpose

**Directed by the operator on 2026-10-25.** Dromon should be able to spin up a
project's local teacher model, so that it can be dogfooded in the operator's epagoge
project, whose corpus generators all call a model served locally by Ollama. A teacher
is a model a project's own tools call, not a session line.

## Table

```toml
[teacher]
model = "qwen3:30b-a3b-instruct-2507-q4_K_M"
endpoint = "http://127.0.0.1:11434"
context = 4096
keep_alive = "30m"
autostart = false
```

| Key | Value | Default |
|---|---|---|
| `model` | An Ollama model name | required |
| `endpoint` | `http` on `127.0.0.1` or `localhost`, with a port | `http://127.0.0.1:11434` |
| `context` | Tokens to load the model with, in [512, 1048576] | 4096 |
| `keep_alive` | `-1`, `0`, or a number followed by `s`, `m` or `h`, as Ollama reads it | `30m` |
| `autostart` | Whether `dromond` brings the teacher up when it starts | `false` |
| `memory_headroom_gb` | Gigabytes that must stay available beyond the model's size when it loads, in [0, 1024] | 4 |

Any other key is an error. The endpoint is restricted to the loopback interface, so a
project file cannot point the teacher at another host.

## Operations

`dromon teacher up`, `dromon teacher status` and `dromon teacher down` act on the
project's teacher without a daemon.

1. **Up.** If nothing answers `GET /api/version` at the endpoint, Dromon starts
   `ollama serve` with `OLLAMA_HOST` set to the endpoint's host and port, records its
   process identifier in `teacher.pid` in the runtime directory, writes its output to
   `teacher.log` there, and waits up to 60 seconds for it to answer. The model must
   appear in `GET /api/tags`, and Dromon never pulls it, because a pull downloads many
   gigabytes and is the operator's decision. Ollama receives only `PATH`, `HOME`,
   `LANG`, `TERM` and `TMPDIR` from the daemon environment when present, plus Dromon's
   `OLLAMA_HOST`. Other daemon variables are omitted. Unless the model is already loaded
   or `--force` is given, Dromon refuses to load it when the memory the operating system
   reports as available is less than the model's size in `GET /api/tags` plus
   `memory_headroom_gb`. Available memory is `kern.memorystatus_level` percent of the
   physical memory on macOS and `MemAvailable` on Linux, and where neither exists the
   check is skipped. Dromon then loads the model with
   `POST /api/generate`, an empty prompt, `keep_alive` and `options.num_ctx` set to the
   context, and checks that `GET /api/ps` lists it. A failure stops a server this call
   started, so a failed attempt leaves nothing running.
2. **Status.** Whether the server answers, its version, whether the model is pulled
   and loaded, its resident size and context as `GET /api/ps` reports them, whether
   Dromon started the server, and the memory available.
3. **Down.** A loaded model is unloaded with `keep_alive` set to 0. The server is
   stopped only if Dromon started it, and only after the recorded process is confirmed
   to be `ollama`, since a stale identifier can be reused by another process. A server
   the operator runs, such as the desktop application's, is left running.

With `autostart`, `dromond` brings the teacher up before any line runs, refuses to
start if that fails, and releases it when it stops.

## Lines and the teacher

A line reaches the teacher through its own tools, such as a project script run from
Bash. A Claude Code line's sandbox must let the shell connect to the endpoint. A Codex
line runs without network access, so it cannot.

**Fact, verified on 2026-10-25 on macOS with Ollama 0.40.10.** `dromon teacher up`
loaded `qwen3:30b-a3b-instruct-2507-q4_K_M` in 17.7 seconds, and Ollama reported 18.9 GB
resident at a context of 4096. On a 32 GB host, free memory fell from 69 to 14 percent
and returned to 71 percent after `down`. Pointed at a free port with a model that was
not pulled, `up` started its own `ollama serve`, refused to pull, and stopped that
server, while the desktop application's server kept running. A Claude Haiku 5.5 line
whose sandbox was enabled with `network.allowLocalBinding` reached the loaded teacher
with `curl` on `127.0.0.1:11434`. **Not tested.** Whether the connection needs
`allowLocalBinding`.

**Fact, observed on 2026-10-25 on macOS.** With the model unloaded, `vm_stat` showed
about 1.6 GB of free pages and 12.8 GB inactive, while `kern.memorystatus_level`
reported 82 percent, about 28 GB, available. Free pages alone would have refused a load
that the earlier measurement shows the host can take, which is why they are not used.

## Trust

The model's answers are untrusted text. Dromon only loads and unloads the model and
never reads its output, and a project's own tools validate what they take from it.

## Not yet specified

- Teachers served by anything but Ollama.
- A tool through which a line asks the daemon to bring the teacher up.

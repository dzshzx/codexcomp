# codexcomp

[![PyPI](https://img.shields.io/pypi/v/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Python](https://img.shields.io/pypi/pyversions/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![License: MIT](https://img.shields.io/pypi/l/codexcomp.svg)](https://github.com/dzshzx/codexcomp/blob/main/LICENSE)

**English** · [简体中文](README.zh-CN.md)

A tiny local Responses proxy for the **OpenAI Codex CLI** that cures the gpt-5.5
**"516" reasoning-truncation degradation** — while leaving your `model_provider`
untouched, so session grouping, remote compaction and remote-control keep working.

```bash
uv tool install codexcomp      # install
codexcomp                      # run (127.0.0.1:8787)
# then add one line to ~/.codex/config.toml:  openai_base_url = "http://127.0.0.1:8787/v1"
```

> **Credits.** The detection-and-continue idea comes from
> [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont) (MIT) — thank you.
> This project is an independent, from-scratch implementation that keeps the built-in
> provider intact; see [Differences](#differences-from-codexcont).

---

## The problem: gpt-5.5 "516" degradation

On the OpenAI Codex CLI, gpt-5.5's reasoning sometimes gets cut short at a very
specific token count — `reasoning_tokens == 518 * n − 2` (i.e. **516, 1034, 1552, …**).
When a turn lands on that fingerprint, the model stops thinking early and the answer
quality drops sharply. It is an upstream issue with no official fix
([openai/codex#30364](https://github.com/openai/codex/issues/30364)).

`codexcomp` sits on `127.0.0.1` between Codex and the upstream Responses API.
When it sees a turn truncate on the `518n−2` fingerprint, it **makes the model keep
thinking** and **folds the extra rounds into a single downstream response**, so Codex
sees one clean, complete answer.

## How it works

The proxy streams every upstream round and runs a small state machine (`codexcomp/fold.py`):

1. **Detect.** At the end of each round it reads
   `usage.output_tokens_details.reasoning_tokens`. If it equals `518n − 2` (with
   `1 ≤ n ≤ 6`, and at most 3 continuation rounds), the round was truncated.
2. **Continue.** It discards that round's *tentative* output (the message / tool calls —
   they were produced on truncated thinking), then replays the round's reasoning items
   (including `encrypted_content`) plus a single `phase:"commentary"` assistant message
   (`"Continue thinking..."`) as the next round's input. That nudges the model to resume
   reasoning where it left off.
3. **Fold.** Reasoning is streamed live to Codex the whole time; only the *clean* final
   round's output is flushed. The terminal event is rebuilt as if the whole thing were
   one response — `input`/`cached` come from round 1 (so it never looks like a blown
   context window), reasoning is summed, and the true cumulative cost is recorded under
   `metadata.proxy_billed_usage`.

### Wiring: why the built-in provider stays intact

Codex is pointed at the proxy with **one top-level config key**, not a new provider:

```toml
# ~/.codex/config.toml  (top level, before the first [table])
openai_base_url = "http://127.0.0.1:8787/v1"
```

`openai_base_url` overrides the base URL of the **built-in `openai` provider** in place.
This is the officially supported key
([openai/codex#16719](https://github.com/openai/codex/issues/16719); the same-name
`[model_providers.openai]` override is rejected by the maintainers, and the
`OPENAI_BASE_URL` env var was removed). Because the provider id stays `openai`:

- your conversation history is **not** re-bucketed/hidden by provider,
- **remote compaction** keeps working (`supports_remote_compaction` stays true),
- **remote-control** is unaffected (it uses the separate `chatgpt_base_url`).

### Differences from CodexCont

The 518n−2 detection + fold-continuation mechanism is [CodexCont]'s idea; the
implementation here is new and diverges on a few deliberate points:

|  | codexcomp | CodexCont |
| --- | --- | --- |
| **Codex wiring** | top-level `openai_base_url` (**built-in provider unchanged**) | a new `[model_providers]` entry (history hidden per-provider, remote-control unusable, remote compaction lost) |
| **Downstream transport** | **WebSocket-first** — full `responses_websockets` protocol, plus SSE fallback | SSE only (Codex tries ws → 405 → ~5 reconnect warnings per session, then falls back) |
| **zstd request bodies** (0.142.x built-in provider) | decompressed natively, no Codex config change | needs `[features] enable_request_compression = false` |
| **`GET /v1/models`** (model-catalog refresh) | passed through (`/v1/*`) | not proxied (silently fails, relies on cache) |
| **Continuation** | commentary method only | commentary + legacy tool-pair + cross-turn repair, more knobs |

[CodexCont]: https://github.com/neteroster/CodexCont

## Install

Requires [uv](https://docs.astral.sh/uv/) (which manages Python for you) and the Codex
CLI (ChatGPT OAuth login; tested on 0.142.x).

```bash
uv tool install codexcomp          # from PyPI
# or straight from source:
# uv tool install git+https://github.com/dzshzx/codexcomp
```

uv puts the executable in its bin dir (`~/.local/bin` on Unix/macOS; on Windows run
`where.exe codexcomp`; `uv tool update-shell` adds it to PATH). Then:

```bash
codexcomp                          # run in foreground (default 127.0.0.1:8787)
codexcomp --port 8790 --log-level debug
```

Wire Codex to it (one line in `~/.codex/config.toml`, see above), and you're done.
**Disable** by commenting out the `openai_base_url` line and stopping the proxy. (If the
key stays but the proxy is down, Codex errors on an unreachable upstream.)

Upgrade / uninstall: `uv tool upgrade codexcomp` / `uv tool uninstall codexcomp`.

### Ports

The proxy's port **must match** the port in Codex's `openai_base_url`. If the default
port (8787) is busy, the proxy **exits with a clear message** rather than drifting — a
wired proxy that silently binds another port would just be unreachable. To use a
different port, pass `--port N` and set `openai_base_url` to the same `N`.

`--auto-port` is for interactive one-off runs only: on a conflict it scans for the next
free port and prints which `openai_base_url` to use. Don't use it for a wired service.

## Autostart (optional, off by default)

Installing registers **no** autostart — it's entirely your choice.

```bash
codexcomp install-service     # register + start (current platform)
codexcomp uninstall-service   # remove
```

`install-service` picks the per-user, runs-in-your-session mechanism (a system service
runs in a session with no user environment and can't reach the uv executable or your
proxy settings under your profile):

- **Linux / WSL** → a systemd **user** unit (`~/.config/systemd/user/`). Run
  `loginctl enable-linger` once to start it at boot without logging in. Manual equivalent:
  see `systemd/codexcomp.service.example`.
- **macOS** → a launchd **LaunchAgent** in `~/Library/LaunchAgents/` (starts at login, in
  your GUI session). Load with `launchctl bootstrap gui/$(id -u) <plist>` /
  `launchctl kickstart -k …`; remove with `launchctl bootout …`.
- **Windows** → **prints manual steps, registers nothing** (see below).

### Windows autostart is manual — on purpose

A program that writes an autostart entry (Startup VBS / Run key / scheduled task) and
launches a hidden process trips behavioral antivirus as trojan-like persistence —
Kaspersky's proactive-defense module flags the launching `python.exe` as
`PDM:Trojan.Win32.Generic`. A **user-created** Startup shortcut is trusted by the same AV.

So this package ships a windowless launcher, `codexcompw` (a Windows GUI-subsystem
exe — no console window at logon), and `install-service` just tells you how to point a
shortcut at it:

1. `Win+R` → `shell:startup` (opens the Startup folder).
2. New → Shortcut → target = the path from `where.exe codexcompw` (append
   `--port N` if you use a custom port).

Delete the shortcut to disable it.

### Mirrored-networking shortcut (WSL ↔ Windows)

If your WSL2 uses `networkingMode=mirrored`, Windows and WSL **share `127.0.0.1`**. Then
you only need **one** proxy on either side — run it in WSL (as a systemd service), and on
the Windows side just add the `openai_base_url` line to `~/.codex/config.toml` pointing at
the same `127.0.0.1:8787`. No second proxy or Windows autostart needed (the only cost is
that Windows Codex depends on the WSL proxy being up).

## Verify

```bash
curl -sS http://127.0.0.1:8787/healthz            # {"ok":true,...}
journalctl --user -u codexcomp -f | grep -E 'round|done'   # Linux/WSL
```

A live fold looks like this (two chained 516s beaten, answer correct):

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

## Develop

```bash
git clone https://github.com/dzshzx/codexcomp && cd codexcomp
uv sync
uv run python test_fold.py        # fold state-machine self-test → ALL PASS
uv run codexcomp            # run locally
```

Releases go out via PyPI Trusted Publishing (`.github/workflows/release.yml`, OIDC, no
stored token): push a `v*` tag and it builds + publishes automatically.

Layout:

- `codexcomp/fold.py` — fingerprint detection + fold state machine (transport-agnostic;
  covered by `test_fold.py`).
- `codexcomp/server.py` — starlette transport: ws / SSE downstream, SSE upstream,
  zstd/gzip request decompression, `/v1/*` passthrough.
- `codexcomp/cli.py` — CLI entry (`codexcomp`; loopback only; auth passthrough, stores
  no credentials).

## Security & disclaimer

- The proxy is **auth passthrough** only: it forwards Codex's `Authorization` header and
  never reads, stores, or logs any credential.
- It listens on the **loopback** address only — do not expose it on a non-loopback interface.
- **Unofficial**: it depends on upstream behavior that isn't a public contract (the
  truncation fingerprint, the ws frame format). An OpenAI-side change may break it. Use at
  your own risk.
- Continuation spends **extra real tokens** (see `metadata.proxy_billed_usage`); codexcomp
  bounds this with an `n` window and a 3-round cap.

## Community

Built for and shared with the [**LINUX DO**](https://linux.do) community, where the
gpt-5.5 "516" degradation was diagnosed and discussed. Feedback and issues welcome there
and on [GitHub Issues](https://github.com/dzshzx/codexcomp/issues).

## License

[MIT](LICENSE). Fully open source, no closed parts.

Mechanism credit: [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont) (MIT) —
this project reuses its 518n−2 detect-and-continue *idea* with an independent, from-scratch
implementation, and keeps the built-in provider intact (see [Differences](#differences-from-codexcont)).
CodexCont's MIT copyright notice is retained in [LICENSE](LICENSE).

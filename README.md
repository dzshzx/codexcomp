<div align="center">

# codexcomp

**Codex + Complete** — a lightweight local proxy that folds gpt-5.5's **"516" reasoning
truncation** into complete, untruncated answers for the [OpenAI Codex CLI](https://github.com/openai/codex).

[![PyPI](https://img.shields.io/pypi/v/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Python](https://img.shields.io/pypi/pyversions/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Downloads](https://img.shields.io/pypi/dm/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![License: MIT](https://img.shields.io/pypi/l/codexcomp.svg)](https://github.com/dzshzx/codexcomp/blob/main/LICENSE)

**English** · [简体中文](README.zh-CN.md)

</div>

```bash
uv tool install codexcomp      # install
codexcomp                      # run (127.0.0.1:8787)
# then append to ~/.codex/config.toml:  openai_base_url = "http://127.0.0.1:8787/v1"
```

It overrides the built-in provider's base URL **in place** — `model_provider` is unchanged,
so session grouping, remote compaction, and remote-control keep working.

> **Credits.** The detect-and-continue mechanism originates from
> [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont) (MIT); this is an
> independent, from-scratch implementation that keeps the built-in provider intact.

---

## The problem

gpt-5.5's reasoning is intermittently truncated at `reasoning_tokens == 518·n − 2`
(**516, 1034, 1552, …**): the turn stops mid-reasoning and answers from an incomplete
thought, degrading quality sharply. Aggregate telemetry in the upstream report shows ~44 %
of gpt-5.5 responses that reach 516 reasoning tokens end at exactly that boundary — an
upstream defect with no official fix
([openai/codex#30364](https://github.com/openai/codex/issues/30364)).

`codexcomp` sits on `127.0.0.1` between Codex and the upstream Responses API. On a `518n−2`
truncation it drives the model to keep reasoning and folds the extra rounds into a single
downstream response — Codex sees one complete, untruncated answer.

## Features

- **Detect → continue → fold** — spots the `518n−2` fingerprint, replays the round's
  reasoning with a continue nudge, and folds all rounds into one response.
- **Zero-footprint wiring** — one official top-level `openai_base_url` key; no
  `[model_providers]` entry, no provider id change, no session re-bucketing.
- **WebSocket-first transport** — native `responses_websockets` protocol (envelope frames,
  serial connection reuse, prewarm); no "Falling back" noise in Codex logs.
- **Resilient SSE fallback** — the POST path transparently decompresses zstd/gzip upstream
  responses.
- **Full `/v1/*` passthrough** — including `GET /v1/models` (model catalog refresh).
- **Protocol-faithful headers & compaction** — upstream response headers reach Codex on the
  POST path (`x-codex-*` rate-limit snapshots, `x-models-etag`, request ids), the
  `x-codex-turn-state` sticky-routing token is replayed across fold rounds when the upstream
  issues one, and remote-compaction requests are never folded.
- **Live streaming** — reasoning streams in real time even mid-fold; only the final clean
  round's output is released downstream.
- **Honest accounting** — the true cumulative cost of folded rounds is reported under
  `metadata.proxy_billed_usage`.
- **Loopback-only, auth passthrough** — forwards Codex's `Authorization` header; never
  reads, persists, or logs a credential.
- **Opt-in autostart** — installation registers nothing; one command sets up a systemd user
  unit (Linux/WSL) or LaunchAgent (macOS).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and the Codex CLI (ChatGPT OAuth; tested on 0.142.x).

```bash
uv tool install codexcomp                                  # from PyPI
# uv tool install git+https://github.com/dzshzx/codexcomp  # or from source
codexcomp                                                  # foreground, 127.0.0.1:8787
```

Point Codex at the proxy with one top-level config key:

```toml
# ~/.codex/config.toml  (top level, before the first [table])
openai_base_url = "http://127.0.0.1:8787/v1"
```

That's it. Disable by removing that line and stopping the proxy; upgrade / uninstall with
`uv tool upgrade codexcomp` / `uv tool uninstall codexcomp`.

## How it works

A state machine (`codexcomp/fold.py`) runs per round:

1. **Detect** — `reasoning_tokens == 518n − 2` (any tier by default; cuts up to n=21 observed —
   see `--max-n` / `--max-continue`) marks the round as truncated.
2. **Continue** — discard the tentative output and replay the round's reasoning items (incl.
   `encrypted_content`) plus one `phase:"commentary"` `"Continue thinking..."` message as the
   next input.
3. **Fold** — stream reasoning live, flush only the final clean round, and rebuild the terminal
   event as one response (reasoning summed, true cost under `metadata.proxy_billed_usage`).

## CLI reference

| Command | Description |
| --- | --- |
| `codexcomp` / `codexcomp run` | Start the proxy in the foreground. |
| `codexcomp install-service` | Opt-in autostart registration for the current platform. |
| `codexcomp uninstall-service` | Remove the autostart entry. |
| `codexcompw` | Windowless entry (Windows); logs to `%LOCALAPPDATA%\codexcomp\codexcompw.log`. |

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address — keep it loopback. |
| `--port` | `8787` | Must match `openai_base_url`; if busy the proxy exits. |
| `--upstream` | `https://chatgpt.com/backend-api/codex` | Upstream base URL. |
| `--log-level` | `info` | One of `critical` / `error` / `warning` / `info` / `debug`. |
| `--max-n` | `0` | Highest `518n−2` tier to auto-continue; `0` = no cap (cuts up to n=21 observed). |
| `--max-continue` | `3` | Max continuation rounds per request (runaway guard). |

## Autostart (optional, off by default)

```bash
codexcomp install-service     # register + start (current platform)
codexcomp uninstall-service   # remove
```

- **Linux / WSL** — systemd **user** unit; `loginctl enable-linger` starts it at boot without
  login.
- **macOS** — launchd **LaunchAgent** in `~/Library/LaunchAgents/`.
- **Windows** — prints manual steps only: point a Startup shortcut (`Win+R` → `shell:startup`)
  at the windowless `codexcompw` (`where.exe codexcompw`). Delete it to disable.

With WSL2 `networkingMode=mirrored`, Windows and WSL share `127.0.0.1`: run one proxy in WSL
and just add the `openai_base_url` line on the Windows side — no second proxy needed.

## Verify

```bash
curl -sS http://127.0.0.1:8787/healthz            # {"ok":true,...}
journalctl --user -u codexcomp -f | grep -E 'round|done'   # Linux/WSL
```

A live fold — two consecutive 516s folded, answer correct:

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

Round verdicts: `continue` (cut detected → will continue), `clean` (natural end),
`tier_out_of_window` / `max_continue` / `no_encrypted_content` (cut detected but released
as-is), `compaction_request` (remote-compaction request — never folded),
`upstream_eof` (stream ended without a terminal event). `done:` lines end with
`stop=natural` or the release reason. A fold torn down by a client disconnect logs
`fold aborted downstream after N round(s)` instead of a `done:` line.

## Evals

`evals/candy_eval.py` measures the fix end-to-end: it runs a model × effort ×
proxy-on/off grid of `codex exec` calls on the candy pigeonhole puzzle from
[haowang02/codex-candy-eval](https://github.com/haowang02/codex-candy-eval)
(answer: 21, independently re-verified by brute force) and reports boundary-cut
rate, reasoning tokens and accuracy per condition. Both modes wire
`openai_base_url` explicitly, so the ambient config doesn't matter; interrupted
grids resume by re-running the same command.

```bash
codexcomp &                                    # proxy must be running for `on`
python evals/candy_eval.py -m gpt-5.5 -r xhigh -n 5   # small grid
python evals/candy_eval.py                            # full 80-run grid
```

An 80-run grid of this eval (2026-07-06) found every unmitigated gpt-5.5 run
cut exactly on a `518n−2` boundary, 15% vs 90% accuracy off/on — details in
[openai/codex#30364](https://github.com/openai/codex/issues/30364#issuecomment-4893087004).

## FAQ

**Does it touch normal turns?**
No. Clean rounds pass through byte-for-byte; the fold path only engages on a detected
`518n−2` truncation.

**What does a fold cost?**
Continuation rounds spend extra real tokens, bounded by the continuation cap
(`--max-continue`, default 3). The true cumulative usage is reported under
`metadata.proxy_billed_usage`.

**What happens when OpenAI fixes this upstream?**
Nothing breaks — the detector simply stops firing and the proxy becomes a transparent
passthrough. Unwire it by deleting the `openai_base_url` line whenever you like.

**Why not a separate `[model_providers]` entry?**
That changes the provider id, which re-buckets session history and drops remote compaction
and remote-control. `openai_base_url` is the official in-place override of the built-in
`openai` provider.

**Is my credential safe?**
The proxy forwards the `Authorization` header untouched and binds to loopback only; it never
reads, persists, or logs a credential.

## Security & disclaimer

- **Auth passthrough only** — forwards Codex's `Authorization` header; never reads, persists,
  or logs a credential.
- **Loopback only** — do not expose it on a non-loopback interface.
- **Unofficial** — it relies on non-contract upstream behavior; an OpenAI-side change may break
  it. Use at your own risk.
- Continuation spends **extra real tokens** (`metadata.proxy_billed_usage`), bounded by the
  `--max-continue` cap (default 3 rounds).

## Development

```bash
git clone https://github.com/dzshzx/codexcomp && cd codexcomp
uv sync
uv run python test_fold.py        # fold state-machine self-test → ALL PASS
uv run python test_ws.py          # transport self-test (WS protocol, headers) → ALL PASS
uv run codexcomp                  # run locally
```

Releases go out via PyPI Trusted Publishing (OIDC, no stored token): push a `v*` tag to build
and publish. Version history: [CHANGELOG.md](CHANGELOG.md).

## Contributing

Bug reports, fold-log excerpts, and reproduction details are the most valuable
contributions — please file them on
[GitHub Issues](https://github.com/dzshzx/codexcomp/issues). For code changes, run
`uv run python test_fold.py` and `uv run python test_ws.py` before opening a PR and keep
changes focused.

## Community

Built for and shared with the [**LINUX DO**](https://linux.do) community, where the gpt-5.5
"516" degradation was diagnosed. Feedback and issues welcome there and on
[GitHub Issues](https://github.com/dzshzx/codexcomp/issues).

## License

[MIT](LICENSE) — mechanism credit to
[**neteroster/CodexCont**](https://github.com/neteroster/CodexCont) (MIT), whose 518n−2
detect-and-continue idea this reuses with an independent, from-scratch implementation.

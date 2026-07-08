# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`codexcomp` is a local loopback proxy (127.0.0.1:8787) that sits between the OpenAI Codex CLI and the upstream Responses API. It detects gpt-5.5's `518n − 2` reasoning-truncation fingerprint (516, 1034, 1552, … reasoning tokens — openai/codex#30364), drives the model to continue thinking, and folds all rounds into one complete downstream response. Codex is wired to it via the official top-level `openai_base_url` key — deliberately NOT a `[model_providers]` entry, because changing the provider id re-buckets session history and drops remote compaction/remote-control.

## Commands

```bash
uv sync                        # install deps into .venv
uv run python test_fold.py     # fold state-machine self-test → "ALL PASS"
uv run python test_ws.py       # WS stateful-protocol (prewarm/incremental) self-test → "ALL PASS"
uv run codexcomp               # run the proxy locally (foreground, 127.0.0.1:8787)
uv build                       # build sdist + wheel
```

There is no pytest/lint/typecheck setup — both tests are plain scripts with asserts, run them directly. Run `test_fold.py` before any change to `fold.py`, `test_ws.py` before any change to `server.py`'s WebSocket path.

Release checklist (do every step, in order): bump `version` in `pyproject.toml` → commit → push `main` → push the matching `v*` tag → wait for `.github/workflows/release.yml` to go green (it builds and publishes to PyPI via Trusted Publishing — OIDC, no stored token) → confirm the version is live on PyPI → create the GitHub Release with `gh release create v* --title … --notes-file …` (the workflow does NOT do this; a pushed tag with no Release leaves the Releases page showing a stale latest; notes are English-first with a short `### 中文说明` supplement after a `---`).

## Architecture

Four small modules under `codexcomp/`, with one central seam:

- **`fold.py`** — the core: a transport-agnostic state machine. `fold(base_body, open_round)` consumes upstream events as dicts and yields downstream events as dicts; it knows nothing about SSE or WebSocket. Per round it classifies output items: `reasoning` items stream through live (with proxy-owned `sequence_number` and renumbered `output_index`), everything else (messages, tool calls) is **buffered** as tentative. On a `518n−2` terminal it replays the original input + accumulated reasoning items (incl. `encrypted_content`) + a `phase:"commentary"` "Continue thinking..." nudge as the next round's input; only the final clean round's buffered output is flushed. A continuation round (round ≥ 2) that comes back with `reasoning_tokens == 0` is a **zero-stall** — the nudge failed, so it is re-nudged rather than accepted, sharing the same `MAX_CONTINUE` budget (round 1's zero reasoning is a legitimate complete answer and never enters this path). Constants `STEP`/`MIN_N`/`MAX_N`/`MAX_CONTINUE` bound the fold. A request whose input ends with a `compaction_trigger` item is a remote-compaction request and is NEVER folded (the trigger is positional and Codex expects exactly one `compaction` output item back) — it passes through as a single round with `proxy_stopped_reason: "compaction_request"`. The `DONE` sentinel object represents SSE `data: [DONE]` across the transport boundary. `fold()` is the sole owner of downstream terminal shapes — `RoundOpenError` never escapes it (a rejected round 1 becomes a `response.failed` yielded by fold itself).
- **`server.py`** — Starlette transports around `fold()`. Downstream: WebSocket `/v1/responses` first (Codex's `responses_websockets` protocol: `response.create` envelope frames, connection reused across turns). That protocol is STATEFUL — Codex sends `generate:false` prewarm frames and compresses follow-ups to `previous_response_id` + incremental (possibly empty) input; `WsSession` implements this contract per connection (prewarm acked locally with a synthetic `resp_codexcomp_prewarm_*` id, incremental frames rebuilt to full input from `last_input + last_output + delta`, unknown ids fail loud and close the socket so Codex resends full input). Neither `generate` nor `previous_response_id` may ever reach the upstream SSE endpoint (it 400s on `generate`). Also: POST SSE fallback (request body may be zstd/gzip-compressed), plus transparent passthrough for everything else under `/v1/*` (e.g. `GET /v1/models`) and `/healthz`. Both transports drive one folded request through the shared `drive_fold` async generator (owns the `UpstreamRounds` lifecycle); the handlers only serialize frames. Upstream is always plain SSE POST — `UpstreamRounds.open` is the `RoundOpener` handed to `fold()`; it keeps round 1's response headers for the transports and replays the `x-codex-turn-state` sticky-routing token on continuation rounds (set-once, mirroring Codex's per-turn OnceLock; skipped if the client pinned its own). Codex reads real signal from response headers, so they must reach it: the POST handler mirrors round-1 upstream headers onto the downstream response — live-verified 2026-07-07, the backend's POST response carries the full `x-codex-*` rate-limit snapshot family, `x-models-etag` and `x-oai-request-id` (but NO `x-reasoning-included` / `x-codex-turn-state` today; the replay code is contract-faithful dormant support). The WS accept deliberately stays bare: the real backend's 101 handshake carries no `x-reasoning-included` either, and declaring flags the upstream doesn't would skew Codex's context accounting vs a direct connection. The app is assembled by `build_app(upstream_base)` (called by the CLI with `--upstream`; falls back to `CODEXCOMP_UPSTREAM_BASE` env, then the official backend) — there is no module-level `app`. Wiring defaults (`DEFAULT_HOST`/`DEFAULT_PORT`/`DEFAULT_UPSTREAM`) live once in `codexcomp/__init__.py`.
- **`cli.py`** — argparse entry for both `codexcomp` (console) and `codexcompw` (Windows GUI-subsystem, windowless). `_bind_headless_streams()` exists because pythonw starts with `sys.stdout/stderr = None`, which would crash uvicorn at startup — don't remove it. A wired proxy must own its exact port: if the port is busy it fails loudly and exits (no port drift, by design).
- **`service.py`** — strictly opt-in autostart: systemd user unit (Linux/WSL), launchd LaunchAgent (macOS), manual Startup-shortcut instructions only on Windows (no silent registration — AV heuristics). Installing the package never registers anything.

## Invariants to preserve

- **Auth passthrough only**: the `Authorization` header is forwarded untouched and never read, persisted, or logged. Keep it that way in any logging change.
- **Loopback only**: default bind is 127.0.0.1 and docs tell users to keep it there.
- **Clean rounds pass through byte-for-byte**; the fold path only engages on a detected truncation. Terminal events from a fold report single-response usage (input from round 1, reasoning summed), with the true cumulative cost under `metadata.proxy_billed_usage` and per-round breakdown under `metadata.proxy_rounds`.
- Upstream EOF without a terminal event, mid-stream errors, and failed continuation opens all end in a synthesized `response.incomplete`; a rejected round 1 ends in `response.failed` — never silently drop or fabricate a completed answer. All of these are minted inside `fold()`, not in the transports.
- `README.md` and `README.zh-CN.md` are maintained in parallel — user-visible changes go to both.

Mechanism credit (neteroster/CodexCont, MIT) is retained in the READMEs and the `fold.py` docstring. `LICENSE` stays pure MIT text with no appended notes.

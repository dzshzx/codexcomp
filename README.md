# codexcomp

[![PyPI](https://img.shields.io/pypi/v/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Python](https://img.shields.io/pypi/pyversions/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![License: MIT](https://img.shields.io/pypi/l/codexcomp.svg)](https://github.com/dzshzx/codexcomp/blob/main/LICENSE)

**English** · [简体中文](README.zh-CN.md)

A lightweight local Responses proxy for the **OpenAI Codex CLI** that mitigates gpt-5.5's
**"516" reasoning truncation** — it overrides the built-in provider's base URL in place, so
`model_provider` is unchanged and session grouping, remote compaction, and remote-control
keep working.

```bash
uv tool install codexcomp      # install
codexcomp                      # run (127.0.0.1:8787)
# then append to ~/.codex/config.toml:  openai_base_url = "http://127.0.0.1:8787/v1"
```

> **Credits.** The detect-and-continue mechanism originates from
> [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont) (MIT); this is an
> independent, from-scratch implementation that keeps the built-in provider intact.

---

## The problem

gpt-5.5's reasoning is intermittently truncated at `reasoning_tokens == 518·n − 2`
(**516, 1034, 1552, …**): the turn stops mid-reasoning and answers from an incomplete
thought, degrading quality sharply. It's an upstream defect with no official fix
([openai/codex#30364](https://github.com/openai/codex/issues/30364)).

`codexcomp` sits on `127.0.0.1` between Codex and the upstream Responses API. On a `518n−2`
truncation it drives the model to keep reasoning and folds the extra rounds into a single
downstream response — Codex sees one complete, untruncated answer.

## How it works

A state machine (`codexcomp/fold.py`) runs per round:

1. **Detect** — `reasoning_tokens == 518n − 2` (`1 ≤ n ≤ 6`, ≤ 3 continuations) marks the
   round as truncated.
2. **Continue** — discard the tentative output and replay the round's reasoning items (incl.
   `encrypted_content`) plus one `phase:"commentary"` `"Continue thinking..."` message as the
   next input.
3. **Fold** — stream reasoning live, flush only the final clean round, and rebuild the terminal
   event as one response (reasoning summed, true cost under `metadata.proxy_billed_usage`).

## Wiring

One top-level config key points Codex at the proxy:

```toml
# ~/.codex/config.toml  (top level, before the first [table])
openai_base_url = "http://127.0.0.1:8787/v1"
```

It overrides the base URL of the built-in `openai` provider in place. The provider id stays
`openai`, so history isn't re-bucketed, remote compaction stays on, and remote-control is
untouched — unlike a separate `[model_providers]` entry.

## Install

Requires [uv](https://docs.astral.sh/uv/) and the Codex CLI (ChatGPT OAuth; tested on 0.142.x).

```bash
uv tool install codexcomp                                  # from PyPI
# uv tool install git+https://github.com/dzshzx/codexcomp  # or from source
```

Run `codexcomp` (foreground, `127.0.0.1:8787`) and wire Codex with the config line above.
Disable by removing that line and stopping the proxy; upgrade / uninstall with
`uv tool upgrade codexcomp` / `uv tool uninstall codexcomp`.

The port must match `openai_base_url`; if 8787 is busy the proxy exits — pass `--port N` and
update `openai_base_url` to match.

## Autostart (optional, off by default)

Installation registers nothing; opt in explicitly.

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

## Develop

```bash
git clone https://github.com/dzshzx/codexcomp && cd codexcomp
uv sync
uv run python test_fold.py        # fold state-machine self-test → ALL PASS
uv run codexcomp                  # run locally
```

Releases go out via PyPI Trusted Publishing (OIDC, no stored token): push a `v*` tag to build
and publish.

## Security & disclaimer

- **Auth passthrough only** — forwards Codex's `Authorization` header; never reads, persists,
  or logs a credential.
- **Loopback only** — do not expose it on a non-loopback interface.
- **Unofficial** — it relies on non-contract upstream behavior; an OpenAI-side change may break
  it. Use at your own risk.
- Continuation spends **extra real tokens** (`metadata.proxy_billed_usage`), bounded by an `n`
  window and a 3-round cap.

## Community

Built for and shared with the [**LINUX DO**](https://linux.do) community, where the gpt-5.5
"516" degradation was diagnosed. Feedback and issues welcome there and on
[GitHub Issues](https://github.com/dzshzx/codexcomp/issues).

## License

[MIT](LICENSE) — mechanism credit to
[**neteroster/CodexCont**](https://github.com/neteroster/CodexCont) (MIT), whose 518n−2
detect-and-continue idea this reuses with an independent implementation; its copyright notice
is retained in [LICENSE](LICENSE).

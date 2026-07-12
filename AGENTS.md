# Agent Rules

Full development guide (architecture, commands, release checklist) lives in `CLAUDE.md` — reading @CLAUDE.md once at the start of a session is recommended; no need to re-read it later in the same session.
Machine-wide behavior contract and machine facts are carried by the global instruction layer; this file only records project facts a non-Claude agent must know.

## Project boundaries

- codexcomp is a local loopback proxy (`127.0.0.1:8787`) between the OpenAI Codex CLI and the upstream Responses API; it is wired into Codex via the top-level `openai_base_url` key (NOT a `[model_providers]` entry).
- Invariants: Authorization header is passthrough-only (never read/log/persist); bind stays loopback-only; clean rounds pass through byte-for-byte; on EOF/error synthesize `response.incomplete` — never silently drop or fabricate a completed answer.
- Test-before-touch: run `test_fold.py` before changing `fold.py`, `test_ws.py` before changing `server.py`'s WebSocket path (plain assert scripts ending in `ALL PASS`; there is no pytest/lint setup).
- Run `codexcomp-eval` / `codexcomp-sudoku-eval` only intentionally: they invoke Codex and consume real tokens/quota.
- `README.md` and `README.zh-CN.md` are maintained in parallel; keep the neteroster/CodexCont mechanism credit; `LICENSE` stays pure MIT text.
- Follow the release checklist in `CLAUDE.md` end to end — including the final local-deploy upgrade (`uv tool upgrade codexcomp` + `systemctl --user restart codexcomp`); the systemd unit never self-updates.

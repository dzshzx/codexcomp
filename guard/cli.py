"""codex-516-guard CLI entry point (installed via [project.scripts])."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex-516-guard",
        description=(
            "Local Responses proxy for Codex CLI: detects the gpt-5.5 518n-2 "
            "reasoning-truncation fingerprint, auto-continues thinking, and folds "
            "all rounds into one response. Wire Codex to it with the top-level "
            'config key: openai_base_url = "http://127.0.0.1:8787/v1"'
        ),
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1; keep it loopback)")
    parser.add_argument("--port", type=int, default=8787, help="bind port (default: 8787)")
    parser.add_argument("--upstream", default=None,
                        help="upstream base URL (default: https://chatgpt.com/backend-api/codex)")
    parser.add_argument("--log-level", default="info",
                        choices=["critical", "error", "warning", "info", "debug"])
    args = parser.parse_args()

    if args.upstream:
        os.environ["GUARD_UPSTREAM_BASE"] = args.upstream
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s:%(name)s:%(message)s")
    uvicorn.run("guard.server:app", host=args.host, port=args.port,
                log_level=args.log_level)


if __name__ == "__main__":
    main()

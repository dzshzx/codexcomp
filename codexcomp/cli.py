"""codexcomp CLI entry point (installed via [project.scripts]).

Usage:
  codexcomp [--host H] [--port P] [--upstream U] [--log-level L]   run the proxy
  codexcomp install-service   [same flags]   opt-in autostart for this platform
  codexcomp uninstall-service                remove the autostart entry
"""
from __future__ import annotations

import argparse
import logging
import os
import socket

from . import service

PORT_SCAN_TRIES = 20


def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1; keep it loopback)")
    p.add_argument("--port", type=int, default=8787,
                   help="bind port (default: 8787). Must match Codex's openai_base_url; "
                        "if busy the proxy exits (a wired proxy must own its exact port).")
    p.add_argument("--auto-port", action="store_true",
                   help="if --port is busy, scan for the next free port and print it. "
                        "Only for interactive one-off runs — you must then wire Codex to "
                        "the printed port. Not for a wired background service.")
    p.add_argument("--upstream", default=None,
                   help="upstream base URL (default: https://chatgpt.com/backend-api/codex)")
    p.add_argument("--log-level", default="info",
                   choices=["critical", "error", "warning", "info", "debug"])


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _pick_port(host: str, start: int) -> int:
    """First free port at or after `start` (scans up to PORT_SCAN_TRIES). Falls
    back to `start` so the bind fails loudly if nothing free was found."""
    for port in range(start, start + PORT_SCAN_TRIES):
        if not _port_in_use(host, port):
            return port
    return start


def _serve(args) -> int:
    import uvicorn
    if args.upstream:
        os.environ["CODEXCOMP_UPSTREAM_BASE"] = args.upstream
    port = args.port
    if args.auto_port:
        port = _pick_port(args.host, args.port)
        if port != args.port:
            print(f"port {args.port} in use; bound {port} instead — "
                  f"wire Codex to  openai_base_url = \"http://{args.host}:{port}/v1\"",
                  flush=True)
    elif _port_in_use(args.host, args.port):
        # A wired proxy must own its exact port — fail loudly, don't drift.
        print(f"error: port {args.port} is already in use. Free it, or pick another "
              f"port with --port N (and set Codex's openai_base_url to match). "
              f"Use --auto-port only for interactive one-off runs.", flush=True)
        return 1
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s:%(name)s:%(message)s")
    uvicorn.run("codexcomp.server:app", host=args.host, port=port,
                log_level=args.log_level)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codexcomp",
        description=(
            "Local Responses proxy for Codex CLI: detects the gpt-5.5 518n-2 "
            "reasoning-truncation fingerprint, auto-continues thinking, and folds "
            "all rounds into one response. Wire Codex to it with the top-level "
            'config key: openai_base_url = "http://127.0.0.1:8787/v1". '
            "Run with no subcommand to start the proxy."
        ),
    )
    _add_run_flags(parser)
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser(
        "install-service",
        help="opt-in: register autostart (systemd user / launchd / scheduled task)")
    _add_run_flags(p_install)

    sub.add_parser("uninstall-service", help="remove the autostart entry")
    p_run = sub.add_parser("run", help="start the proxy (default when no subcommand)")
    _add_run_flags(p_run)

    args = parser.parse_args()

    if args.cmd == "install-service":
        raise SystemExit(service.install(args.host, args.port, args.upstream, args.log_level))
    if args.cmd == "uninstall-service":
        raise SystemExit(service.uninstall())
    raise SystemExit(_serve(args))


if __name__ == "__main__":
    main()

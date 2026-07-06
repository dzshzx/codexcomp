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
import sys
import tempfile

from . import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_UPSTREAM, fold, service


def _bind_headless_streams() -> None:
    """pythonw (the codexcompw gui-scripts entry) starts with sys.stdout/sys.stderr
    = None; print() and uvicorn's stderr logging would then crash the process at
    startup. Bind both to an append-mode log file so the windowless entry survives
    and stays observable."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    log_dir = os.path.join(base, "codexcomp")
    os.makedirs(log_dir, exist_ok=True)
    stream = open(os.path.join(log_dir, "codexcompw.log"), "a",
                  buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_RUN_FLAG_DEFAULTS = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "upstream": None,
                      "log_level": "info", "max_n": None, "max_continue": None}


def _add_run_flags(p: argparse.ArgumentParser, *, subcommand: bool = False) -> None:
    # Subparsers get SUPPRESS defaults: with a real default they would clobber
    # a value the top-level parser already read (`codexcomp --port 9999 run`
    # would silently run on 8787). Missing attrs are backfilled after parsing.
    def dflt(key: str):
        return argparse.SUPPRESS if subcommand else _RUN_FLAG_DEFAULTS[key]

    p.add_argument("--host", default=dflt("host"),
                   help=f"bind address (default: {DEFAULT_HOST}; keep it loopback)")
    p.add_argument("--port", type=int, default=dflt("port"),
                   help=f"bind port (default: {DEFAULT_PORT}). Must match Codex's openai_base_url; "
                        "if busy the proxy exits (a wired proxy must own its exact port).")
    p.add_argument("--upstream", default=dflt("upstream"),
                   help=f"upstream base URL (default: {DEFAULT_UPSTREAM})")
    p.add_argument("--log-level", default=dflt("log_level"),
                   choices=["critical", "error", "warning", "info", "debug"])
    p.add_argument("--max-n", type=int, default=dflt("max_n"), metavar="N",
                   help="highest 518n-2 tier to auto-continue, 0 = no cap "
                        f"(default: {fold.MAX_N})")
    p.add_argument("--max-continue", type=int, default=dflt("max_continue"), metavar="K",
                   help="max continuation rounds per request "
                        f"(default: {fold.MAX_CONTINUE})")


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _serve(args) -> int:
    import uvicorn

    from .server import build_app
    if _port_in_use(args.host, args.port):
        # A wired proxy must own its exact port — fail loudly, don't drift.
        print(f"error: port {args.port} is already in use. Free it, or pick another "
              f"port with --port N (and set Codex's openai_base_url to match).",
              flush=True)
        return 1
    if args.max_n is not None:
        fold.MAX_N = args.max_n
    if args.max_continue is not None:
        fold.MAX_CONTINUE = args.max_continue
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s:%(name)s:%(message)s")
    uvicorn.run(build_app(args.upstream), host=args.host, port=args.port,
                log_level=args.log_level)
    return 0


def main() -> None:
    _bind_headless_streams()
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
    _add_run_flags(p_install, subcommand=True)

    sub.add_parser("uninstall-service", help="remove the autostart entry")
    p_run = sub.add_parser("run", help="start the proxy (default when no subcommand)")
    _add_run_flags(p_run, subcommand=True)

    args = parser.parse_args()
    for key, value in _RUN_FLAG_DEFAULTS.items():
        if not hasattr(args, key):  # SUPPRESS left it unset
            setattr(args, key, value)
    if args.max_n is not None and args.max_n < 0:
        parser.error("--max-n must be >= 0 (0 = no cap)")
    if args.max_continue is not None and args.max_continue < 0:
        parser.error("--max-continue must be >= 0")

    if args.cmd == "install-service":
        raise SystemExit(service.install(args.host, args.port, args.upstream,
                                         args.log_level, args.max_n, args.max_continue))
    if args.cmd == "uninstall-service":
        raise SystemExit(service.uninstall())
    raise SystemExit(_serve(args))


if __name__ == "__main__":
    main()

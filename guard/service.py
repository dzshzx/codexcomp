"""Optional autostart registration — strictly opt-in.

`codex-516-guard install-service` writes and activates a per-user autostart entry
for the current platform; `uninstall-service` removes it. Plain `uv tool install`
never touches any of this — autostart is always the user's explicit choice.

Per-user (not system-wide) on every platform: the proxy is a loopback service
used by Codex inside the user's own login session, needs the user's proxy
environment to reach upstream, and requires no root.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "codex-516-guard"
MAC_LABEL = "com.dzshzx.codex-516-guard"


def _resolve_exe() -> str:
    """Absolute path to the installed console-script executable.

    Prefer PATH lookup (stable ~/.local/bin entry). Fall back to sys.argv[0]
    when the tool dir isn't on PATH — and on Windows re-attach the .exe suffix,
    which sys.argv[0] drops for console-script launchers.
    """
    found = shutil.which(LABEL)
    if found:
        return found
    cand = os.path.abspath(sys.argv[0])
    if os.name == "nt" and not cand.lower().endswith(".exe") and os.path.exists(cand + ".exe"):
        cand += ".exe"
    return cand


def _exe_and_args(host: str | None, port: int | None,
                  upstream: str | None, log_level: str | None) -> list[str]:
    """Resolved executable path plus any non-default run flags."""
    argv = [_resolve_exe()]
    if host and host != "127.0.0.1":
        argv += ["--host", host]
    if port and port != 8787:
        argv += ["--port", str(port)]
    if upstream:
        argv += ["--upstream", upstream]
    if log_level and log_level != "info":
        argv += ["--log-level", log_level]
    return argv


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


# --- Linux (systemd user unit) ----------------------------------------------


def _systemd_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user" / f"{LABEL}.service"


def _install_linux(argv: list[str]) -> None:
    if not shutil.which("systemctl"):
        raise RuntimeError(
            "systemctl not found. Write a unit manually or use your init system; "
            "see the systemd example in the project README.")
    exec_start = " ".join(argv)
    unit = f"""[Unit]
Description=codex-516-guard: local Responses proxy folding gpt-5.5 518n-2 reasoning truncation
After=network-online.target

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
# Drop a possible SOCKS proxy from the user manager env; httpx honors HTTP(S)_PROXY.
UnsetEnvironment=ALL_PROXY all_proxy SOCKS_PROXY socks_proxy

[Install]
WantedBy=default.target
"""
    path = _systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", f"{LABEL}.service"])
    print(f"installed + started systemd user service: {path}")
    print("  tip: run 'loginctl enable-linger' once to start it at boot without login")
    print(f"  disable: codex-516-guard uninstall-service   (or systemctl --user disable --now {LABEL})")


def _uninstall_linux() -> None:
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{LABEL}.service"], check=False)
    path = _systemd_unit_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"removed systemd user service{'' if existed else ' (was not present)'}: {path}")


# --- macOS (launchd LaunchAgent) --------------------------------------------


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _install_macos(argv: list[str]) -> None:
    args_xml = "\n".join(f"        <string>{a}</string>" for a in argv)
    log = f"/tmp/{LABEL}.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>{MAC_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>
    <key>StandardOutPath</key>  <string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist)
    uid = os.getuid()
    _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=False)
    _run(["launchctl", "enable", f"gui/{uid}/{MAC_LABEL}"], check=False)
    _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{MAC_LABEL}"], check=False)
    print(f"installed + started launchd LaunchAgent: {path}")
    print(f"  logs: {log}")
    print("  disable: codex-516-guard uninstall-service")


def _uninstall_macos() -> None:
    path = _plist_path()
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    print(f"removed launchd LaunchAgent{'' if existed else ' (was not present)'}: {path}")


# --- Windows (Startup-folder VBS launcher; no admin, hidden window) ----------
#
# Task Scheduler (onlogon) would also work but `schtasks /create` requires an
# elevated shell on locked-down machines. A Startup-folder launcher needs no
# admin; a tiny VBS wrapper runs the console-less proxy fully hidden.


def _startup_vbs_path() -> Path:
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / \
        "Start Menu" / "Programs" / "Startup"
    return startup / f"{LABEL}.vbs"


def _install_windows(argv: list[str]) -> None:
    cmd = subprocess.list2cmdline(argv)              # e.g. "C:\...\codex-516-guard.exe"
    vbs_literal = '"' + cmd.replace('"', '""') + '"'  # VBScript string literal
    vbs = f'CreateObject("WScript.Shell").Run {vbs_literal}, 0, False\n'
    path = _startup_vbs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(vbs, encoding="utf-8")
    _run(["taskkill", "/im", f"{LABEL}.exe", "/f"], check=False)  # drop any stale instance
    _run(["wscript", str(path)], check=False)                    # start now, hidden
    print(f"installed + started Startup launcher (no admin, hidden): {path}")
    print("  runs at next logon; disable with: codex-516-guard uninstall-service")


def _uninstall_windows() -> None:
    path = _startup_vbs_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    _run(["taskkill", "/im", f"{LABEL}.exe", "/f"], check=False)  # stop the running proxy
    print(f"removed Startup launcher{'' if existed else ' (was not present)'}: {path}")


# --- dispatch ----------------------------------------------------------------


def install(host=None, port=None, upstream=None, log_level=None) -> int:
    argv = _exe_and_args(host, port, upstream, log_level)
    system = platform.system()
    try:
        if system == "Linux":
            _install_linux(argv)
        elif system == "Darwin":
            _install_macos(argv)
        elif system == "Windows":
            _install_windows(argv)
        else:
            print(f"unsupported platform: {system}", file=sys.stderr)
            return 2
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"install-service failed: {detail}", file=sys.stderr)
        return 1
    return 0


def uninstall() -> int:
    system = platform.system()
    if system == "Linux":
        _uninstall_linux()
    elif system == "Darwin":
        _uninstall_macos()
    elif system == "Windows":
        _uninstall_windows()
    else:
        print(f"unsupported platform: {system}", file=sys.stderr)
        return 2
    return 0

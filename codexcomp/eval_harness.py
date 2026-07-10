#!/usr/bin/env python3
"""Shared A/B eval harness for codexcomp's continuation folding.

Drives one puzzle through a model x effort x proxy-on/off grid of `codex exec`
calls, records reasoning tokens + correctness + per-round fold data per run, and
prints a per-condition summary. The puzzle itself — prompt, grader, output dir —
is supplied by the caller as an `EvalSpec`; `codexcomp-eval` (candy) and
`codexcomp-sudoku-eval` are two such callers. This is the harness behind the
measurements posted to openai/codex#30364 (issuecomment-4893087004).

Both modes wire `openai_base_url` explicitly, so results do not depend on the
ambient ~/.codex/config.toml wiring:
  on  -> the local codexcomp proxy (must already be running)
  off -> the upstream backend directly

Per-round fold data for `on` runs is read from the systemd journal when the
codexcomp user unit is active; attribution uses journal cursors captured per
run. Without the unit (e.g. a foreground `codexcomp`), the summary falls back
to final-usage fingerprints, which undercounts folded runs (folded usage is
summed across rounds and usually leaves the 518n-2 lattice).

Runs are serial by default because per-round journal capture attributes fold
lines to a run by a time-cursor window, which needs one run at a time (and the
proxy serving no other traffic). `--parallel N` opts into N concurrent runs:
faster, but it disables per-round journal capture (folded runs are then detected
from usage fingerprints only) and spends tokens faster. In serial mode, if a run
times out the proxy may still be finishing that fold when the next starts, so the
harness waits after a failed `on` run to keep the next cursor window clean.

Results append to <out>/results.jsonl; completed run ids are skipped, so an
interrupted eval resumes by re-running the same command.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class EvalSpec:
    """One puzzle to drive through the A/B grid.

    grader receives the run's final-message text and returns whether it counts
    as correct; it is the only puzzle-specific logic in the whole harness.
    """
    description: str                 # argparse description (first docstring line)
    prompt: str                      # task prompt handed to `codex exec`
    grader: Callable[[str], bool]    # final-message text -> correct?
    default_out: str                 # default output directory
    default_models: str = "gpt-5.5"


STEP = 518
BOUNDARIES = {STEP * n - 2 for n in range(1, 41)}
TIMEOUTS = {"low": 600, "medium": 600, "high": 1200, "xhigh": 1800,
            "max": 2400, "ultra": 3600}
# fold.py round verdicts that mean "a 518n-2 truncation was detected"
TRUNCATION_VERDICTS = {"continue", "tier_out_of_window", "max_continue",
                       "no_encrypted_content"}
ORPHAN_FOLD_GRACE_S = 15  # wait after a failed `on` run for its fold to finish
FOLD_ROUND_RE = re.compile(
    # cached= may be absent (>=0.3.4 omits it when unknown) or literally
    # `cached=None` (<=0.3.3 printed the raw value) — both must not break the match
    r"round (\d+): in=(\d+) (?:cached=(\d+|None) )?out=(\d+) reason=(\d+) total=(\d+) \| "
    r"n=(None|\d+) buffered=(\[.*?\]) -> (\w+)"
)


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def proxy_unit_active() -> bool:
    """Per-round data needs journald entries, i.e. codexcomp under systemd."""
    if not (shutil.which("systemctl") and shutil.which("journalctl")):
        return False
    probe = subprocess.run(["systemctl", "--user", "is-active", "codexcomp"],
                           capture_output=True, text=True)
    return probe.returncode == 0


def journal_cursor() -> tuple[bool, str | None]:
    """(ok, cursor). ok=False means journalctl itself failed — the caller must
    skip journal capture for the run rather than fall back to an unbounded read
    that would attribute the whole history to it."""
    probe = subprocess.run(
        ["journalctl", "--user", "-u", "codexcomp", "-n", "1",
         "--show-cursor", "-q", "--no-pager"],
        capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"warning: journalctl failed (rc={probe.returncode}) — "
              "skipping fold capture for this run")
        return False, None
    for line in probe.stdout.splitlines():
        if line.startswith("-- cursor:"):
            return True, line.split("-- cursor:", 1)[1].strip()
    return True, None  # empty journal: read unbounded, everything there is new


def journal_fold_lines(cursor: str | None) -> list[str]:
    cmd = ["journalctl", "--user", "-u", "codexcomp",
           "--no-pager", "-q", "--output=short-iso"]
    if cursor:
        cmd += ["--after-cursor", cursor]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "codexcomp.fold" in l]


def fold_rounds(lines: list[str]) -> list[dict]:
    rounds = []
    for line in lines:
        m = FOLD_ROUND_RE.search(line)
        if m:
            rounds.append({"reason": int(m[5]), "verdict": m[9]})
    return rounds


def run_once(args, out_dir: Path, run_id: str, model: str, effort: str,
             mode: str, use_journal: bool, workdir: Path, spec: EvalSpec) -> dict:
    last_f = out_dir / f"last_{run_id}.txt"
    ev_f = out_dir / f"events_{run_id}.jsonl"
    fold_f = out_dir / f"fold_{run_id}.log"
    # A prior interrupted attempt may have left artifacts; a stale last_*.txt
    # would grade a failed re-run as correct.
    last_f.unlink(missing_ok=True)
    fold_f.unlink(missing_ok=True)

    base_url = args.proxy if mode == "on" else args.upstream
    cmd = ["timeout", str(TIMEOUTS[effort]), "codex", "exec", "--json",
           "--ephemeral", "-C", str(workdir), "--skip-git-repo-check",
           "-s", "read-only", "--disable", "memories",
           "-m", model, "-c", f"model_reasoning_effort={effort}",
           "-c", f'openai_base_url="{base_url}"',
           "-o", str(last_f), spec.prompt]

    t0 = time.time()
    started = iso_now()
    capture, cursor = (journal_cursor() if (mode == "on" and use_journal)
                       else (False, None))
    with open(ev_f, "w") as evh:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=evh,
                              stderr=subprocess.PIPE, text=True)
    time.sleep(1)  # journald flush margin

    usage = None
    for line in ev_f.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")

    answer = last_f.read_text().strip() if last_f.exists() else ""
    lines = journal_fold_lines(cursor) if capture else []
    if lines:
        fold_f.write_text("\n".join(lines) + "\n")

    return {
        "id": run_id, "model": model, "effort": effort, "mode": mode,
        "exit": proc.returncode, "duration_s": round(time.time() - t0, 1),
        "started": started,
        "correct": bool(spec.grader(answer)),
        "reasoning_tokens": (usage or {}).get("reasoning_output_tokens"),
        "usage": usage,
        "fold_rounds": fold_rounds(lines),
        "stderr_tail": proc.stderr[-300:] if proc.returncode != 0 else "",
    }


def is_boundary_cut(rec: dict) -> bool:
    if rec["fold_rounds"]:
        return any(r["verdict"] in TRUNCATION_VERDICTS for r in rec["fold_rounds"])
    return rec["reasoning_tokens"] in BOUNDARIES


def summarize(recs: list[dict]) -> str:
    lines = [f"{'model':9} {'effort':7} {'mode':4} {'cut':>5} {'ok':>5}  reasoning tokens"]
    effort_order = {e: i for i, e in enumerate(TIMEOUTS)}
    conds = sorted({(r["model"], r["effort"], r["mode"]) for r in recs},
                   key=lambda c: (c[0], effort_order.get(c[1], len(effort_order)), c[2]))
    for model, effort, mode in conds:
        rs = [r for r in recs
              if (r["model"], r["effort"], r["mode"]) == (model, effort, mode)]
        ok = sum(r["correct"] for r in rs)
        cut = sum(1 for r in rs if is_boundary_cut(r))
        toks = sorted((r["reasoning_tokens"] if r["reasoning_tokens"] is not None else -1)
                      for r in rs)
        lines.append(f"{model:9} {effort:7} {mode:4} {cut:>2}/{len(rs)} {ok:>2}/{len(rs)}"
                     f"  {toks}")
    return "\n".join(lines)


def load_results(path: Path) -> list[dict]:
    recs = []
    if not path.exists():
        return recs
    for i, line in enumerate(path.read_text().splitlines(), 1):
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"note: skipping unparsable {path}:{i} "
                  "(interrupted write from a previous attempt)")
    return recs


def run_eval(spec: EvalSpec) -> int:
    parser = argparse.ArgumentParser(
        description=spec.description,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-m", "--models", default=spec.default_models,
                        help="comma-separated model list")
    parser.add_argument("-r", "--efforts", default="medium,xhigh",
                        help="comma-separated reasoning efforts")
    parser.add_argument("--modes", default="on,off",
                        help="comma-separated: on (via proxy) / off (direct)")
    parser.add_argument("-n", "--reps", type=int, default=4)
    parser.add_argument("--proxy", default="http://127.0.0.1:8787/v1",
                        help="codexcomp base URL for `on` runs")
    parser.add_argument("--upstream", default="https://chatgpt.com/backend-api/codex",
                        help="direct base URL for `off` runs")
    parser.add_argument("--out", default=spec.default_out,
                        help="output directory (results.jsonl + per-run artifacts)")
    parser.add_argument("--parallel", type=int, default=1, metavar="N",
                        help="run up to N codex exec calls concurrently (default 1 = "
                             "serial). N>1 speeds the grid up but disables per-round "
                             "journal capture and spends tokens faster (expect more "
                             "rate-limit errors); opt in only when you want it.")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for effort in efforts:
        if effort not in TIMEOUTS:
            parser.error(f"unknown effort {effort!r} (choose from {list(TIMEOUTS)})")
    for mode in modes:
        if mode not in ("on", "off"):
            parser.error(f"unknown mode {mode!r} (choose from on, off)")
    if args.parallel < 1:
        parser.error("--parallel must be >= 1")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "workdir"
    workdir.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"

    recs = load_results(results_path)
    done_ids = {r["id"] for r in recs}

    use_journal = "on" in modes and proxy_unit_active()
    if use_journal and args.parallel > 1:
        print("note: --parallel N>1 interleaves runs in the journal, so per-round "
              "fold attribution is disabled; folded runs are detected from usage "
              "fingerprints only (the 518n-2 lattice), which undercounts them.")
        use_journal = False
    if "on" in modes and not use_journal:
        print("note: per-round fold data unavailable (systemd unit inactive or "
              "--parallel set) — folded runs are undercounted from usage alone.")

    grid = [(model, effort, mode, rep)
            for rep in range(1, args.reps + 1)  # interleave conditions across time
            for model in models for effort in efforts for mode in modes]
    grid_ids = {f"{m}_{e}_{md}_r{r}" for m, e, md, r in grid}
    pending = [c for c in grid
               if f"{c[0]}_{c[1]}_{c[2]}_r{c[3]}" not in done_ids]
    counter = {"done": len(grid_ids & done_ids), "warned_no_fold": False}
    lock = threading.Lock()

    def record(rec: dict) -> None:
        # serialize result persistence + progress print across worker threads
        with lock:
            recs.append(rec)
            counter["done"] += 1
            with open(results_path, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{iso_now()}] {counter['done']}/{len(grid)} {rec['id']} "
                  f"exit={rec['exit']} reason={rec['reasoning_tokens']} "
                  f"correct={rec['correct']}", flush=True)
            if (use_journal and rec["mode"] == "on" and rec["exit"] == 0
                    and not rec["fold_rounds"] and not counter["warned_no_fold"]):
                print("warning: an `on` run produced no fold lines in the journal — "
                      "the unit may log above info level, journald may lag, or Codex "
                      "traffic isn't reaching the systemd codexcomp unit")
                counter["warned_no_fold"] = True

    def do(cell: tuple) -> dict:
        model, effort, mode, rep = cell
        run_id = f"{model}_{effort}_{mode}_r{rep}"
        return run_once(args, out_dir, run_id, model, effort, mode,
                        use_journal, workdir, spec)

    if args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            for fut in as_completed([pool.submit(do, c) for c in pending]):
                record(fut.result())
    else:
        for cell in pending:
            rec = do(cell)
            record(rec)
            if cell[2] == "on" and rec["exit"] != 0:
                # the proxy may still be finishing this run's fold; keep its late
                # log lines out of the next run's cursor window
                time.sleep(ORPHAN_FOLD_GRACE_S)
            time.sleep(2)

    print()
    grid_recs = [r for r in recs if r["id"] in grid_ids]  # match the counter's scope
    print(summarize(grid_recs))
    failures = [r["id"] for r in grid_recs if r["exit"] != 0]
    if failures:
        print(f"\nfailed runs (excluded from nothing, judge for yourself): {failures}")
    return 0

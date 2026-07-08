#!/usr/bin/env python3
"""A/B eval: does codexcomp's continuation folding fix the candy puzzle?

Runs a model x effort x proxy-on/off grid of `codex exec` calls on the candy
pigeonhole puzzle (answer: 21), records reasoning tokens + correctness per run,
and prints a per-condition summary. This is the harness behind the measurements
posted to openai/codex#30364 (issuecomment-4893087004).

Task credit: haowang02/codex-candy-eval (the community degradation eval);
prompt reproduced verbatim, graded by the same standalone-21 rule. The answer
(21) was independently verified by brute-force min-max before adoption.

Both modes wire `openai_base_url` explicitly, so results do not depend on the
ambient ~/.codex/config.toml wiring:
  on  -> the local codexcomp proxy (must already be running)
  off -> the upstream backend directly

Per-round fold data for `on` runs is read from the systemd journal when the
codexcomp user unit is active; attribution uses journal cursors captured per
run. Without the unit (e.g. a foreground `codexcomp`), the summary falls back
to final-usage fingerprints, which undercounts folded runs (folded usage is
summed across rounds and usually leaves the 518n-2 lattice).

Assumptions: runs are serial and the proxy serves no other traffic during the
eval. If a run times out, the proxy may still be finishing that fold when the
next run starts; the harness waits after a failed `on` run to keep the next
cursor window clean, but concurrent foreign traffic cannot be told apart.

Usage:
  codexcomp-eval                                      # default grid, 5 reps
  codexcomp-eval -m gpt-5.5 -r xhigh -n 3 --modes on,off
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
import time
from datetime import datetime, timezone
from pathlib import Path

PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")

STEP = 518
BOUNDARIES = {STEP * n - 2 for n in range(1, 41)}
TIMEOUTS = {"low": 600, "medium": 600, "high": 1200, "xhigh": 1800}
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
             mode: str, use_journal: bool, workdir: Path) -> dict:
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
           "-o", str(last_f), PROMPT]

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
        "correct": bool(ANSWER_PATTERN.search(answer)),
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-m", "--models", default="gpt-5.4,gpt-5.5",
                        help="comma-separated model list")
    parser.add_argument("-r", "--efforts", default="low,medium,high,xhigh",
                        help="comma-separated reasoning efforts")
    parser.add_argument("--modes", default="on,off",
                        help="comma-separated: on (via proxy) / off (direct)")
    parser.add_argument("-n", "--reps", type=int, default=5)
    parser.add_argument("--proxy", default="http://127.0.0.1:8787/v1",
                        help="codexcomp base URL for `on` runs")
    parser.add_argument("--upstream", default="https://chatgpt.com/backend-api/codex",
                        help="direct base URL for `off` runs")
    parser.add_argument("--out", default="evals/results",
                        help="output directory (results.jsonl + per-run artifacts)")
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

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "workdir"
    workdir.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"

    recs = load_results(results_path)
    done_ids = {r["id"] for r in recs}

    use_journal = "on" in modes and proxy_unit_active()
    if "on" in modes and not use_journal:
        print("note: codexcomp systemd user unit not active — per-round fold "
              "data will be missing; folded runs are undercounted from usage alone.")

    grid = [(model, effort, mode, rep)
            for rep in range(1, args.reps + 1)  # interleave conditions across time
            for model in models for effort in efforts for mode in modes]
    grid_ids = {f"{m}_{e}_{md}_r{r}" for m, e, md, r in grid}
    done_in_grid = len(grid_ids & done_ids)
    warned_no_fold = False

    for model, effort, mode, rep in grid:
        run_id = f"{model}_{effort}_{mode}_r{rep}"
        if run_id in done_ids:
            continue
        rec = run_once(args, out_dir, run_id, model, effort, mode,
                       use_journal, workdir)
        recs.append(rec)
        done_in_grid += 1
        with open(results_path, "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{iso_now()}] {done_in_grid}/{len(grid)} {run_id} "
              f"exit={rec['exit']} reason={rec['reasoning_tokens']} "
              f"correct={rec['correct']}", flush=True)
        if (use_journal and mode == "on" and rec["exit"] == 0
                and not rec["fold_rounds"] and not warned_no_fold):
            print("warning: an `on` run produced no fold lines in the journal — "
                  "the unit may log above info level, journald may lag, or Codex "
                  "traffic isn't reaching the systemd codexcomp unit")
            warned_no_fold = True
        if mode == "on" and rec["exit"] != 0:
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


if __name__ == "__main__":
    sys.exit(main())

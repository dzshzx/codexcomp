#!/usr/bin/env python3
"""A/B eval: does codexcomp's continuation folding fix the candy puzzle?

Drives the candy pigeonhole puzzle (answer: 21) through the shared eval harness
(codexcomp.eval_harness) across a model x effort x proxy-on/off grid, recording
reasoning tokens + correctness per run and printing a per-condition summary.

Task credit: haowang02/codex-candy-eval (the community degradation eval);
prompt reproduced verbatim, graded by the same standalone-21 rule. The answer
(21) was independently verified by brute-force min-max before adoption.

Usage:
  codexcomp-eval                                      # default grid, 5 reps
  codexcomp-eval -m gpt-5.5 -r xhigh -n 3 --modes on,off
See codexcomp.eval_harness for the full harness contract (journal capture,
resume, summary) and the shared CLI flags.
"""
from __future__ import annotations

import re
import sys

from .eval_harness import EvalSpec, run_eval

PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")

SPEC = EvalSpec(
    description=__doc__.splitlines()[0],
    prompt=PROMPT,
    grader=lambda answer: bool(ANSWER_PATTERN.search(answer)),
    default_out="evals/results",
)


def main() -> int:
    return run_eval(SPEC)


if __name__ == "__main__":
    sys.exit(main())

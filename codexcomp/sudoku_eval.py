#!/usr/bin/env python3
"""A/B eval: does codexcomp's folding hold up on a long-reasoning sudoku?

Drives a hard 6x6 arithmetic-cage sudoku (KenKen-style) through the shared eval
harness (codexcomp.eval_harness) across a model x effort x proxy-on/off grid.
The four cells not covered by any cage (r1c3, r4c1, r4c4, r6c1) are given as
fixed anchors, which bounds run times; the rest still needs a chained deduction,
so runs spend far more reasoning tokens than the candy pigeonhole puzzle and hit
the 518n-2 truncation lattice across many rounds — a stress test for whether
continuation folding stays stable.

The unique solution's 10-digit verification code is 5322366662 (read from
r1c4 r2c6 r3c2 r4c5 r5c1 r6c6 r2c3 r3c5 r5c4 r6c1); a run is graded correct iff
that code appears in the final message, ignoring any separators between the
digits. Full grid (rows top to bottom):
  6 3 4 5 1 2 / 1 5 6 2 4 3 / 4 2 1 3 6 5 / 5 6 3 1 2 4 / 3 4 2 6 5 1 / 2 1 5 4 3 6

Usage:
  codexcomp-sudoku-eval                               # default grid, 5 reps
  codexcomp-sudoku-eval -m gpt-5.5 -r xhigh -n 3 --modes on,off
See codexcomp.eval_harness for the full harness contract (journal capture,
resume, summary) and the shared CLI flags.
"""
from __future__ import annotations

import re
import sys

from .eval_harness import EvalSpec, run_eval

CODE = "5322366662"


def grade(answer: str) -> bool:
    """Correct iff the verification code appears in the final message. Any
    non-digit separators a model may place between the ten digits (spaces,
    punctuation, formatting) are ignored, so `5 3 2 2 3 6 6 6 6 2` also counts."""
    return CODE in re.sub(r"\D", "", answer)


PROMPT = """你必须完全依靠内部推理作答。
不得联网搜索，不得调用任何工具，不得调取记忆，不得写代码运行，不得使用计算器、表格、脚本或外部求解器。
即使你曾经见过类似题目，也必须只根据下面题面重新推理。
请仔细推理，并在作答前进行两轮自检。
最终只输出完整 6×6 方阵和最后的 10 位验证码，省略推理过程。

题目如下：

在一个 6×6 方格中填入数字 1 到 6。每一行、每一列都必须恰好包含 1、2、3、4、5、6 各一次。

方格坐标用 r行c列 表示，例如 r3c5 表示第 3 行第 5 列。

每个笼区满足给出的运算结果。
+ 表示笼区内所有数字之和。
× 表示笼区内所有数字之积。
- 只用于两个格子，表示两个数字差的绝对值。
÷ 只用于两个格子，表示较大数除以较小数。
= 表示该格固定为给定数字。

没有列入笼区列表的格子没有额外笼区约束，只需满足行列规则。

笼区列表：

1. r6c3 r6c2：+6
2. r2c6 r1c6 r1c5 r1c4：×30
3. r4c3 r4c2：÷2
4. r1c2 r1c1：+9
5. r5c5 r4c5：+7
6. r3c2 r3c3 r2c3：×12
7. r6c4 r6c5：-1
8. r3c6 r3c5：-1
9. r4c6 r5c6 r6c6：×24
10. r2c5 r2c4 r3c4：×24
11. r2c1 r2c2 r3c1：+10
12. r5c1 r5c2：+7
13. r5c3 r5c4：-4
14. r1c3：=4
15. r4c1：=5
16. r4c4：=1
17. r6c1：=2

解出完整方阵后，按以下坐标读取数字，拼成 10 位验证码：

r1c4 r2c6 r3c2 r4c5 r5c1 r6c6 r2c3 r3c5 r5c4 r6c1

最终输出格式：

方阵：
行1: ...
行2: ...
行3: ...
行4: ...
行5: ...
行6: ...

验证码：XXXXXXXXXX
"""

SPEC = EvalSpec(
    description=__doc__.splitlines()[0],
    prompt=PROMPT,
    grader=grade,
    default_out="evals/sudoku",
)


def main() -> int:
    return run_eval(SPEC)


if __name__ == "__main__":
    sys.exit(main())

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

`codexcomp` 是一个位于 OpenAI Codex CLI 与上游 Responses API 之间的本地环回代理（127.0.0.1:8787）。它检测 gpt-5.5 的 `518n − 2` 推理截断指纹（516、1034、1552、… reasoning tokens——openai/codex#30364），驱动模型继续思考，并把所有轮次折叠成一个完整的下游响应。Codex 通过官方顶层 `openai_base_url` key 接入它——刻意不是 `[model_providers]` 条目，因为更改 provider id 会把会话历史重新分桶，并丢掉 remote compaction/remote-control。

## 命令

```bash
uv sync                        # install deps into .venv
uv run python test_fold.py     # fold state-machine self-test → "ALL PASS"
uv run python test_ws.py       # WS stateful-protocol (prewarm/incremental) self-test → "ALL PASS"
uv run codexcomp               # run the proxy locally (foreground, 127.0.0.1:8787)
uv run codexcomp-eval          # candy A/B eval (spends real tokens; run manually as needed)
uv run codexcomp-sudoku-eval   # hard 6×6 sudoku A/B eval — longer reasoning stress test (spends real tokens)
uv build                       # build sdist + wheel
```

没有 pytest/lint/typecheck 设置——两个测试都是带 assert 的纯脚本，直接运行它们。改 `fold.py` 前先跑 `test_fold.py`，改 `server.py` 的 WebSocket 路径前先跑 `test_ws.py`。

发布清单（每一步都做，按顺序）：在 `pyproject.toml` 中 bump `version` → commit → push `master` → push 匹配的 `v*` tag → 等 `.github/workflows/release.yml` 变绿（它经 Trusted Publishing 构建并发布到 PyPI——OIDC，无存储 token）→ 确认该版本已在 PyPI 上线 → 用 `gh release create v* --title … --notes-file …` 创建 GitHub Release（workflow 不会做这一步；推了 tag 却没有 Release 会让 Releases 页显示过时的 latest；notes 以英文为主，在一条 `---` 之后附一小段 `### 中文说明`）→ 如果本机有活跃的本地部署，升级它：`uv tool upgrade codexcomp` + `systemctl --user restart codexcomp`。systemd unit 运行来自 `~/.local/bin/codexcomp` 的 uv-tool 快照，且绝不自更新——跳过升级会让活跃代理停留在过时版本（v0.3.5/v0.3.6 于 2026-07-08 发布，而 unit 直到 2026-07-10 仍在提供 0.3.4）。截至 2026-07-10 本地部署已卸载（gpt-5.6 显示 0pp 提升，见 README FAQ），所以在为 gpt-5.5 用途重新安装代理之前，这一步是空操作。

## 架构

`codexcomp/` 下四个小模块，带一个中心接缝：

- **`fold.py`** —— 核心：一个与传输无关的状态机。`fold(base_body, open_round)` 把上游事件当作 dict 消费，产出当作 dict 的下游事件；它对 SSE 或 WebSocket 一无所知。每轮它对 output items 分类：`reasoning` items 实时流式透传（带代理自有的 `sequence_number` 和重新编号的 `output_index`），其余一切（messages、tool calls）作为暂定项**缓冲**。在 `518n−2` 终止时，它把原始 input + 累积的 reasoning items（含 `encrypted_content`）+ 一条 `phase:"commentary"` 的 "Continue thinking..." 提示作为下一轮的 input 重放；只有最后一个干净轮次的缓冲输出会被 flush。一个返回 `reasoning_tokens == 0` 的续写轮次（round ≥ 2）是**零停滞**——提示失败了，于是重新提示而非接受，共享同一个 `MAX_CONTINUE` 预算（round 1 的零 reasoning 是合法的完整回答，绝不进入这条路径）。常量 `STEP`/`MIN_N`/`MAX_N`/`MAX_CONTINUE` 约束折叠。input 以 `compaction_trigger` item 结尾的请求是 remote-compaction 请求，绝不折叠（该 trigger 是位置相关的，Codex 期望恰好返回一个 `compaction` output item）——它作为单轮透传，带 `proxy_stopped_reason: "compaction_request"`。`DONE` 哨兵对象在传输边界上代表 SSE `data: [DONE]`。`fold()` 是下游终止形态的唯一所有者——`RoundOpenError` 绝不逃出它（被拒的 round 1 会变成由 fold 自身产出的 `response.failed`）。
- **`server.py`** —— 围绕 `fold()` 的 Starlette 传输层。下游：优先 WebSocket `/v1/responses`（Codex 的 `responses_websockets` 协议：`response.create` envelope 帧，连接跨轮复用）。该协议是有状态的——Codex 发送 `generate:false` prewarm 帧，并把后续请求压缩成 `previous_response_id` + 增量（可能为空）input；`WsSession` 按连接实现这个契约（prewarm 在本地用一个合成的 `resp_codexcomp_prewarm_*` id 应答，增量帧从 `last_input + last_output + delta` 重建成完整 input，未知 id 高声失败并关闭 socket，让 Codex 重发完整 input）。`generate` 和 `previous_response_id` 都绝不可到达上游 SSE endpoint（它遇到 `generate` 会 400）。此外：POST SSE 回退（请求体可能是 zstd/gzip 压缩的），外加对 `/v1/*` 下其余一切（如 `GET /v1/models`）和 `/healthz` 的透明透传。两种传输都通过共享的 `drive_fold` 异步生成器驱动一个折叠请求（拥有 `UpstreamRounds` 生命周期）；handler 只负责序列化帧。上游始终是纯 SSE POST——`UpstreamRounds.open` 是交给 `fold()` 的 `RoundOpener`；它为传输层保留 round 1 的响应 headers，并在续写轮次重放 `x-codex-turn-state` sticky-routing token（一次性设定，镜像 Codex 的 per-turn OnceLock；若客户端已钉住自己的则跳过）。Codex 从响应 headers 读取真实信号，所以它们必须到达它：POST handler 把 round-1 上游 headers 镜像到下游响应上——2026-07-07 实时验证，后端的 POST 响应携带完整的 `x-codex-*` rate-limit 快照家族、`x-models-etag` 和 `x-oai-request-id`（但今天没有 `x-reasoning-included` / `x-codex-turn-state`；重放代码是忠于契约的休眠支持）。WS accept 刻意保持裸态：真实后端的 101 握手同样不携带 `x-reasoning-included`，声明上游没有的 flag 会相较直连扭曲 Codex 的上下文核算。app 由 `build_app(upstream_base)` 组装（由 CLI 用 `--upstream` 调用；回退到 `CODEXCOMP_UPSTREAM_BASE` env，再到官方后端）——没有模块级 `app`。接线默认值（`DEFAULT_HOST`/`DEFAULT_PORT`/`DEFAULT_UPSTREAM`）只在 `codexcomp/__init__.py` 中存在一处。
- **`cli.py`** —— `codexcomp`（控制台）和 `codexcompw`（Windows GUI 子系统，无窗口）两者的 argparse 入口。`_bind_headless_streams()` 存在是因为 pythonw 启动时 `sys.stdout/stderr = None`，那会让 uvicorn 在启动时崩溃——别删它。一个已接线的代理必须占有它确切的端口：若端口被占用，它高声失败并退出（不做端口漂移，是设计使然）。
- **`service.py`** —— 严格 opt-in 的自启动：systemd user unit（Linux/WSL）、launchd LaunchAgent（macOS），Windows 上只给手动 Startup 快捷方式说明（不做静默注册——AV 启发式）。安装该包绝不注册任何东西。

`eval_harness.py`、`candy_eval.py` 和 `sudoku_eval.py` 也位于 `codexcomp/` 下，但不属于代理数据通路：它们是随包发布的仅用 stdlib 的 A/B eval harness。`eval_harness.py` 拥有所有与谜题无关的机制（`codex exec` 的 model×effort×proxy-on/off 网格、journal 折叠轮次捕获、518n−2 边界检测、resume、summary），并暴露 `run_eval(spec)`；每个 eval 脚本只是一个 `EvalSpec`（prompt + grader + output dir）——`candy_eval.py` 是糖果鸽笼谜题（答案 21），作为 `codexcomp-eval`；`sudoku_eval.py` 是更难的 6×6 算术笼数独（verification code 5322366662），作为 `codexcomp-sudoku-eval`，其更长的链式推理把 reasoning 推得足够远，以跨多轮锻炼折叠。要加新谜题就再写一个 `EvalSpec`，而不是复制 harness。

## 需保持的不变量

- **仅 Auth 透传**：`Authorization` header 原封不动地转发，绝不读取、持久化或记录。任何日志相关改动都要保持这一点。
- **仅环回**：默认 bind 是 127.0.0.1，文档也告诉用户保持在那里。
- **干净轮次逐字节透传**；折叠路径只在检测到截断时才介入。折叠的终止事件报告单响应用量（input 取自 round 1，reasoning 求和），真实累计成本在 `metadata.proxy_billed_usage` 下，逐轮明细在 `metadata.proxy_rounds` 下。
- 没有终止事件的上游 EOF、流中途错误、以及续写打开失败，都以一个合成的 `response.incomplete` 收尾；被拒的 round 1 以 `response.failed` 收尾——绝不静默丢弃或伪造已完成的回答。这些全部在 `fold()` 内部铸造，而非在传输层。
- `README.md` 与 `README.zh-CN.md` 并行维护——用户可见的改动两者都要改。

机制致谢（neteroster/CodexCont，MIT）保留在两个 README 和 `fold.py` docstring 中。`LICENSE` 保持纯 MIT 文本，不附加任何说明。

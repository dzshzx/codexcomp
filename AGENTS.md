# Agent Rules

完整开发指南（架构、命令、发布清单）在 `CLAUDE.md`；建议会话开始时读一次 @CLAUDE.md，同一会话内不必重读。
通用行为契约与机器事实由全局指令层承载；本文件只记非 Claude agent 需额外知道的项目事实。

## 项目边界

- codexcomp 是 OpenAI Codex CLI 与上游 Responses API 之间的本地环回代理（`127.0.0.1:8787`）；它通过顶层 `openai_base_url` key 接入 Codex（不是 `[model_providers]` 条目）。
- 不变量：Authorization header 仅透传（绝不读取/记录/持久化）；bind 仅保持环回；干净轮次逐字节透传；遇 EOF/error 合成 `response.incomplete`——绝不静默丢弃或伪造已完成的回答。
- 改前先测：改 `fold.py` 前先跑 `test_fold.py`，改 `server.py` 的 WebSocket 路径前先跑 `test_ws.py`（以 `ALL PASS` 结尾的纯 assert 脚本；没有 pytest/lint 设置）。
- `codexcomp-eval` / `codexcomp-sudoku-eval` 只在有意为之时运行：它们会调用 Codex 并消耗真实 tokens/quota。
- `README.md` 与 `README.zh-CN.md` 并行维护；保留 neteroster/CodexCont 的机制致谢；`LICENSE` 保持纯 MIT 文本。
- 完整走一遍 `CLAUDE.md` 中的发布清单——包括最后的本地部署升级（`uv tool upgrade codexcomp` + `systemctl --user restart codexcomp`）；systemd unit 绝不自更新。

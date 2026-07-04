# codexcomp

[![PyPI](https://img.shields.io/pypi/v/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Python](https://img.shields.io/pypi/pyversions/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![License: MIT](https://img.shields.io/pypi/l/codexcomp.svg)](https://github.com/dzshzx/codexcomp/blob/main/LICENSE)

[English](README.md) · **简体中文**

面向 **OpenAI Codex CLI** 的轻量本地 Responses 代理，用于消解 gpt-5.5 的
**「516 降智」推理截断**——它就地覆盖内置 provider 的 base URL，**不改动 `model_provider`**，
因此会话分组、远程压缩与 remote-control 均不受影响。

```bash
uv tool install codexcomp      # 安装
codexcomp                      # 运行（127.0.0.1:8787）
# 随后在 ~/.codex/config.toml 顶层追加：  openai_base_url = "http://127.0.0.1:8787/v1"
```

> **致谢。** 「检测截断 + 续写」的机制思路源自
> [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont)（MIT）；本项目为独立的
> 全新实现，并改为保留内置 provider 不变。

---

## 问题

gpt-5.5 的推理偶发在 `reasoning_tokens == 518·n − 2`（**516、1034、1552 …**）处被截断：该轮推理
中途终止、基于不完整的思考给出答案，质量骤降。此为上游缺陷，尚无官方修复
（[openai/codex#30364](https://github.com/openai/codex/issues/30364)）。

`codexcomp` 监听 `127.0.0.1`，位于 Codex 与上游 Responses API 之间。命中 `518n−2` 截断时，
它驱动模型继续推理，并将多出的续写轮折叠为单个下游响应——Codex 收到的是一次完整、未截断的答案。

## 工作原理

状态机（`codexcomp/fold.py`）逐轮运行：

1. **检测** — `reasoning_tokens == 518n − 2`（`1 ≤ n ≤ 6`，续写上限 3 轮）即判定该轮被截断。
2. **续写** — 丢弃该轮暂定输出，将其 reasoning items（含 `encrypted_content`）连同一条
   `phase:"commentary"` 的 `"Continue thinking..."` 消息重放为下一轮 input。
3. **折叠** — 推理流全程实时透传，仅放行收尾轮的最终输出，并将 terminal 事件重建为单个响应
   （reasoning 累加，真实累计开销记于 `metadata.proxy_billed_usage`）。

## 接线

一个顶层 config key 将 Codex 指向代理：

```toml
# ~/.codex/config.toml  （顶层，须位于第一个 [table] 之前）
openai_base_url = "http://127.0.0.1:8787/v1"
```

它就地覆盖内置 `openai` provider 的 base URL。provider id 仍为 `openai`，因此会话历史不会按
provider 重新分桶、远程压缩保持可用、remote-control 不受影响——这与单独的 `[model_providers]`
条目不同。

## 安装

依赖 [uv](https://docs.astral.sh/uv/) 与 Codex CLI（ChatGPT OAuth 登录；在 0.142.x 上验证）。

```bash
uv tool install codexcomp                                  # 从 PyPI 安装
# uv tool install git+https://github.com/dzshzx/codexcomp  # 或从源码安装
```

运行 `codexcomp`（前台，`127.0.0.1:8787`），并按上面的配置行将 Codex 接入。**停用**：删除该行并
停止代理；升级 / 卸载用 `uv tool upgrade codexcomp` / `uv tool uninstall codexcomp`。

端口须与 `openai_base_url` 一致；若 8787 被占用，代理会报错退出——用 `--port N` 并同步
`openai_base_url`。

## 开机自启（可选，默认关闭）

安装本身不注册任何自启项，需显式启用。

```bash
codexcomp install-service     # 注册并启动（当前平台）
codexcomp uninstall-service   # 撤销
```

- **Linux / WSL** — systemd **user** unit；执行一次 `loginctl enable-linger` 可开机（无需登录）启动。
- **macOS** — `~/Library/LaunchAgents/` 下的 launchd **LaunchAgent**。
- **Windows** — 仅打印手动步骤：将启动项快捷方式（`Win+R` → `shell:startup`）指向无窗口入口
  `codexcompw`（`where.exe codexcompw`）。删除该快捷方式即取消。

若 WSL2 为 `networkingMode=mirrored`，Windows 与 WSL 共享 `127.0.0.1`：在 WSL 内跑单个代理，
Windows 侧仅需追加同样的 `openai_base_url` 行——无需第二个代理。

## 验证

```bash
curl -sS http://127.0.0.1:8787/healthz            # {"ok":true,...}
journalctl --user -u codexcomp -f | grep -E 'round|done'   # Linux/WSL
```

命中折叠时的日志——两个连续的 516 被折叠，答案正确：

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

## 开发

```bash
git clone https://github.com/dzshzx/codexcomp && cd codexcomp
uv sync
uv run python test_fold.py        # 折叠状态机自测 → ALL PASS
uv run codexcomp                  # 本地运行
```

发布经 PyPI Trusted Publishing（OIDC，无存储 token）：推 `v*` tag 即自动构建并上传。

## 安全与免责

- **仅 auth passthrough** — 透传 Codex 的 `Authorization` 头，不读取、不持久化、不记录任何凭据。
- **仅回环** — 请勿暴露于非回环接口。
- **非官方** — 依赖上游非公开契约的行为，OpenAI 侧变更可能使其失效，风险自负。
- 续写会消耗**额外的实际 token**（`metadata.proxy_billed_usage`），由 `n` 窗口与 3 轮上限约束。

## 社区

本项目为 [**LINUX DO**](https://linux.do) 社区而作并在其中分享——gpt-5.5「516 降智」即于此社区
被定位。欢迎在社区帖或 [GitHub Issues](https://github.com/dzshzx/codexcomp/issues) 反馈。

## 许可

[MIT](LICENSE) — 机制思路 credit：[**neteroster/CodexCont**](https://github.com/neteroster/CodexCont)
（MIT），本项目复用其 518n−2「检测截断 + 续写」的思路、代码为独立实现；其版权声明保留于
[LICENSE](LICENSE)。

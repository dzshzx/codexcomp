<div align="center">

# codexcomp

**Codex + Complete** — 面向 [OpenAI Codex CLI](https://github.com/openai/codex) 的轻量本地代理，
将 gpt-5.5 的**「516 降智」推理截断**折叠为完整、未截断的答案。

[![PyPI](https://img.shields.io/pypi/v/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Python](https://img.shields.io/pypi/pyversions/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![Downloads](https://img.shields.io/pypi/dm/codexcomp.svg)](https://pypi.org/project/codexcomp/)
[![License: MIT](https://img.shields.io/pypi/l/codexcomp.svg)](https://github.com/dzshzx/codexcomp/blob/master/LICENSE)

[English](README.md) · **简体中文**

</div>

```bash
uv tool install codexcomp      # 安装
codexcomp                      # 运行（127.0.0.1:8787）
# 随后在 ~/.codex/config.toml 顶层追加：  openai_base_url = "http://127.0.0.1:8787/v1"
```

它**就地覆盖**内置 provider 的 base URL——`model_provider` 不变，因此会话分组、远程压缩与
remote-control 均不受影响。

> **致谢。** 「检测截断 + 续写」的机制思路源自
> [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont)（MIT）；本项目为独立的
> 全新实现，并改为保留内置 provider 不变。

---

## 问题

gpt-5.5 的推理偶发在 `reasoning_tokens == 518·n − 2`（**516、1034、1552 …**）处被截断：该轮推理
中途终止、基于不完整的思考给出答案，质量骤降。上游报告的聚合遥测显示，gpt-5.5 达到 516 推理
token 的响应中约 44 % 恰好停在该边界——此为上游缺陷，尚无官方修复
（[openai/codex#30364](https://github.com/openai/codex/issues/30364)）。

`codexcomp` 监听 `127.0.0.1`，位于 Codex 与上游 Responses API 之间。命中 `518n−2` 截断时，
它驱动模型继续推理，并将多出的续写轮折叠为单个下游响应——Codex 收到的是一次完整、未截断的答案。

## 特性

- **检测 → 续写 → 折叠** — 识别 `518n−2` 指纹，重放该轮 reasoning 并附续写提示，将全部轮次
  折叠为单个响应。
- **零侵入接线** — 仅一个官方顶层 `openai_base_url` key；不加 `[model_providers]` 条目、
  不改 provider id、会话历史不重新分桶。
- **WebSocket 第一传输** — 原生实现 `responses_websockets` 协议（信封帧、同连接串行复用、
  prewarm）；Codex 日志中零「Falling back」噪音。
- **健壮的 SSE 兜底** — POST 路径自动解压 zstd/gzip 上游响应。
- **完整 `/v1/*` 透传** — 含 `GET /v1/models`（模型目录刷新）。
- **协议保真：响应头与远程压缩** — POST 路径的上游响应头原样抵达 Codex（`x-codex-*`
  rate-limit 快照、`x-models-etag`、request id），上游若签发 `x-codex-turn-state`
  sticky 路由令牌则在折叠各轮间回传，远程压缩请求永不折叠。
- **实时流式** — 折叠过程中推理流全程实时透传；仅放行收尾轮的最终输出。
- **如实计费** — 折叠各轮的真实累计开销记于 `metadata.proxy_billed_usage`。
- **上游连接池自愈** — 连接池耗尽或僵死（`PoolTimeout`、失效的代理握手）时把共享上游客户端
  轮换到新 generation，而不是每个请求都失败直到重启；per-client 租约保证进行中的流与折叠
  绝不被中断，`/healthz` 报告 generation 与活跃计数。
- **仅回环 + auth passthrough** — 透传 Codex 的 `Authorization` 头，不读取、不持久化、
  不记录任何凭据。
- **自启 opt-in** — 安装不注册任何自启项；一条命令生成 systemd user unit（Linux/WSL）或
  LaunchAgent（macOS）。

## 快速开始

依赖 [uv](https://docs.astral.sh/uv/) 与 Codex CLI（ChatGPT OAuth 登录；在 0.142.x 上验证）。

```bash
uv tool install codexcomp                                  # 从 PyPI 安装
# uv tool install git+https://github.com/dzshzx/codexcomp  # 或从源码安装
codexcomp                                                  # 前台运行，127.0.0.1:8787
```

用一个顶层 config key 将 Codex 指向代理：

```toml
# ~/.codex/config.toml  （顶层，须位于第一个 [table] 之前）
openai_base_url = "http://127.0.0.1:8787/v1"
```

就这些。**停用**：删除该行并停止代理；升级 / 卸载用 `uv tool upgrade codexcomp` /
`uv tool uninstall codexcomp`。

## 工作原理

状态机（`codexcomp/fold.py`）逐轮运行：

1. **检测** — `reasoning_tokens == 518n − 2`（默认不限档位，实测最高见过 n=21 的截断；
   见 `--max-n` / `--max-continue`）即判定该轮被截断。
2. **续写** — 丢弃该轮暂定输出，将其 reasoning items（含 `encrypted_content`）连同一条
   `phase:"commentary"` 的 `"Continue thinking..."` 消息重放为下一轮 input。若某续写轮返回零
   推理 token（续写提示落空），则再次续写而非接受，消耗同一 `--max-continue` 预算。
3. **折叠** — 推理流全程实时透传，仅放行收尾轮的最终输出，并将 terminal 事件重建为单个响应
   （reasoning 累加，真实累计开销记于 `metadata.proxy_billed_usage`）。

## CLI 参考

| 命令 | 说明 |
| --- | --- |
| `codexcomp` / `codexcomp run` | 前台启动代理。 |
| `codexcomp install-service` | opt-in：注册当前平台的自启项。 |
| `codexcomp uninstall-service` | 撤销自启项。 |
| `codexcompw` | 无窗口入口（Windows）；日志写入 `%LOCALAPPDATA%\codexcomp\codexcompw.log`。 |

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 绑定地址——请保持回环。 |
| `--port` | `8787` | 须与 `openai_base_url` 一致；被占用时报错退出。 |
| `--upstream` | `https://chatgpt.com/backend-api/codex` | 上游 base URL。 |
| `--log-level` | `info` | `critical` / `error` / `warning` / `info` / `debug` 之一。 |
| `--max-n` | `0` | 自动续写的最高 `518n−2` 档位；`0` = 不设上限（实测最高见过 n=21 的截断）。 |
| `--max-continue` | `3` | 单请求续写轮数上限（防失控护栏）。 |

## 开机自启（可选，默认关闭）

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

健康检查响应还会报告当前上游客户端 generation、活跃上游请求数和 WebSocket 数。如果共享上游
连接池耗尽，codexcomp 会记录脱敏后的连接池快照、只轮换一次客户端，并让 Codex 的下一次重试
直接使用新连接池，无需重启进程。代理握手失败且没有其他活跃上游流时也会执行同样的恢复，避免
失效握手不断累积并最终耗尽连接池。上游读取停滞超过 120 秒会按正常终止契约超时收尾，
而不是把连接池连接钉住数分钟。

命中折叠时的日志——两个连续的 516 被折叠，答案正确：

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

round 行结论：`continue`（检出截断 → 将续写）、`clean`（自然结束）、
`tier_out_of_window` / `max_continue` / `no_encrypted_content`（检出截断但按原样放行）、
`compaction_request`（远程压缩请求——永不折叠）、
`upstream_eof`（流结束但无 terminal 事件）。`done:` 行以 `stop=natural` 或放行原因收尾。
客户端断连导致的折叠中止会记 `fold aborted downstream after N round(s)`，不会有 `done:` 行。

## 评测

包内内置两个 A/B 评测，安装后直接可用。二者都端到端度量修复效果：跑「模型 ×
effort × 代理开/关」的 `codex exec` 矩阵，按条件统计边界截断率、reasoning tokens
与正确率：

- `codexcomp-eval` ——
  [haowang02/codex-candy-eval](https://github.com/haowang02/codex-candy-eval)
  的糖果抽取题（答案 21，已独立穷举复核）；短小的陷阱题，欠思考的运行很快答错。
- `codexcomp-sudoku-eval` —— 带四个固定格锚点的高难 6×6 算术笼数独；链条推理消耗
  远多的 reasoning tokens，会跨多轮命中 `518n−2` 格点——折叠稳定性的压力测试。

两种模式都显式传 `openai_base_url`，不依赖本机既有接线；结果追加写入
`<out>/results.jsonl`，中断后重跑同一命令即断点续跑。`on` 模式的每轮 fold 明细在
systemd user unit 启用时从其 journal 读取。默认串行；`--parallel N` 可选并发 N 个
运行——更快，但会关闭每轮 journal 抓取（折叠运行只能靠用量指纹检出）、token 消耗
也更快。脚本用 coreutils `timeout` 包裹 `codex exec`，需 Linux/WSL（macOS 需装
coreutils）。

```bash
codexcomp &                                     # `on` 模式需要代理已在运行
codexcomp-eval -m gpt-5.5 -r xhigh -n 5         # 小矩阵
codexcomp-eval                                  # 默认矩阵（gpt-5.5 × medium,xhigh × 开/关 × 4 次）
codexcomp-sudoku-eval -r xhigh,ultra,max        # 长推理压力矩阵
```

在仓库内开发时用 `uv run codexcomp-eval` 等价。

糖果评测的一次 80 跑矩阵（2026-07-06）显示：无代理的 gpt-5.5 全部精确截断在
`518n−2` 边界上，开/关正确率 90% vs 15%——详见
[openai/codex#30364](https://github.com/openai/codex/issues/30364#issuecomment-4893087004)。

## 常见问题

**会影响正常（未截断）的轮次吗？**
不会。干净轮次逐字节透传；折叠路径只在检出 `518n−2` 截断时介入。

**一次折叠的代价是什么？**
续写轮会消耗额外的实际 token，由续写轮数上限（`--max-continue`，默认 3）约束。真实累计用量
记于 `metadata.proxy_billed_usage`。

**gpt-5.6 系列还能用吗？**
机制上能——`518n−2` 格点在 gpt-5.6 上依然出现，折叠也与 gpt-5.5 上完全一致地介入——
但它不再带来正确率收益：192 跑 A/B 矩阵中走代理与直连的正确率完全相同。5.6 命中格点
时答案通常已经完整，而 5.6 真正的失分（reasoning 坍缩到几百 token 的*非格点*值——欠
思考而非截断）任何代理都看不见。只用 5.6 的话建议删掉 `openai_base_url` 接线（折叠只
会多花 token 无收益）；对 gpt-5.5 项目依然完全有效。矩阵数据见
[#11](https://github.com/dzshzx/codexcomp/issues/11)。

**上游修复之后怎么办？**
无需任何操作——检测器不再命中，代理退化为透明透传。随时删除 `openai_base_url` 行即可脱线。

**为什么不用单独的 `[model_providers]` 条目？**
那会改变 provider id：会话历史按 provider 重新分桶，远程压缩与 remote-control 也随之失效。
`openai_base_url` 是就地覆盖内置 `openai` provider 的官方路径。

**我的凭据安全吗？**
代理原样透传 `Authorization` 头且仅绑定回环，不读取、不持久化、不记录任何凭据。

## 安全与免责

- **仅 auth passthrough** — 透传 Codex 的 `Authorization` 头，不读取、不持久化、不记录任何凭据。
- **仅回环** — 请勿暴露于非回环接口。
- **非官方** — 依赖上游非公开契约的行为，OpenAI 侧变更可能使其失效，风险自负。
- 续写会消耗**额外的实际 token**（`metadata.proxy_billed_usage`），由 `--max-continue` 上限（默认 3 轮）约束。

## 开发

```bash
git clone https://github.com/dzshzx/codexcomp && cd codexcomp
uv sync
uv run python test_fold.py        # 折叠状态机自测 → ALL PASS
uv run python test_ws.py          # 传输层自测（WS 协议、响应头）→ ALL PASS
uv run codexcomp                  # 本地运行
```

发布经 PyPI Trusted Publishing（OIDC，无存储 token）：推 `v*` tag 即自动构建并上传。
版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 参与贡献

最有价值的贡献是 bug 报告、折叠日志片段与复现细节——请提交到
[GitHub Issues](https://github.com/dzshzx/codexcomp/issues)。代码改动请在提 PR 前运行
`uv run python test_fold.py` 与 `uv run python test_ws.py`，并保持改动聚焦。

## 社区

本项目为 [**LINUX DO**](https://linux.do) 社区而作并在其中分享——gpt-5.5「516 降智」即于此社区
被定位。欢迎在社区帖或 [GitHub Issues](https://github.com/dzshzx/codexcomp/issues) 反馈。

## 许可

[MIT](LICENSE) — 机制思路 credit：[**neteroster/CodexCont**](https://github.com/neteroster/CodexCont)
（MIT），本项目复用其 518n−2「检测截断 + 续写」的思路、代码为独立从零实现。
糖果题评测任务与其 standalone-21 判分规则来源于
[**haowang02/codex-candy-eval**](https://github.com/haowang02/codex-candy-eval) —— 题面在此
注明来源并复用（上游仓库未声明 license）。

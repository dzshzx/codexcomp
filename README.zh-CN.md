# codex-516-guard

[![PyPI](https://img.shields.io/pypi/v/codex-516-guard.svg)](https://pypi.org/project/codex-516-guard/)
[![Python](https://img.shields.io/pypi/pyversions/codex-516-guard.svg)](https://pypi.org/project/codex-516-guard/)
[![License: MIT](https://img.shields.io/pypi/l/codex-516-guard.svg)](https://github.com/dzshzx/codex-516-guard/blob/main/LICENSE)

[English](README.md) · **简体中文**

一个给 **OpenAI Codex CLI** 用的轻量本地 Responses 代理，用来缓解 gpt-5.5 的
**「516 降智」思考截断**——而且**不改 `model_provider`**，所以会话分组、远程压缩、
remote-control 全部照常。

```bash
uv tool install codex-516-guard      # 安装
codex-516-guard                      # 运行（127.0.0.1:8787）
# 然后在 ~/.codex/config.toml 顶层加一行：  openai_base_url = "http://127.0.0.1:8787/v1"
```

> **致谢。** 「检测截断 + 自动续写」的思路来自
> [**neteroster/CodexCont**](https://github.com/neteroster/CodexCont)（MIT）——感谢原作者。
> 本项目是**全新实现**，改为保留内置 provider 不变；差异见[下方对比](#与-codexcont-的差异)。

---

## 问题：gpt-5.5 的「516 降智」

在 OpenAI Codex CLI 上，gpt-5.5 的思考有时会在一个很特定的 token 数处被截断——
`reasoning_tokens == 518 * n − 2`（也就是 **516、1034、1552 …**）。一旦某轮落在这个指纹上，
模型就提前停止思考，答案质量骤降。这是上游问题、无官方修复
（[openai/codex#30364](https://github.com/openai/codex/issues/30364)）。

`codex-516-guard` 跑在 `127.0.0.1` 上、夹在 Codex 与上游 Responses API 之间。当它发现某轮
命中 `518n−2` 指纹截断时，就**让模型继续思考**，并把多出来的续写轮**折叠成单个下游响应**——
Codex 收到的是一个干净、完整的答案。

## 工作原理

代理逐轮流式转发上游，并跑一个小状态机（`guard/fold.py`）：

1. **检测。** 每轮结束时读 `usage.output_tokens_details.reasoning_tokens`。若等于
   `518n − 2`（`1 ≤ n ≤ 6`，最多续写 3 轮），判定该轮被截断。
2. **续写。** 丢弃该轮的**暂定输出**（message / tool calls——它们基于被截断的思考），把该轮的
   reasoning items（含 `encrypted_content`）+ 一条 `phase:"commentary"` 助手消息
   （`"Continue thinking..."`）追加进下一轮 input 重放，促使模型接着思考。
3. **折叠。** 思考流全程实时透传给 Codex；只有**干净收尾**那一轮的最终输出被放行。terminal 事件
   被重建成「整件事是一个响应」的口径——`input`/`cached` 取第 1 轮（避免看起来像撑爆上下文），
   reasoning 求和，真实累计成本记在 `metadata.proxy_billed_usage`。

### 接线：为什么内置 provider 不变

Codex 用**一个顶层 config key** 指向代理，而不是新建 provider：

```toml
# ~/.codex/config.toml  （顶层，必须在第一个 [table] 之前）
openai_base_url = "http://127.0.0.1:8787/v1"
```

`openai_base_url` 就地覆盖**内置 `openai` provider** 的 base URL。这是官方支持的 key
（[openai/codex#16719](https://github.com/openai/codex/issues/16719)；同名
`[model_providers.openai]` 覆盖被维护者拒绝，`OPENAI_BASE_URL` 环境变量已移除）。因为
provider id 仍是 `openai`：

- 会话历史**不会**按 provider 重新分组 / 被隐藏；
- **远程压缩**照常（`supports_remote_compaction` 仍为真）；
- **remote-control** 不受影响（它走独立的 `chatgpt_base_url`）。

### 与 CodexCont 的差异

518n−2 检测 + 折叠续写这套机制是 [CodexCont] 的思路；这里的实现是全新的，并在几处有意分道：

|  | codex-516-guard | CodexCont |
| --- | --- | --- |
| **Codex 接线** | 顶层 `openai_base_url`（**内置 provider 不变**） | 新建 `[model_providers]`（历史按 provider 隐藏、remote-control 不可用、丢远程压缩） |
| **下游传输** | **WebSocket 第一传输**——完整实现 `responses_websockets` 协议，另有 SSE 兜底 | 仅 SSE（Codex 先试 ws → 405 → 每会话约 5 次重连告警后回退） |
| **zstd 请求体**（0.142.x 内置 provider） | 原生解压，无需改 Codex 配置 | 需 `[features] enable_request_compression = false` |
| **`GET /v1/models`**（模型目录刷新） | 透传（`/v1/*`） | 未代理（静默失败，靠缓存） |
| **续写方法** | 仅 commentary 法 | commentary + legacy tool-pair + 跨轮 repair，更多可配置项 |

[CodexCont]: https://github.com/neteroster/CodexCont

## 安装

需要 [uv](https://docs.astral.sh/uv/)（它帮你管 Python）和 Codex CLI（ChatGPT OAuth 登录，
0.142.x 实测）。

```bash
uv tool install codex-516-guard          # 从 PyPI 安装
# 或直接从源码：
# uv tool install git+https://github.com/dzshzx/codex-516-guard
```

uv 会把可执行文件放进它的 bin 目录（Unix/macOS 是 `~/.local/bin`；Windows 用
`where.exe codex-516-guard` 查；`uv tool update-shell` 可把该目录加进 PATH）。然后：

```bash
codex-516-guard                          # 前台运行（默认 127.0.0.1:8787）
codex-516-guard --port 8790 --log-level debug
```

按上面那一行把 Codex 接到它即可。**关闭**：注释掉 `openai_base_url` 行 + 停掉代理。（key 还在但
代理停了，Codex 会因上游不可达报错。）

升级 / 卸载：`uv tool upgrade codex-516-guard` / `uv tool uninstall codex-516-guard`。

### 端口

代理端口**必须**等于 Codex `openai_base_url` 里的端口。默认端口（8787）被占用时，代理**直接报错
退出**、绝不漂走——一个被接线的代理若静默换个端口绑，只会变成连不上。要换端口就 `--port N`，同时
把 `openai_base_url` 也改成同一个 `N`。

`--auto-port` 只给交互式随手跑：占用时向后找空闲端口，并打印该用哪个 `openai_base_url`。**不要**
用在被接线的后台服务上。

## 开机自启（可选，默认不开）

安装本身**不注册任何自启**——开不开完全由你决定。

```bash
codex-516-guard install-service     # 注册并启动（当前平台）
codex-516-guard uninstall-service   # 撤销
```

`install-service` 选「随用户登录、跑在用户会话内」的方式（系统级服务跑在无用户环境的 session 里，
够不到用户 profile 下 uv 装的 exe 和代理设置）：

- **Linux / WSL** → systemd **user** unit（`~/.config/systemd/user/`）。跑一次
  `loginctl enable-linger` 可让它开机（无需登录）就起。手动等价见
  `systemd/codex-516-guard.service.example`。
- **macOS** → `~/Library/LaunchAgents/` 里的 launchd **LaunchAgent**（随登录、在 GUI session 内）。
  用 `launchctl bootstrap gui/$(id -u) <plist>` / `launchctl kickstart -k …` 加载，
  `launchctl bootout …` 卸载。
- **Windows** → **只打印手动步骤、什么都不注册**（见下）。

### Windows 自启是手动的——有意为之

程序自动写入启动项（Startup VBS / 注册表 Run 键 / 计划任务）再拉起隐藏进程，会被行为杀软判为木马
持久化——Kaspersky 主动防御模块会把执行该动作的 `python.exe` 报 `PDM:Trojan.Win32.Generic`。而
**用户自己建**的启动项则被同一杀软信任。

所以本包提供无窗口入口 `codex-516-guardw`（Windows GUI 子系统 exe，登录时无黑框），
`install-service` 只告诉你怎么把快捷方式指向它：

1. `Win+R` → `shell:startup`（打开启动文件夹）；
2. 新建 → 快捷方式 → 目标填 `where.exe codex-516-guardw` 得到的路径（自定义端口就在后面加
   `--port N`）。

删掉该快捷方式即取消自启。

### 镜像网络捷径（WSL ↔ Windows）

若你的 WSL2 是 `networkingMode=mirrored`，Windows 与 WSL **共享 `127.0.0.1`**。那么两侧**只需一个**
代理——在 WSL 里跑（作为 systemd 服务），Windows 侧只在 `~/.codex/config.toml` 加同样指向
`127.0.0.1:8787` 的 `openai_base_url` 行即可，**不需要第二个代理、也不需要 Windows 自启**（唯一代价
是 Windows Codex 依赖 WSL 的代理在跑）。

## 验证

```bash
curl -sS http://127.0.0.1:8787/healthz            # {"ok":true,...}
journalctl --user -u codex-516-guard -f | grep -E 'round|done'   # Linux/WSL
```

命中折叠时的日志（实测，连环双 516 被击破、答案正确）：

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

## 开发

```bash
git clone https://github.com/dzshzx/codex-516-guard && cd codex-516-guard
uv sync
uv run python test_fold.py        # 折叠状态机自测 → ALL PASS
uv run codex-516-guard            # 本地跑
```

发布走 PyPI Trusted Publishing（`.github/workflows/release.yml`，OIDC，无 token）：推 `v*` tag
即自动构建上传。

结构：

- `guard/fold.py` — 指纹检测 + 折叠状态机（传输无关；`test_fold.py` 覆盖）。
- `guard/server.py` — starlette 传输层：ws / SSE 下游、SSE 上游、zstd/gzip 请求解压、`/v1/*` 透传。
- `guard/cli.py` — CLI 入口（`codex-516-guard`；仅监听回环；auth passthrough，不存任何凭据）。

## 安全与免责

- 代理只做 **auth passthrough**：转发 Codex 的 `Authorization` 头，不读取、不落盘、不打印任何凭据。
- 仅监听**回环**地址——不要暴露到非回环接口。
- **非官方**：依赖上游非公开契约的行为（截断指纹、ws 帧格式），OpenAI 侧变更可能使其失效，风险自负。
- 续写会花**额外的真实 token**（见 `metadata.proxy_billed_usage`）；guard 用 `n` 窗口 + 3 轮上限约束。

## 社区

本项目为 [**LINUX DO**](https://linux.do) 社区而作、并在其中分享——gpt-5.5「516 降智」正是在这里
被定位与讨论。欢迎在社区帖或 [GitHub Issues](https://github.com/dzshzx/codex-516-guard/issues) 反馈。

## 许可

[MIT](LICENSE)。**完整开源，无闭源部分。**

机制思路 credit：[**neteroster/CodexCont**](https://github.com/neteroster/CodexCont)（MIT）——本项目
复用其 518n−2「检测截断 + 续写」的**思路**，代码为全新独立实现，并改为保留内置 provider 不变
（见[与 CodexCont 的差异](#与-codexcont-的差异)）。CodexCont 的 MIT 版权声明保留在 [LICENSE](LICENSE) 中。

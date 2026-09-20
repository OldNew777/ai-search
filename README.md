# AI-Search Pipeline（Codex 自建检索与抓取）

> 目录：`E:\Softwares\AI-search`　设计文档：[`Pipeline-design.md`](./Pipeline-design.md)
> 建立时间：2026-09-20

## 组成

| 组件 | 角色 | 端口 | 状态 |
|---|---|---|---|
| SearXNG (Docker) | 搜索后端（元搜索，聚合上游引擎） | 127.0.0.1:47311 | ⏳ 待 Docker 引擎可用 |
| mcp-searxng | 搜索主链路 MCP（Codex 直连） | stdio | ✅ 已注册（依赖上面的 SearXNG） |
| Scrapling MCP | 抓取主链路（静态/JS/反爬，本机 Chromium） | stdio | ✅ 已验证可用 |
| open-webSearch | 兜底链第 1 级（本地 daemon + MCP） | 127.0.0.1:47313 | ✅ 已验证可用 |
| Firecrawl (keyless) | 兜底链第 2 级 | 远程 MCP | ✅ 已验证可用 |
| DuckDuckGo MCP | 兜底链第 3 级 | stdio | ✅ 已注册（网关内也用 ddgs 实现同级兜底） |
| fallback-search | 自建降级网关（确定性按序 fallback） | stdio | ✅ 三级降级已实测 |

## 常用命令

```powershell
# 启停与状态
& E:\Softwares\AI-search\scripts\start-all.ps1
& E:\Softwares\AI-search\scripts\status.ps1
& E:\Softwares\AI-search\scripts\stop-all.ps1

# MCP 探针（列出工具 / 调用某个工具）
$py = "E:\Softwares\AI-search\fallback-search\.venv\Scripts\python.exe"
& $py E:\Softwares\AI-search\mcp-probe\probe-stdio.py `
    E:\Softwares\AI-search\fallback-search\.venv\Scripts\fallback-search.exe `
    -- web_search '{"query":"test","max_results":3}'

# Codex 看到的 MCP 列表
codex mcp list
```

## Codex 配置

- 全局配置：`C:\Users\chenxin47\.codex\config.toml`（新增 6 个 `[mcp_servers.*]`）
- 全局指令：`C:\Users\chenxin47\.codex\AGENTS.md`（新增“Web 检索与抓取优先级”章节）
- 备份：`E:\Softwares\AI-search\_backup\20260920-203117\`（`codex-config.toml` / `codex-AGENTS.md`，含 SHA256 校验）
- 就地备份：`config.toml.before-ai-search`、`AGENTS.md.before-ai-search`
- 回滚：`& E:\Softwares\AI-search\scripts\rollback.ps1`

> 工具审批：本方案**未**修改 Codex 默认审批策略。交互会话中首次调用某个 MCP 工具时会弹审批；若希望自动放行，请自行在对应服务器下添加
> `default_tools_approval_mode = "approve"`（注意：这会永久关闭该类工具的审批门，请自行评估风险）。

## 已知限制

1. **Docker 引擎未就绪**：本机未启用 Windows「Virtual Machine Platform / WSL2」，Docker Desktop 4.91 的 Linux 引擎无法启动（日志：`checking preconditions: Virtual Machine Platform not enabled`）。因此 SearXNG 容器与 mcp-searxng 的实际检索暂不可用；启用并重启后执行 `scripts\start-all.ps1` 即可。
2. 公开 SearXNG 实例在本机网络下全部不可达（已实测），所以本地自建是唯一可行路径。
3. Firecrawl keyless 按 IP 限流、DuckDuckGo 偶发风控 —— 这正是需要多级降级的原因。
4. 全局 `config.toml` 中既有的 `node_repl` / `renderdoc` / `renderdoc-mcp` 在 Codex 启动时报错（路径失效/握手失败），属既有问题，本方案未改动。

## 更新（2026-09-20 晚）

- 容器运行时由 Docker Desktop 改为 **Podman**（Docker Desktop 商用需付费；Podman 为 Apache-2.0 无限制）。
- SearXNG 改为**单容器**（去掉 valkey）；启动方式：`scripts\ensure-services.ps1`（按需），停止：`scripts\idle-stop.ps1`。
- open-webSearch daemon 变为**可选**：网关自动在 daemon 与一次性 CLI 之间切换；Codex 中该 MCP 固定 `MODE = "stdio"`（不占 3000 端口）。
- 仍需一次性操作：启用 Windows「虚拟机平台」/ WSL 或 Hyper-V 并**重启**，否则 Podman machine 无法运行。

## 最终使用方式（2026-09-20 深夜更新）

```powershell
# 开始用检索前（幂等，约 10-40 秒）
& E:\Softwares\AI-search\scripts\ensure-services.ps1

# 查看状态
& E:\Softwares\AI-search\scripts\status.ps1

# 用完回收（停容器+隧道；加 -StopMachine 连虚拟机一起停，释放 ~2GB）
& E:\Softwares\AI-search\scripts\idle-stop.ps1 -IdleMinutes 30 [-StopMachine]
```

**架构**：Windows `127.0.0.1:47311` ←SSH 隧道← podman machine 内的容器 `10.88.x.x:8080`（SearXNG）。
> WSL 的容器端口发布在 Windows 侧不可达（已实测多种组合），因此用 SSH 隧道直连容器 IP；隧道由脚本按容器生命周期管理（11 MB 内存）。

**实测开销**：容器 122 MB / VM 常驻约 1.9 GB / 隧道 11 MB / 磁盘 3.25 GB(C:) + 479 MB(E:)。

## 自动化与迁移（2026-09-20 22:20 更新）

- **自动回收**：已注册计划任务 `AI-Search Idle Stop`，每 15 分钟运行一次 `scripts\idle-stop.ps1`（空闲 15 分钟则停隧道+容器）。查看：`Get-ScheduledTaskInfo -TaskName 'AI-Search Idle Stop'`。
- **搬迁**：整个目录移到任意位置后运行 `scripts\repair-after-move.ps1`（重装网关 + 重写 Codex 配置 + 重建任务），再重启 Codex。
- **配置集中**：端口/容器名/空闲阈值/相对路径都在 `config/settings.psd1`，脚本不再硬编码路径。
- **编码约定**：`.ps1` 用 UTF-8 **带 BOM**（兼容 PowerShell 5.1 计划任务）；`config.toml`/`AGENTS.md` 用 UTF-8 **不带 BOM**。

## 作为 Skill 使用（推荐）

本管线已封装为技能 `ai-search`，并链接到两个平台：

- Codex：`~/.codex/skills/ai-search`（junction）
- Claude Code：`~/.claude/skills/ai-search`（junction）
- 真身：`<data-root>\skills\ai-search\`（SKILL.md + scripts\ + config\）

**AI 的行为约定**（见 SKILL.md）：需要联网时**自动启动**搜索服务；**只在用户明确要求时**才停止服务，绝不自行静默关闭。

手工调用：

```powershell
# 启动（幂等）
& "<data-root>\skills\ai-search\scripts\ensure-services.ps1"
# 状态 / 自检
& "<data-root>\skills\ai-search\scripts\status.ps1"
& "<data-root>\skills\ai-search\scripts\verify.ps1"
# 停止（仅在明确需要时）
& "<data-root>\skills\ai-search\scripts\stop-all.ps1"
```

维护：`repair-after-move.ps1`（迁移后）、`sync-codex-config.ps1`（重写 config.toml/AGENTS.md/CLAUDE.md）、`register-idle-task.ps1 -Minutes 30`（改回收间隔）。

## Fresh machine setup (one command)

Prerequisites: **Podman Desktop** with a running podman machine, **Node.js + npm**, **Python 3.10+**, **Git** (Windows also needs the built-in OpenSSH client).

```bash
git clone https://github.com/OldNew777/ai-search.git
cd ai-search
python skills/ai-search/scripts/ai_search.py bootstrap
```

`bootstrap` performs, idempotently:

1. creates the Python venvs (Scrapling + the fallback gateway) and downloads the Chromium runtime;
2. clones and builds `open-websearch`, installs `mcp-searxng`;
3. generates `.env` with a random `SEARXNG_SECRET`;
4. links the skill into `~/.codex/skills/ai-search` and `~/.claude/skills/ai-search`;
5. writes the Codex MCP servers into `~/.codex/config.toml` and the guidance block into
   `~/.codex/AGENTS.md` + `~/.claude/CLAUDE.md`;
6. installs the idle scheduler entry (schtasks / cron / launchd);
7. starts the pipeline and runs a real query to verify it.

Then restart the agent so the new skill and MCP servers are loaded. Day-to-day you never need these
commands by hand - the `ai-search` skill tells the agent when to start the service, and immediate
shutdown happens only when you explicitly ask for it.

Moving the checkout later is safe: run `python skills/ai-search/scripts/ai_search.py repair`.

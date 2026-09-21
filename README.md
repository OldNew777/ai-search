# AI-Search Pipeline

自托管的**联网搜索 + 网页抓取**管线，为 **Codex** 与 **Claude Code** 提供实时检索能力。
全部生命周期由一个标准库 Python 文件管理：`skills/ai-search/scripts/ai_search.py`（跨平台，无第三方依赖）。

| 角色 | 组件 | 说明 |
|---|---|---|
| 搜索（主） | **SearXNG**（Podman 容器自建） | 隐私、免费、无需 API key；开启 JSON API 供 MCP 调用 |
| 抓取（主） | **Scrapling** MCP | 本机 Chromium；静态页 / JS 渲染 / 反爬站点 |
| 兜底（按序） | **fallback-search** MCP 网关 | open-webSearch → Firecrawl (keyless) → DuckDuckGo |
| Agent 集成 | skill `ai-search` | 同时链接到 `~/.codex/skills` 与 `~/.claude/skills` |

仓库：<https://github.com/OldNew777/ai-search>

---

## 架构

```
Codex / Claude Code
  │
  ├─ searxng MCP ─────────► http://127.0.0.1:47311 ──┐
  │                                                  │ SSH 隧道（Windows/macOS 需要）
  ├─ scrapling MCP ───────► 本机 Chromium           │
  │                                                  ▼
  └─ fallback_search MCP ─► podman machine (WSL/VM) ─► SearXNG 容器 10.88.x.x:8080
       open-webSearch → Firecrawl → DuckDuckGo
```

- SearXNG 容器运行在 **podman machine**（Windows 用 WSL2 后端）里；
- Windows 下容器端口发布**无法**直接映射到宿主 `127.0.0.1`，因此由 `ai_search.py` 建立 **SSH 隧道**（`127.0.0.1:47311 → 容器IP:8080`），容器 IP 每次动态解析；
- Linux 原生 Podman 无需隧道，脚本会自动直接使用发布端口。

---

## 快速开始（新机器）

前置条件：

- **Podman Desktop**（含一个已创建的 podman machine；Windows 需先启用 WSL2 或 Hyper-V）
- **Node.js + npm**、**Python 3.10+**、**Git**
- Windows 另需系统自带的 **OpenSSH 客户端**

```bash
git clone https://github.com/OldNew777/ai-search.git
cd ai-search
python skills/ai-search/scripts/ai_search.py bootstrap
```

`bootstrap` 幂等地完成：

1. 创建 Python 虚拟环境并安装 **Scrapling**（含 Chromium 运行时）；
2. 安装 **fallback-search** 网关（editable）；
3. 克隆并构建 **open-webSearch**、安装 **mcp-searxng**；
4. 生成 `.env`（随机 `SEARXNG_SECRET`）；
5. 把技能链接到 `~/.codex/skills/ai-search` 与 `~/.claude/skills/ai-search`；
6. 写入 Codex 的 MCP 配置（`~/.codex/config.toml`）与两个平台的引导块（`~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`）；
7. 安装空闲回收的计划任务（Windows `schtasks` / macOS `launchd` / Linux `cron`）；
8. 启动服务并执行一次真实检索自检。

完成后**重启 Agent**以加载新技能与 MCP。

---

## 日常使用

正常情况下**不需要手动操作**：Agent 在需要联网时会按技能说明自动 `start`；只有当你**明确要求**时才会 `stop`。

```bash
python skills/ai-search/scripts/ai_search.py start     # 启动（幂等，可自动调用）
python skills/ai-search/scripts/ai_search.py status    # 端口 / 健康 / 隧道 / 容器
python skills/ai-search/scripts/ai_search.py verify    # 启动 + 真实检索自检
python skills/ai-search/scripts/ai_search.py heal      # 启动 + 容器出网自检/自愈（引擎连不出去时重启 podman machine）
python skills/ai-search/scripts/ai_search.py stop      # 立即停止（仅限用户明确要求；加 --all 连 VM 一起停）
```

### 停止语义（重要）

| 动作 | 命令 | 触发条件 |
|---|---|---|
| **立即停止**（同步，不判断空闲） | `stop [--all]` | **仅当用户明文要求**（"关闭搜索服务"等）；模型不得自行调用 |
| **空闲回收**（先判断空闲时长） | `idle-check --minutes N` | 仅由 OS 计划任务调用，默认每 15 分钟一次；模型不得调用 |

---

## 目录结构

```
<data-root>/
├─ skills/ai-search/          # 技能本体（skill links 指向这里）
│  ├─ SKILL.md                #   Agent 使用说明（英文）
│  ├─ config/settings.json    #   端口 / 容器 / 空闲阈值 / 相对路径
│  └─ scripts/ai_search.py    #   唯一入口（标准库，跨平台）
├─ searxng/
│  ├─ docker-compose.yml      #   （可选）podman/docker compose 方式
│  └─ core-config/            #   settings.yml（开启 JSON API）、limiter.toml
├─ fallback-search/           # 自建降级网关（MCP, stdio）
├─ scrapling/.venv/           # Scrapling + Chromium（bootstrap 生成）
├─ open-websearch/  mcp-searxng/   # 第三方件（bootstrap 还原，不入库）
├─ mcp-probe/probe-stdio.py   # 通用 MCP stdio 探针
├─ logs/  _backup/  .env      # 运行日志 / 配置备份 / 密钥（均不入库）
├─ README.md
└─ Pipeline-design.md
```

---

## 配置

`skills/ai-search/config/settings.json`：

```json
{
  "ports":     { "searxng": 47311, "daemon": 47313, "container": 8080 },
  "container": { "name": "ai-search-searxng", "image": "docker.io/searxng/searxng:latest" },
  "idle":      { "minutes": 15 },
  "paths":     { "...": "全部为相对 data-root 的路径" }
}
```

脚本**不含任何硬编码绝对路径**：技能目录由脚本自身位置推导，数据根目录为其上两级；也可用环境变量 `AI_SEARCH_ROOT` 覆盖。

---

## 维护命令

```bash
python skills/ai-search/scripts/ai_search.py repair                 # 搬迁/重命名目录后：重装、重链、重同步
python skills/ai-search/scripts/ai_search.py sync-agent-config      # 重写 config.toml + AGENTS.md + CLAUDE.md
python skills/ai-search/scripts/ai_search.py link-skill             # 重建 Codex/Claude 技能链接
python skills/ai-search/scripts/ai_search.py install-idle-task --minutes 30
python skills/ai-search/scripts/ai_search.py uninstall-idle-task    # 完全手动停止（禁用空闲回收）
python skills/ai-search/scripts/ai_search.py rollback               # 还原最近一次备份的 Agent 配置
python skills/ai-search/scripts/ai_search.py heal                    # 容器出网自检 + 自愈（安全闸：机器上有其他容器在跑则只报告不重启）
```

---

## 故障排查

| 现象 | 处理 |
|---|---|
| `searxng` MCP 连接失败 | `start` 然后 `status`（隧道或容器未运行） |
| podman machine 未启动 | `start` 会自动拉起；冷启动约 20–40 秒 |
| 检索 0 结果（上游引擎全部超时） | 容器多半丢了出网能力（宿主休眠/恢复后 WSL usernet 掉线）：先跑 `verify`（会自愈并真实检索一次）；仍为 0 则看 `status` 的 `--- egress ---` 段，改用 `fallback_search` 并说明降级 |
| `start` 打印 `[DEGRADED]` | SearXNG 活着但引擎连不出去：直接走 `fallback_search`，不要再重试 SearXNG |
| 结果偏旧 | 检索时传 `time_range`（`day`/`week`/…）或换更精确的查询 |
| 迁移目录后失效 | `repair`，然后重启 Agent |
| 想看接口是否活着 | `python skills/ai-search/scripts/ai_search.py verify` |

---

## 已知限制

- 当前网络下 `duckduckgo`、`wikidata` 两个上游引擎不响应（其余引擎正常，不影响检索）。
- Podman machine 常驻约占 2 GB 内存；空闲回收任务（或 `stop --all`）可释放。
- Windows 必须依赖 SSH 隧道（WSL 端口发布限制），因此需要 OpenSSH 客户端。
- podman machine 的 user-mode networking（usernet）在宿主休眠/恢复后可能掉线，表现为容器无 DNS/无路由、所有引擎超时、检索 0 结果；`start`/`heal` 会自检并自愈（若该机器上还有其他容器在运行，则只报告不重启）。

## 第三方组件

| 组件 | 来源 | 许可 |
|---|---|---|
| SearXNG | `searxng/searxng`（容器镜像） | AGPL-3.0 |
| Scrapling | `D4Vinci/Scrapling` | BSD-3-Clause |
| open-webSearch | `Aas-ee/open-webSearch` | Apache-2.0 |
| mcp-searxng | `ihor-sokoliuk/mcp-searxng` | MIT |
| Firecrawl keyless | `mcp.firecrawl.dev`（远程服务） | 商业服务，免费限流 |

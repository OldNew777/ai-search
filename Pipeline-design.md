# AI-Search Pipeline — 设计文档

> 最后更新：2026-09-21
> 仓库：<https://github.com/OldNew777/ai-search>　分支：`master`
> 数据根目录：`<data-root>`（本机为 `E:\Softwares\AI-search`；脚本不含硬编码路径，可整体搬迁）

---

## 1. 目标与非目标

**目标**：为 Codex / Claude Code 提供**本机自建、免费、可降级**的联网检索与抓取能力。

- 搜索主链路：自建 SearXNG（容器）→ `mcp-searxng`
- 抓取主链路：`scrapling` MCP（本机 Chromium）
- 兜底链路：`fallback-search` 网关，严格按序 open-webSearch → Firecrawl(keyless) → DuckDuckGo
- Agent 侧零手工：作为技能 `ai-search` 被自动发现与调用

**非目标**：

- 不部署本地 LLM（SearXNG 是元搜索引擎，不含模型；LLM 层由 Agent 自身承担）
- 不引入 Perplexica/Morphic 之类"AI 搜索前端"
- 不把服务暴露到公网/局域网（默认只监听 `127.0.0.1`）

---

## 2. 架构与数据流

```
┌──────────────────────── Agent (Codex / Claude Code) ────────────────────────┐
│                                                                             │
│  searxng MCP (stdio)  ──►  http://127.0.0.1:47311  ──┐                       │
│                                                      │ SSH 隧道（Windows/macOS）│
│  scrapling MCP (stdio) ──►  本机 Chromium            │                       │
│                                                      ▼                       │
│  fallback_search MCP ──►  podman machine (WSL/VM)  ──►  SearXNG 容器         │
│     open-webSearch → Firecrawl → DuckDuckGo          (10.88.x.x:8080)       │
└─────────────────────────────────────────────────────────────────────────────┘
```

- 容器运行在 **podman machine** 内；宿主侧只通过 `127.0.0.1:47311` 访问；
- Windows 下 WSL 的容器端口发布**不可达**（见 §6），故由脚本建立 **SSH 隧道**并把目标指向**容器 IP**（每次启动动态解析）；
- Linux 原生 Podman 无需隧道，脚本自动回退到直接使用发布端口。

---

## 3. 组件清单

| 组件 | 形态 | 传输 | 来源 | 许可 | 端口 |
|---|---|---|---|---|---|
| SearXNG | Podman 容器 | HTTP(JSON) | `docker.io/searxng/searxng:latest` | AGPL-3.0 | 宿主 `127.0.0.1:47311` → 容器 8080 |
| mcp-searxng | MCP server | stdio | npm `mcp-searxng` | MIT | — |
| Scrapling | MCP server | stdio | PyPI `scrapling[ai]` | BSD-3-Clause | — |
| fallback-search | 自建 MCP 网关 | stdio | 本仓库 `fallback-search/` | 本项目 | — |
| open-webSearch | MCP / CLI / daemon | stdio + HTTP | `Aas-ee/open-webSearch` | Apache-2.0 | 可选 `127.0.0.1:47313` |
| Firecrawl keyless | 远程 MCP | Streamable HTTP | `https://mcp.firecrawl.dev/v2/mcp` | 商业（免费限流） | — |
| DuckDuckGo | 库 / MCP | — | `ddgs` / `duckduckgo-mcp-server` | MIT | — |
| 管理 CLI | 单文件 Python | — | 本仓库 `skills/ai-search/scripts/ai_search.py` | 本项目 | — |

---

## 4. 目录结构与仓库边界

```
<data-root>/
├─ skills/ai-search/          # 技能本体（入库）
│  ├─ SKILL.md                #   Agent 指南（英文）
│  ├─ config/settings.json    #   端口/容器/空闲阈值/相对路径（入库）
│  └─ scripts/ai_search.py    #   唯一入口，标准库跨平台（入库）
├─ searxng/docker-compose.yml + core-config/*   # （入库）
├─ fallback-search/{pyproject.toml,src/*}       # 自建网关（入库）
├─ mcp-probe/probe-stdio.py                     # 通用 MCP 探针（入库）
├─ README.md / Pipeline-design.md / .gitignore  # （入库）
├─ scrapling/.venv/  fallback-search/.venv/     # bootstrap 生成（不入库）
├─ open-websearch/  mcp-searxng/                # 上游代码（不入库，bootstrap 还原）
├─ logs/  _backup/  .env                        # 运行态（不入库）
```

**仓库不包含**：虚拟环境、`node_modules`、容器镜像、日志、备份、`.env` 密钥。
**不使用 submodule**：第三方代码由 `bootstrap` 按上游 URL 还原（仓库保持 ~100 KB，避免 submodule 操作成本）。

---

## 5. 端口分配

| 用途 | 端口 | 绑定 | 说明 |
|---|---|---|---|
| SearXNG（隧道入口） | 47311 | 127.0.0.1 | Windows 侧由 SSH 隧道提供 |
| open-webSearch daemon（可选） | 47313 | 127.0.0.1 | 不启动时网关走一次性 CLI |
| 容器内部 | 8080 | 容器内 | 发布为 `127.0.0.1:47311:8080` |

---

## 6. 网络：Windows/WSL 端口发布的坑与结论

实测矩阵（Podman 6.0.2 + WSL2 + user-mode networking）：

| 配置 | VM 内可达 | Windows `127.0.0.1` | Windows → VM IP |
|---|---|---|---|
| `-p 127.0.0.1:47311:8080` + UM 网络 | ✅ | ❌ | ❌ |
| `-p 47311:8080`（全网卡）+ UM 网络 | ❌ | ❌ | 偶发 |
| 关闭 user-mode networking | 不稳定 | ❌ | ❌ |
| WSL `networkingMode=mirrored` | — | ❌ | ⚠️ 会切断 podman machine 的 SSH 通道（已弃用） |
| **SSH 隧道 → 容器 IP:8080** | — | ✅ | — |

**结论**：Podman 的端口映射只在 VM 内部生效；由 `ai_search.py` 用 `ssh -L` 把宿主 `127.0.0.1:<port>` 转发到**容器 IP 的容器端口**，容器 IP 每次启动用 `podman inspect` 动态解析（实测会变：.3 → .4 → … → .8）。SSH 参数（端口/私钥/用户）从 `podman machine inspect` 读取，rootful 机器用 `root@`。

---

## 7. SearXNG 配置要点

- `core-config/settings.yml`：`search.formats: [html, json]`（mcp-searxng 必需）、`server.limiter: false`、`base_url` 指向 `http://127.0.0.1:47311/`；
- 密钥走环境变量 `SEARXNG_SECRET`（由 `bootstrap` 随机生成写入 `.env`，不入库）；
- 单容器部署（**已去掉 valkey/redis**：单机自用不需要缓存/限流服务）；
- 当前网络实测：`brave`、`google cse`、`wikipedia` 正常；`duckduckgo`、`wikidata` 无响应（不影响整体检索）。

---

## 8. 兜底链设计

`fallback-search`（自建 MCP 网关，stdio）暴露三个工具：

| 工具 | 内部顺序 |
|---|---|
| `web_search` | open-webSearch（daemon → 一次性 CLI）→ Firecrawl keyless → DuckDuckGo |
| `fetch_page` | open-webSearch `/fetch-web` → Firecrawl `firecrawl_scrape` |
| `provider_status` | 熔断状态与端点配置（不发网络请求） |

特性：每个 provider 独立超时（默认 25s）、连续失败达阈值后熔断 90s、返回体带 `provider` 与 `attempts`（可定位是哪一跳应答/失败）。搜索类工具按"一次主链路 + 一次兜底"使用，不做多 provider 扇出。

---

## 9. 技能与 Agent 集成

### 9.1 技能本体

`skills/ai-search/` 是**唯一真身**，通过链接同时挂到两个平台：

| 平台 | 链接 | 类型 |
|---|---|---|
| Codex | `~/.codex/skills/ai-search` | Windows junction（`mklink /J`，免管理员） |
| Claude Code | `~/.claude/skills/ai-search` | Windows junction / POSIX symlink |

`SKILL.md`（英文）定义：触发场景、主/兜底链路用法、启动命令、停止规则、排障表、维护命令。链接创建与刷新由 `ai_search.py link-skill` 负责。

### 9.2 全局指令块

`~/.codex/AGENTS.md` 与 `~/.claude/CLAUDE.md` 中由 `sync-agent-config` 维护同一段 `ai-search-pipeline` 块：

- **全英文、零绝对路径**，只指示"在你自己的 skills 目录里找名为 `ai-search` 的技能，细节见其 SKILL.md"；
- 三条硬规则：联网前自动 `start`；主链路优先、兜底链仅在失败时使用；**不得自行停止服务**。

### 9.3 MCP 注册

`~/.codex/config.toml` 由 `sync-agent-config` 重写 AI-search 块，注册 6 个 server：`searxng`、`scrapling`、`open_websearch`、`firecrawl`、`duckduckgo`、`fallback_search`。
Scrapling 用 `enabled_tools` 裁到 9 个常用工具以降上下文；`open_websearch` 固定 `MODE = "stdio"`（避免额外占用 3000 端口）。

---

## 10. 停止语义（重要）

| 动作 | 命令 | 触发条件 |
|---|---|---|
| **立即停止**（同步、不判断空闲） | `ai_search.py stop [--all]` | **仅当用户明文要求**（"关闭搜索服务"/"stop it now"）；Agent **不得**自行调用 |
| **空闲回收** | `ai_search.py idle-check --minutes N` | 仅由 OS 计划任务调用（默认 15 分钟一次）；Agent 不得调用或绕过 |

设计理由：检索是交互式工作流，随时可能继续追问；服务被静默关闭会让下一次检索无故变慢。因此把"立即关闭"作为**用户专有动作**，把"空闲回收"交给机器策略。

---

## 11. 跨平台实现要点

单文件 `ai_search.py`，**仅标准库**（Python 3.9+）：

| 事项 | Windows | macOS | Linux |
|---|---|---|---|
| 容器端口可达性 | SSH 隧道（必需） | SSH 隧道 | 原生发布端口（隧道失败自动回退） |
| known_hosts | `NUL` | `/dev/null` | `/dev/null` |
| 后台进程 | `DETACHED_PROCESS + CREATE_NO_WINDOW` | `start_new_session` | `start_new_session` |
| 定时任务 | `schtasks`（用 `pythonw.exe`，无黑框） | `launchd`（LaunchAgents plist） | `crontab` |
| 技能链接 | junction | symlink | symlink |

命令面：`start / stop / idle-check / status / verify / heal / install-idle-task / uninstall-idle-task / sync-agent-config / link-skill / bootstrap / repair / rollback`。

### 11.1 容器出网自检与自愈（2026-09-21 修复）

**现象**：`start` 打印 `[OK] SearXNG is ready`，但每次检索都是 0 条结果，`unresponsive_engines` 里所有上游引擎均为“超时”
（2026-09-21「查询今日杭州天气」会话即此症状，Agent 只能降级到 `fallback_search` 才拿到数据）。

**根因**：`searxng_ready()` 只验证“宿主隧道 → 容器 HTTP 端口”是否应答，无法反映**容器自身的出网能力**。
当时 podman machine（WSL 后端）的 user-mode networking 掉线：容器 `/etc/resolv.conf` 指向失效的 gvproxy 网关 `192.168.127.1`，
机器路由表缺少 `default`（`podman-usermode` 接口不在），容器内连 IP 都 `Network is unreachable`——SearXNG 活着，检索能力为零。

**修复**（`skills/ai-search/scripts/ai_search.py`）：

1. `container_egress()`：`podman exec` 进容器执行标准库探针（解析 + 443 TCP 连接 `www.bing.com`/`www.baidu.com`），给出容器视角的 OK/FAIL/UNKNOWN；
2. `check_egress()`：`start` 在服务就绪后默认执行探针，失败打印 `[WARN]`→`[DEGRADED]` 并明确指向 `fallback_search`，不再“假装就绪”；
3. `heal_network()`：探针失败时自动 `machine stop` → `machine start` 并重建容器 + SSH 隧道；**安全闸**：该机器上若还有其他 running 容器则只报告、不重启；
4. `verify` 遇到 0 结果改为 `[FAIL]` + 返回码 1；`status` 新增 `--- egress ---` 段；新增 `heal` 命令；`start` 支持 `--quick`（跳过探针）与 `--no-heal`（只报告不重启 VM）。

**实测**（2026-09-21）：坏状态探针 FAIL(`no-dns`) → 自愈重启机器（约 35 s）→ 探针 OK → `verify` 返回 30 条结果。

---

## 12. 运维命令手册

```bash
python skills/ai-search/scripts/ai_search.py start            # 启动（幂等，Agent 可自动调用）
python skills/ai-search/scripts/ai_search.py status           # 端口/健康/隧道/容器
python skills/ai-search/scripts/ai_search.py verify           # 启动 + 真实检索
python skills/ai-search/scripts/ai_search.py heal             # 启动 + 容器出网自检/自愈（必要时重启 podman machine）
python skills/ai-search/scripts/ai_search.py stop [--all]     # 立即停止（仅用户明确要求）
python skills/ai-search/scripts/ai_search.py idle-check --minutes 15
python skills/ai-search/scripts/ai_search.py install-idle-task --minutes 15
python skills/ai-search/scripts/ai_search.py uninstall-idle-task
python skills/ai-search/scripts/ai_search.py sync-agent-config
python skills/ai-search/scripts/ai_search.py link-skill
python skills/ai-search/scripts/ai_search.py bootstrap        # 新机器一键安装
python skills/ai-search/scripts/ai_search.py repair           # 搬迁后修复
python skills/ai-search/scripts/ai_search.py rollback         # 还原最近备份的 Agent 配置
```

---

## 13. 当前验证状态（2026-09-21）

| 验证项 | 结果 |
|---|---|
| 技能发现 | Codex `YES`；Claude Code `YES` |
| 通过两个链接调用脚本 | ✅ 均正确解析到 `<data-root>` |
| `verify`（真实检索） | ✅ SearXNG 返回 38–46 条结果 |
| 容器出网自检/自愈（2026-09-21 补测） | ✅ 坏状态探针 FAIL(`no-dns`) → 自愈重启机器（约 35 s）→ 探针 OK → `verify` 30 条结果 |
| 生命周期 | ✅ `stop` → 端口 0 监听 → `start` → 容器 IP 变化（.7→.8）后隧道重建成功 |
| 计划任务 | ✅ `LastTaskResult = 0`，动作为 `pythonw.exe ... ai_search.py idle-check` |
| 新机器克隆 | ✅ 克隆体积 ~96 KB，脚本在新位置正确定位 data-root |
| 密钥/大文件 | ✅ 仓库内无 `.env`、venv、镜像、备份 |

---

## 14. 决策与演进（精简）

| 时间 | 决策 / 收获 |
|---|---|
| 初期 | 评估 crawl4ai 系 MCP：最大的 `coleam00/mcp-crawl4ai-rag`(2261★) 已停更 14 个月且强依赖 Supabase+OpenAI；改用 **Scrapling**（82.5k★ 引擎自带 MCP） |
| 搜索选型 | `mcp-searxng`（1249★，官方提供 Codex 配方、OpenSSF 徽章） |
| 容器运行时 | Docker Desktop 商用需付费 → 改 **Podman**（Apache-2.0，无商用限制）；WSL2/VirtualMachinePlatform 仍是前置条件 |
| 端口 | 统一 473xx 段并全部绑定 `127.0.0.1`，避开常用端口 |
| 常驻开销 | 去掉 valkey（容器 2→1）；open-webSearch daemon 变为**可选**（网关自动在 daemon 与一次性 CLI 间切换） |
| 端口转发 | WSL 端口发布在 Windows 侧不可达 → **SSH 隧道直连容器 IP**（详见 §6） |
| 脚本形态 | 先 PowerShell → 因跨平台与可维护性**全部重写为 Python 单文件**（仅标准库） |
| 编码教训 | PowerShell 5.1 需 UTF-8 **带 BOM**；`config.toml`/`AGENTS.md` 必须**无 BOM**（TOML 规范）——同一约定保留在 Python 版：`.ps1` 已删除，写入 TOML/Markdown 一律无 BOM |
| 发布 | 代码托管 GitHub；venv/镜像/备份/密钥全部 gitignore，第三方件由 `bootstrap` 还原 |
| 2026-09-21 | 修复“服务已就绪但检索 0 结果”：`start` 默认探测**容器内**出网（DNS + 443 TCP），失败即自愈（重启 podman machine + 重建隧道），`verify`/`status` 如实报告；只检查 HTTP 的 `searxng_ready()` 是本次误判根因 |

---

## 15. 已知限制与后续可选项

**限制**

- 当前网络下 `duckduckgo`、`wikidata` 上游引擎无响应（其余引擎正常）；
- Windows 必须依赖 SSH 隧道（WSL 限制），因此需要 OpenSSH 客户端；
- podman machine 常驻约占 2 GB 内存；
- usernet（user-mode networking）在宿主休眠/恢复后可能掉线：容器无 DNS/无路由 → 所有引擎超时、检索 0 结果。已由 `start`/`heal` 自检自愈覆盖（机器上有其他容器时只报告不重启）。

**可选项**

1. 在 `settings.yml` 中禁用不响应的引擎，减少每次检索等待（约省 2–4 秒）；
2. 调整空闲回收阈值为 30 分钟，或对高内存场景使用 `stop --all` 释放 VM；
3. 为 SearXNG 增加 HTTP 代理以启用 Google/DDG 等引擎；
4. 把 `mcp-probe/probe-stdio.py` 扩展为回归测试脚本，纳入 CI。

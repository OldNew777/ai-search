# AI Search Pipeline — 设计文档

> 生成时间：2026-09-20
> 存储根目录：`E:\Softwares\AI-search`
> 目标宿主：Codex (ChatGPT desktop app, codex-cli 0.155.0-alpha)

---

## 1. 目标

为 Codex 提供一套**本机自建、免费、可离线降级**的联网检索与抓取能力：

- **搜索主链路**：自建 SearXNG（Docker）→ mcp-searxng（MCP，stdio）
- **抓取主链路**：Scrapling MCP（本机 Chromium，stdio，13 个工具）
- **兜底链路（严格按序）**：open-webSearch → Firecrawl(keyless) → DuckDuckGo
- 全部服务只绑定 `127.0.0.1`，不暴露到局域网；不占用常用端口（不使用 8080/3000/8000/5000/5432 等）。

### 非目标

- 不部署任何本地 LLM；SearXNG 不需要模型，Codex 自身即 LLM 层。
- 不引入 SearXNG 之外的“AI 搜索前端”（Perplexica/Morphic 等）。
- 不把服务暴露到公网或局域网。

---

## 2. 架构

```
                    ┌─────────────────────────── Codex ───────────────────────────┐
                    │                                                             │
   主检索            │  mcp-searxng ──stdio──► SearXNG(Docker, 127.0.0.1:47311)     │
                    │                                     └─ 上游引擎聚合结果      │
                    │                                                             │
   主抓取            │  Scrapling MCP ──stdio──► 本机 Chromium (fetch/stealthy)     │
                    │                                                             │
   兜底（A+B）       │  fallback-search (自建 MCP 网关) ──严格按序──┐                │
                    │        1) open-webSearch (本地 daemon :47313) │                │
                    │        2) Firecrawl keyless (mcp.firecrawl.dev)                │
                    │        3) DuckDuckGo (本机 npx / ddgs)         │                │
                    └─────────────────────────────────────────────────────────────┘
```

调度策略写入全局 `~/.codex/AGENTS.md`（方案 A 的声明式部分）：
主链路优先 → 报错/超时/空结果/429 时调用 `fallback-search`（方案 B 的确定性部分）。

---

## 3. 组件清单

| 组件 | 形态 | 传输 | 来源 | 许可 | 端口 |
|---|---|---|---|---|---|
| SearXNG | Docker 容器 | HTTP(JSON) | 官方镜像 `searxng/searxng` | AGPL-3.0 | `127.0.0.1:47311` → 容器 8080 |
| mcp-searxng | MCP server | stdio（首选）/ HTTP 备选 | `npx -y mcp-searxng` | MIT | 无 / 47312 |
| Scrapling MCP | MCP server | stdio（首选）/ HTTP 备选 | `pip install "scrapling[ai]"` | BSD-3-Clause | 无 / 47314 |
| open-webSearch | MCP server / daemon | stdio + HTTP daemon | 本项目内 clone | Apache-2.0 | 47313 |
| Firecrawl keyless | 远程 MCP | Streamable HTTP | `https://mcp.firecrawl.dev/v2/mcp` | 商业服务（免费限流） | 无 |
| DuckDuckGo MCP | MCP server | stdio | `npx -y duckduckgo-mcp-server` | MIT | 无 |
| fallback-search | 自建 MCP 网关 | stdio | 本目录 `fallback-search/` | 本项目 | 无 / 47315 |

---

## 4. 目录结构

```
E:\Softwares\AI-search\
├─ Pipeline-design.md          # 本文档
├─ README.md                   # 使用/启停/排障说明（交付时生成）
├─ .env                        # 端口与 token（不入库）
├─ _backup\<时间戳>\            # Codex 全局配置备份 + restore.ps1
├─ searxng\                    # docker-compose.yml / settings.yml / data / cache
├─ scrapling\.venv\            # 独立虚拟环境（避免污染全局 Python 3.14）
├─ open-websearch\             # 上游仓库 clone（保留 upstream remote）
├─ fallback-search\            # 自建网关 MCP（Python + 官方 MCP SDK）
├─ mcp-probe\                  # MCP 探针（list tools / 真实调用）
├─ scripts\                    # start-all / stop-all / status / verify / rollback
└─ logs\                       # 各阶段验证日志
```

---

## 5. 端口分配

统一使用 473xx 段（不与常用端口冲突），实施前用 `Get-NetTCPConnection -State Listen` 校验，占用则顺延：

| 服务 | 端口 | 绑定 |
|---|---|---|
| SearXNG | 47311 | 127.0.0.1 |
| mcp-searxng HTTP（备选） | 47312 | 127.0.0.1 |
| open-webSearch daemon | 47313 | 127.0.0.1 |
| Scrapling MCP HTTP（备选） | 47314 | 127.0.0.1 |
| fallback-search HTTP（备选） | 47315 | 127.0.0.1 |

---

## 6. SearXNG 配置要点

- `settings.yml`：
  - `server.secret_key`：随机生成 32 字节；
  - `search.formats: [html, json]`（mcp-searxng 需要 JSON）；
  - `server.limiter`：默认关闭（服务只监听 127.0.0.1；若需开启则另行配置 botdetection 白名单）；
  - 引擎集：优先启用实测可达的引擎（国内网络下 Google/DDG 可能超时，逐引擎验证后再定稿）。
- 容器：`restart: unless-stopped`，卷映射到 `E:\Softwares\AI-search\searxng\{data,cache}`。
- **不需要任何 LLM / API token**：SearXNG 是元搜索引擎，聚合上游结果，本身不含模型。

---

## 7. 兜底链设计（A + B）

### A. 声明式（`~/.codex/AGENTS.md`）

追加“Web 检索与抓取优先级”章节，规定：

1. 需要搜索：优先 `mcp-searxng`；
2. 需要抓正文：优先 Scrapling 的 `fetch` / `stealthy_fetch`；
3. 当主链路返回**错误 / 超时 / 空结果 / 429** 时，改调 `fallback-search`；
4. `fallback-search` 内部自行按序降级，模型无需关心顺序。

### B. 确定性网关（`fallback-search/`）

- 暴露 `web_search(query, max_results)` 与 `fetch_page(url)`；
- 内部顺序：open-webSearch（本地 daemon，失败则 CLI） → Firecrawl keyless REST → DuckDuckGo；
- 每个 provider 独立超时（默认 15s）、单次重试、连续失败熔断（进程内计数）；
- 返回结构包含 `provider` 字段，明确本次由谁应答，便于日志与排障；
- 密钥/开关通过环境变量注入，不写入代码。

---

## 8. Codex 全局配置改动（先备份）

| 文件 | 改动 |
|---|---|
| `~/.codex/config.toml` | 新增 `[mcp_servers.searxng]`、`[mcp_servers.scrapling]`、`[mcp_servers.open_websearch]`、`[mcp_servers.firecrawl]`、`[mcp_servers.duckduckgo]`、`[mcp_servers.fallback_search]`；Scrapling 用 `enabled_tools` 裁剪工具集；按需设置 `startup_timeout_sec` / `tool_timeout_sec` |
| `~/.codex/AGENTS.md` | **追加**（不覆盖）第 7 节 A 的优先级策略 |

- 备份：改动前复制到 `_backup\<时间戳>\`，记录 SHA256，并生成 `scripts\rollback.ps1`。
- 备份文件：`codex-config.toml`、`codex-AGENTS.md`（本次基线：20260920-203117）。

---

## 9. 执行阶段与验收标准

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 环境侦察（Docker/端口/工具链） | 基线报告，无阻塞 |
| P1 | 目录结构 + 配置备份 | 备份哈希一致 |
| P2 | SearXNG 容器 | `curl "http://127.0.0.1:47311/search?q=test&format=json"` 返回真实结果，≥2 个引擎可用 |
| P3 | mcp-searxng | 探针调用 `searxng_web_search` 成功 |
| P4 | Scrapling MCP | 静态页 + JS 渲染页抓取成功（stealth 过 CF 视网络而定） |
| P5 | 兜底三件套 | open-webSearch / Firecrawl keyless / DuckDuckGo 各自可用 |
| P6 | fallback 网关 | 故障注入：停 SearXNG → 降级 open-webSearch → 停 daemon → 降级 Firecrawl → 模拟 429 → 降级 DuckDuckGo，全程有日志 |
| P7 | Codex 端到端 | `codex mcp list` 全部在线；`codex exec` 三条用例通过 |
| P8 | 交付 | README + 启停/健康检查/回滚脚本 + 报告 |

---

## 10. 风险与已知限制

1. **Docker 后端**：本机原本未安装 WSL；Docker Desktop 4.91 首次启动会建立自己的运行时。若必须启用 WSL2/Hyper-V，需要管理员权限 + 重启（属于需要用户决策的动作）。
2. **搜索引擎可达性**：国内网络下部分上游引擎（Google、DDG、Brave）可能超时，需按实测结果裁剪引擎集。
3. **限流**：Firecrawl keyless 按 IP 限流；DuckDuckGo 可能触发风控 —— 这正是需要多级降级的原因。
4. **上下文成本**：Scrapling 暴露 13 个工具，需用 `enabled_tools` 裁剪。
5. **Cookie/登录态**：本方案不接管任何账号 Cookie（不使用 crawl4ai-skill 那类登录态爬取）。
6. **合规**：仅抓取公开页面，遵守目标站点 robots 与频率限制。

---

## 11. 决策记录

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | 放弃 `potterdigital/crawl4ai-mcp`（2★） | 过于小众；改用 82.5k★ 的 Scrapling 自带 MCP |
| D2 | 搜索用 mcp-searxng（1249★，官方 Codex 配方） | MIT、活跃、有 Codex 专用配置文档 |
| D3 | 兜底用 A+B | 声明式给模型策略，网关给确定性保证 |
| D4 | 端口 473xx | 避开常用端口，冲突时顺延 |
| D5 | 全部绑定 127.0.0.1 | 不需要对外暴露，降低攻击面 |
| D6 | 不部署本地 LLM | SearXNG 不含模型；LLM 层由 Codex 承担 |

---

## 12. 执行记录（2026-09-20）

### 12.1 时间线与结果

| 步骤 | 结果 |
|---|---|
| 环境侦察 | E: 剩余 2.2TB；47300-47500 端口段空闲；Python 3.14 / Node 24 / uv / git 就绪 |
| Docker 检查 | Docker Desktop 4.91.0 于 20:28 安装成功（per-user：`%LOCALAPPDATA%\Programs\DockerDesktop`），CLI 29.8.0 / compose v5.5.1 可用；**Linux 引擎启动失败**：`Virtual Machine Platform not enabled`、`hasNoVirtualization: true` |
| 目录与备份 | 建立目录树；备份 `config.toml`(SHA256 F288CF42…) 与 `AGENTS.md`(7403A7B1…) 到 `_backup\20260920-203117`，哈希一致 |
| SearXNG 交付物 | 已写好 `searxng\docker-compose.yml`（127.0.0.1:47311→8080、非 root、cap_drop ALL、健康检查、valkey 缓存）与 `core-config\settings.yml`（`formats: [html, json]`、limiter 关闭、随机 secret） |
| Scrapling | venv(Python 3.12.13) + scrapling 0.4.15 + `scrapling install`（Chromium 1243）；**实测**：13 个工具、静态页 fetch 成功、JS 渲染页（quotes.toscrape.com/js）返回 1768 字符正文 |
| mcp-searxng | 本地安装 v2.3.0，Codex 以 `node ...\dist\cli.js` 调用；**MCP 层已验证**（4 个工具：searxng_web_search / searxng_search_suggestions / searxng_instance_info / web_url_read），实际检索待 SearXNG 容器起来 |
| open-webSearch | clone v2.1.11 → `npm install` + `npm run build`；CLI 与 daemon 实测（Bing 引擎返回真实结果）；daemon 固定 127.0.0.1:47313 |
| Firecrawl keyless | 实测握手成功（server firecrawl-fastmcp 3.24.1，协议 2025-11-25），3 个工具：`firecrawl_scrape` / `firecrawl_search` / `firecrawl_parse` |
| fallback 网关 | 自建 `fallback-search`（Python 3.12 + mcp 2.2.0，304 行）：3 个工具（web_search / fetch_page / provider_status），含独立超时、失败熔断、`provider`+`attempts` 可观测性 |
| 三级降级实测 | ① daemon 在线 → `provider=open-websearch`(344ms)；② daemon 指死端口 → `provider=firecrawl`；③ daemon+firecrawl 均不可用 → `provider=duckduckgo`，三次 attempts 全部记录 ✅ |
| Codex 注册 | `config.toml` 新增 6 个 `[mcp_servers.*]`；`AGENTS.md` 追加优先级策略；`codex mcp list` 全部可见；`codex exec` 日志确认 `mcp: fallback_search/provider_status started` |

### 12.2 决策与偏差

| 编号 | 决策/偏差 | 原因 |
|---|---|---|
| D7 | 去掉 `type = "stdio"` 字段 | Codex 报“忽略无法识别的配置项”，命令存在即隐含 stdio |
| D8 | **不**设置 `default_tools_approval_mode = "approve"` | 安全审查判定其“永久关闭工具审批门、影响所有未来会话”，予以否决；保持默认审批，用户可按需自行开启 |
| D9 | DDG 兜底采用双实现 | 网关内直连 `ddgs` 库（低开销）＋ 另注册 `duckduckgo` MCP（独立可用） |
| D10 | SearXNG 采用官方推荐 compose（core + valkey），非废弃的 searxng-docker | 官方文档已标记旧仓库 superseded |
| D11 | 端口 47311/47313 实际空闲未占用，按原计划使用 | 侦察确认 |
| D12 | 未使用公开 SearXNG 实例做过渡 | 实测 6 个公开实例在本机网络下均不可达 |

### 12.3 待用户决策

**启用 Windows 虚拟化（开启 Docker Linux 引擎）**：需管理员权限执行一次并**重启**：

```powershell
# 管理员 PowerShell
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
# 或更简单：wsl.exe --install
# 之后重启系统
```

重启后执行 `E:\Softwares\AI-search\scripts\start-all.ps1` 即可拉起 SearXNG；随后验证：

```powershell
curl.exe "http://127.0.0.1:47311/search?q=test&format=json"
```

若 BIOS 未开启 VT-x/AMD-V，则还需在 BIOS 中启用 CPU 虚拟化。

---

## 13. 修订（2026-09-20 晚）：Docker → Podman，并优化常驻开销

### 13.1 为什么换 Podman

- Docker Desktop 对**公司商用**需付费订阅（小企业豁免条件：<250 员工且 <1000 万美元年营收），企业电脑存在版权风险；
- Podman / Podman Desktop 为 Apache-2.0，**无商用限制**；
- ⚠️ 但 Windows 上 Podman 仍需 Linux 虚拟机（WSL2 或 Hyper-V），**虚拟化功能（VirtualMachinePlatform / Microsoft-Hyper-V-All）依然必须先启用 + 重启**，这一条不因换 Podman 而消失。

### 13.2 消除常驻开销

| 项 | 调整 |
|---|---|
| open-webSearch daemon | **不再必需**：网关 `web_search` 先试 daemon（快路径），不可用时自动退回「一次性 CLI 调用」（`node build/index.js search … --json`，用完即退）。已实测两种模式均可用 |
| valkey/redis 容器 | **已移除**：单机自用不需要缓存/限流服务，容器数从 2 → 1 |
| open-webSearch MCP | Codex 中固定 `MODE = "stdio"`，不再额外监听 3000 端口 |
| 启动策略 | 改为**按需启动**：默认不自启，`scripts\ensure-services.ps1` 幂等拉起（并写入活动标记） |
| 停止策略 | `scripts\idle-stop.ps1 -IdleMinutes 60`：超过阈值无检索活动则停容器；加 `-StopMachine` 还可停整个 Podman 虚拟机（下次冷启动约 20–40 秒） |

### 13.3 脚本清单（全部为 Podman/Docker 自适应）

| 脚本 | 用途 |
|---|---|
| `ensure-services.ps1` | 按需启动：podman machine → `podman compose up -d` → 等待 SearXNG 健康；`-WithDaemon` 才启动 open-webSearch daemon |
| `idle-stop.ps1` | 空闲自动停止（阈值可调，可选停 machine） |
| `start-all.ps1` | 等价于 `ensure-services.ps1` |
| `stop-all.ps1` | 停 daemon + `compose down` |
| `status.ps1` | 端口/健康/容器状态一览（自动识别 podman 或 docker） |
| `rollback.ps1` | 还原 Codex 全局配置 |

---

## 14. 最终可用架构（2026-09-20 深夜，已端到端验证）

### 14.1 结论：WSL 的容器端口发布在 Windows 侧不可达，改用 SSH 隧道直连容器 IP

实测矩阵（Podman 6.0.2 + WSL2 + user-mode networking）：

| 配置 | VM 内可达 | Windows 127.0.0.1 可达 | Windows → VM IP 可达 |
|---|---|---|---|
| `-p 127.0.0.1:47311:8080` + UM 网络 | ✅ (200) | ❌ | ❌ |
| `-p 47311:8080`（全网卡）+ UM 网络 | ❌ | ❌ | ⚠️ 曾成功一次，之后复现失败 |
| 关闭 user-mode networking + 任一种发布 | ❌/不稳定 | ❌ | ❌ |
| WSL `networkingMode=mirrored` | — | ❌ | **导致 podman machine SSH 通道断开，已回退** |
| **SSH 隧道 → 容器 IP:8080** | — | ✅ **200** | — |

最终方案（已固化进脚本）：

```
Codex (Windows)
  └─ mcp-searxng (node, stdio)  →  http://127.0.0.1:47311
                                     └─ ssh.exe 隧道 (PID 记录在 logs\ssh-tunnel.pid)
                                          └─ podman machine (WSL) 内网 10.88.x.x:8080
                                               └─ SearXNG 容器
```

要点：
- 隧道目标用**容器 IP**（`podman inspect --format '{{.NetworkSettings.IPAddress}}'`），每次启动动态解析（容器重建会换 IP，实测 .3 → .4 → .5）；
- SSH 参数从 `podman machine inspect` 取（`SSHConfig.Port` / `IdentityPath`），用户固定 `root`（rootful machine）；
- 隧道随容器生命周期：`ensure-services.ps1` 建立、`idle-stop.ps1` 回收。

### 14.2 脚本（最终版）

| 脚本 | 行为 |
|---|---|
| `ensure-services.ps1` | 幂等：podman machine → 容器（不存在则 `podman run`，存在则 `start`）→ 等 running+IP → 建 SSH 隧道 → 等健康；`-WithDaemon` 额外起 open-webSearch daemon |
| `idle-stop.ps1` | `-IdleMinutes N`（默认 60，基于 `logs\last-search.marker`）→ 停隧道 + 停容器；`-StopMachine` 连 VM 一起停；`-Force` 忽略空闲判定 |
| `status.ps1` | 端口/健康/隧道 PID/容器状态一览 |
| `start-all.ps1` / `stop-all.ps1` | 等价于 ensure / idle -Force |
| `rollback.ps1` | 还原 Codex 全局配置 |

### 14.3 实测性能数据

| 项 | 数值 |
|---|---|
| SearXNG 容器 | 内存 **121.6 MB**，CPU 6.3%（检索时采样） |
| podman machine（VM 内） | 已用 1.1 GB / 可用 30 GB（动态） |
| Windows 侧 `vmmemWSL` | **1928 MB** 工作集（VM 常驻内存） |
| `gvproxy` | 32 MB |
| SSH 隧道 `ssh.exe` | **11 MB** |
| 磁盘 | C: `~\.local\share\containers` **3.25 GB**；E:\Softwares\AI-search **479 MB** |

省内存三档（按需选择）：
1. **最快**：保持现状（VM 常驻 ~2 GB，容器 120 MB，热启动 ~0 秒）；
2. **推荐**：`idle-stop.ps1 -IdleMinutes 30`（停容器，保留 VM；下次 `ensure-services` 约 10–15 秒）；
3. **最省**：`idle-stop.ps1 -IdleMinutes 30 -StopMachine`（连 VM 一起停，释放 ~2 GB；下次冷启动约 20–40 秒）。

### 14.4 端到端验证记录

- `codex exec` + `searxng` MCP：日志 `mcp: searxng/searxng_web_search started → completed`，返回 3 条真实结果 ✅
- SearXNG 直连：`curl "http://127.0.0.1:47311/search?q=podman&format=json"` → 37–45 条结果 ✅
- 启停循环：`status(OK) → idle-stop -Force(DOWN) → ensure-services(OK)` ✅
- 引擎健康：brave / google cse / wikipedia 正常；duckduckgo、wikidata 在本机网络下不响应（可在 settings.yml 中禁用，或保留不影响）

---

## 15. 路径无关重构 + 自动回收任务（2026-09-20 22:20）

### 15.1 去硬编码（整目录可迁移）

新增：
- `config/settings.psd1`：集中管理端口（SearXNG 47311 / daemon 47313 / 容器 8080）、容器名与镜像、空闲阈值（**15 分钟**）、全部相对路径。
- `scripts/_common.ps1`：所有脚本 dot-source 它；`$Root = Split-Path -Parent $PSScriptRoot` 推导根目录，`Import-PowerShellDataFile` 读配置。
- `scripts/sync-codex-config.ps1`：按当前根目录**重写** `~/.codex/config.toml` 的 AI-search 块 + 同步 `AGENTS.md` 里的根路径（自动备份到 `_backup/sync-<时间戳>/`）。
- `scripts/repair-after-move.ps1`：**搬迁后一键修复** = 重装 editable 网关 + 同步 Codex 配置 + 重建计划任务。
- 网关 `fallback_search/server.py`：`OPEN_WEBSEARCH_DIR` 改为 `Path(__file__).resolve().parents[3] / "open-websearch"` 推导（可用环境变量覆盖）。

验证：`scripts\*.ps1` 与 `config\settings.psd1` 中 **0 处** `E:\Softwares` 硬编码；功能回归通过。

**搬迁流程**：移动整个 `AI-search` 目录 → `scripts\repair-after-move.ps1` → 重启 Codex。

### 15.2 自动回收计划任务

- 任务名 `AI-Search Idle Stop`，每 **15 分钟**运行 `scripts\idle-stop.ps1`（阈值取 `config/settings.psd1` 的 `Idle.Minutes = 15`）。
- 触发器：`Once + RepetitionInterval 15min`；设置：`StartWhenAvailable`、`MultipleInstances IgnoreNew`、`ExecutionTimeLimit 10min`。
- 运行用户 `chenxin47`（Interactive / Limited），实测 `LastTaskResult = 0` ✅。
- 注册脚本：`scripts\register-idle-task.ps1`（可改 `-Minutes` / `-TaskName`）。

### 15.3 踩坑记录：PowerShell 编码（重要）

- **症状**：手动跑脚本正常，计划任务返回 `2147942401`（0x80070001），事件日志显示操作失败。
- **根因**：脚本为 UTF-8 **无 BOM**，而计划任务调用的是 **Windows PowerShell 5.1**（`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`），5.1 默认按 ANSI/GBK 解析 `.ps1`，中文注释与提示字符串被解析成乱码 → 语法错误。
- **修复**：`scripts\*.ps1` 与 `config\settings.psd1` 全部重存为 **UTF-8 with BOM**（5.1 与 7.x 都能正确解析）。
- **反向要求**：`config.toml` / `AGENTS.md` **必须无 BOM**（TOML 规范不认 BOM），因此 `sync-codex-config.ps1` 对这两个文件改用 `UTF8Encoding($false)` 写入。
- 经验：**含有非 ASCII 的 .ps1 一律带 BOM；给 Codex 的 TOML/Markdown 一律不带 BOM**。

### 15.4 其他

- Podman 目录已在用户 PATH 中（无需追加），脚本仍保留绝对路径回退以兼容未刷新环境。
- `idle-stop.ps1` 默认阈值改为读配置（15 分钟），仍支持 `-IdleMinutes` 覆盖、`-Force` 立即回收、`-StopMachine` 连 VM 一起停。

---

## 16. Skill 化与双平台接入（2026-09-20 22:30）

### 16.1 结构

```
<data-root>\
├─ skills\ai-search\           ← 技能本体（唯一真身）
│  ├─ SKILL.md                 ← 英文；含"何时用/如何启动/绝不自行停止/故障排查"
│  ├─ config\settings.psd1     ← 端口、容器名、空闲阈值、相对路径
│  └─ scripts\*.ps1            ← 全部脚本（原 <root>\scripts 迁入）
│     ├─ ensure-services.ps1   ← 自动启动（幂等）
│     ├─ status.ps1 / verify.ps1
│     ├─ idle-stop.ps1 / stop-all.ps1     ← 仅在用户明确要求时使用
│     ├─ register-idle-task.ps1
│     ├─ sync-codex-config.ps1 ← 重写 config.toml + AGENTS.md + CLAUDE.md
│     └─ repair-after-move.ps1 ← 迁移后一键修复
└─ searxng\ open-websearch\ fallback-search\ mcp-searxng\ scrapling\ ...  ← 数据/服务目录（不变）
```

链接（junction，无需管理员，双平台共用同一份真身）：

| 平台 | 链接路径 | 目标 |
|---|---|---|
| Codex | `%USERPROFILE%\.codex\skills\ai-search` | `<data-root>\skills\ai-search` |
| Claude Code | `%USERPROFILE%\.claude\skills\ai-search` | 同上 |

### 16.2 关键实现：junction 感知

通过链接调用脚本时 `$PSScriptRoot` 会是**链接路径**，因此 `scripts\_common.ps1` 增加了 `Resolve-ReparsePath`：逐级解析路径中的 junction/symlink 得到真实路径，再据此推导 `$SkillDir` 与数据根目录 `$Root`（`<data-root>`）。可用环境变量 `AI_SEARCH_ROOT` 覆盖。

### 16.3 行为契约（写在 SKILL.md 与全局指令里）

1. **首次联网请求前自动启动**搜索服务（允许、鼓励、幂等）；
2. **绝不自行停止**——搜索完成、会话空闲都不是关机理由；仅当用户明确要求（"stop the search service"/"关闭搜索服务"等）才执行 `stop-all.ps1`；
3. 主链路（searxng / scrapling）失败才走内置兜底链（open-webSearch → Firecrawl → DuckDuckGo）；
4. OS 计划任务 `AI-Search Idle Stop` 仍会每 15 分钟空闲回收一次（这是用户显式要求保留的机器级机制，不是 AI 的静默行为）。

### 16.4 全局指令文件（全英文、零绝对路径）

`~/.codex/AGENTS.md` 与 `~/.claude/CLAUDE.md` 中的 `ai-search-pipeline` 块由 `sync-codex-config.ps1` 统一维护：英文、无盘符路径、只指示"在你自己的 skills 目录里找名为 `ai-search` 的技能，细节见其 SKILL.md"。校验：两个文件命中盘符路径 **0 处**。

### 16.5 验证记录

| 验证 | 结果 |
|---|---|
| Codex 技能发现（`codex exec` 询问） | **YES** |
| Claude Code 技能发现（`claude -p` 询问） | **YES** |
| 通过 `~/.codex/skills/ai-search/.../status.ps1` 调用 | ✅ 根目录正确解析为 `<data-root>` |
| 通过 `~/.claude/skills/ai-search/.../status.ps1` 调用 | ✅ 同上 |
| `config.toml` TOML 解析 | ✅ 10 个 MCP server，6 个 AI-search 服务器路径正确 |
| 计划任务 | ✅ 动作已指向 `<data-root>\skills\ai-search\scripts\idle-stop.ps1` |

---

## 17. Python 化 + 开源发布（2026-09-20 23:00）

### 17.1 停止语义（明确区分两种）

| 场景 | 入口 | 谁可以触发 |
|---|---|---|
| **立即停止**（同步，不检查空闲） | `ai_search.py stop [--all]` | **仅当用户明文要求**（"关闭搜索服务"/"stop it now"）；模型**不得**自行调用 |
| 空闲回收（先检查空闲时长） | `ai_search.py idle-check --minutes N` | OS 计划任务，每 15 分钟；模型不得调用，也不得绕过 |

该契约写入 SKILL.md 的 Golden rules、`~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`（全英文、无硬编码路径，只提示"去 skills 目录找 ai-search 技能"）。

### 17.2 全量 Python 重写（跨平台）

- 单一入口 `skills/ai-search/scripts/ai_search.py`（**仅用标准库**，Python 3.9+，Windows/macOS/Linux 通用）。
- 命令：`start / stop / idle-check / status / verify / install-idle-task / uninstall-idle-task / sync-agent-config / link-skill / bootstrap / repair / rollback`。
- 计划任务：Windows `schtasks`（调用 `pythonw.exe` 避免黑框）、macOS `launchd`、Linux `crontab`。
- 技能链接：Windows 用 junction（`mklink /J`），POSIX 用 `os.symlink`。
- 配置由 `config/settings.json` 统一管理（替代 PowerShell 的 `.psd1`），脚本**零硬编码路径**。
- 原 `scripts/*.ps1` 已全部删除。

### 17.3 平台差异处理

| 事项 | Windows | macOS / Linux |
|---|---|---|
| 容器端口可达性 | WSL 端口发布在 Windows 侧不可用 → **SSH 隧道直连容器 IP** | 原生 podman 直接用发布端口（隧道失败时自动回退） |
| known_hosts | `UserKnownHostsFile=NUL` | `/dev/null` |
| 后台进程 | `DETACHED_PROCESS + CREATE_NO_WINDOW` | `start_new_session=True` |
| 定时任务 | schtasks | launchd / cron |

### 17.4 开源发布

- 远端：`https://github.com/OldNew777/ai-search.git`（分支 `master`）。
- `.gitignore` 排除：`.env`、`_backup/`、`logs/`、所有 venv、`node_modules/`、`open-websearch/`、`mcp-searxng/`（后两者由 `bootstrap` 重建）。
- 已提交 14 个文件（技能、网关源码、SearXNG 配置、文档、探针）；**不含**任何密钥、venv、镜像或备份。
- 首次推送完成：`bf4abb6..a7ab6d1 master -> master`。

### 17.5 新机器开箱即用

```bash
git clone https://github.com/OldNew777/ai-search.git && cd ai-search
python skills/ai-search/scripts/ai_search.py bootstrap
```

`bootstrap` 依序：建 venv 装 Scrapling(+Chromium) → 装 fallback 网关（editable）→ clone/build open-websearch → 装 mcp-searxng → 生成 `.env` → 建立 Codex/Claude 技能链接 → 写 `config.toml`/`AGENTS.md`/`CLAUDE.md` → 装空闲回收任务 → 启动并真实检索验证。全流程幂等，可重复执行。

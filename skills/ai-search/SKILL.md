---
name: ai-search
description: Self-hosted web search, page fetching and crawling pipeline (SearXNG inside a Podman machine, plus mcp-searxng, Scrapling and a deterministic fallback chain open-webSearch -> Firecrawl -> DuckDuckGo). Use whenever a task needs live web information - searching the web, reading or crawling a URL, checking current versions, changelogs, release notes or issue threads, or verifying facts online.
---

# AI Search - self-hosted web search & crawl pipeline

Cross-platform (Windows / macOS / Linux). Everything is driven by one standard-library Python CLI:
`scripts/ai_search.py` next to this file.

## What this gives you

| MCP server | Role | Key tools |
|---|---|---|
| `searxng` | **Primary web search** (self-hosted, private, no API key) | `searxng_web_search`, `web_url_read` |
| `scrapling` | **Primary fetch / crawl** (local Chromium) | `fetch`, `stealthy_fetch`, `bulk_fetch`, `open_session` + `session_fetch`, `screenshot` |
| `fallback_search` | **Deterministic fallback** (open-webSearch -> Firecrawl keyless -> DuckDuckGo) | `web_search`, `fetch_page`, `provider_status` |
| `open_websearch`, `firecrawl`, `duckduckgo` | Direct access | only when `fallback_search` itself is unavailable |

## Golden rules

1. **Start the service before your first web request** - this is expected and approved. The start command is idempotent (about a second when it is already up, 10-40 s on a cold start).
2. **Immediate shutdown is user-only.** Run `stop` (or `stop --all`) **only when the user explicitly asks to stop the search service right now**, e.g. "stop the search service", "shut it down", "kill searxng", "stop podman". Never run it on your own initiative - not after a search, not when idle, not "to be tidy".
3. **Idle reclaiming is machine-managed.** An OS scheduler entry runs `idle-check` (every 15 minutes by default) and only stops the service after it has been idle past the threshold. Do not call `idle-check` yourself and do not work around it.
4. **Prefer the primary path**: `searxng` for search, `scrapling` for fetching. Switch to `fallback_search` only when the primary path errors, times out, returns nothing or is rate limited.

## Locating this skill (never hardcode a path)

`SKILL.md`, `config/settings.json` and `scripts/ai_search.py` live in the same skill directory.
Resolve that directory from your own skills root at runtime:

- Codex: `$CODEX_HOME/skills/ai-search` (normally `~/.codex/skills/ai-search`)
- Claude Code: `~/.claude/skills/ai-search`

Both are links to the same canonical directory. If neither exists, ask the user where the skill is installed.
Below, `<skill>` means that resolved directory.

## Commands

```bash
# 1. Start (idempotent, safe to run automatically before any web work)
python "<skill>/scripts/ai_search.py" start            # add --with-daemon for many open-webSearch calls

# 2. Health
python "<skill>/scripts/ai_search.py" status           # ports, SearXNG health, tunnel pid, container
python "<skill>/scripts/ai_search.py" verify           # start + one real query, prints the result count

# 3. Immediate stop - ONLY on an explicit user request
python "<skill>/scripts/ai_search.py" stop             # stop tunnel + container
python "<skill>/scripts/ai_search.py" stop --all       # also stop the podman machine

# 4. Maintenance
python "<skill>/scripts/ai_search.py" install-idle-task --minutes 15
python "<skill>/scripts/ai_search.py" uninstall-idle-task
python "<skill>/scripts/ai_search.py" sync-agent-config    # rewrite Codex MCP config + AGENTS.md/CLAUDE.md
python "<skill>/scripts/ai_search.py" link-skill           # link into the Codex/Claude skills dirs
python "<skill>/scripts/ai_search.py" bootstrap            # fresh machine: install everything
python "<skill>/scripts/ai_search.py" repair               # after moving the data root
python "<skill>/scripts/ai_search.py" rollback             # restore the last backed-up agent config
```

Use `python3` instead of `python` on macOS/Linux if that is the available interpreter name.

If the sandbox blocks running the script, request approval - this pipeline is user-installed and approved.

## Using the tools well

- Search: `searxng_web_search` with `query`, optional `num_results`, `language`, `time_range` (`day`/`week`/`month`/`year`), `engines`.
- Fetch: `fetch` for normal pages, `stealthy_fetch` for JS-heavy or protected pages, `bulk_fetch` for several URLs, `open_session` + `session_fetch` when cookies/session state matter.
- Fallback: `fallback_search.web_search` / `fetch_page`; report which `provider` answered if you had to fall back.
- One primary attempt plus one fallback attempt is enough - do not fan out across providers for the same question.

## Troubleshooting

| Symptom | Action |
|---|---|
| `searxng` MCP returns connection errors | `start`, then `status`. The tunnel or container is down. |
| Podman machine stopped | `start` handles it; a cold start takes 20-40 s. |
| Zero search results | Some upstream engines may be blocked on this network; use `fallback_search` and say so. |
| Stale results | Pass `time_range`, or use a more specific query. |
| Everything fails | Fall back to your built-in web tools and tell the user the local pipeline is down. |
| Moved/renamed the folder | `repair`, then restart the agent. |

## Layout

```
<data-root>/
  skills/ai-search/           <- this skill (SKILL.md, scripts/ai_search.py, config/settings.json)
  searxng/core-config/        <- SearXNG settings mounted into the container
  fallback-search/            <- local MCP gateway implementing the fallback chain
  scrapling/.venv/            <- Scrapling + Chromium
  open-websearch/  mcp-searxng/   <- third-party pieces, restored by `bootstrap`
  logs/  _backup/  .env
```

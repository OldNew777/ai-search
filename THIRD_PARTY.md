# Third-party components

This repository **does not redistribute any third-party code**. Everything below is either

- **installed from upstream** by `python skills/ai-search/scripts/ai_search.py bootstrap`, or
- **pulled as an official container image** at run time, or
- an **external prerequisite** the user installs themselves, or
- a **hosted service** called over the network.

That matters for licensing: attribution obligations of MIT / BSD / Apache-2.0 apply when you
*redistribute* the code, which this repository does not do. The list below is kept accurate anyway,
both as good practice and so that operators know what they are running.

## Components used at run time

| Component | Role in this project | License | How it is obtained | Redistributed here? |
|---|---|---|---|---|
| [SearXNG](https://github.com/searxng/searxng) | Primary search backend (metasearch) | **AGPL-3.0** | Official image `docker.io/searxng/searxng:latest` pulled by Podman; we only supply `searxng/core-config/settings.yml` | No (image only, unmodified) |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | Primary page fetching / crawling (MCP server) | **BSD-3-Clause** | `pip install "scrapling[ai]"` + `scrapling install` (Chromium) | No |
| [open-webSearch](https://github.com/Aas-ee/open-webSearch) | Fallback chain step 1 (search + fetch) | **Apache-2.0** | `git clone` + `npm install` + `npm run build` | No |
| [mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng) | MCP server that talks to SearXNG | **MIT** | `npm install mcp-searxng` | No |
| [ddgs](https://github.com/deedy5/ddgs) | Fallback chain step 3 (DuckDuckGo, library) | **MIT** | pip dependency of `fallback-search` | No |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp`) | MCP runtime for the fallback gateway | **MIT** | pip dependency | No |
| [httpx](https://github.com/encode/httpx) | HTTP client in the fallback gateway | **BSD-3-Clause** | pip dependency | No |
| [duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server) (optional) | Extra DuckDuckGo MCP server | **MIT** | `uvx duckduckgo-mcp-server` at run time | No |
| Firecrawl keyless (`mcp.firecrawl.dev`) | Fallback chain step 2 | **Commercial hosted service** (no code shipped) | Remote MCP endpoint; free, rate-limited | No (service only) |

## External prerequisites (installed by the user, not by this repo)

| Component | Role | License |
|---|---|---|
| [Podman](https://github.com/containers/podman) / Podman Desktop | Container runtime | Apache-2.0 |
| WSL2 | Linux VM backend on Windows | Microsoft proprietary licence (Windows EULA) |
| [Node.js](https://github.com/nodejs/node) | Runs open-webSearch / mcp-searxng | MIT |
| [Python](https://www.python.org/) | Runs `ai_search.py` and the fallback gateway | PSF-2.0 |
| [uv](https://github.com/astral-sh/uv) | Venv + package installation | Apache-2.0 / MIT |

## What this means in practice

1. **This repository's own code** (`skills/ai-search/`, `fallback-search/`, `searxng/core-config/`, docs)
   is licensed under **Apache-2.0** — see `LICENSE` and `NOTICE`.
2. **SearXNG is AGPL-3.0.** We do not ship or modify its source; we pull the official image and talk
   to it over its documented HTTP/JSON API, and we ship only a configuration file. AGPL's copyleft
   therefore does not extend to this repository.
   *If you modify SearXNG, or redistribute the image together with changes, the AGPL-3.0 obligations
   (including the network-use source offer of §13) become yours to satisfy.*
3. **If you ever vendor one of these components into your own repository**, the usual conditions
   apply: MIT / BSD-3-Clause require keeping the copyright notice and licence text; Apache-2.0
   additionally requires keeping the `NOTICE` file and stating changes; AGPL-3.0 would require the
   combined distribution to be AGPL-3.0 and the source to be offered.
4. **Search services have terms of use, separate from the library licences.** The `ddgs` library is
   MIT, but that grants no rights to DuckDuckGo's service; likewise Firecrawl's hosted endpoint is
   governed by its own terms. Keep request rates polite and follow each service's ToS.

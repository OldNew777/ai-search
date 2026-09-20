"""fallback-search — 确定性降级检索网关（MCP, stdio）。

搜索链路（严格按序）：open-webSearch(本地 daemon) -> Firecrawl(keyless MCP) -> DuckDuckGo(ddgs)
抓取链路（严格按序）：open-webSearch(/fetch-web) -> Firecrawl(keyless firecrawl_scrape)

设计要点：
- 每个 provider 独立超时；连续失败达阈值后熔断一段时间（cooldown），避免每次都等超时；
- 返回体带 provider 与 attempts，便于确认"这次是谁应答的"；
- 不写死任何密钥；端点与开关通过环境变量注入。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from pathlib import Path

try:
    from ddgs import DDGS
except Exception:  # pragma: no cover
    DDGS = None  # type: ignore[assignment]

OPEN_WEBSEARCH_URL = os.getenv("OPEN_WEBSEARCH_URL", "http://127.0.0.1:47313").rstrip("/")
FIRECRAWL_MCP_URL = os.getenv("FIRECRAWL_MCP_URL", "https://mcp.firecrawl.dev/v2/mcp")
PROVIDER_TIMEOUT = float(os.getenv("FALLBACK_PROVIDER_TIMEOUT", "25"))
FAILURE_THRESHOLD = int(os.getenv("FALLBACK_FAILURE_THRESHOLD", "2"))
COOLDOWN_SECONDS = float(os.getenv("FALLBACK_COOLDOWN_SECONDS", "90"))

# 默认路径由包位置推导（<root>/fallback-search/src/fallback_search/server.py -> <root>），
# 便于整目录迁移；也可用 OPEN_WEBSEARCH_DIR 覆盖。
_DEFAULT_ROOT = Path(__file__).resolve().parents[3]
OPEN_WEBSEARCH_DIR = os.getenv("OPEN_WEBSEARCH_DIR", str(_DEFAULT_ROOT / "open-websearch"))
OPEN_WEBSEARCH_NODE = os.getenv("OPEN_WEBSEARCH_NODE", "node")


def _parse_cli_json(raw: str) -> dict[str, Any]:
    idx = raw.find("{")
    if idx < 0:
        raise RuntimeError("open-websearch CLI 输出中没有 JSON")
    return json.loads(raw[idx:])


async def _ow_cli_search(query: str, limit: int) -> list[dict[str, Any]]:
    """无 daemon 时的一次性 CLI 调用（用完即退，无常驻进程）。"""
    proc = await asyncio.create_subprocess_exec(
        OPEN_WEBSEARCH_NODE,
        os.path.join(OPEN_WEBSEARCH_DIR, "build", "index.js"),
        "search", query, "--limit", str(limit), "--json",
        cwd=OPEN_WEBSEARCH_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=PROVIDER_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("open-websearch CLI 超时")
    payload = _parse_cli_json(out.decode("utf-8", "replace"))
    if payload.get("status") != "ok":
        raise RuntimeError(f"CLI 返回异常: {payload.get('error')}")
    return [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": r.get("description") or r.get("snippet"),
            "engine": r.get("engine"),
        }
        for r in payload["data"]["results"]
    ]


async def _ow_search_with_fallback(client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
    """优先本地 daemon（快），不可用时退回一次性 CLI（无常驻进程）。"""
    try:
        return await _ow_search(client, query, limit)
    except Exception:
        return await _ow_cli_search(query, limit)


_state: dict[str, dict[str, float]] = {}


def _is_open(name: str) -> bool:
    st = _state.get(name)
    if not st:
        return False
    until = st.get("open_until", 0.0)
    if until and time.monotonic() < until:
        return True
    if until and time.monotonic() >= until:
        st["failures"] = 0.0
        st["open_until"] = 0.0
    return False


def _record(name: str, ok: bool) -> None:
    st = _state.setdefault(name, {"failures": 0.0, "open_until": 0.0})
    if ok:
        st["failures"] = 0.0
        st["open_until"] = 0.0
        return
    st["failures"] += 1
    if st["failures"] >= FAILURE_THRESHOLD:
        st["open_until"] = time.monotonic() + COOLDOWN_SECONDS


# ---------------------------------------------------------------- providers


async def _ow_search(client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
    resp = await client.post(
        f"{OPEN_WEBSEARCH_URL}/search",
        json={"query": query, "limit": limit},
        timeout=PROVIDER_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"open-websearch status={payload.get('status')} error={payload.get('error')}")
    return [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": r.get("description") or r.get("snippet"),
            "engine": r.get("engine"),
        }
        for r in payload["data"]["results"]
    ]


async def _ow_fetch(client: httpx.AsyncClient, url: str, max_chars: int) -> dict[str, Any]:
    resp = await client.post(
        f"{OPEN_WEBSEARCH_URL}/fetch-web",
        json={"url": url, "maxChars": max_chars, "renderMode": "auto"},
        timeout=PROVIDER_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"open-websearch status={payload.get('status')} error={payload.get('error')}")
    data = payload["data"]
    return {"url": data.get("url", url), "content": data.get("content") or data.get("markdown") or ""}


def _tool_payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured:
        return structured
    texts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            texts.append(text)
    raw = "\n".join(texts).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"text": raw}


async def _firecrawl_call(tool: str, arguments: dict[str, Any]) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(FIRECRAWL_MCP_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            if getattr(result, "is_error", False):
                raise RuntimeError(f"{tool} returned isError")
            return _tool_payload(result)


async def _fc_search(query: str, limit: int) -> list[dict[str, Any]]:
    payload = await _firecrawl_call("firecrawl_search", {"query": query, "limit": limit})
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected firecrawl_search payload")
    items = payload.get("data") or payload.get("results") or payload.get("web") or []
    if isinstance(items, dict):
        items = items.get("results") or items.get("web") or []
    results = [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": r.get("description") or r.get("snippet"),
            "engine": "firecrawl",
        }
        for r in items
        if isinstance(r, dict)
    ]
    if not results:
        raise RuntimeError("firecrawl_search returned no results")
    return results


async def _fc_fetch(url: str, max_chars: int) -> dict[str, Any]:
    payload = await _firecrawl_call(
        "firecrawl_scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    )
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected firecrawl_scrape payload")
    data = payload.get("data") or payload
    content = data.get("markdown") or data.get("content") or ""
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    if not content:
        raise RuntimeError("firecrawl_scrape returned empty content")
    return {"url": data.get("url", url), "content": str(content)[:max_chars]}


def _ddg_search_sync(query: str, limit: int) -> list[dict[str, Any]]:
    if DDGS is None:
        raise RuntimeError("ddgs 未安装")
    with DDGS() as ddgs:
        rows = list(ddgs.text(query, max_results=limit))
    return [
        {
            "title": r.get("title"),
            "url": r.get("href") or r.get("url"),
            "snippet": r.get("body") or r.get("snippet"),
            "engine": "duckduckgo",
        }
        for r in rows
    ]


async def _ddg_search(query: str, limit: int) -> list[dict[str, Any]]:
    return await asyncio.wait_for(asyncio.to_thread(_ddg_search_sync, query, limit), timeout=PROVIDER_TIMEOUT)


# ---------------------------------------------------------------- MCP tools

mcp = MCPServer("fallback-search")


@mcp.tool()
async def web_search(query: str, max_results: int = 8) -> dict[str, Any]:
    """按序降级搜索：open-webSearch -> Firecrawl(keyless) -> DuckDuckGo。

    返回 provider（最终应答者）与 attempts（每一跳的结果），便于排障。
    """
    attempts: list[dict[str, Any]] = []
    limit = max(1, min(int(max_results), 20))

    async with httpx.AsyncClient(headers={"User-Agent": "fallback-search/1.0"}) as client:
        chain: list[tuple[str, Any]] = []
        if not _is_open("open-websearch"):
            chain.append(("open-websearch", lambda: _ow_search_with_fallback(client, query, limit)))
        if not _is_open("firecrawl"):
            chain.append(("firecrawl", lambda: _fc_search(query, limit)))
        if not _is_open("duckduckgo"):
            chain.append(("duckduckgo", lambda: _ddg_search(query, limit)))

        for name, fn in chain:
            started = time.monotonic()
            try:
                results = await fn()
                if not results:
                    raise RuntimeError("empty result set")
                _record(name, True)
                attempts.append({"provider": name, "ok": True, "ms": round((time.monotonic() - started) * 1000)})
                return {
                    "ok": True,
                    "provider": name,
                    "query": query,
                    "count": len(results),
                    "results": results,
                    "attempts": attempts,
                }
            except Exception as exc:  # noqa: BLE001
                _record(name, False)
                attempts.append(
                    {
                        "provider": name,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "ms": round((time.monotonic() - started) * 1000),
                    }
                )

    return {"ok": False, "provider": None, "query": query, "results": [], "attempts": attempts}


@mcp.tool()
async def fetch_page(url: str, max_chars: int = 20000) -> dict[str, Any]:
    """按序降级抓取正文：open-webSearch(/fetch-web) -> Firecrawl(keyless scrape)。"""
    attempts: list[dict[str, Any]] = []
    limit = max(1000, min(int(max_chars), 120000))

    async with httpx.AsyncClient(headers={"User-Agent": "fallback-search/1.0"}) as client:
        chain: list[tuple[str, Any]] = []
        if not _is_open("open-websearch"):
            chain.append(("open-websearch", lambda: _ow_fetch(client, url, limit)))
        if not _is_open("firecrawl"):
            chain.append(("firecrawl", lambda: _fc_fetch(url, limit)))

        for name, fn in chain:
            started = time.monotonic()
            try:
                page = await fn()
                if not page.get("content"):
                    raise RuntimeError("empty content")
                _record(name, True)
                attempts.append({"provider": name, "ok": True, "ms": round((time.monotonic() - started) * 1000)})
                return {"ok": True, "provider": name, "attempts": attempts, **page}
            except Exception as exc:  # noqa: BLE001
                _record(name, False)
                attempts.append(
                    {
                        "provider": name,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "ms": round((time.monotonic() - started) * 1000),
                    }
                )

    return {"ok": False, "provider": None, "url": url, "content": "", "attempts": attempts}


@mcp.tool()
async def provider_status() -> dict[str, Any]:
    """查看各 provider 的熔断状态与端点配置（不触发网络请求）。"""
    return {
        "endpoints": {
            "open-websearch": OPEN_WEBSEARCH_URL,
            "firecrawl": FIRECRAWL_MCP_URL,
            "duckduckgo": "ddgs (local library)",
        },
        "timeout_seconds": PROVIDER_TIMEOUT,
        "failure_threshold": FAILURE_THRESHOLD,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "state": {
            name: {
                "failures": int(st["failures"]),
                "open": _is_open(name),
            }
            for name, st in _state.items()
        },
    }


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()

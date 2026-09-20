"""通用 stdio MCP 探针：列出工具并按需调用一次。

用法：python probe-stdio.py <command> [args...] -- <tool> <json-args>
"""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
try:
    from mcp.client.stdio import stdio_client
except Exception:
    from mcp.client.stdio import stdio_client  # type: ignore


def split_argv(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


async def main():
    cmd_argv, call_argv = split_argv(sys.argv[1:])
    command, args = cmd_argv[0], cmd_argv[1:]
    env = dict(os.environ)
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            print("INIT:", getattr(info, "name", "?"), "| protocol:", getattr(init, "protocol_version", "?"))
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOLS(%d): %s" % (len(names), ", ".join(names)))
            if call_argv:
                tool, raw = call_argv[0], (call_argv[1] if len(call_argv) > 1 else "{}")
                res = await session.call_tool(tool, json.loads(raw))
                payload = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                if payload is None:
                    texts = [getattr(c, "text", "") for c in (getattr(res, "content", []) or [])]
                    raw_text = "\n".join(t for t in texts if t)
                    try:
                        payload = json.loads(raw_text)
                    except Exception:
                        payload = raw_text[:2000]
                print("RESULT:", json.dumps(payload, ensure_ascii=False)[:2500])


asyncio.run(main())

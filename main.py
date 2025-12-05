import asyncio
import json
import logging
import os
from typing import Any, Dict, Iterable

import websockets
from dotenv import load_dotenv
from mcp import ClientSession, sse_client
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("bridge")


def load_config() -> Dict[str, str]:
    load_dotenv()
    xiaozhi_wss = os.getenv("XIAOZHI_WSS_URL")
    sse_url = os.getenv("SUPERMEMORY_SSE_URL", "https://api.supermemory.ai/mcp")
    token = os.getenv("SUPERMEMORY_TOKEN")

    missing = [name for name, value in {
        "XIAOZHI_WSS_URL": xiaozhi_wss,
        "SUPERMEMORY_TOKEN": token,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return {
        "xiaozhi_wss": xiaozhi_wss,
        "sse_url": sse_url,
        "token": token,
    }


def register_remote_tools(fast_mcp: FastMCP, session: ClientSession, tools: Iterable[Any]) -> None:
    """Wrap remote Supermemory tools and expose them through FastMCP."""

    for tool in tools:
        name = getattr(tool, "name", None) or tool.get("name")
        description = getattr(tool, "description", None) or tool.get("description", "")
        input_schema = getattr(tool, "input_schema", None) or tool.get("input_schema") or tool.get("inputSchema")

        if not name:
            logger.warning("Skipping tool with no name: %s", tool)
            continue

        async def _runner(_name=name, **kwargs):
            return await session.call_tool(_name, arguments=kwargs)

        register_tool = getattr(fast_mcp, "register_tool", None)
        register_decorator = getattr(fast_mcp, "tool", None)

        if register_tool:
            register_tool(name=name, description=description, func=_runner, schema=input_schema)
        elif register_decorator:
            register_decorator(name=name, description=description, schema=input_schema)(_runner)
        else:
            raise RuntimeError("FastMCP does not expose a tool registration method")

        logger.info("Registered remote tool: %s", name)


async def bridge() -> None:
    cfg = load_config()
    headers = {"Authorization": f"Bearer {cfg['token']}", "Accept": "text/event-stream"}

    async with sse_client(cfg["sse_url"], headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

            fast_mcp = FastMCP("supermemory-bridge")
            register_remote_tools(fast_mcp, session, tools)

            async with websockets.connect(cfg["xiaozhi_wss"]) as ws:
                logger.info("Connected to Xiaozhi WebSocket")
                async for msg in ws:
                    try:
                        request = json.loads(msg)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON message; ignoring")
                        continue

                    try:
                        response = await fast_mcp._server.process_request(request)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Error handling request: %s", exc)
                        continue

                    if response:
                        await ws.send(json.dumps(response))


if __name__ == "__main__":
    asyncio.run(bridge())

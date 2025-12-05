import asyncio
import json
import logging
import os
from typing import Any, Dict, Iterable, List

import httpx
import websockets
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("bridge")


def load_config() -> Dict[str, str]:
    load_dotenv()
    xiaozhi_wss = os.getenv("XIAOZHI_WSS_URL")
    mcp_url = os.getenv("SUPERMEMORY_MCP_URL", "https://api.supermemory.ai/mcp")
    token = os.getenv("SUPERMEMORY_TOKEN")
    request_timeout = float(os.getenv("SUPERMEMORY_TIMEOUT", "30"))
    retry_delay = float(os.getenv("SUPERMEMORY_RETRY_DELAY", "5"))

    missing = [name for name, value in {
        "XIAOZHI_WSS_URL": xiaozhi_wss,
        "SUPERMEMORY_TOKEN": token,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return {
        "xiaozhi_wss": xiaozhi_wss,
        "mcp_url": mcp_url,
        "token": token,
        "request_timeout": request_timeout,
        "retry_delay": retry_delay,
    }


class SupermemoryClient:
    """Simple JSON-RPC client for Supermemory MCP API."""

    def __init__(self, url: str, token: str, timeout: float = 30):
        self.url = url
        self.token = token
        self.client = httpx.AsyncClient(timeout=timeout)
        self.request_id = 0

    async def call(self, method: str, params: Dict[str, Any] | None = None) -> Any:
        """Send JSON-RPC request and get immediate response."""
        self.request_id += 1
        
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        
        logger.debug("Calling %s: %s", method, params)
        logger.debug("Request payload: %s", json.dumps(payload))
        
        response = await self.client.post(self.url, json=payload, headers=headers)
        
        logger.info("Response status: %s", response.status_code)
        logger.debug("Response headers: %s", dict(response.headers))
        logger.debug("Response body: %s", response.text[:500])
        
        response.raise_for_status()
        
        data = response.json()
        
        if "error" in data:
            raise RuntimeError(f"JSON-RPC error: {data['error']}")
        
        return data.get("result")

    async def close(self):
        await self.client.aclose()


def register_remote_tools(fast_mcp: FastMCP, client: SupermemoryClient, tools: List[Dict[str, Any]]) -> None:
    """Wrap remote Supermemory tools and expose them through FastMCP."""

    for tool in tools:
        name = tool.get("name")
        description = tool.get("description", "")
        input_schema = tool.get("inputSchema") or tool.get("input_schema")

        if not name:
            logger.warning("Skipping tool with no name: %s", tool)
            continue

        async def _runner(_name=name, **kwargs):
            result = await client.call("tools/call", {"name": _name, "arguments": kwargs})
            return result

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
    logger.info("Connecting to Supermemory MCP at %s", cfg["mcp_url"])

    while True:
        client = None
        try:
            client = SupermemoryClient(cfg["mcp_url"], cfg["token"], cfg["request_timeout"])

            # Initialize session
            init_result = await client.call("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xiaozhi-bridge", "version": "1.0"}
            })
            logger.info("Initialized: %s", init_result)

            # List tools
            tools_result = await client.call("tools/list", {})
            tools = tools_result.get("tools", [])
            logger.info("Found %d tools", len(tools))

            fast_mcp = FastMCP("supermemory-bridge")
            register_remote_tools(fast_mcp, client, tools)

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

        except Exception as exc:  # noqa: BLE001
            logger.exception("Bridge error: %s", exc)
        finally:
            if client:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

        logger.info("Retrying connection in %.1f seconds...", cfg["retry_delay"])
        await asyncio.sleep(cfg["retry_delay"])


if __name__ == "__main__":
    asyncio.run(bridge())




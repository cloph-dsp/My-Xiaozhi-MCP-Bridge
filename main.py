import asyncio
import json
import logging
import os
from typing import Any, Dict, Iterable

import httpx
import websockets
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioClientBackend
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("bridge")


def load_config() -> Dict[str, str]:
    load_dotenv()
    xiaozhi_wss = os.getenv("XIAOZHI_WSS_URL")
    jsonrpc_url = os.getenv("SUPERMEMORY_JSONRPC_URL", "https://api.supermemory.ai/mcp")
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
        "jsonrpc_url": jsonrpc_url,
        "token": token,
        "request_timeout": request_timeout,
        "retry_delay": retry_delay,
    }


class JsonRpcRemoteClient:
    """Client for JSON-RPC MCP servers (Supermemory)."""

    def __init__(self, url: str, token: str, timeout: float = 30):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.request_id = 0
        self.client = httpx.AsyncClient(timeout=timeout)

    async def call(self, method: str, params: Dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC 2.0 request."""
        self.request_id += 1
        request_body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self.request_id,
        }

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        logger.debug("POST %s: %s", self.url, method)
        response = await self.client.post(self.url, json=request_body, headers=headers)
        response.raise_for_status()

        result = response.json()
        if "error" in result and result["error"]:
            raise RuntimeError(f"JSON-RPC error: {result['error']}")

        return result.get("result")

    async def close(self):
        await self.client.aclose()


def register_remote_tools(fast_mcp: FastMCP, client: JsonRpcRemoteClient, tools: Iterable[Any]) -> None:
    """Wrap remote Supermemory tools and expose them through FastMCP."""

    for tool in tools:
        name = getattr(tool, "name", None) or tool.get("name")
        description = getattr(tool, "description", None) or tool.get("description", "")
        input_schema = getattr(tool, "input_schema", None) or tool.get("input_schema") or tool.get("inputSchema")

        if not name:
            logger.warning("Skipping tool with no name: %s", tool)
            continue

        async def _runner(_name=name, **kwargs):
            return await client.call("tools/call", {"name": _name, "arguments": kwargs})

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

    logger.info("Connecting to Supermemory JSON-RPC at %s", cfg["jsonrpc_url"])

    while True:
        try:
            client = JsonRpcRemoteClient(cfg["jsonrpc_url"], cfg["token"], cfg["request_timeout"])

            # Initialize and list tools
            await client.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "xiaozhi-bridge", "version": "1.0"}})
            tools = await client.call("tools/list")

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

        except httpx.TimeoutException:
            logger.error("Timed out connecting to Supermemory at %s; increase SUPERMEMORY_TIMEOUT if needed", cfg["jsonrpc_url"])
        except httpx.HTTPError as exc:
            logger.error("HTTP error: %s", exc)
        except websockets.WebSocketException as exc:
            logger.error("WebSocket error: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Bridge loop error: %s", exc)
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

        logger.info("Retrying connection in %.1f seconds...", cfg["retry_delay"])
        await asyncio.sleep(cfg["retry_delay"])


if __name__ == "__main__":
    asyncio.run(bridge())


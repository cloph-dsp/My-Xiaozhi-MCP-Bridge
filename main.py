import asyncio
import json
import logging
import os
from typing import Any, Dict, List

import httpx
import websockets
from dotenv import load_dotenv
from mcp.server import Server
from mcp.types import Tool, TextContent

load_dotenv()
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level), format="[%(levelname)s] %(message)s")
logger = logging.getLogger("bridge")


def load_config() -> Dict[str, str]:
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
        self.session_id = None

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
            "Accept": "application/json, text/event-stream",
        }
        
        # Add session ID for subsequent requests after initialize
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        
        logger.debug("Calling %s: %s", method, params)
        logger.debug("Request payload: %s", json.dumps(payload))
        
        response = await self.client.post(self.url, json=payload, headers=headers)
        
        logger.info("Response status: %s", response.status_code)
        logger.debug("Response headers: %s", dict(response.headers))
        
        # Capture session ID from response headers
        if "mcp-session-id" in response.headers:
            self.session_id = response.headers["mcp-session-id"]
            logger.info("Captured session ID: %s", self.session_id[:16] + "...")
        
        response.raise_for_status()
        
        # Parse SSE format if content-type is text/event-stream
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # Parse SSE: extract data field from "event: message\ndata: {...}\n\n"
            text = response.text
            logger.debug("SSE Response body: %s", text[:500])
            
            for line in text.split("\n"):
                if line.startswith("data: "):
                    data_json = line[6:]  # Strip "data: " prefix
                    data = json.loads(data_json)
                    
                    if "error" in data:
                        raise RuntimeError(f"JSON-RPC error: {data['error']}")
                    
                    return data.get("result")
            
            raise RuntimeError("No data field found in SSE response")
        else:
            # Plain JSON response
            data = response.json()
            
            if "error" in data:
                raise RuntimeError(f"JSON-RPC error: {data['error']}")
            
            return data.get("result")

    async def close(self):
        await self.client.aclose()


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

            # Create MCP server
            server = Server("supermemory-bridge")
            
            # Register list_tools handler
            @server.list_tools()
            async def list_tools():
                return [
                    Tool(
                        name=tool["name"],
                        description=tool.get("description", ""),
                        inputSchema=tool.get("inputSchema", {})
                    )
                    for tool in tools
                ]
            
            # Register call_tool handler
            @server.call_tool()
            async def call_tool(name: str, arguments: dict):
                result = await client.call("tools/call", {"name": name, "arguments": arguments})
                return [TextContent(type="text", text=json.dumps(result))]

            async with websockets.connect(cfg["xiaozhi_wss"]) as ws:
                logger.info("Connected to Xiaozhi WebSocket")
                async for msg in ws:
                    try:
                        request = json.loads(msg)
                        method = request.get("method")
                        req_id = request.get("id")
                        logger.info("◀ Received: %s (id=%s)", method, req_id)
                        logger.debug("◀ Full request: %s", json.dumps(request)[:500])
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON message; ignoring")
                        continue

                    try:
                        response = None
                        
                        if method == "initialize":
                            # Handle initialize
                            response = {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {
                                        "tools": {}
                                    },
                                    "serverInfo": {
                                        "name": "supermemory-bridge",
                                        "version": "1.0.0"
                                    }
                                }
                            }
                        elif method == "tools/list":
                            # Handle tools/list
                            logger.info("Returning %d tools", len(tools))
                            response = {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {
                                    "tools": [
                                        {
                                            "name": tool["name"],
                                            "description": tool.get("description", ""),
                                            "inputSchema": tool.get("inputSchema", {})
                                        }
                                        for tool in tools
                                    ]
                                }
                            }
                        elif method == "tools/call":
                            # Handle tools/call (may take longer, add timeout)
                            params = request.get("params", {})
                            tool_name = params.get("name")
                            arguments = params.get("arguments", {})
                            logger.info("➤ Calling tool: %s", tool_name)
                            logger.debug("➤ Tool arguments: %s", json.dumps(arguments)[:200])
                            
                            try:
                                result = await asyncio.wait_for(
                                    client.call("tools/call", {"name": tool_name, "arguments": arguments}),
                                    timeout=60.0  # 60 second timeout for tool calls
                                )
                                logger.info("✓ Tool result received")
                                logger.debug("✓ Tool result: %s", json.dumps(result)[:500])
                                response = {
                                    "jsonrpc": "2.0",
                                    "id": request.get("id"),
                                    "result": result
                                }
                            except asyncio.TimeoutError:
                                logger.error("✗ Tool call timeout for %s", tool_name)
                                response = {
                                    "jsonrpc": "2.0",
                                    "id": request.get("id"),
                                    "error": {
                                        "code": -32603,
                                        "message": f"Tool call timeout: {tool_name}"
                                    }
                                }
                        elif method == "ping":
                            # Handle ping (heartbeat)
                            response = {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {}
                            }
                        elif method and method.startswith("notifications/"):
                            # Notifications don't require responses
                            logger.info("Received notification: %s", method)
                            response = None
                        else:
                            logger.warning("Unknown method: %s", method)
                            response = {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "error": {
                                    "code": -32601,
                                    "message": f"Method not found: {method}"
                                }
                            }
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Error handling request: %s", exc)
                        response = {
                            "jsonrpc": "2.0",
                            "id": request.get("id"),
                            "error": {
                                "code": -32603,
                                "message": f"Internal error: {str(exc)}"
                            }
                        }

                    if response:
                        try:
                            response_str = json.dumps(response)
                            logger.info("▶ Sending: %s (id=%s)", method, response.get("id"))
                            logger.debug("▶ Full response: %s", response_str[:500])
                            await ws.send(response_str)
                            logger.info("✓ Sent successfully")
                        except websockets.exceptions.ConnectionClosedError:
                            logger.warning("✗ WebSocket closed while sending response; will reconnect")
                            break
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("✗ Error sending response: %s", exc)
                            break
                    else:
                        logger.debug("◇ No response needed (notification)")

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




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


def load_config() -> Dict[str, Any]:
    xiaozhi_wss = os.getenv("XIAOZHI_WSS_URL")
    retry_delay = float(os.getenv("SUPERMEMORY_RETRY_DELAY", "5"))

    # Supermemory configuration
    supermemory_config = {
        "url": os.getenv("SUPERMEMORY_MCP_URL", "https://api.supermemory.ai/mcp"),
        "token": os.getenv("SUPERMEMORY_TOKEN"),
        "timeout": float(os.getenv("SUPERMEMORY_TIMEOUT", "30")),
        "type": "sse",
    }

    # Google Workspace configuration (optional)
    google_workspace_config = None
    if os.getenv("GOOGLE_WORKSPACE_ENABLED", "false").lower() == "true":
        google_workspace_config = {
            "url": os.getenv("GOOGLE_WORKSPACE_MCP_URL"),
            "timeout": float(os.getenv("GOOGLE_WORKSPACE_TIMEOUT", "30")),
            "type": "sse",
        }

    # Validate required fields
    missing = []
    if not xiaozhi_wss:
        missing.append("XIAOZHI_WSS_URL")
    if not supermemory_config["token"]:
        missing.append("SUPERMEMORY_TOKEN")
    if google_workspace_config and not google_workspace_config["url"]:
        missing.append("GOOGLE_WORKSPACE_MCP_URL")
    
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return {
        "xiaozhi_wss": xiaozhi_wss,
        "retry_delay": retry_delay,
        "servers": {
            "supermemory": supermemory_config,
            "google_workspace": google_workspace_config,
        }
    }


class MCPClient:
    """Generic JSON-RPC client for MCP servers (SSE or HTTP)."""

    def __init__(self, name: str, url: str, timeout: float = 30, token: str | None = None):
        self.name = name
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
        }
        
        # Add params based on what's provided
        # Some servers require params to be present (even if empty), others don't accept empty params
        if params is not None:
            payload["params"] = params
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        
        # Add authentication if token is provided
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        
        # Add session ID for subsequent requests after initialize
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        
        logger.debug("[%s] Calling %s with params: %s", self.name, method, params)
        logger.debug("[%s] Full request payload: %s", self.name, json.dumps(payload))
        
        # Stream the response for SSE
        async with self.client.stream("POST", self.url, json=payload, headers=headers) as response:
            logger.info("[%s] Response status: %s", self.name, response.status_code)
            logger.debug("[%s] Response headers: %s", self.name, dict(response.headers))
            
            # Capture session ID from response headers
            if "mcp-session-id" in response.headers:
                self.session_id = response.headers["mcp-session-id"]
                logger.info("[%s] Captured session ID: %s", self.name, self.session_id[:16] + "...")
            
            response.raise_for_status()
            
            # Parse SSE format if content-type is text/event-stream
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                # Read SSE stream line by line
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    
                    # Process complete lines
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        
                        if line.startswith("data: "):
                            data_json = line[6:]  # Strip "data: " prefix
                            try:
                                data = json.loads(data_json)
                                logger.debug("[%s] SSE data line: %s", self.name, data_json[:500])
                                
                                if "error" in data:
                                    raise RuntimeError(f"JSON-RPC error: {data['error']}")
                                
                                if "result" in data:
                                    return data.get("result")
                            except json.JSONDecodeError:
                                logger.warning("[%s] Invalid JSON in SSE data: %s", self.name, data_json[:100])
                
                raise RuntimeError(f"[{self.name}] No result found in SSE response")
            else:
                # Plain JSON response
                text = await response.aread()
                data = json.loads(text)
                
                if "error" in data:
                    raise RuntimeError(f"[{self.name}] JSON-RPC error: {data['error']}")
                
                return data.get("result")

    async def close(self):
        await self.client.aclose()


async def bridge() -> None:
    cfg = load_config()
    
    # Initialize all enabled MCP clients
    clients = {}
    all_tools = []
    
    while True:
        try:
            # Connect to all enabled MCP servers
            for server_name, server_config in cfg["servers"].items():
                if server_config is None:
                    continue
                    
                logger.info("Connecting to %s MCP at %s", server_name, server_config["url"])
                
                client = MCPClient(
                    name=server_name,
                    url=server_config["url"],
                    timeout=server_config["timeout"],
                    token=server_config.get("token")
                )
                
                # Initialize session
                init_result = await client.call("initialize", {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "xiaozhi-bridge", "version": "1.0"}
                })
                logger.info("[%s] Initialized: %s", server_name, init_result)
                
                # List tools
                tools_result = await client.call("tools/list", {})
                server_tools = tools_result.get("tools", [])
                
                # Add server prefix to tool names to avoid conflicts
                for tool in server_tools:
                    tool["_server"] = server_name  # Track which server owns this tool
                    tool["name"] = f"{server_name}_{tool['name']}"  # Prefix tool name
                
                logger.info("[%s] Found %d tools", server_name, len(server_tools))
                
                clients[server_name] = client
                all_tools.extend(server_tools)
            
            logger.info("Total tools from all servers: %d", len(all_tools))

            # Create MCP server (kept for potential future use)
            server = Server("multi-mcp-bridge")
            
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
                                        "name": "multi-mcp-bridge",
                                        "version": "1.0.0"
                                    }
                                }
                            }
                        elif method == "tools/list":
                            # Handle tools/list - return tools from all servers
                            logger.info("Returning %d tools from %d servers", len(all_tools), len(clients))
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
                                        for tool in all_tools
                                    ]
                                }
                            }
                        elif method == "tools/call":
                            # Handle tools/call - route to correct server
                            params = request.get("params", {})
                            tool_name = params.get("name")
                            arguments = params.get("arguments", {})
                            
                            # Find which server owns this tool
                            target_server = None
                            original_tool_name = None
                            for tool in all_tools:
                                if tool["name"] == tool_name:
                                    target_server = tool["_server"]
                                    # Remove server prefix to get original tool name
                                    original_tool_name = tool_name.replace(f"{target_server}_", "", 1)
                                    break
                            
                            if not target_server or target_server not in clients:
                                logger.error("Tool %s not found in any server", tool_name)
                                response = {
                                    "jsonrpc": "2.0",
                                    "id": request.get("id"),
                                    "error": {
                                        "code": -32601,
                                        "message": f"Tool not found: {tool_name}"
                                    }
                                }
                            else:
                                logger.info("➤ Calling tool: %s on server %s", original_tool_name, target_server)
                                logger.debug("➤ Tool arguments: %s", json.dumps(arguments)[:200])
                                
                                try:
                                    client = clients[target_server]
                                    result = await asyncio.wait_for(
                                        client.call("tools/call", {"name": original_tool_name, "arguments": arguments}),
                                        timeout=60.0  # 60 second timeout for tool calls
                                    )
                                    logger.info("✓ Tool result received from %s", target_server)
                                    logger.debug("✓ Tool result: %s", json.dumps(result)[:500])
                                    response = {
                                        "jsonrpc": "2.0",
                                        "id": request.get("id"),
                                        "result": result
                                    }
                                except asyncio.TimeoutError:
                                    logger.error("✗ Tool call timeout for %s on %s", original_tool_name, target_server)
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
            # Close all clients
            for client in clients.values():
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
            clients.clear()
            all_tools.clear()

        logger.info("Retrying connection in %.1f seconds...", cfg["retry_delay"])
        await asyncio.sleep(cfg["retry_delay"])


async def list_tools_only() -> None:
    """List all available tools from all configured MCP servers."""
    cfg = load_config()
    
    print("\n" + "="*60)
    print("Available Tools from All MCP Servers")
    print("="*60 + "\n")
    
    total_tools = 0
    
    for server_name, server_config in cfg["servers"].items():
        if server_config is None:
            continue
        
        try:
            print(f"📡 Connecting to {server_name}...")
            
            client = MCPClient(
                name=server_name,
                url=server_config["url"],
                timeout=server_config["timeout"],
                token=server_config.get("token")
            )
            
            # Initialize session
            await client.call("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xiaozhi-bridge-cli", "version": "1.0"}
            })
            
            # List tools
            tools_result = await client.call("tools/list", {})
            server_tools = tools_result.get("tools", [])
            
            print(f"✓ Found {len(server_tools)} tools from {server_name}\n")
            
            for tool in server_tools:
                prefixed_name = f"{server_name}_{tool['name']}"
                description = tool.get("description", "No description")
                print(f"  • {prefixed_name}")
                print(f"    {description}")
                
                # Show input schema if available
                input_schema = tool.get("inputSchema", {})
                properties = input_schema.get("properties", {})
                if properties:
                    print(f"    Parameters: {', '.join(properties.keys())}")
                print()
            
            total_tools += len(server_tools)
            
            await client.close()
            
        except Exception as exc:
            print(f"✗ Error connecting to {server_name}: {exc}\n")
    
    print("="*60)
    print(f"Total: {total_tools} tools from {len([s for s in cfg['servers'].values() if s])} servers")
    print("="*60 + "\n")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--list-tools":
        asyncio.run(list_tools_only())
    else:
        asyncio.run(bridge())




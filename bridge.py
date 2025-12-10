"""Bridge server that connects Xiaozhi to multiple MCP servers."""
import asyncio
import json
import logging
import os
from typing import Any, Dict, List

import websockets

from config import Config
from gemini_service import GeminiService
from google_workspace_handler import GoogleWorkspaceHandler
from mcp_clients import MCPClient, StdioMCPClient
from tool_manager import ToolManager

logger = logging.getLogger("bridge")


class MCPBridge:
    """Main bridge server connecting Xiaozhi to multiple MCP servers."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.clients: Dict[str, Any] = {}
        self.tool_manager = ToolManager()
        self.gemini_service = GeminiService()
        self.google_workspace_handler = None
    
    async def run(self):
        """Run the bridge with automatic reconnection."""
        while True:
            try:
                await self._connect_and_serve()
            except Exception as exc:
                logger.exception("Bridge error: %s", exc)
            finally:
                await self._cleanup()
            
            logger.info("Retrying connection in %.1f seconds...", self.config["retry_delay"])
            await asyncio.sleep(self.config["retry_delay"])
    
    async def _connect_and_serve(self):
        """Connect to all MCP servers and serve requests from Xiaozhi."""
        # Initialize all MCP servers
        await self._initialize_mcp_servers()
        
        if not self.clients:
            raise RuntimeError("No MCP servers successfully connected!")
        
        logger.info("Total tools: %d from %d servers", len(self.tool_manager.all_tools), len(self.clients))
        
        # Connect to Xiaozhi WebSocket and serve requests
        async with websockets.connect(self.config["xiaozhi_wss"]) as ws:
            logger.info("Connected to Xiaozhi WebSocket")
            await self._serve_requests(ws)
    
    async def _initialize_mcp_servers(self):
        """Initialize all configured MCP servers."""
        for server_name, server_config in self.config["servers"].items():
            if server_config is None:
                continue
            
            try:
                client = await self._create_and_initialize_client(server_name, server_config)
                tools = await self._fetch_server_tools(client, server_name)
                
                self.clients[server_name] = client
                self.tool_manager.register_server_tools(server_name, tools)
                
                # Create Google Workspace handler if needed
                if server_name == "google_workspace":
                    self.google_workspace_handler = GoogleWorkspaceHandler(client)
                
            except Exception as exc:
                logger.error("[%s] Failed to initialize: %s", server_name, exc)
                logger.warning("[%s] Skipping this server and continuing with others", server_name)
                continue
    
    async def _create_and_initialize_client(self, server_name: str, server_config: Dict[str, Any]) -> Any:
        """Create and initialize an MCP client."""
        server_type = server_config.get("type", "http")
        
        if server_type == "stdio":
            client = await self._create_stdio_client(server_name, server_config)
        else:
            client = self._create_http_client(server_name, server_config)
        
        # Initialize session
        init_result = await client.call("initialize", {
            "protocolVersion": Config.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": Config.BRIDGE_NAME, "version": Config.BRIDGE_VERSION}
        })
        logger.info("[%s] Initialized: %s", server_name, init_result)
        
        # Send initialized notification (skip for Google Workspace)
        if server_name != "google_workspace":
            try:
                await client.call("notifications/initialized")
                logger.debug("[%s] Sent initialized notification", server_name)
            except Exception as e:
                logger.debug("[%s] Initialized notification not supported: %s", server_name, e)
        
        return client
    
    @staticmethod
    def _create_http_client(server_name: str, server_config: Dict[str, Any]) -> MCPClient:
        """Create HTTP/SSE MCP client."""
        logger.info("Connecting to %s MCP at %s", server_name, server_config["url"])
        return MCPClient(
            name=server_name,
            url=server_config["url"],
            timeout=server_config["timeout"],
            token=server_config.get("token")
        )
    
    @staticmethod
    async def _create_stdio_client(server_name: str, server_config: Dict[str, Any]) -> StdioMCPClient:
        """Create and start stdio subprocess MCP client."""
        logger.info("Starting %s MCP via stdio: %s %s",
                    server_name, server_config["command"], " ".join(server_config["args"]))
        
        # For Google Workspace, pass all Google-related env vars
        subprocess_env = None
        if server_name == "google_workspace":
            subprocess_env = {
                k: v for k, v in os.environ.items()
                if k.startswith(("GOOGLE_", "USER_GOOGLE_", "OAUTHLIB_"))
            }
            logger.info("Passing %d Google env vars to subprocess", len(subprocess_env))
        
        client = StdioMCPClient(
            name=server_name,
            command=server_config["command"],
            args=server_config["args"],
            cwd=server_config.get("cwd"),
            env=subprocess_env
        )
        
        await client.start()
        return client
    
    @staticmethod
    async def _fetch_server_tools(client: Any, server_name: str) -> List[Dict[str, Any]]:
        """Fetch tools list from an MCP server."""
        tools_result = None
        
        # Try multiple formats for tools/list
        for attempt in ["no params", "empty params", "with _meta"]:
            try:
                logger.debug("[%s] Trying tools/list: %s", server_name, attempt)
                
                if attempt == "no params":
                    tools_result = await client.call("tools/list")
                elif attempt == "empty params":
                    tools_result = await client.call("tools/list", {})
                else:  # with _meta
                    if "google" in server_name.lower():
                        tools_result = await client.call("tools/list", {}, include_meta=True)
                
                if tools_result:
                    break
            except RuntimeError as e:
                logger.debug("[%s] Failed with %s: %s", server_name, attempt, str(e)[:100])
        
        if tools_result is None:
            raise RuntimeError(f"[{server_name}] All tools/list attempts failed")
        
        return tools_result.get("tools", [])
    
    async def _serve_requests(self, ws):
        """Serve requests from Xiaozhi WebSocket."""
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
            
            response = await self._handle_request(request)
            
            if response:
                await self._send_response(ws, response, method)
            else:
                logger.debug("◇ No response needed (notification)")
    
    async def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any] | None:
        """Handle a JSON-RPC request from Xiaozhi."""
        method = request.get("method")
        req_id = request.get("id")
        
        try:
            if method == "initialize":
                return self._handle_initialize(req_id)
            elif method == "tools/list":
                return self._handle_tools_list(req_id)
            elif method == "tools/call":
                return await self._handle_tools_call(request)
            elif method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}
            elif method and method.startswith("notifications/"):
                logger.info("Received notification: %s", method)
                return None
            else:
                logger.warning("Unknown method: %s", method)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }
        except Exception as exc:
            logger.exception("Error handling request: %s", exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {str(exc)}"}
            }
    
    @staticmethod
    def _handle_initialize(req_id: Any) -> Dict[str, Any]:
        """Handle initialize request."""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": Config.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": Config.BRIDGE_NAME,
                    "version": Config.BRIDGE_VERSION
                }
            }
        }
    
    def _handle_tools_list(self, req_id: Any) -> Dict[str, Any]:
        """Handle tools/list request."""
        logger.info("Returning %d tools from %d servers", 
                   len(self.tool_manager.all_tools), len(self.clients))
        
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": self.tool_manager.get_tools_list()}
        }
    
    async def _handle_tools_call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request."""
        params = request.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        req_id = request.get("id")
        
        # Find which server owns this tool
        target_server, original_tool_name = self.tool_manager.find_tool_server(tool_name)
        
        if not target_server or target_server not in self.clients:
            logger.error("Tool %s not found in any server", tool_name)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool not found: {tool_name}"}
            }
        
        logger.info("➤ Calling tool: %s on server %s", original_tool_name, target_server)
        
        # Inject user_google_email for Google Workspace tools
        if target_server == "google_workspace":
            arguments = self.tool_manager.inject_google_email(arguments)
        
        # Handle virtual tools
        if original_tool_name == "search_gemini":
            return await self._handle_gemini_search(req_id, arguments)
        elif original_tool_name == "calendar_overview":
            return await self._handle_calendar_overview(req_id, arguments)
        
        # Handle normal tool call
        return await self._handle_normal_tool_call(req_id, target_server, original_tool_name, arguments)
    
    async def _handle_gemini_search(self, req_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Gemini search virtual tool."""
        logger.info("➤ Calling Gemini search (virtual tool)")
        query = arguments.get("q", "")
        search_result = await self.gemini_service.search_news(query)
        
        result = {
            "content": [
                {"type": "text", "text": search_result}
            ]
        }
        
        logger.info("✓ Gemini search completed")
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    
    async def _handle_calendar_overview(self, req_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Handle calendar overview virtual tool."""
        logger.info("➤ Calling Google Workspace calendar overview (virtual tool)")
        
        if not self.google_workspace_handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Google Workspace not initialized"}
            }
        
        try:
            overview = await asyncio.wait_for(
                self.google_workspace_handler.get_calendar_overview(arguments),
                timeout=Config.CALENDAR_TOTAL_TIMEOUT
            )
            logger.info("✓ Calendar overview completed")
            return {"jsonrpc": "2.0", "id": req_id, "result": overview}
        except asyncio.TimeoutError:
            logger.error("✗ calendar_overview sequence timed out")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "calendar_overview sequence timed out"}
            }
        except Exception as exc:
            logger.exception("✗ calendar_overview failed: %s", exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"calendar_overview error: {str(exc)}"}
            }
    
    async def _handle_normal_tool_call(self, req_id: Any, server_name: str, 
                                      tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Handle normal tool call to MCP server."""
        try:
            client = self.clients[server_name]
            result = await asyncio.wait_for(
                client.call("tools/call", {"name": tool_name, "arguments": arguments}),
                timeout=Config.DEFAULT_TOOL_CALL_TIMEOUT
            )
            logger.info("✓ Tool result received from %s", server_name)
            logger.debug("✓ Tool result: %s", json.dumps(result)[:500])
            
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except asyncio.TimeoutError:
            logger.error("✗ Tool call timeout for %s on %s", tool_name, server_name)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Tool call timeout: {tool_name}"}
            }
    
    @staticmethod
    async def _send_response(ws, response: Dict[str, Any], method: str):
        """Send JSON-RPC response to Xiaozhi."""
        try:
            response_str = json.dumps(response)
            logger.info("▶ Sending: %s (id=%s)", method, response.get("id"))
            logger.debug("▶ Full response: %s", response_str[:500])
            await ws.send(response_str)
            logger.info("✓ Sent successfully")
        except websockets.exceptions.ConnectionClosedError:
            logger.warning("✗ WebSocket closed while sending response; will reconnect")
            raise
        except Exception as exc:
            logger.exception("✗ Error sending response: %s", exc)
            raise
    
    async def _cleanup(self):
        """Cleanup all client connections."""
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:
                pass
        
        self.clients.clear()
        self.tool_manager.all_tools.clear()
        self.google_workspace_handler = None

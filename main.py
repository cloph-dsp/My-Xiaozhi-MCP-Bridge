import asyncio
import json
import logging
import os
import subprocess
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

    # Supermemory configuration (hardcoded defaults)
    supermemory_config = {
        "url": "https://api.supermemory.ai/mcp",
        "token": os.getenv("SUPERMEMORY_TOKEN"),
        "timeout": 30.0,
        "type": "http",
    }
    
    # Google Workspace Stdio configuration (optional, recommended)
    google_workspace_stdio_config = None
    google_workspace_enabled = os.getenv("GOOGLE_WORKSPACE_STDIO_ENABLED", "false").lower()
    google_workspace_cwd = os.getenv("GOOGLE_WORKSPACE_STDIO_CWD")
    google_workspace_cmd = os.getenv("GOOGLE_WORKSPACE_STDIO_COMMAND", "uv")
    google_workspace_args = os.getenv(
        "GOOGLE_WORKSPACE_STDIO_ARGS",
        "run,main.py,--transport,stdio"
    ).split(",")
    google_workspace_args = [arg.strip() for arg in google_workspace_args if arg.strip()]

    user_google_email = os.getenv("USER_GOOGLE_EMAIL", "").strip()
    valid_email = user_google_email and "@" in user_google_email and len(user_google_email) > 5
    
    logger.debug(
        "Google Workspace config check: ENABLED='%s', CWD='%s', CMD='%s', ARGS=%s, USER_GOOGLE_EMAIL='%s'",
        google_workspace_enabled,
        google_workspace_cwd,
        google_workspace_cmd,
        google_workspace_args,
        (user_google_email[-4:] if valid_email else "(missing/invalid)"),
    )
    
    if google_workspace_enabled == "true":
        google_workspace_stdio_config = {
            "command": google_workspace_cmd,
            "args": google_workspace_args,
            "cwd": google_workspace_cwd,
            "type": "stdio",
        }
        logger.info(
            "Google Workspace stdio enabled with CWD: %s (command=%s args=%s)",
            google_workspace_cwd,
            google_workspace_cmd,
            google_workspace_args,
        )

    # Validate required fields
    missing = []
    if not xiaozhi_wss:
        missing.append("XIAOZHI_WSS_URL")
    if not supermemory_config["token"]:
        missing.append("SUPERMEMORY_TOKEN")
    if google_workspace_stdio_config:
        if not google_workspace_stdio_config["cwd"]:
            missing.append("GOOGLE_WORKSPACE_STDIO_CWD")
        if not valid_email:
            missing.append("USER_GOOGLE_EMAIL (must look like an email)")
    
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    servers = {"supermemory": supermemory_config}
    
    if google_workspace_stdio_config:
        servers["google_workspace"] = google_workspace_stdio_config

    return {
        "xiaozhi_wss": xiaozhi_wss,
        "retry_delay": 5.0,  # Hardcoded retry delay
        "servers": servers
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

    async def call(self, method: str, params: Dict[str, Any] | None = None, include_meta: bool = False) -> Any:
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
        
        # Some servers (like Google Workspace MCP) may require _meta field
        if include_meta:
            payload["_meta"] = {
                "clientId": None,
                "serialNumber": None
            }
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        
        # Add authentication if token is provided
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        
        # Add session ID for all requests (some servers require it even for initialize)
        # If not set yet, try to use one from previous requests
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


class StdioMCPClient:
    """JSON-RPC client for MCP servers running as stdio subprocesses."""

    def __init__(self, name: str, command: str, args: List[str], cwd: str | None = None, env: Dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self.process = None
        self.request_id = 0
        self.pending_responses = {}

    async def start(self):
        """Start the subprocess."""
        # Merge parent env with any custom env vars
        proc_env = os.environ.copy()
        if self.env:
            proc_env.update(self.env)
        
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=proc_env
        )
        logger.info("[%s] Started stdio subprocess: %s %s", self.name, self.command, " ".join(self.args))
        
        # Start background task to read responses
        asyncio.create_task(self._read_responses())
        # Start background task to log stderr
        asyncio.create_task(self._read_stderr())

    async def _read_responses(self):
        """Background task to read JSON-RPC responses from stdout (chunked, no line limit)."""
        buffer = b""
        try:
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        response = json.loads(line.decode())
                        req_id = response.get("id")
                        if req_id and req_id in self.pending_responses:
                            future = self.pending_responses.pop(req_id)
                            if "error" in response:
                                future.set_exception(RuntimeError(f"JSON-RPC error: {response['error']}"))
                            else:
                                future.set_result(response.get("result"))
                        else:
                            logger.debug("[%s] Received response without pending request: %s", self.name, response)
                    except json.JSONDecodeError:
                        logger.warning("[%s] Invalid JSON from stdout: %s", self.name, line[:200])
        except Exception as e:
            logger.error("[%s] Error reading responses: %s", self.name, e)

    async def _read_stderr(self):
        """Background task to read stderr for diagnostics."""
        try:
            async for line in self.process.stderr:
                logger.error("[%s][stderr] %s", self.name, line.decode(errors="ignore").rstrip())
        except Exception as e:
            logger.error("[%s] Error reading stderr: %s", self.name, e)

    async def call(self, method: str, params: Dict[str, Any] | None = None) -> Any:
        """Send JSON-RPC request via stdin and wait for response from stdout."""
        self.request_id += 1
        
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
        }
        
        if params is not None:
            payload["params"] = params
        
        logger.debug("[%s] Calling %s with params: %s", self.name, method, params)
        
        # Create future for response
        future = asyncio.Future()
        self.pending_responses[self.request_id] = future
        
        # Send request to stdin
        request_line = json.dumps(payload) + "\n"
        self.process.stdin.write(request_line.encode())
        await self.process.stdin.drain()
        
        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=60.0)
            return result
        except asyncio.TimeoutError:
            self.pending_responses.pop(self.request_id, None)
            raise RuntimeError(f"[{self.name}] Timeout waiting for {method} response")

    async def close(self):
        """Terminate the subprocess."""
        if self.process:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


async def _summarize_with_gemini(text: str, gemini_api_key: str | None = None) -> str:
    """Summarize news articles using Gemini 2.5 Flash Lite."""
    if not gemini_api_key:
        gemini_api_key = os.getenv("GEMINI_API_KEY")
    
    if not gemini_api_key:
        logger.warning("GEMINI_API_KEY not set, skipping summarization")
        return text
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
                json={
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": f"""Summarize this news content in Portuguese (pt-BR) in 2-3 sentences maximum.
                                    
Content:
{text}

Provide ONLY the summary, nothing else."""
                                }
                            ]
                        }
                    ],
                    "generationConfig": {
                        "maxOutputTokens": 150,
                        "temperature": 0.3
                    }
                },
                params={"key": gemini_api_key}
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("candidates"):
                    summary = result["candidates"][0]["content"]["parts"][0]["text"]
                    logger.info(f"Summarized news with Gemini (from {len(text)} to {len(summary)} chars)")
                    return summary.strip()
            else:
                logger.error(f"Gemini API error: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
    
    return text


async def _search_with_gemini(query: str, gemini_api_key: str | None = None) -> str:
    """Search and summarize news using Gemini 2.5 Flash with web search enabled."""
    if not gemini_api_key:
        gemini_api_key = os.getenv("GEMINI_API_KEY")
    
    if not gemini_api_key:
        logger.warning("GEMINI_API_KEY not set, cannot search")
        return f"Error: GEMINI_API_KEY not configured"
    
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                json={
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": f"""Search for recent news about: {query}

Provide a concise summary in Portuguese (pt-BR) with:
1. 2-3 main points about the topic
2. Key sources/outlets mentioned
3. Keep it to 2-3 sentences maximum

Be direct and factual."""
                                }
                            ]
                        }
                    ],
                    "generationConfig": {
                        "maxOutputTokens": 200,
                        "temperature": 0.3
                    },
                    "tools": [
                        {
                            "googleSearch": {}
                        }
                    ]
                },
                params={"key": gemini_api_key}
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("candidates"):
                    answer = result["candidates"][0]["content"]["parts"][0]["text"]
                    logger.info(f"Gemini search completed for query: {query}")
                    return answer.strip()
            else:
                logger.error(f"Gemini API error: {response.status_code} - {response.text}")
                return f"Error: API returned {response.status_code}"
    except Exception as e:
        logger.error(f"Error calling Gemini search: {e}")
        return f"Error: {str(e)}"


async def _optimize_search_result(result: Any, gemini_api_key: str | None = None) -> Any:
    """Convert search result to use Gemini search instead."""
    # For now, just pass through - the search will be handled by Gemini directly
    return result


async def bridge() -> None:
    cfg = load_config()
    
    # Initialize all enabled MCP clients
    clients = {}
    all_tools = []
    
    while True:
        try:
                        
                    if response.status_code == 200:
                        result = response.json()
                        logger.debug(f"Gemini response: {json.dumps(result)[:1000]}")
                        if result.get("candidates") and len(result["candidates"]) > 0:
                            candidate = result["candidates"][0]
                            logger.debug(f"First candidate: {json.dumps(candidate)[:500]}")
                            if "content" in candidate and "parts" in candidate["content"]:
                                answer = candidate["content"]["parts"][0].get("text", "")
                                logger.info(f"Gemini search completed for query: {query}")
                                return answer.strip() if answer else "No answer returned"
                            else:
                                logger.error(f"Missing content/parts in candidate: {list(candidate.keys())}")
                                return f"Error: Unexpected response format"
                        else:
                            logger.error(f"No candidates in response: {list(result.keys())}")
                            return f"Error: No candidates in response"
                    else:
                        logger.error(f"Gemini API error: {response.status_code} - {response.text}")
                        return f"Error: API returned {response.status_code}"
            except Exception as e:
                logger.error(f"Error calling Gemini search: {e}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return f"Error: {str(e)}"
                        # For Google Workspace, pass all Google-related env vars to subprocess
                        subprocess_env = None
                        if server_name == "google_workspace":
                            subprocess_env = {
                                k: v for k, v in os.environ.items()
                                if k.startswith(("GOOGLE_", "USER_GOOGLE_", "OAUTHLIB_"))
                            }
                            logger.info("Passing %d Google env vars to subprocess: %s", len(subprocess_env), list(subprocess_env.keys()))
                            if "USER_GOOGLE_EMAIL" in subprocess_env:
                                logger.info("USER_GOOGLE_EMAIL is set in subprocess env: %s", subprocess_env["USER_GOOGLE_EMAIL"])
                            else:
                                logger.error("USER_GOOGLE_EMAIL NOT found in subprocess env!")
                        
                        client = StdioMCPClient(
                            name=server_name,
                            command=server_config["command"],
                            args=server_config["args"],
                            cwd=server_config.get("cwd"),
                            env=subprocess_env
                        )
                        
                        await client.start()
                        
                    else:
                        # HTTP/SSE client
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
                    
                    # Send initialized notification (skip for Google Workspace, which rejects it)
                    if server_name != "google_workspace":
                        try:
                            await client.call("notifications/initialized")
                            logger.debug("[%s] Sent initialized notification", server_name)
                        except Exception as e:
                            logger.debug("[%s] Initialized notification not supported: %s", server_name, e)
                    
                    # List tools - try multiple formats
                    tools_result = None
                    
                    # First try: no params
                    try:
                        logger.debug("[%s] Trying tools/list without params", server_name)
                        tools_result = await client.call("tools/list")
                    except RuntimeError as e:
                        logger.debug("[%s] Failed without params: %s", server_name, str(e)[:100])
                    
                    # Second try: empty params
                    if tools_result is None:
                        try:
                            logger.debug("[%s] Trying tools/list with empty params", server_name)
                            tools_result = await client.call("tools/list", {})
                        except RuntimeError as e:
                            logger.debug("[%s] Failed with empty params: %s", server_name, str(e)[:100])
                    
                    # Third try: with _meta field (for FastMCP)
                    if tools_result is None and "google" in server_name.lower():
                        try:
                            logger.debug("[%s] Trying tools/list with _meta field", server_name)
                            tools_result = await client.call("tools/list", {}, include_meta=True)
                        except RuntimeError as e:
                            logger.debug("[%s] Failed with _meta: %s", server_name, str(e)[:100])
                    
                    if tools_result is None:
                        raise RuntimeError(f"[{server_name}] All tools/list attempts failed")
                    
                    server_tools = tools_result.get("tools", [])
                    
                    # Filter tools based on server
                    if server_name == "google_workspace":
                        # Keep calendar, tasks, and search tools
                        allowed = {
                            "get_events",
                            # Tasks
                            "list_task_lists",
                            "list_tasks",
                            "get_task",
                            "create_task",
                            "update_task"
                            # Removed search tools - using Gemini search instead
                        }
                        server_tools = [t for t in server_tools if t.get("name") in allowed]
                    elif server_name == "supermemory":
                        # Only keep core Supermemory tools
                        allowed = {"search", "addMemory"}
                        server_tools = [t for t in server_tools if t.get("name") in allowed]
                    
                    # Add server prefix to tool names to avoid conflicts
                    for tool in server_tools:
                        tool["_server"] = server_name  # Track which server owns this tool
                        tool["name"] = f"{server_name}_{tool['name']}"  # Prefix tool name
                    
                    logger.info("[%s] Found %d tools", server_name, len(server_tools))
                    
                    # Add virtual Gemini search tool for google_workspace
                    if server_name == "google_workspace":
                        gemini_search_tool = {
                            "name": "google_workspace_search_gemini",
                            "_server": "google_workspace",
                            "description": "Search news with Gemini AI web search - returns concise summaries",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "q": {
                                        "type": "string",
                                        "description": "Search query"
                                    }
                                },
                                "required": ["q"]
                            }
                        }
                        server_tools.append(gemini_search_tool)
                    
                    clients[server_name] = client
                    all_tools.extend(server_tools)
                    
                except Exception as exc:
                    logger.error("[%s] Failed to initialize: %s", server_name, exc)
                    logger.warning("[%s] Skipping this server and continuing with others", server_name)
                    # Continue with other servers even if this one fails
                    continue
            
            if not clients:
                logger.error("No MCP servers successfully connected!")
                raise RuntimeError("All MCP servers failed to initialize")
            
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
                                logger.debug("➤ Tool arguments (before injection): %s", json.dumps(arguments)[:200])
                                
                                # ALWAYS inject user_google_email for Google Workspace tools
                                if target_server == "google_workspace":
                                    env_email = os.getenv("USER_GOOGLE_EMAIL", "").strip()
                                    valid_email = env_email and "@" in env_email and len(env_email) > 5
                                    if valid_email:
                                        arguments["user_google_email"] = env_email
                                        logger.info("➤ FORCE-INJECTED user_google_email: %s", env_email)
                                    else:
                                        logger.error("USER_GOOGLE_EMAIL env is not set/invalid: '%s'", env_email)
                                        raise RuntimeError("USER_GOOGLE_EMAIL not valid; aborting Google Workspace tool call")
                                
                                logger.info("➤ Final tool arguments (after injection): %s", json.dumps(arguments)[:300])
                                
                                # Handle virtual Gemini search tool
                                if original_tool_name == "search_gemini":
                                    logger.info("➤ Calling Gemini search (virtual tool)")
                                    query = arguments.get("q", "")
                                    gemini_key = os.getenv("GEMINI_API_KEY")
                                    search_result = await _search_with_gemini(query, gemini_key)
                                    result = {
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": search_result
                                            }
                                        ]
                                    }
                                    logger.info("✓ Gemini search completed")
                                    response = {
                                        "jsonrpc": "2.0",
                                        "id": request.get("id"),
                                        "result": result
                                    }
                                else:
                                    # Normal tool call to MCP server
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
            
            # List tools (no params needed for this method)
            tools_result = await client.call("tools/list")
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




"""MCP client implementations for HTTP/SSE and Stdio transports."""
import asyncio
import json
import logging
import os
from typing import Any, Dict, List

import httpx

logger = logging.getLogger("bridge")


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
        
        if params is not None:
            payload["params"] = params
        
        if include_meta:
            payload["_meta"] = {
                "clientId": None,
                "serialNumber": None
            }
        
        headers = self._build_headers()
        
        logger.debug("[%s] Calling %s with params: %s", self.name, method, params)
        
        async with self.client.stream("POST", self.url, json=payload, headers=headers) as response:
            return await self._handle_response(response)

    def _build_headers(self) -> Dict[str, str]:
        """Build HTTP headers for the request."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        
        return headers

    async def _handle_response(self, response: httpx.Response) -> Any:
        """Handle HTTP response, supporting both JSON and SSE formats."""
        logger.info("[%s] Response status: %s", self.name, response.status_code)
        
        # Capture session ID from response headers
        if "mcp-session-id" in response.headers:
            self.session_id = response.headers["mcp-session-id"]
            logger.info("[%s] Captured session ID: %s", self.name, self.session_id[:16] + "...")
        
        response.raise_for_status()
        
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return await self._parse_sse_response(response)
        else:
            return await self._parse_json_response(response)

    async def _parse_sse_response(self, response: httpx.Response) -> Any:
        """Parse Server-Sent Events (SSE) response."""
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                
                if line.startswith("data: "):
                    data_json = line[6:]
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

    async def _parse_json_response(self, response: httpx.Response) -> Any:
        """Parse plain JSON response."""
        text = await response.aread()
        data = json.loads(text)
        
        if "error" in data:
            raise RuntimeError(f"[{self.name}] JSON-RPC error: {data['error']}")
        
        return data.get("result")

    async def close(self):
        """Close the HTTP client."""
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
        
        asyncio.create_task(self._read_responses())
        asyncio.create_task(self._read_stderr())

    async def _read_responses(self):
        """Background task to read JSON-RPC responses from stdout."""
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
        
        future = asyncio.Future()
        self.pending_responses[self.request_id] = future
        
        request_line = json.dumps(payload) + "\n"
        self.process.stdin.write(request_line.encode())
        await self.process.stdin.drain()
        
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

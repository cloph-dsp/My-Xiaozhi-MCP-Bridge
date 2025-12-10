"""Tool management for MCP bridge."""
import logging
import os
from typing import Any, Dict, List

from config import Config

logger = logging.getLogger("bridge")


class ToolManager:
    """Manages tools from multiple MCP servers."""
    
    def __init__(self):
        self.all_tools: List[Dict[str, Any]] = []
    
    def register_server_tools(self, server_name: str, server_tools: List[Dict[str, Any]]):
        """Register tools from a server with filtering and prefixing."""
        # Filter tools based on server type
        filtered_tools = self._filter_tools(server_name, server_tools)
        
        # Add server prefix to tool names
        for tool in filtered_tools:
            tool["_server"] = server_name
            tool["name"] = f"{server_name}_{tool['name']}"
        
        # Add virtual tools for specific servers
        if server_name == "google_workspace":
            filtered_tools.extend(self._create_google_workspace_virtual_tools())
        
        self.all_tools.extend(filtered_tools)
        logger.info("[%s] Registered %d tools", server_name, len(filtered_tools))
    
    def add_gemini_search_tool(self):
        """Add Gemini search as a standalone virtual tool."""
        gemini_tool = {
            "name": "gemini_search",
            "_server": "gemini",
            "description": "Search the web with Gemini AI - get accurate, AI-summarized answers to any question in Portuguese",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "Search query or question"
                    }
                },
                "required": ["q"]
            }
        }
        self.all_tools.append(gemini_tool)
        logger.info("[gemini] Registered 1 virtual tool")
    
    @staticmethod
    def _filter_tools(server_name: str, server_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter tools based on server-specific allowed lists."""
        if server_name == "google_workspace":
            allowed = Config.GOOGLE_WORKSPACE_ALLOWED_TOOLS
            return [t for t in server_tools if t.get("name") in allowed]
        elif server_name == "supermemory":
            allowed = Config.SUPERMEMORY_ALLOWED_TOOLS
            return [t for t in server_tools if t.get("name") in allowed]
        
        return server_tools
    
    @staticmethod
    def _create_google_workspace_virtual_tools() -> List[Dict[str, Any]]:
        """Create virtual tools for Google Workspace."""
        return [
            {
                "name": "google_workspace_calendar_overview",
                "_server": "google_workspace",
                "description": "Fetch calendar events + tasks overview (events, task lists and tasks per list)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "time_min": {"type": "string", "description": "ISO timeMin for events (optional)"},
                        "time_max": {"type": "string", "description": "ISO timeMax for events (optional)"},
                        "user_google_email": {"type": "string", "description": "User Google email (required)"}
                    },
                    "required": ["user_google_email"]
                }
            }
        ]
    
    def find_tool_server(self, tool_name: str) -> tuple[str | None, str | None]:
        """Find which server owns a tool and return the original tool name."""
        for tool in self.all_tools:
            if tool["name"] == tool_name:
                server_name = tool["_server"]
                original_name = tool_name.replace(f"{server_name}_", "", 1)
                return server_name, original_name
        
        return None, None
    
    def get_tools_list(self) -> List[Dict[str, Any]]:
        """Get list of all tools for MCP tools/list response."""
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {})
            }
            for tool in self.all_tools
        ]
    
    @staticmethod
    def inject_google_email(arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Inject USER_GOOGLE_EMAIL into arguments if needed."""
        env_email = os.getenv("USER_GOOGLE_EMAIL", "").strip()
        valid_email = env_email and "@" in env_email and len(env_email) > 5
        
        if valid_email:
            arguments["user_google_email"] = env_email
            logger.info("FORCE-INJECTED user_google_email: %s", env_email)
        else:
            logger.error("USER_GOOGLE_EMAIL env is not set/invalid: '%s'", env_email)
            raise RuntimeError("USER_GOOGLE_EMAIL not valid; aborting Google Workspace tool call")
        
        return arguments

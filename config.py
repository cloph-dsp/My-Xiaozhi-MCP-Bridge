"""Configuration management for Xiaozhi MCP Bridge."""
import os
from typing import Any, Dict
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration."""
    
    # Protocol and versioning
    PROTOCOL_VERSION = "2024-11-05"
    BRIDGE_NAME = "multi-mcp-bridge"
    BRIDGE_VERSION = "1.0.0"
    
    # Timeouts (in seconds)
    DEFAULT_HTTP_TIMEOUT = 30.0
    DEFAULT_TOOL_CALL_TIMEOUT = 60.0
    DEFAULT_RETRY_DELAY = 5.0
    
    # Calendar tool specific timeouts
    CALENDAR_TOTAL_TIMEOUT = int(os.getenv("CALENDAR_TOOL_TOTAL_TIMEOUT", "90"))
    CALENDAR_PER_CALL_TIMEOUT = int(os.getenv("CALENDAR_TOOL_PER_CALL_TIMEOUT", "20"))
    CALENDAR_LIST_TASKS_TIMEOUT = int(os.getenv("CALENDAR_TOOL_LIST_TASKS_TIMEOUT", "10"))
    
    # Gemini configuration
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    GEMINI_FLASH_MODEL = "gemini-2.5-flash"
    GEMINI_FLASH_LITE_MODEL = "gemini-2.5-flash-lite"
    GEMINI_MAX_SUMMARY_TOKENS = 150
    GEMINI_MAX_SEARCH_TOKENS = 800
    GEMINI_TEMPERATURE = 0.3
    
    # Tool filters
    # Note: get_events, list_task_lists, list_tasks are replaced by calendar_overview virtual tool
    GOOGLE_WORKSPACE_ALLOWED_TOOLS = {
        "get_task",
        "create_task",
        "update_task"
    }
    
    SUPERMEMORY_ALLOWED_TOOLS = {"search"}  # Only search - addMemory has complex requirements
    
    @classmethod
    def load_server_config(cls) -> Dict[str, Any]:
        """Load server configuration from environment variables."""
        xiaozhi_wss = os.getenv("XIAOZHI_WSS_URL")
        
        # Supermemory configuration
        supermemory_config = {
            "url": "https://api.supermemory.ai/mcp",
            "token": os.getenv("SUPERMEMORY_TOKEN"),
            "timeout": cls.DEFAULT_HTTP_TIMEOUT,
            "type": "http",
        }
        
        # Google Workspace Stdio configuration
        google_workspace_stdio_config = None
        google_workspace_enabled = os.getenv("GOOGLE_WORKSPACE_STDIO_ENABLED", "false").lower()
        
        if google_workspace_enabled == "true":
            google_workspace_cwd = os.getenv("GOOGLE_WORKSPACE_STDIO_CWD")
            google_workspace_cmd = os.getenv("GOOGLE_WORKSPACE_STDIO_COMMAND", "uv")
            google_workspace_args = os.getenv(
                "GOOGLE_WORKSPACE_STDIO_ARGS",
                "run,main.py,--transport,stdio"
            ).split(",")
            google_workspace_args = [arg.strip() for arg in google_workspace_args if arg.strip()]
            
            google_workspace_stdio_config = {
                "command": google_workspace_cmd,
                "args": google_workspace_args,
                "cwd": google_workspace_cwd,
                "type": "stdio",
            }
        
        # Validate required fields
        cls._validate_config(xiaozhi_wss, supermemory_config, google_workspace_stdio_config)
        
        servers = {"supermemory": supermemory_config}
        if google_workspace_stdio_config:
            servers["google_workspace"] = google_workspace_stdio_config
        
        return {
            "xiaozhi_wss": xiaozhi_wss,
            "retry_delay": cls.DEFAULT_RETRY_DELAY,
            "servers": servers
        }
    
    @staticmethod
    def _validate_config(xiaozhi_wss: str, supermemory_config: Dict, google_workspace_config: Dict | None):
        """Validate required configuration fields."""
        missing = []
        
        if not xiaozhi_wss:
            missing.append("XIAOZHI_WSS_URL")
        if not supermemory_config["token"]:
            missing.append("SUPERMEMORY_TOKEN")
        
        if google_workspace_config:
            if not google_workspace_config["cwd"]:
                missing.append("GOOGLE_WORKSPACE_STDIO_CWD")
            
            user_google_email = os.getenv("USER_GOOGLE_EMAIL", "").strip()
            valid_email = user_google_email and "@" in user_google_email and len(user_google_email) > 5
            if not valid_email:
                missing.append("USER_GOOGLE_EMAIL (must look like an email)")
        
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    
    @staticmethod
    def get_user_google_email() -> str:
        """Get and validate user Google email from environment."""
        email = os.getenv("USER_GOOGLE_EMAIL", "").strip()
        if not email or "@" not in email or len(email) <= 5:
            raise RuntimeError("USER_GOOGLE_EMAIL not valid")
        return email

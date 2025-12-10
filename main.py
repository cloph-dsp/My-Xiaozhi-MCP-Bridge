"""Xiaozhi MCP Bridge - Connects Xiaozhi to multiple MCP servers."""
import asyncio
import json
import logging
import os

from config import Config
from bridge import MCPBridge
from mcp_clients import MCPClient
from tool_manager import ToolManager

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level), format="[%(levelname)s] %(message)s")
logger = logging.getLogger("bridge")


def list_tools_only():
    """List all tools that will be registered by the bridge."""
    print("\nRegistered Tools:")
    print("=" * 50)
    
    # Supermemory tools
    print("\nSupermemory (1 tool):")
    print("  • supermemory_search")
    
    # Google Workspace tools
    print("\nGoogle Workspace (4 tools):")
    print("  • google_workspace_get_task")
    print("  • google_workspace_create_task")
    print("  • google_workspace_update_task")
    print("  • google_workspace_calendar_overview  [virtual - replaces get_events, list_task_lists, list_tasks]")
    
    # Gemini virtual tool
    print("\nGemini AI (1 tool):")
    print("  • gemini_search  [virtual - web search for any information]")
    
    print("\n" + "=" * 50)
    print("Total: 6 tools\n")


async def main():
    """Main entry point for the bridge."""
    config = Config.load_server_config()
    bridge = MCPBridge(config)
    await bridge.run()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--list-tools":
        list_tools_only()
    else:
        asyncio.run(main())




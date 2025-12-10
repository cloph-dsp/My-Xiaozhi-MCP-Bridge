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


async def list_tools_only():
    """List all available tools from all configured MCP servers."""
    cfg = Config.load_server_config()
    
    print("\n" + "="*60)
    print("Available Tools from All MCP Servers")
    print("="*60 + "\n")
    
    total_tools = 0
    tool_manager = ToolManager()
    
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
                "protocolVersion": Config.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "xiaozhi-bridge-cli", "version": "1.0"}
            })
            
            # List tools
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


async def main():
    """Main entry point for the bridge."""
    config = Config.load_server_config()
    bridge = MCPBridge(config)
    await bridge.run()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--list-tools":
        asyncio.run(list_tools_only())
    else:
        asyncio.run(main())




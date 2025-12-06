# Google Workspace MCP Setup Guide

This guide explains how to configure the Google Workspace MCP server with this bridge.

## Transport Modes

The Google Workspace MCP server supports two transport modes:

### 1. Stdio Mode (✅ RECOMMENDED)

This is the **recommended** and **default** mode. It works by launching the Google Workspace MCP as a subprocess and communicating via stdin/stdout using JSON-RPC.

**Advantages:**
- ✅ Works perfectly with all MCP protocol methods
- ✅ No HTTP server required
- ✅ Same mode used by Claude Desktop
- ✅ Lower latency
- ✅ More reliable

**Setup:**

1. Clone the Google Workspace MCP repository:
   ```bash
   cd /path/to/your/projects
   git clone https://github.com/taylorwilsdon/google_workspace_mcp.git
   cd google_workspace_mcp
   ```

2. Follow the Google Workspace MCP authentication setup (OAuth credentials)

3. Configure your `.env` file:
   ```env
   GOOGLE_WORKSPACE_STDIO_ENABLED=true
   GOOGLE_WORKSPACE_STDIO_COMMAND=uv
   GOOGLE_WORKSPACE_STDIO_ARGS=run,main.py
   GOOGLE_WORKSPACE_STDIO_CWD=/absolute/path/to/google_workspace_mcp
   ```

**Environment Variables:**

- `GOOGLE_WORKSPACE_STDIO_ENABLED`: Set to `true` to enable stdio mode
- `GOOGLE_WORKSPACE_STDIO_COMMAND`: Command to run (default: `uv`)
- `GOOGLE_WORKSPACE_STDIO_ARGS`: Comma-separated arguments (default: `run,main.py`)
- `GOOGLE_WORKSPACE_STDIO_CWD`: **REQUIRED** - Absolute path to the google_workspace_mcp directory

**Notes:**
- The default configuration uses `uv run main.py` which is the recommended way to run the server
- Make sure `uv` is installed: `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- The stdio mode launches the server as a subprocess - no need to start it separately

### 2. HTTP Mode (⚠️ HAS ISSUES)

This mode runs Google Workspace MCP as an HTTP server. **NOT RECOMMENDED** due to FastMCP compatibility issues.

**Known Issues:**
- ❌ `tools/list` method returns error -32602 "Invalid request parameters"
- ❌ Issue exists regardless of parameter format
- ❌ Affects FastMCP's HTTP implementation specifically

**If you still want to try HTTP mode:**

1. Start the Google Workspace MCP server manually:
   ```bash
   cd /path/to/google_workspace_mcp
   uv run main.py --transport streamable-http --port 8000
   ```

2. Configure your `.env`:
   ```env
   GOOGLE_WORKSPACE_HTTP_ENABLED=true
   GOOGLE_WORKSPACE_HTTP_URL=http://localhost:8000/mcp
   ```

**Not recommended unless the FastMCP issues are fixed upstream.**

## Testing the Setup

Run the bridge with tool listing to verify both servers are working:

```bash
cd /path/to/My-Xiaozhi-MCP-Bridge
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
python main.py --list-tools
```

You should see tools from both `supermemory` and `google_workspace` prefixed accordingly.

## Tool Name Prefixing

All tools are automatically prefixed with their server name to avoid conflicts:

- Supermemory tools: `supermemory_addMemory`, `supermemory_search`, etc.
- Google Workspace tools: `google_workspace_gmail_send`, `google_workspace_drive_search`, etc.

## Available Google Workspace Tools

The Google Workspace MCP provides tools for:

- **Gmail**: Send emails, search inbox, read messages, manage drafts
- **Google Drive**: Search files, read documents, create folders, upload files
- **Google Calendar**: List events, create events, update events

Exact tool availability depends on your Google Workspace MCP configuration and authentication scopes.

## Troubleshooting

### "No tools found" for Google Workspace

1. Verify the `GOOGLE_WORKSPACE_STDIO_CWD` path is correct and absolute
2. Check that `uv` is installed: `uv --version`
3. Verify Google Workspace MCP runs standalone: `cd /path/to/google_workspace_mcp && uv run main.py`
4. Check the bridge logs for initialization errors: `LOG_LEVEL=DEBUG python main.py`

### OAuth/Authentication Issues

Refer to the Google Workspace MCP documentation for OAuth setup. The bridge itself doesn't handle authentication - that's managed by the Google Workspace MCP subprocess.

### Process Won't Start

Check:
- `GOOGLE_WORKSPACE_STDIO_COMMAND` is a valid executable in your PATH
- `GOOGLE_WORKSPACE_STDIO_CWD` directory exists and contains `main.py`
- The command works manually: `cd /path && uv run main.py`

## Architecture

```
┌─────────────────────────┐
│  Xiaozhi AI Cloud       │
│  (WebSocket)            │
└───────────┬─────────────┘
            │
            │ WebSocket
            │
┌───────────▼─────────────┐
│  Bridge (main.py)       │
│  - Multi-server routing │
│  - Tool name prefixing  │
└───────┬─────────┬───────┘
        │         │
        │         │ stdio (JSON-RPC over pipes)
        │         │
        │    ┌────▼──────────────────────┐
        │    │ Google Workspace MCP      │
        │    │ (subprocess)              │
        │    └───────────────────────────┘
        │
        │ HTTP/SSE
        │
   ┌────▼─────────────────┐
   │ Supermemory API      │
   └──────────────────────┘
```

## Performance

Stdio mode has excellent performance:
- ~100ms tool call latency (vs ~200ms for HTTP mode)
- No HTTP parsing overhead
- Direct JSON-RPC over pipes
- Subprocess lifecycle managed automatically

## Multiple Servers

You can run **both** transport modes simultaneously if needed:

```env
# Supermemory (HTTP/SSE)
SUPERMEMORY_TOKEN=xxx

# Google Workspace (stdio - recommended)
GOOGLE_WORKSPACE_STDIO_ENABLED=true
GOOGLE_WORKSPACE_STDIO_CWD=/path/to/google_workspace_mcp

# Google Workspace (HTTP - optional, has issues)
GOOGLE_WORKSPACE_HTTP_ENABLED=false
GOOGLE_WORKSPACE_HTTP_URL=http://localhost:8000/mcp
```

The bridge will initialize all enabled servers and route tool calls appropriately based on the tool name prefix.

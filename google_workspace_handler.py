"""Google Workspace handler for calendar and tasks operations."""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict

from config import Config

logger = logging.getLogger("bridge")


def _parse_task_lists_from_text(text: str):
    """Parse task list IDs from text content using regex.
    
    Expects format: "- Name (ID: SOME_ID)"
    Returns: list of dicts with 'id' field, or None if no IDs found
    """
    pattern = r'\(ID:\s*([^\)]+)\)'
    matches = re.findall(pattern, text)
    if matches:
        logger.info(f"Extracted {len(matches)} task list IDs from text: {matches}")
        return [{"id": task_id.strip()} for task_id in matches]
    logger.warning("No task list IDs found in text content")
    return None


def _extract_mcp_content(response: Any) -> Any:
    """Extract meaningful content from MCP response.
    
    MCP responses have structure: {"content": [...], "structuredContent": {...}, "isError": bool}
    Extract the text content or structuredContent.result
    """
    if not isinstance(response, dict):
        return response
    
    # Check if this is an MCP response structure
    if "content" in response and isinstance(response["content"], list):
        # Extract text from content array
        for item in response["content"]:
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                return item["text"]
    
    # Check structuredContent for result
    if "structuredContent" in response and isinstance(response["structuredContent"], dict):
        if "result" in response["structuredContent"]:
            return response["structuredContent"]["result"]
    
    # If it has isError field, it's likely an MCP response but we couldn't extract content
    if "isError" in response:
        logger.warning("Could not extract content from MCP response: %s", list(response.keys()))
        return None
    
    # Not an MCP response, return as-is
    return response


class GoogleWorkspaceHandler:
    """Handler for Google Workspace calendar and tasks operations."""
    
    def __init__(self, client: Any):
        self.client = client
    
    async def get_calendar_overview(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Run the calendar/tasks sequence and return aggregated results.
        
        Sequence:
          1) get_events (with time_min/time_max/user_google_email)
          2) list_task_lists (with user_google_email)
          3) For each list, call list_tasks and aggregate results
        
        Returns only events and aggregated tasks (no raw list structures).
        Filters out empty fields to save tokens.
        """
        # 1) Get events
        get_events_args = self._build_events_args(arguments)
        logger.info("Calling get_events with args: %s", get_events_args)
        events_raw = await self._call_tool_with_timeout(
            "get_events", 
            get_events_args, 
            Config.CALENDAR_PER_CALL_TIMEOUT
        )
        events = _extract_mcp_content(events_raw)
        logger.info("get_events returned: %s", str(events)[:200] if events else "empty")
        
        # 2) List task lists
        task_lists_args = {"user_google_email": arguments.get("user_google_email")}
        task_lists_res = await self._call_tool_with_timeout(
            "list_task_lists",
            task_lists_args,
            Config.CALENDAR_PER_CALL_TIMEOUT
        )
        
        # 3) List tasks per list
        tasks_per_list = await self._get_tasks_per_list(task_lists_res, arguments)
        
        # Build response, only including non-empty fields
        response: Dict[str, Any] = {}
        
        if events:
            response["events"] = events
        
        if tasks_per_list:
            # Extract content from each task response
            tasks_cleaned = {}
            for list_id, task_response in tasks_per_list.items():
                task_content = _extract_mcp_content(task_response)
                if task_content:
                    tasks_cleaned[list_id] = task_content
            
            if tasks_cleaned:
                response["tasks"] = tasks_cleaned
        
        # If nothing was found, provide a friendly message
        if not response:
            logger.info("No events or tasks found for the given criteria")
            response["message"] = "No events or tasks found for the specified time range."
        
        return response
    
    @staticmethod
    def _build_events_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Build arguments for get_events call."""
        args: Dict[str, Any] = {}
        
        if arguments.get("user_google_email"):
            args["user_google_email"] = arguments.get("user_google_email")
        
        # Support both snake_case and camelCase input, but always output snake_case
        time_min = arguments.get("time_min") or arguments.get("timeMin")
        time_max = arguments.get("time_max") or arguments.get("timeMax")
        
        # If no dates provided, default to a 7-day range starting from today
        if not time_min or not time_max:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            if not time_min:
                # Start of today in UTC
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                time_min = today_start.isoformat()
                logger.debug("No time_min provided, defaulting to today start: %s", time_min)
            
            if not time_max:
                # End of 7 days from now in UTC (to catch more events)
                week_end = now + timedelta(days=7)
                week_end = week_end.replace(hour=23, minute=59, second=59, microsecond=0)
                time_max = week_end.isoformat()
                logger.debug("No time_max provided, defaulting to 7 days from now: %s", time_max)
        
        if time_min:
            args["time_min"] = time_min
        if time_max:
            args["time_max"] = time_max
        
        logger.info("get_events request: time_min=%s, time_max=%s", time_min, time_max)
        
        return args
    
    async def _get_tasks_per_list(self, task_lists_res: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch tasks for each task list."""
        tasks_per_list: Dict[str, Any] = {}
        
        logger.debug("task_lists_res type: %s, value: %s", type(task_lists_res), str(task_lists_res)[:500])
        
        list_items = self._extract_list_items(task_lists_res)
        logger.debug("list_items extracted: %s", list_items)
        
        if not list_items:
            logger.warning("No task list items found to process")
            return tasks_per_list
        
        logger.info("Processing %d task lists", len(list_items))
        
        for lst in list_items:
            list_id = self._extract_list_id(lst)
            logger.debug("Extracted list_id: %s from lst: %s", list_id, str(lst)[:200])
            
            if not list_id:
                logger.warning("Could not extract list_id from: %s", str(lst)[:200])
                continue
            
            task_args = {
                "task_list_id": list_id,
                "user_google_email": arguments.get("user_google_email")
            }
            
            logger.info("Fetching tasks for list_id: %s", list_id)
            
            try:
                tasks_res = await self._call_tool_with_timeout(
                    "list_tasks",
                    task_args,
                    Config.CALENDAR_LIST_TASKS_TIMEOUT
                )
                tasks_per_list[list_id] = tasks_res
                logger.debug("Successfully fetched tasks for list %s, response keys: %s", list_id, list(tasks_res.keys()) if isinstance(tasks_res, dict) else type(tasks_res))
            except asyncio.TimeoutError:
                tasks_per_list[list_id] = {"error": "timeout"}
            except Exception as e:
                tasks_per_list[list_id] = {"error": str(e)}
        
        logger.info("Finished processing task lists, total lists with tasks: %d", len(tasks_per_list))
        return tasks_per_list
    
    @staticmethod
    def _extract_list_items(task_lists_res: Any) -> list | None:
        """Extract list items from task lists response.
        
        Google Workspace MCP returns responses in MCP format:
        {
            "content": [{"type": "text", "text": "..."}],
            "structuredContent": { actual structured data },
            "isError": false
        }
        """
        if not task_lists_res:
            return None
        
        logger.debug("_extract_list_items input type: %s", type(task_lists_res))
        
        if isinstance(task_lists_res, dict):
            # Log all keys to understand the structure
            logger.debug("_extract_list_items dict keys: %s", list(task_lists_res.keys()))
            
            # Check for structuredContent field (Google Workspace MCP format)
            if "structuredContent" in task_lists_res:
                structured = task_lists_res.get("structuredContent")
                logger.debug("Found structuredContent, type: %s, keys: %s", 
                           type(structured), 
                           list(structured.keys()) if isinstance(structured, dict) else "not a dict")
                
                if isinstance(structured, dict):
                    # Look for task lists in various possible locations
                    if "taskLists" in structured:
                        logger.debug("Found 'taskLists' in structuredContent")
                        return structured.get("taskLists")
                    
                    if "items" in structured:
                        logger.debug("Found 'items' in structuredContent")
                        return structured.get("items")
                    
                    # Check if the whole structured content IS the list
                    if "data" in structured and isinstance(structured["data"], dict):
                        data = structured["data"]
                        if "taskLists" in data or "items" in data:
                            logger.debug("Found structured data in 'data' field")
                            return data.get("taskLists") or data.get("items")
                    
                    # Log what we found to help debug
                    logger.debug("structuredContent structure: %s", str(structured)[:1000])
                    
                    # Fallback: Try to parse text content if structuredContent only has 'result' with text
                    if "result" in structured and isinstance(structured["result"], str):
                        logger.info("Attempting to parse task list IDs from text content")
                        return _parse_task_lists_from_text(structured["result"])
            
            # Fallback: Check for direct structured data fields
            if "taskLists" in task_lists_res:
                logger.debug("Found 'taskLists' key in response")
                return task_lists_res.get("taskLists")
            
            if "items" in task_lists_res:
                logger.debug("Found 'items' key in response")
                return task_lists_res.get("items")
            
            # Final fallback: Try parsing from content array
            if "content" in task_lists_res and isinstance(task_lists_res["content"], list):
                for item in task_lists_res["content"]:
                    if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                        logger.info("Attempting to parse task list IDs from content text")
                        parsed = _parse_task_lists_from_text(item["text"])
                        if parsed:
                            return parsed
            
            logger.warning("Could not find task lists in structured format, only found keys: %s", list(task_lists_res.keys()))
        
        return None
    
    @staticmethod
    def _extract_list_id(lst: Any) -> str | None:
        """Extract list ID from a task list item."""
        if not isinstance(lst, dict):
            return None
        
        return lst.get("id") or lst.get("tasklistId") or lst.get("taskListId")
    
    async def _call_tool_with_timeout(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 60.0) -> Any:
        """Call a tool on the client with a timeout and extract the result."""
        logger.debug("Calling %s with args: %s", tool_name, arguments)
        
        try:
            resp = await asyncio.wait_for(
                self.client.call("tools/call", {"name": tool_name, "arguments": arguments}),
                timeout=timeout,
            )
            logger.debug("%s returned successfully, response type: %s", tool_name, type(resp))
            logger.debug("%s response keys: %s", tool_name, resp.keys() if isinstance(resp, dict) else "not a dict")
            
            # The response from MCP tools is already the result, not wrapped in another layer
            # It contains the 'content' array directly
            return resp
        except asyncio.TimeoutError:
            logger.error("Timeout calling %s", tool_name)
            raise

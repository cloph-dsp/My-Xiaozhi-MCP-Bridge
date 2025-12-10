"""Gemini AI service for summarization and search."""
import json
import logging
from typing import Any, Dict

import httpx

from config import Config

logger = logging.getLogger("bridge")


class GeminiService:
    """Service for interacting with Google Gemini AI."""
    
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or Config.GEMINI_API_KEY
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set, Gemini features will be disabled")
    
    async def summarize_text(self, text: str) -> str:
        """Summarize text using Gemini 2.5 Flash Lite."""
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set, skipping summarization")
            return text
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{Config.GEMINI_API_URL}/{Config.GEMINI_FLASH_LITE_MODEL}:generateContent",
                    json={
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": self._build_summary_prompt(text)
                                    }
                                ]
                            }
                        ],
                        "generationConfig": {
                            "maxOutputTokens": Config.GEMINI_MAX_SUMMARY_TOKENS,
                            "temperature": Config.GEMINI_TEMPERATURE
                        }
                    },
                    params={"key": self.api_key}
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("candidates"):
                        summary = result["candidates"][0]["content"]["parts"][0]["text"]
                        logger.info(f"Summarized text (from {len(text)} to {len(summary)} chars)")
                        return summary.strip()
                else:
                    logger.error(f"Gemini API error: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error calling Gemini API: {e}")
        
        return text
    
    async def search_web(self, query: str) -> str:
        """Search the web and get AI-summarized results using Gemini 2.5 Flash."""
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set, cannot search")
            return "Error: GEMINI_API_KEY not configured"
        
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{Config.GEMINI_API_URL}/{Config.GEMINI_FLASH_MODEL}:generateContent",
                    json={
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": self._build_search_prompt(query)
                                    }
                                ]
                            }
                        ],
                        "generationConfig": {
                            "maxOutputTokens": Config.GEMINI_MAX_SEARCH_TOKENS,
                            "temperature": Config.GEMINI_TEMPERATURE
                        },
                        "tools": [
                            {
                                "googleSearch": {}
                            }
                        ]
                    },
                    params={"key": self.api_key}
                )
                
                if response.status_code != 200:
                    logger.error(f"Gemini API error: {response.status_code} - {response.text}")
                    return f"Error: API returned {response.status_code}"
                
                result = response.json()
                answer = self._extract_answer(result)
                
                if answer:
                    logger.info(f"Gemini web search completed for query: {query}")
                    return answer.strip()
                
                return "Error: Could not extract answer from Gemini response"
                
        except Exception as e:
            logger.error(f"Error calling Gemini search: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()[:500]}")
            return f"Error: {str(e)}"
    
    @staticmethod
    def _build_summary_prompt(text: str) -> str:
        """Build prompt for text summarization."""
        return f"""Summarize this news content in 2-3 sentences maximum.

Content:
{text}

Provide ONLY the summary, nothing else."""
    
    @staticmethod
    def _build_search_prompt(query: str) -> str:
        """Build prompt for web search."""
        return f"""Search the web for: {query}

IMPORTANT: Be extremely concise and direct.

Provide a brief, helpful answer:
1. Answer the query directly with only the most essential information
2. Maximum 2-3 sentences
3. No unnecessary details or elaboration
4. Be factual and accurate

Keep your response SHORT and to the point."""
    
    @staticmethod
    def _extract_answer(result: Dict[str, Any]) -> str | None:
        """Extract answer text from Gemini API response."""
        logger.debug(f"Gemini response keys: {list(result.keys())}")
        
        candidates = result.get("candidates", [])
        if not candidates:
            if "promptFeedback" in result:
                logger.error(f"Prompt feedback: {result['promptFeedback']}")
            logger.error(f"No candidates in response: {list(result.keys())}")
            return None
        
        candidate = candidates[0]
        logger.debug(f"Candidate keys: {list(candidate.keys())}")
        
        content = candidate.get("content")
        if not content:
            logger.error(f"No content in candidate. Keys: {list(candidate.keys())}")
            logger.debug(f"Full candidate: {json.dumps(candidate)[:1000]}")
            return None
        
        logger.debug(f"Content keys: {list(content.keys())}")
        parts = content.get("parts", []) or []
        
        # Fallbacks for different response formats
        if not parts and content.get("text"):
            parts = [{"text": content.get("text")}]
        
        if not parts and candidate.get("text"):
            parts = [{"text": candidate.get("text")}]
        
        if not parts:
            logger.error("No parts/text in content; cannot extract answer")
            logger.debug(f"Full content: {json.dumps(content)[:1000]}")
            logger.debug(f"Full candidate: {json.dumps(candidate)[:1000]}")
            return None
        
        answer = parts[0].get("text", "")
        if not answer:
            logger.error("No text in first part")
            logger.debug(f"First part: {json.dumps(parts[0])[:500]}")
            logger.debug(f"Full content: {json.dumps(content)[:1000]}")
            return None
        
        return answer

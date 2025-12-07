#!/usr/bin/env python3
"""Test Gemini API response structure"""
import asyncio
import json
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

async def test_gemini():
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        print("GEMINI_API_KEY not set")
        return
    
    query = "notícias Portugal hoje"
    
    print(f"Testing Gemini API with query: {query}")
    print(f"API Key: {gemini_api_key[:10]}...")
    
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
            
            print(f"Response status: {response.status_code}")
            result = response.json()
            
            print("\n=== FULL RESPONSE ===")
            print(json.dumps(result, indent=2))
            print("\n=== RESPONSE STRUCTURE ===")
            print(f"Top-level keys: {list(result.keys())}")
            
            if "candidates" in result:
                print(f"Number of candidates: {len(result['candidates'])}")
                if result["candidates"]:
                    cand = result["candidates"][0]
                    print(f"First candidate keys: {list(cand.keys())}")
                    if "content" in cand:
                        print(f"Content keys: {list(cand['content'].keys())}")
                        if "parts" in cand["content"]:
                            print(f"Number of parts: {len(cand['content']['parts'])}")
                            if cand["content"]["parts"]:
                                print(f"First part: {cand['content']['parts'][0]}")
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_gemini())

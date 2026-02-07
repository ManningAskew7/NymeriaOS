"""
Tools for the ReAct Agent

Built-in tools that come with the agent.
Frameworks can add more tools via the ToolRegistry.
"""

import json
import os
import httpx
from langchain_core.tools import tool

from .tool_registry import default_registry, register_tool


@tool
def perplexity_search(query: str) -> str:
    """
    Search the web using Perplexity Sonar Pro.

    Use this tool when you need to find current information,
    research topics, or answer questions that require up-to-date
    web data. This performs deep multi-step research.

    Args:
        query: The search query or question to research

    Returns:
        Research findings and analysis from Perplexity
    """
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        return "Error: PERPLEXITY_API_KEY not set in environment variables"

    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "sonar-pro",
        "messages": [{"role": "user", "content": query}],
        "stream": True,
        "web_search_options": {"search_type": "pro"},
    }

    try:
        content_chunks = []
        debug_info = []

        with httpx.Client(timeout=120.0) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_body = response.read().decode('utf-8', errors='ignore')
                    return f"Error: Perplexity API returned status {response.status_code}: {error_body[:500]}"

                for line in response.iter_lines():
                    if not line:
                        continue

                    if line.startswith("data: "):
                        data = line[6:]
                    elif line.startswith(":"):
                        continue
                    else:
                        data = line

                    if data == "[DONE]":
                        break

                    if not data.strip():
                        continue

                    try:
                        chunk = json.loads(data)
                        debug_info.append(f"chunk_keys: {list(chunk.keys())}")

                        if "choices" in chunk and chunk["choices"]:
                            choice = chunk["choices"][0]

                            if "delta" in choice:
                                delta = choice["delta"]
                                if "content" in delta and delta["content"]:
                                    content_chunks.append(delta["content"])
                            elif "message" in choice:
                                message = choice["message"]
                                if "content" in message and message["content"]:
                                    content_chunks.append(message["content"])

                    except json.JSONDecodeError as e:
                        debug_info.append(f"JSON error: {str(e)[:50]} for data: {data[:100]}")
                        continue

        result = "".join(content_chunks)

        if not result:
            return f"No content received. Debug: processed {len(debug_info)} chunks. Last few: {debug_info[-5:] if debug_info else 'none'}"

        return result

    except httpx.TimeoutException:
        return "Error: Request to Perplexity timed out (120s). The query may be too complex."
    except httpx.RequestError as e:
        return f"Error connecting to Perplexity: {str(e)}"
    except Exception as e:
        return f"Error during Perplexity search: {str(e)}"


# Register built-in tools with the default registry
register_tool(perplexity_search)

# Export list for backward compatibility
# Frameworks should use ToolRegistry for new code
TOOLS = default_registry.get_enabled_tools()

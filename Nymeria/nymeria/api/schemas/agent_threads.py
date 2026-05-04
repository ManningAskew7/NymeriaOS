"""Agent Threads API schemas."""

from typing import Optional

from pydantic import BaseModel, Field


class AgentThreadCreateRequest(BaseModel):
    """Request to create a new callable thread."""

    # callable_name becomes the LangChain tool name and is bound to the LLM
    # via tool/function specs. OpenAI and Anthropic both reject names outside
    # ^[a-zA-Z0-9_-]{1,64}$, so reject early instead of crashing on the first
    # invocation attempt.
    callable_name: str = Field(
        ..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    callable_description: str = Field(default="", max_length=500)
    system_prompt: str = Field(default="", max_length=50000)
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_temperature: Optional[float] = None
    llm_max_tokens: Optional[int] = None

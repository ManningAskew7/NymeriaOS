"""
Centralized Configuration for the ReAct Agent

All settings in one place for easy management and framework integration.
Frameworks can override these by passing custom AgentConfig instances.
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Literal
from dotenv import load_dotenv

load_dotenv()


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    provider: Literal["openrouter", "openai", "anthropic", "custom"] = "openrouter"
    model: str = field(default_factory=lambda: os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001"))
    api_key: Optional[str] = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY"))
    base_url: Optional[str] = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    temperature: Optional[float] = 1.0  # None = don't send (use model defaults)
    max_tokens: Optional[int] = None

    # Advanced sampling parameters
    top_p: Optional[float] = None  # Nucleus sampling (0.0-1.0)
    top_k: Optional[int] = None  # Top-k sampling (1-100)
    frequency_penalty: Optional[float] = None  # Reduce repetition (-2.0 to 2.0)
    presence_penalty: Optional[float] = None  # Encourage new topics (-2.0 to 2.0)
    reasoning_effort: Optional[str] = None  # For reasoning models: "low", "medium", "high"
    extended_thinking: bool = False  # Enable extended thinking/reasoning tokens

    # HTTP timeout for LLM API calls (seconds). Prevents hanging on stalled connections.
    # Applies as the read timeout — if the server sends no data for this long, the call fails.
    # Default None = no timeout (relies on tool_timeout for execution bounds).
    # SubAgentExecutor sets 120s explicitly for sub-agent LLM calls.
    request_timeout: Optional[int] = None

    # For custom providers
    custom_llm: Optional[object] = field(default=None, repr=False)


@dataclass
class CheckpointerConfig:
    """State persistence configuration."""

    backend: Literal["memory", "sqlite", "sqlite_async", "postgres", "custom"] = "memory"

    # SQLite settings
    sqlite_path: str = field(default_factory=lambda: os.getenv("SQLITE_PATH", "./agent_memory.db"))

    # Postgres settings
    postgres_uri: Optional[str] = field(default_factory=lambda: os.getenv("POSTGRES_URI"))

    # For custom checkpointers
    custom_checkpointer: Optional[object] = field(default=None, repr=False)


@dataclass
class AgentConfig:
    """
    Main configuration container for the ReAct agent.

    Usage:
        # Default config (uses env vars)
        config = AgentConfig()

        # Custom config
        config = AgentConfig(
            llm=LLMConfig(provider="anthropic", model="claude-3-5-sonnet"),
            system_prompt="You are a helpful assistant.",
        )

        # Framework integration
        graph = create_graph(config=config)
    """

    llm: LLMConfig = field(default_factory=LLMConfig)
    checkpointer: CheckpointerConfig = field(default_factory=CheckpointerConfig)

    # Agent behavior
    system_prompt: str = field(default_factory=lambda: os.getenv("SYSTEM_PROMPT", """You are a helpful AI assistant with access to tools.

Guidelines:
- Use available tools when they would help answer the question
- Keep responses concise and conversational
- For complex topics, use brief bullet points
- Get straight to the answer without unnecessary preamble"""))

    # Execution settings
    max_iterations: int = 70  # Max ReAct loops before forcing stop
    recursion_limit: int = 150  # LangGraph recursion limit (must exceed 2x max_iterations)
    tool_timeout: int = 300  # Per-tool-node timeout in seconds (5 minutes)

    # Debug settings
    verbose: bool = field(default_factory=lambda: os.getenv("AGENT_VERBOSE", "false").lower() == "true")


# Default configuration instance
default_config = AgentConfig()

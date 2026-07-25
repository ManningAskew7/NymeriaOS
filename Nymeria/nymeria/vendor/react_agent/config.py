"""
Centralized Configuration for the ReAct Agent

All settings in one place for easy management and framework integration.
Frameworks can override these by passing custom AgentConfig instances.
"""

import os
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Optional, Literal


@dataclass
class LLMFallbackConfig:
    """One fallback model candidate for transient provider failures."""

    model: str
    provider: Optional[str] = None
    provider_route: Optional[Literal["native", "openai_compat", "anthropic_messages"]] = None
    api_key: Optional[str] = field(default=None, repr=False)
    base_url: Optional[str] = None
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
    context_length: Optional[int] = None
    ollama_num_ctx: Optional[int] = None


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    provider: str = "openrouter"
    model: str = field(default_factory=lambda: os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001"))
    api_key: Optional[str] = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY"))
    base_url: Optional[str] = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    temperature: Optional[float] = 1.0  # None = don't send (use model defaults)
    max_tokens: Optional[int] = None
    context_length: Optional[int] = None
    ollama_num_ctx: Optional[int] = None

    # Advanced sampling parameters
    top_p: Optional[float] = None  # Nucleus sampling (0.0-1.0)
    top_k: Optional[int] = None  # Top-k sampling (1-100)
    frequency_penalty: Optional[float] = None  # Reduce repetition (-2.0 to 2.0)
    presence_penalty: Optional[float] = None  # Encourage new topics (-2.0 to 2.0)
    reasoning_effort: Optional[str] = None  # "off", "low", "medium", "high", "xhigh", "max" (None = unset/inherit; "off" wins over extended_thinking)
    extended_thinking: bool = False  # Enable extended thinking/reasoning tokens
    provider_route: Optional[Literal["native", "openai_compat", "anthropic_messages"]] = None
    # Adapter route for providers with both a native partner package and an
    # OpenAI-compatible shim. None = use the provider registry default.
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
    # OpenAI-compatible API mode. None = use the provider registry default
    # (Responses for OpenAI and Responses-native gateways, Chat Completions for
    # OpenRouter and the rest); the factories resolve it via
    # provider_default_api_mode. Set explicitly to force a mode per thread.

    # HTTP timeout for LLM API calls (seconds). Prevents hanging on stalled connections.
    # Applies as the read timeout — if the server sends no data for this long, the call fails.
    # Default None = no timeout (relies on tool_timeout for execution bounds).
    # SubAgentExecutor sets 120s explicitly for sub-agent LLM calls.
    request_timeout: Optional[int] = None

    # Retry transient provider/transport failures. Post-stream failures are
    # replayed by Nymeria's graph stream processor from the latest checkpoint.
    stream_max_retries: int = 2
    stream_retry_initial_delay: float = 1.0
    stream_retry_max_delay: float = 8.0
    fallback_hold_seconds: int = 7200
    fallbacks: list[LLMFallbackConfig] = field(default_factory=list)
    active_fallback_candidate_index: int = field(default=0, repr=False)
    fallback_activation_callback: Optional[Callable[[dict[str, Any]], dict[str, Any] | None]] = field(
        default=None,
        repr=False,
    )
    # Consent gate for model switches (user-consented fallback switching + the
    # refusal swap). This callback is the ONLY consent surface the vendored
    # runtime knows: the policy (switch/refusal modes, prompt timeout, holds)
    # lives host-side inside the closure, so the boundary carries one thing.
    # ASYNC, distinct from the sync activation callback above. Called with a
    # context dict (the fallback payload plus "kind": "transport"|"refusal"
    # and turn-source fields) before a switch is applied. Returns
    # {"action": "swap", "hold_seconds"?, "hold_permanent"?} |
    # {"action": "fail"} | {"action": "auto"} (proceed as if unwired).
    # None (the host did not wire consent) or a callback error = "auto";
    # for the refusal swap a None callback means the swap cannot apply.
    fallback_decision_callback: Optional[Callable[[dict[str, Any]], Any]] = field(
        default=None,
        repr=False,
    )

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
    # Shared checkpointer connection-pool bounds (postgres backend only).
    # The first postgres checkpointer created in the process fixes the pool
    # size for the process lifetime.
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10

    # Worker-thread ceiling for the process-wide checkpoint executor that
    # async paths dispatch blocking checkpoint I/O on (sqlite and postgres).
    # The first checkpointer created in the process fixes the size for the
    # process lifetime.
    checkpoint_executor_max_workers: int = 8

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
            llm=LLMConfig(provider="anthropic", model="claude-sonnet-4-6"),
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
    max_iterations: int = 500  # Max tool calls per turn before forcing stop
    recursion_limit: int = 1025  # LangGraph recursion limit (must exceed 2x max_iterations)
    repeated_tool_result_limit: int = 5  # Stop repeated same tool+args+result loops
    tool_timeout: int = 300  # Per-tool-node timeout in seconds (5 minutes)
    tool_output_max_chars: int = 100000  # Max stored characters per tool result
    on_timeout: Optional[object] = field(default=None, repr=False)  # Callback for tool timeout: fn(input_dict, config=None) -> None

    # Debug settings
    verbose: bool = field(default_factory=lambda: os.getenv("AGENT_VERBOSE", "false").lower() == "true")


# Default configuration instance
default_config = AgentConfig()

"""Thread configuration and callable-team API schemas."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

_VALID_REASONING_EFFORTS = {"off", "low", "medium", "high", "xhigh", "max"}


class ThreadLLMConfigRequest(BaseModel):
    """Partial LLM override update for a thread."""

    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    extended_thinking: bool | None = None
    # "off", "low", "medium", "high", "xhigh", "max"; "" = inherit global.
    reasoning_effort: str | None = None
    use_model_defaults: bool | None = None
    provider_route: Literal["native", "openai_compat", "anthropic_messages"] | None = None
    openai_api_mode: Literal["chat_completions", "responses"] | None = None
    base_url: str | None = None
    context_length: int | None = Field(default=None, ge=1_000, le=2_000_000)
    ollama_num_ctx: int | None = Field(default=None, ge=1_000, le=2_000_000)
    api_key: str | None = None
    compact_threshold_mode: Literal["percentage", "tokens"] | None = None
    compact_threshold: float | None = Field(default=None, ge=0.05, le=0.95)
    compact_threshold_tokens: int | None = Field(default=None, ge=1_000, le=2_000_000)

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        normalized = value.strip().lower()
        if normalized not in _VALID_REASONING_EFFORTS:
            raise ValueError(
                "reasoning_effort must be one of: off, low, medium, high, "
                "xhigh, max (or '' to inherit the global setting)"
            )
        return normalized


class DreamingConfigRequest(BaseModel):
    """Partial dreaming-config update. Bookkeeping fields (last_dream_at,
    last_dream_thread_id) are intentionally excluded — only the scheduler
    writes them.
    """

    enabled: bool | None = None
    min_interval_hours: int | None = Field(default=None, ge=1, le=168)
    min_idle_minutes: int | None = Field(default=None, ge=5, le=10080)
    min_turns_since_last: int | None = Field(default=None, ge=1, le=10000)
    model: str | None = None
    system_prompt: str | None = Field(default=None, max_length=50000)
    kickoff_prompt: str | None = Field(default=None, max_length=10000)


class ThreadConfigUpdateRequest(BaseModel):
    """Partial thread-configuration update."""

    instructions: str | None = Field(default=None, max_length=5000)
    disabled_tools: list[str] | None = None
    enabled_tools: list[str] | None = None
    enabled_skills: list[str] | None = None
    disabled_skills: list[str] | None = None
    llm_config: ThreadLLMConfigRequest | None = None
    system_prompt: str | None = Field(default=None, max_length=50000)
    callable: bool | None = None
    callable_name: str | None = Field(default=None, max_length=64)
    callable_description: str | None = Field(default=None, max_length=500)
    callable_max_iterations: int | None = Field(default=None, ge=1, le=1000)
    callable_team_id: str | None = Field(default=None, max_length=120)
    callable_team_name: str | None = Field(default=None, max_length=120)
    inject_todos_in_prompt: bool | None = None
    show_autonomous_prompts: bool | None = None
    show_prompt_metadata: bool | None = None
    telegram_autonomous_delivery: Literal["full", "notify_only", "off"] | None = None
    in_app_notification_level: Literal["notify_only", "all_autonomous", "off"] | None = None
    notification_profile: str | None = Field(default=None, max_length=120)
    memory_char_limit: int | None = Field(default=None, ge=1, le=2_000_000)
    image_window_size: int | None = Field(default=None, ge=1, le=3000)
    dreaming: DreamingConfigRequest | None = None
    clear_instructions: bool = False
    clear_disabled_tools: bool = False
    clear_enabled_tools: bool = False
    clear_enabled_skills: bool = False
    clear_disabled_skills: bool = False
    clear_llm_config: bool = False
    clear_system_prompt: bool = False
    clear_notification_profile: bool = False
    clear_memory_char_limit: bool = False
    clear_image_window_size: bool = False
    clear_dreaming: bool = False


class NotepadUpdateRequest(BaseModel):
    """Replace a thread's notepad content. Blank content clears the notepad."""

    content: str = Field(default="", max_length=2_000_000)


class ThreadTeamCreateRequest(BaseModel):
    """Create a callable visibility team."""

    name: str = Field(..., min_length=1, max_length=120)
    thread_ids: list[str] = Field(default_factory=list)


class ThreadTeamUpdateRequest(BaseModel):
    """Rename a callable visibility team or replace membership."""

    name: str | None = Field(default=None, max_length=120)
    thread_ids: list[str] | None = None

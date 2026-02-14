"""Per-thread configuration for Nymeria threads.

Each thread can override:
- System prompt instructions (appended to soul.md)
- Disabled tools (removed from the graph entirely)
- LLM settings (provider, model, temperature, etc.)
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Thread-safe locks for config operations (keyed by thread_id)
_config_locks: Dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()


class ThreadLLMConfig(BaseModel):
    """LLM overrides for a thread. None values inherit from global settings."""

    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    extended_thinking: Optional[bool] = None
    reasoning_effort: Optional[str] = None


class ThreadConfig(BaseModel):
    """Configuration for a specific thread."""

    thread_id: str
    instructions: Optional[str] = Field(default=None, max_length=5000)
    disabled_tools: List[str] = Field(default_factory=list)
    enabled_tools: List[str] = Field(default_factory=list)
    llm_config: Optional[ThreadLLMConfig] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def has_customizations(self) -> bool:
        """Check if this config has any non-default values."""
        if self.instructions:
            return True
        if self.disabled_tools:
            return True
        if self.enabled_tools:
            return True
        if self.llm_config:
            d = self.llm_config.model_dump(exclude_none=True)
            if d:
                return True
        return False


class ThreadConfigManager:
    """Manages per-thread configuration files on disk with thread-safe operations."""

    def __init__(self, data_dir: Path):
        self.configs_dir = data_dir / "thread_configs"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ThreadConfigManager initialized: {self.configs_dir}")

    def _get_lock(self, thread_id: str) -> threading.RLock:
        """Get or create a lock for a specific thread."""
        global _config_locks
        with _locks_lock:
            if thread_id not in _config_locks:
                _config_locks[thread_id] = threading.RLock()
            return _config_locks[thread_id]

    def _get_config_path(self, thread_id: str) -> Path:
        """Get the path to a thread's config file."""
        safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_")
        if not safe_id:
            safe_id = "default"
        return self.configs_dir / f"{safe_id}.json"

    def get_config(self, thread_id: str) -> Optional[ThreadConfig]:
        """Load a thread's config from disk, or return None if no config exists."""
        config_path = self._get_config_path(thread_id)
        if not config_path.exists():
            return None

        lock = self._get_lock(thread_id)
        with lock:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ThreadConfig.model_validate(data)
            except Exception as e:
                logger.error(f"Failed to load thread config for {thread_id}: {e}")
                return None

    def save_config(self, config: ThreadConfig) -> bool:
        """Save a thread's config to disk."""
        config_path = self._get_config_path(config.thread_id)
        lock = self._get_lock(config.thread_id)

        with lock:
            try:
                config.updated_at = datetime.utcnow()
                temp_path = config_path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(config.model_dump(mode="json"), f, indent=2, default=str)
                temp_path.replace(config_path)
                logger.debug(f"Saved thread config for {config.thread_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to save thread config for {config.thread_id}: {e}")
                return False

    def delete_config(self, thread_id: str) -> bool:
        """Delete a thread's config file."""
        config_path = self._get_config_path(thread_id)
        lock = self._get_lock(thread_id)

        with lock:
            if config_path.exists():
                try:
                    config_path.unlink()
                    logger.info(f"Deleted thread config for {thread_id}")
                    return True
                except Exception as e:
                    logger.error(f"Failed to delete thread config for {thread_id}: {e}")
                    return False
        return False

    def list_configured_threads(self) -> List[str]:
        """Return thread IDs that have custom configs."""
        result = []
        if self.configs_dir.exists():
            for path in self.configs_dir.iterdir():
                if path.is_file() and path.suffix == ".json":
                    result.append(path.stem)
        return sorted(result)

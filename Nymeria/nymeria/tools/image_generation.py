"""Image generation tool backed by OpenAI GPT Image and Gemini Nano Banana."""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from ..core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY
from ..core.http_policy import (
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
    httpx_request_with_policy,
)
from ..core.user_profile import UserProfileManager
from .utils import get_user_id

logger = logging.getLogger(__name__)

TOOL_NAME = "image_generate"

_DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "native_context_enabled": True,
    "openai_model": "gpt-image-2",
    "openai_size": "auto",
    "openai_quality": "auto",
    "openai_output_format": "png",
    "openai_moderation": "auto",
    "gemini_model": "gemini-3-pro-image-preview",
    "gemini_aspect_ratio": "1:1",
    "gemini_image_size": "auto",
}

_OPENAI_MODELS = {
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
}
_OPENAI_SIZES = {
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
}
_OPENAI_QUALITIES = {"auto", "low", "medium", "high"}
_OPENAI_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
_OPENAI_MODERATION = {"auto", "low"}
_GEMINI_MODELS = {
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
}
_GEMINI_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"}
_GEMINI_IMAGE_SIZES = {"auto", "1K", "2K", "4K"}
_GEMINI_MODELS_WITH_IMAGE_SIZE = {
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
}

_profile_manager: UserProfileManager | None = None


def _get_profile_manager() -> UserProfileManager:
    global _profile_manager
    if _profile_manager is None:
        settings = get_settings()
        _profile_manager = UserProfileManager(settings.data_dir)
    return _profile_manager


def _workspace_dir() -> Path:
    return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()


def _safe_slug(value: str, *, fallback: str = "image", max_chars: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._").lower()
    if not slug:
        slug = fallback
    return slug[:max_chars].strip("-._") or fallback


def _safe_user_id(value: str) -> str:
    return _safe_slug(value, fallback="default", max_chars=64)


def _resolve_config(user_id: str) -> dict[str, Any]:
    config = dict(_DEFAULT_CONFIG)
    try:
        profile = _get_profile_manager().get_profile(user_id)
        config.update(profile.tool_preferences.get_tool_config(TOOL_NAME))
    except Exception as exc:
        logger.warning("Failed to read %s config for user %s: %s", TOOL_NAME, user_id, exc)
    return config


def _choice(config: dict[str, Any], key: str, allowed: set[str]) -> str:
    value = str(config.get(key, _DEFAULT_CONFIG[key]) or _DEFAULT_CONFIG[key])
    if value not in allowed:
        return str(_DEFAULT_CONFIG[key])
    return value


def _bool_config(config: dict[str, Any], key: str) -> bool:
    value = config.get(key, _DEFAULT_CONFIG[key])
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _write_image_file(
    *,
    user_id: str,
    raw: bytes,
    mime_type: str,
    output_name: Optional[str],
    prompt: str,
) -> Path:
    extension = mimetypes.guess_extension(mime_type) or ".png"
    if extension == ".jpe":
        extension = ".jpg"

    name_source = output_name or prompt
    slug = _safe_slug(name_source)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{slug}-{uuid4().hex[:8]}{extension}"

    output_dir = _workspace_dir() / "image-generation" / _safe_user_id(user_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = (output_dir / filename).resolve()

    workspace = _workspace_dir()
    if not path.is_relative_to(workspace):
        raise RuntimeError("Resolved output path escaped the workspace")

    path.write_bytes(raw)
    return path


def _openai_key() -> str | None:
    settings = get_settings()
    return settings.openai_api_key or os.environ.get("OPENAI_API_KEY")


def _gemini_key() -> str | None:
    settings = get_settings()
    return settings.gemini_api_key or os.environ.get("GEMINI_API_KEY")


def _read_openai_image_bytes(image_item: Any) -> bytes:
    b64_json = getattr(image_item, "b64_json", None)
    if not b64_json and isinstance(image_item, dict):
        b64_json = image_item.get("b64_json")
    if isinstance(b64_json, str) and b64_json:
        return base64.b64decode(b64_json)

    image_url = getattr(image_item, "url", None)
    if not image_url and isinstance(image_item, dict):
        image_url = image_item.get("url")
    if isinstance(image_url, str) and image_url:
        try:
            response, _redirect_chain, _policy = httpx_request_with_policy(
                "GET",
                image_url,
                timeout=120.0,
                follow_redirects=True,
            )
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as exc:
            raise RuntimeError(f"OpenAI image URL blocked by HTTP egress policy: {exc}") from exc
        response.raise_for_status()
        return response.content

    raise RuntimeError("OpenAI returned no image bytes or image URL")


def _generate_openai(prompt: str, config: dict[str, Any]) -> tuple[bytes, str, str]:
    api_key = _openai_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The openai package is not installed.") from exc

    model = _choice(config, "openai_model", _OPENAI_MODELS)
    output_format = _choice(config, "openai_output_format", _OPENAI_OUTPUT_FORMATS)
    params: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": _choice(config, "openai_size", _OPENAI_SIZES),
        "quality": _choice(config, "openai_quality", _OPENAI_QUALITIES),
        "output_format": output_format,
        "moderation": _choice(config, "openai_moderation", _OPENAI_MODERATION),
        "response_format": "b64_json",
    }

    client = OpenAI(api_key=api_key)
    result = client.images.generate(**params)
    data = getattr(result, "data", None)
    if not data:
        raise RuntimeError("OpenAI returned an empty image response.")

    mime_type = f"image/{'jpeg' if output_format == 'jpeg' else output_format}"
    return _read_openai_image_bytes(data[0]), mime_type, model


def _iter_gemini_parts(response: Any) -> list[Any]:
    parts = getattr(response, "parts", None)
    if parts:
        return list(parts)

    candidates = getattr(response, "candidates", None) or []
    found: list[Any] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        candidate_parts = getattr(content, "parts", None)
        if candidate_parts:
            found.extend(candidate_parts)
    return found


def _inline_data_bytes(inline_data: Any) -> tuple[bytes, str]:
    data = getattr(inline_data, "data", None)
    if data is None and isinstance(inline_data, dict):
        data = inline_data.get("data")

    mime_type = getattr(inline_data, "mime_type", None)
    if mime_type is None:
        mime_type = getattr(inline_data, "mimeType", None)
    if mime_type is None and isinstance(inline_data, dict):
        mime_type = inline_data.get("mime_type") or inline_data.get("mimeType")
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = "image/png"

    if isinstance(data, bytes):
        return data, mime_type
    if isinstance(data, str):
        return base64.b64decode(data), mime_type
    raise RuntimeError("Gemini returned image metadata without image bytes.")


def _generate_gemini(prompt: str, config: dict[str, Any]) -> tuple[bytes, str, str]:
    api_key = _gemini_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("The google-genai package is not installed.") from exc

    model = _choice(config, "gemini_model", _GEMINI_MODELS)
    image_config_kwargs: dict[str, Any] = {
        "aspectRatio": _choice(config, "gemini_aspect_ratio", _GEMINI_ASPECT_RATIOS),
    }
    image_size = _choice(config, "gemini_image_size", _GEMINI_IMAGE_SIZES)
    if image_size != "auto" and model in _GEMINI_MODELS_WITH_IMAGE_SIZE:
        image_config_kwargs["imageSize"] = image_size

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            # pyrefly: ignore[unexpected-keyword]
            imageConfig=types.ImageConfig(**image_config_kwargs),
        ),
    )

    text_parts: list[str] = []
    for part in _iter_gemini_parts(response):
        inline_data = getattr(part, "inline_data", None)
        if inline_data is None:
            inline_data = getattr(part, "inlineData", None)
        if inline_data is not None:
            raw, mime_type = _inline_data_bytes(inline_data)
            return raw, mime_type, model

        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            text_parts.append(text)

    detail = f" Response text: {' '.join(text_parts)[:500]}" if text_parts else ""
    raise RuntimeError(f"Gemini returned no image data.{detail}")


def _native_artifact(
    *,
    path: Path,
    prompt: str,
    provider: str,
    model: str,
    mime_type: str,
    native_context_enabled: bool,
) -> dict[str, Any]:
    return {
        NATIVE_IMAGE_ARTIFACT_KEY: {
            "path": str(path),
            "mime_type": mime_type,
            "prompt": prompt,
            "provider": provider,
            "model": model,
            "native_context_enabled": native_context_enabled,
        }
    }


@tool(response_format="content_and_artifact")
def image_generate(
    prompt: str,
    output_name: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict[str, Any]]:
    """Generate a new image from a text prompt and return it as a workspace artifact.

    Use this when the user asks Nymeria to create, design, render, illustrate,
    or generate an image. Write the full final image prompt in `prompt`.
    The generated file is saved in the workspace and attached to the chat.
    Vision-capable chat models will also receive the generated image as native
    image input on the next reasoning step.

    Args:
        prompt: Full image generation prompt. Include subject, style, layout,
            text to render, composition, colors, and any important constraints.
        output_name: Optional short filename hint without an extension.

    Returns:
        A summary with an attached generated image file.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required.", {}

    user_id = get_user_id(config)
    tool_config = _resolve_config(user_id)
    provider = _choice(tool_config, "provider", {"openai", "gemini"})
    native_context_enabled = _bool_config(tool_config, "native_context_enabled")

    try:
        if provider == "openai":
            raw, mime_type, model = _generate_openai(prompt, tool_config)
        else:
            raw, mime_type, model = _generate_gemini(prompt, tool_config)

        path = _write_image_file(
            user_id=user_id,
            raw=raw,
            mime_type=mime_type,
            output_name=output_name,
            prompt=prompt,
        )
    except Exception as exc:
        logger.exception("Image generation failed via %s", provider)
        return f"[Error]: Image generation failed via {provider}: {exc}", {}

    content = (
        f"Generated image via {provider} ({model}) and saved it to {path}.\n"
        f"Prompt used: {prompt}\n"
        f"[attach:{path}]"
    )
    artifact = _native_artifact(
        path=path,
        prompt=prompt,
        provider=provider,
        model=model,
        mime_type=mime_type,
        native_context_enabled=native_context_enabled,
    )
    return content, artifact


IMAGE_GENERATION_TOOLS = [image_generate]

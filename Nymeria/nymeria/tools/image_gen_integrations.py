"""Per-provider image generation tools for Nymeria.

One opt-in tool per image-generation provider, mirroring the ``web_search_*``
suite so users can search for and swap between providers individually:

- ``image_gen_openai``    -> OpenAI GPT Image (gpt-image-2), current top-ranked
- ``image_gen_gemini``    -> Google Gemini "Nano Banana Pro" (gemini-3-pro-image)
- ``image_gen_flux``      -> Black Forest Labs FLUX.2 (async submit + poll)
- ``image_gen_replicate`` -> Replicate budget host (FLUX.1 schnell / Z-Image Turbo)
- ``image_gen_fal``       -> fal.ai budget host (Z-Image Turbo / FLUX.1 schnell)

Each tool resolves its API key through the credential vault (vault, then
settings, then env), generates an image, and returns it via the shared helpers
in ``image_generation.py`` (``finalize_image`` writes the workspace file, embeds
the ``[attach:]`` marker, and attaches the native-vision artifact). The
OpenAI/Gemini tools reuse the ``_generate_openai`` / ``_generate_gemini`` byte
producers; the URL-returning providers download bytes through the SSRF egress
policy via ``download_image_bytes``. ``tools/__init__.py`` folds
``IMAGE_GEN_INTEGRATION_TOOLS`` into ``CATALOG_TOOLS``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Annotated, Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .image_generation import (
    _generate_gemini,
    _generate_openai,
    download_image_bytes,
    finalize_image,
)

logger = logging.getLogger(__name__)

# Bounds the synchronous poll loop for async providers (~90s ceiling) so a slow
# job returns "[Error]: ... timed out" rather than hanging the tool call.
_POLL_MAX_ATTEMPTS = 45
_POLL_INTERVAL = 2.0

# --- Black Forest Labs (FLUX) ------------------------------------------------
_BFL_BASE_URL = "https://api.bfl.ai/v1"
# Friendly model name -> BFL endpoint slug.
_BFL_MODELS = {
    "flux-2-pro": "flux-2-pro",
    "flux-2-max": "flux-2-max",
    "flux-2-flex": "flux-2-flex",
    "flux-1.1-pro": "flux-pro-1.1",
    "flux-1.1-pro-ultra": "flux-pro-1.1-ultra",
}
_BFL_DEFAULT_MODEL = "flux-2-pro"
_BFL_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "9:21"}
_BFL_ERROR_STATUSES = {"Error", "Content Moderated", "Request Moderated", "Task not found"}

# --- Replicate (budget aggregator) -------------------------------------------
_REPLICATE_BASE_URL = "https://api.replicate.com/v1"
_REPLICATE_MODELS = {
    "flux-schnell": "black-forest-labs/flux-schnell",
    "z-image-turbo": "prunaai/z-image-turbo",
}
_REPLICATE_DEFAULT_MODEL = "flux-schnell"
_REPLICATE_ASPECT_RATIOS = {
    "1:1", "16:9", "21:9", "3:2", "2:3", "4:5", "5:4", "3:4", "4:3", "9:16", "9:21",
}

# --- fal.ai (budget aggregator) ----------------------------------------------
_FAL_BASE_URL = "https://fal.run"
_FAL_MODELS = {
    "z-image-turbo": "fal-ai/z-image/turbo",
    "flux-schnell": "fal-ai/flux/schnell",
}
_FAL_DEFAULT_MODEL = "z-image-turbo"
_FAL_IMAGE_SIZES = {
    "square_hd", "square", "portrait_4_3", "portrait_16_9",
    "landscape_4_3", "landscape_16_9",
}


# --- Shared helpers ----------------------------------------------------------
def _missing_key_error(label: str, provider: str, tool_name: str, env_var: str) -> str:
    return (
        f"[Error]: No {label} credential found. Set {env_var} or call "
        f'request_credential(provider="{provider}", '
        f'bind_target="native_tool:{tool_name}") to provision one.'
    )


def _vault_key(
    provider: str,
    aliases: tuple[str, ...],
    tool_name: str,
    config: Optional[RunnableConfig],
) -> Optional[str]:
    """Return a credential-vault API key for a native tool, or None."""
    from .native_credentials import get_native_credential_value

    cred = get_native_credential_value(
        provider=provider,
        provider_aliases=aliases,
        field_names=("api_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    )
    return cred.value if cred and cred.value else None


def _poll_json(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
    is_done: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """Poll a GET endpoint until ``is_done(json)`` is truthy.

    Bounds total wait to ~``_POLL_MAX_ATTEMPTS * _POLL_INTERVAL`` seconds.
    ``is_done`` may raise to signal a terminal provider error. Raises
    RuntimeError on timeout.
    """
    last: dict[str, Any] = {}
    for attempt in range(_POLL_MAX_ATTEMPTS):
        response = client.get(url, headers=headers)
        response.raise_for_status()
        last = response.json()
        if is_done(last):
            return last
        if attempt < _POLL_MAX_ATTEMPTS - 1:
            time.sleep(_POLL_INTERVAL)
    raise RuntimeError("image generation timed out while polling for the result")


# --- OpenAI ------------------------------------------------------------------
def _get_openai_image_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the OpenAI key: credential vault, then settings, then env.

    Note: in some deployments OPENAI_API_KEY is a CLIProxy gatekeeper value
    (cpx-*) that cannot call the real OpenAI image API, so a genuine OpenAI key
    supplied through the vault is the primary path for this tool.
    """
    key = _vault_key("openai", ("openai_api", "gpt_image"), "image_gen_openai", config)
    if key:
        return key
    from ..config import get_settings
    return get_settings().openai_api_key or os.environ.get("OPENAI_API_KEY")


@tool(response_format="content_and_artifact")
def image_gen_openai(
    prompt: str,
    size: Optional[str] = None,
    quality: Optional[str] = None,
    output_format: Optional[str] = None,
    model: Optional[str] = None,
    output_name: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict[str, Any]]:
    """Generate an image with OpenAI GPT Image (gpt-image-2), the current top-ranked text-to-image model.

    Best all-round prompt adherence, photorealism, and in-image text. Synchronous
    and a few seconds per image. For best-in-class infographics/diagrams or 4K,
    prefer image_gen_gemini; for the cheapest output, use image_gen_replicate or
    image_gen_fal.

    Args:
        prompt: Full image prompt (subject, style, layout, any text to render,
            composition, colors, constraints).
        size: "auto" (default), "1024x1024", "1536x1024", "1024x1536",
            "2048x2048", "2048x1152", "3840x2160", or "2160x3840".
        quality: "auto" (default), "low", "medium", or "high". Higher costs more.
        output_format: "png" (default), "jpeg", or "webp".
        model: "gpt-image-2" (default), "gpt-image-1.5", "gpt-image-1", or
            "gpt-image-1-mini".
        output_name: Optional short filename hint without an extension.

    Returns:
        A summary with the generated image attached, or "[Error]: <reason>".
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required.", {}

    api_key = _get_openai_image_api_key(config)
    if not api_key:
        return _missing_key_error("OpenAI", "openai", "image_gen_openai", "OPENAI_API_KEY"), {}

    cfg = {
        "openai_model": model,
        "openai_size": size,
        "openai_quality": quality,
        "openai_output_format": output_format,
        "openai_moderation": "auto",
    }
    try:
        raw, mime_type, used_model = _generate_openai(prompt, cfg, api_key=api_key)
        return finalize_image(
            raw=raw,
            mime_type=mime_type,
            provider="openai",
            model=used_model,
            prompt=prompt,
            output_name=output_name,
            config=config,
        )
    except Exception as exc:
        logger.exception("image_gen_openai failed")
        return f"[Error]: image_gen_openai failed: {exc}", {}


# --- Gemini ------------------------------------------------------------------
def _get_gemini_image_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Gemini key: credential vault, then settings, then env."""
    key = _vault_key("gemini", ("google_gemini", "genai"), "image_gen_gemini", config)
    if key:
        return key
    from ..config import get_settings
    return get_settings().gemini_api_key or os.environ.get("GEMINI_API_KEY")


@tool(response_format="content_and_artifact")
def image_gen_gemini(
    prompt: str,
    aspect_ratio: Optional[str] = None,
    image_size: Optional[str] = None,
    model: Optional[str] = None,
    output_name: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict[str, Any]]:
    """Generate an image with Google Gemini "Nano Banana Pro" (gemini-3-pro-image).

    Best in class for in-image text rendering, infographics/diagrams, 4K output,
    and real-world grounding. Synchronous. For general photorealism use
    image_gen_openai; for the cheapest output use image_gen_replicate or
    image_gen_fal.

    Args:
        prompt: Full image prompt (subject, style, any text to render, layout).
        aspect_ratio: "1:1" (default), "2:3", "3:2", "3:4", "4:3", "9:16",
            "16:9", or "21:9".
        image_size: "auto" (default), "1K", "2K", or "4K" (4K honored by the
            gemini-3 image models). 4K PNGs over 8 MB still attach but are not
            replayed to the model as native vision input.
        model: "gemini-3-pro-image-preview" (default),
            "gemini-3.1-flash-image-preview", or "gemini-2.5-flash-image".
        output_name: Optional short filename hint without an extension.

    Returns:
        A summary with the generated image attached, or "[Error]: <reason>".
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required.", {}

    api_key = _get_gemini_image_api_key(config)
    if not api_key:
        return _missing_key_error("Gemini", "gemini", "image_gen_gemini", "GEMINI_API_KEY"), {}

    cfg = {
        "gemini_model": model,
        "gemini_aspect_ratio": aspect_ratio,
        "gemini_image_size": image_size,
    }
    try:
        raw, mime_type, used_model = _generate_gemini(prompt, cfg, api_key=api_key)
        return finalize_image(
            raw=raw,
            mime_type=mime_type,
            provider="gemini",
            model=used_model,
            prompt=prompt,
            output_name=output_name,
            config=config,
        )
    except Exception as exc:
        logger.exception("image_gen_gemini failed")
        return f"[Error]: image_gen_gemini failed: {exc}", {}


# --- Black Forest Labs FLUX --------------------------------------------------
def _get_bfl_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Black Forest Labs key: credential vault, then settings, then env."""
    key = _vault_key("bfl", ("black_forest_labs", "flux"), "image_gen_flux", config)
    if key:
        return key
    from ..config import get_settings
    return get_settings().bfl_api_key or os.environ.get("BFL_API_KEY")


@tool(response_format="content_and_artifact")
def image_gen_flux(
    prompt: str,
    aspect_ratio: Optional[str] = None,
    model: Optional[str] = None,
    seed: Optional[int] = None,
    output_name: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict[str, Any]]:
    """Generate an image with Black Forest Labs FLUX.2, a flagship photorealism and multi-reference model.

    Asynchronous: the job is submitted and polled until ready (up to ~90s). Strong
    benchmark photorealism. For best in-image text prefer image_gen_gemini; for the
    cheapest output use image_gen_replicate or image_gen_fal.

    Args:
        prompt: Full image prompt.
        aspect_ratio: "1:1" (default), "16:9", "9:16", "4:3", "3:4", "3:2",
            "2:3", "21:9", or "9:21".
        model: "flux-2-pro" (default, recommended), "flux-2-max" (highest
            quality), "flux-2-flex", "flux-1.1-pro", or "flux-1.1-pro-ultra".
        seed: Optional integer seed for reproducibility.
        output_name: Optional short filename hint without an extension.

    Returns:
        A summary with the generated image attached, or "[Error]: <reason>".
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required.", {}

    api_key = _get_bfl_api_key(config)
    if not api_key:
        return _missing_key_error("Black Forest Labs", "bfl", "image_gen_flux", "BFL_API_KEY"), {}

    model_key = model if model in _BFL_MODELS else _BFL_DEFAULT_MODEL
    endpoint_slug = _BFL_MODELS[model_key]
    ar = aspect_ratio if aspect_ratio in _BFL_ASPECT_RATIOS else "1:1"
    payload: dict[str, Any] = {"prompt": prompt, "aspect_ratio": ar, "output_format": "png"}
    if seed is not None:
        payload["seed"] = seed

    submit_headers = {"x-key": api_key, "Content-Type": "application/json", "accept": "application/json"}
    poll_headers = {"x-key": api_key, "accept": "application/json"}

    def _is_ready(data: dict[str, Any]) -> bool:
        status = data.get("status")
        if status in _BFL_ERROR_STATUSES:
            raise RuntimeError(f"FLUX job {status}: {data.get('details') or ''}".strip())
        return status == "Ready"

    try:
        import httpx

        with httpx.Client(timeout=60.0) as client:
            submit = client.post(
                f"{_BFL_BASE_URL}/{endpoint_slug}", headers=submit_headers, json=payload
            )
            submit.raise_for_status()
            submitted = submit.json()
            polling_url = submitted.get("polling_url")
            if not polling_url:
                job_id = submitted.get("id")
                if not job_id:
                    return "[Error]: image_gen_flux returned no polling URL or job id.", {}
                polling_url = f"{_BFL_BASE_URL}/get_result?id={job_id}"
            result = _poll_json(client, polling_url, headers=poll_headers, is_done=_is_ready)

        sample_url = ((result or {}).get("result") or {}).get("sample")
        if not sample_url:
            return "[Error]: image_gen_flux returned no image URL.", {}
        raw, mime_type = download_image_bytes(sample_url)
        return finalize_image(
            raw=raw,
            mime_type=mime_type,
            provider="bfl",
            model=model_key,
            prompt=prompt,
            output_name=output_name,
            config=config,
        )
    except Exception as exc:
        logger.exception("image_gen_flux failed")
        return f"[Error]: image_gen_flux failed: {exc}", {}


# --- Replicate ---------------------------------------------------------------
def _get_replicate_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the Replicate token: credential vault, then settings, then env."""
    key = _vault_key("replicate", ("replicate_api", "r8"), "image_gen_replicate", config)
    if key:
        return key
    from ..config import get_settings
    return (
        get_settings().replicate_api_key
        or os.environ.get("REPLICATE_API_KEY")
        or os.environ.get("REPLICATE_API_TOKEN")
    )


@tool(response_format="content_and_artifact")
def image_gen_replicate(
    prompt: str,
    aspect_ratio: Optional[str] = None,
    model: Optional[str] = None,
    seed: Optional[int] = None,
    output_name: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict[str, Any]]:
    """Generate an image cheaply via Replicate (FLUX.1 schnell or Z-Image Turbo), a low-cost budget option.

    Roughly 10-20x cheaper than the flagship tools, good for drafts, thumbnails,
    concepts, and high volume. Uses a synchronous-wait request and falls back to
    polling (up to ~90s). For top quality use image_gen_openai, image_gen_gemini,
    or image_gen_flux.

    Args:
        prompt: Full image prompt.
        aspect_ratio: "1:1" (default), "16:9", "9:16", "3:2", "2:3", "4:5",
            "5:4", "3:4", "4:3", "9:16", or "21:9".
        model: "flux-schnell" (default, cheapest) or "z-image-turbo" (best
            quality-per-dollar; excellent at in-image text).
        seed: Optional integer seed for reproducibility.
        output_name: Optional short filename hint without an extension.

    Returns:
        A summary with the generated image attached, or "[Error]: <reason>".
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required.", {}

    api_key = _get_replicate_api_key(config)
    if not api_key:
        return _missing_key_error("Replicate", "replicate", "image_gen_replicate", "REPLICATE_API_KEY"), {}

    model_key = model if model in _REPLICATE_MODELS else _REPLICATE_DEFAULT_MODEL
    slug = _REPLICATE_MODELS[model_key]
    ar = aspect_ratio if aspect_ratio in _REPLICATE_ASPECT_RATIOS else "1:1"
    inputs: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": ar,
        "num_outputs": 1,
        "output_format": "png",
    }
    if seed is not None:
        inputs["seed"] = seed
    payload = {"input": inputs}

    submit_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "wait",
    }

    def _is_done(data: dict[str, Any]) -> bool:
        status = data.get("status")
        if status in {"failed", "canceled"}:
            raise RuntimeError(f"Replicate prediction {status}: {data.get('error') or ''}".strip())
        return status == "succeeded"

    try:
        import httpx

        with httpx.Client(timeout=120.0) as client:
            submit = client.post(
                f"{_REPLICATE_BASE_URL}/models/{slug}/predictions",
                headers=submit_headers,
                json=payload,
            )
            submit.raise_for_status()
            data = submit.json()
            if data.get("status") != "succeeded":
                get_url = (data.get("urls") or {}).get("get")
                if not get_url:
                    return "[Error]: image_gen_replicate returned no polling URL.", {}
                data = _poll_json(
                    client,
                    get_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    is_done=_is_done,
                )

        output = data.get("output")
        if isinstance(output, list):
            image_url = output[0] if output else None
        elif isinstance(output, str):
            image_url = output
        else:
            image_url = None
        if not image_url:
            return "[Error]: image_gen_replicate returned no image.", {}
        raw, mime_type = download_image_bytes(image_url)
        return finalize_image(
            raw=raw,
            mime_type=mime_type,
            provider="replicate",
            model=model_key,
            prompt=prompt,
            output_name=output_name,
            config=config,
        )
    except Exception as exc:
        logger.exception("image_gen_replicate failed")
        return f"[Error]: image_gen_replicate failed: {exc}", {}


# --- fal.ai ------------------------------------------------------------------
def _get_fal_api_key(config: Optional[RunnableConfig] = None) -> Optional[str]:
    """Resolve the fal.ai key: credential vault, then settings, then env."""
    key = _vault_key("fal", ("fal_ai", "falai"), "image_gen_fal", config)
    if key:
        return key
    from ..config import get_settings
    return (
        get_settings().fal_api_key
        or os.environ.get("FAL_API_KEY")
        or os.environ.get("FAL_KEY")
    )


@tool(response_format="content_and_artifact")
def image_gen_fal(
    prompt: str,
    image_size: Optional[str] = None,
    model: Optional[str] = None,
    seed: Optional[int] = None,
    output_name: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> tuple[str, dict[str, Any]]:
    """Generate an image cheaply via fal.ai (Z-Image Turbo or FLUX.1 schnell), a fast low-cost budget option.

    Roughly 10-20x cheaper than the flagship tools and typically sub-second to a
    few seconds. The fal endpoint waits for the result, so no polling. For top
    quality use image_gen_openai, image_gen_gemini, or image_gen_flux.

    Args:
        prompt: Full image prompt.
        image_size: "square_hd" (default), "square", "portrait_4_3",
            "portrait_16_9", "landscape_4_3", or "landscape_16_9".
        model: "z-image-turbo" (default, best quality-per-dollar) or
            "flux-schnell" (cheapest).
        seed: Optional integer seed for reproducibility.
        output_name: Optional short filename hint without an extension.

    Returns:
        A summary with the generated image attached, or "[Error]: <reason>".
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required.", {}

    api_key = _get_fal_api_key(config)
    if not api_key:
        return _missing_key_error("fal.ai", "fal", "image_gen_fal", "FAL_API_KEY"), {}

    model_key = model if model in _FAL_MODELS else _FAL_DEFAULT_MODEL
    slug = _FAL_MODELS[model_key]
    size = image_size if image_size in _FAL_IMAGE_SIZES else "square_hd"
    payload: dict[str, Any] = {"prompt": prompt, "image_size": size, "num_images": 1}
    if seed is not None:
        payload["seed"] = seed

    headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}

    try:
        import httpx

        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{_FAL_BASE_URL}/{slug}", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        images = data.get("images") or []
        first = images[0] if images and isinstance(images[0], dict) else {}
        image_url = first.get("url")
        if not image_url:
            return "[Error]: image_gen_fal returned no image.", {}
        raw, mime_type = download_image_bytes(image_url)
        return finalize_image(
            raw=raw,
            mime_type=mime_type,
            provider="fal",
            model=model_key,
            prompt=prompt,
            output_name=output_name,
            config=config,
        )
    except Exception as exc:
        logger.exception("image_gen_fal failed")
        return f"[Error]: image_gen_fal failed: {exc}", {}


# Opt-in per-provider image generation tools. tools/__init__.py folds this into
# CATALOG_TOOLS; metadata.py maps it to ToolCategory.IMAGE (security MODERATE).
IMAGE_GEN_INTEGRATION_TOOLS = [
    image_gen_openai,
    image_gen_gemini,
    image_gen_flux,
    image_gen_replicate,
    image_gen_fal,
]

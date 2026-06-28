"""Provider registry for LLM API compatibility.

Nymeria has historically treated most OpenAI-compatible services as the
``openai`` provider plus ``LLM_BASE_URL``.  This registry keeps the same runtime
adapter while giving first-class names, default endpoints, key env vars, and
documentation links to providers that speak the OpenAI Chat Completions shape.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, cast


logger = logging.getLogger(__name__)

ProviderTier = Literal["native", "gateway", "unverified"]

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover - python-dotenv is expected via pydantic.
    dotenv_values = None


ApiMode = str

# Route taxonomy — which adapter path a provider's traffic takes.
# - "native": dedicated langchain-<provider> partner package (or canonical
#   OpenAI / Anthropic API).
# - "openai_compat": the OpenAI Chat Completions shim (Nymeria's
#   ChatOpenAIWithReasoning subclass).
# Providers that have BOTH a partner package AND a working OpenAI-compat
# endpoint advertise both routes in `supported_routes`; users can toggle the
# route per thread or globally. Single-route providers hide the toggle in the
# picker UI.
#   anthropic_messages: route a gateway's Claude models through langchain-anthropic
#     against the gateway's own /v1/messages endpoint, for native thinking +
#     signature round-trip (gateways that drop the signature on their OpenAI-compat
#     path; see anthropic_native_for_claude).
ProviderRoute = Literal["native", "openai_compat", "anthropic_messages"]
_PROVIDER_ROUTE_VALUES: tuple[ProviderRoute, ...] = (
    "native",
    "openai_compat",
    "anthropic_messages",
)


@dataclass(frozen=True)
class LLMProviderSpec:
    """Runtime metadata for one LLM provider."""

    id: str
    label: str
    api_format: str
    default_base_url: str | None = None
    api_key_env_vars: tuple[str, ...] = ()
    base_url_env_vars: tuple[str, ...] = ()
    default_model: str | None = None
    default_api_mode: ApiMode = "chat_completions"
    supports_chat_completions: bool = True
    supports_responses: bool = False
    requires_api_key: bool = True
    requires_base_url: bool = False
    docs_url: str | None = None
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # Three-state classification surfaced in the picker UI.
    #   native    : dedicated langchain-<provider> partner package adopted, or
    #               canonical OpenAI/Anthropic API. Reasoning round-trips natively.
    #   gateway   : multiplexes many upstream providers behind an OpenAI-compatible
    #               surface. Reasoning depends on upstream provider.
    #   unverified: OpenAI-chat compatible but not smoke-tested. Tool-call deltas,
    #               response_format, finish_reason, usage shapes vary; reasoning
    #               may not round-trip across tool calls.
    tier: ProviderTier = "unverified"
    # Short warning shown under unverified picker rows. Empty for tiers that
    # don't need user warnings.
    notes_for_user: str = ""
    # Which adapter routes this provider can run through. Most entries have a
    # single route. Providers like google or ollama advertise both
    # ("native", "openai_compat") and the picker exposes a per-thread toggle.
    supported_routes: tuple[ProviderRoute, ...] = ("native",)
    # Default route when neither the per-thread override nor the global setting
    # is set. Must be a member of supported_routes.
    default_route: ProviderRoute = "native"
    # Base URL to use when route is forced to "openai_compat" for a provider
    # whose primary route is "native". Required for multi-route providers; left
    # as None for single-route entries that already store their base URL in
    # default_base_url.
    openai_compat_base_url: str | None = None
    # When True, the picker warns that this gateway's OpenAI-compatible path may
    # drop Claude's signed thinking blocks and offers a one-click switch to the
    # Anthropic-native route (provider=anthropic, same base URL / key). Set only
    # for gateways that serve Claude AND lose the signature on their compat path
    # (e.g. LiteLLM, whose /v1/messages endpoint handles thinking natively).
    # Gateways that round-trip it via reasoning_details (OpenRouter, Vercel,
    # AIHubMix) leave this False: their compat path is already signature-safe.
    anthropic_native_for_claude: bool = False
    # Base URL to use when route is "anthropic_messages": the gateway's Anthropic
    # Messages endpoint root (the Anthropic SDK appends /v1/messages). Differs
    # from the OpenAI base for most gateways (e.g. OpenCode .../zen, ZenMux
    # .../api/anthropic). None means "reuse the configured/default base URL"
    # (correct for proxies like LiteLLM whose root serves both surfaces).
    anthropic_messages_base_url: str | None = None

    @property
    def verified(self) -> bool:
        """Back-compat: a provider is verified if it has a known tier."""
        return self.tier in ("native", "gateway")

    @property
    def is_multi_route(self) -> bool:
        """True when the provider exposes more than one adapter path."""
        return len(self.supported_routes) > 1


def _spec(
    provider_id: str,
    label: str,
    *,
    base_url: str | None,
    env: Iterable[str] = (),
    base_url_env: Iterable[str] = (),
    default_model: str | None = None,
    docs_url: str | None = None,
    notes: str = "",
    aliases: Iterable[str] = (),
    supports_responses: bool = False,
    requires_api_key: bool = True,
    default_api_mode: ApiMode = "chat_completions",
    requires_base_url: bool = False,
    api_format: str = "openai_chat",
    supports_chat_completions: bool = True,
    tier: ProviderTier = "unverified",
    notes_for_user: str = "",
    supported_routes: Iterable[ProviderRoute] | None = None,
    default_route: ProviderRoute | None = None,
    openai_compat_base_url: str | None = None,
    anthropic_native_for_claude: bool = False,
    anthropic_messages_base_url: str | None = None,
) -> LLMProviderSpec:
    # Default route inference: native partner-package providers default to
    # ("native",), while generic OpenAI-compatible providers default to
    # ("openai_compat",). Multi-route entries pass supported_routes explicitly.
    if supported_routes is None:
        if provider_id in {"anthropic", "openai"} or api_format != "openai_chat":
            resolved_routes = ("native",)
        else:
            resolved_routes = ("openai_compat",)
    else:
        resolved_routes = tuple(supported_routes)
    if default_route is None:
        default_route = resolved_routes[0]
    if default_route not in resolved_routes:
        raise ValueError(
            f"default_route={default_route!r} not in supported_routes={resolved_routes!r} for provider {provider_id!r}"
        )
    return LLMProviderSpec(
        id=provider_id,
        label=label,
        api_format=api_format,
        default_base_url=base_url,
        api_key_env_vars=tuple(env),
        base_url_env_vars=tuple(base_url_env),
        default_model=default_model,
        docs_url=docs_url,
        notes=notes,
        aliases=tuple(aliases),
        supports_chat_completions=supports_chat_completions,
        supports_responses=supports_responses,
        requires_api_key=requires_api_key,
        default_api_mode=default_api_mode,
        requires_base_url=requires_base_url,
        tier=tier,
        notes_for_user=notes_for_user,
        supported_routes=resolved_routes,
        default_route=default_route,
        openai_compat_base_url=openai_compat_base_url,
        anthropic_native_for_claude=anthropic_native_for_claude,
        anthropic_messages_base_url=anthropic_messages_base_url,
    )


# Curated from Nymeria's existing providers, /opt/openclaw, /opt/hermes-agent,
# /opt/opencode, models.dev, and official provider documentation where linked.
# Providers with unusual endpoint roots intentionally keep the provider's
# documented OpenAI-compatible base URL instead of forcing a /v1 suffix.
_PROVIDER_SPECS: tuple[LLMProviderSpec, ...] = (
    _spec(
        "anthropic",
        "Anthropic",
        base_url="https://api.anthropic.com",
        env=("ANTHROPIC_DIRECT_API_KEY", "ANTHROPIC_API_KEY"),
        base_url_env=("ANTHROPIC_BASE_URL",),
        default_model="claude-sonnet-4-6",
        docs_url="https://docs.anthropic.com/en/api/messages",
        api_format="anthropic_messages",
        supports_chat_completions=False,
        aliases=("claude",),
        tier="native",
    ),
    _spec(
        "openai",
        "OpenAI",
        base_url="https://api.openai.com/v1",
        env=("OPENAI_API_KEY",),
        default_model="gpt-4o-mini",
        docs_url="https://platform.openai.com/docs/api-reference/chat/create",
        supports_responses=True,
        default_api_mode="responses",
        tier="native",
    ),
    _spec(
        "openrouter",
        "OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env=("OPENROUTER_API_KEY",),
        default_model="anthropic/claude-sonnet-4.5",
        docs_url="https://openrouter.ai/docs/api-reference/chat-completion",
        supports_responses=True,
        default_api_mode="responses",
        tier="gateway",
    ),
    _spec(
        "azure-openai",
        "Azure OpenAI",
        base_url=None,
        env=("AZURE_OPENAI_API_KEY", "AZURE_API_KEY"),
        base_url_env=("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_ENDPOINT"),
        docs_url="https://learn.microsoft.com/azure/ai-services/openai/quickstart",
        notes="Set LLM_BASE_URL to https://<resource>.openai.azure.com/openai/v1/ and use deployment names as model IDs.",
        aliases=("azure", "azure-openai-service"),
        supports_responses=True,
        default_api_mode="responses",
        requires_base_url=True,
    ),
    _spec(
        "azure-foundry",
        "Azure AI Foundry",
        base_url=None,
        env=("AZURE_FOUNDRY_API_KEY", "AZURE_OPENAI_AUTH_TOKEN", "AZURE_API_KEY"),
        base_url_env=("AZURE_FOUNDRY_BASE_URL",),
        docs_url="https://learn.microsoft.com/azure/foundry/foundry-models/concepts/endpoints",
        notes="Set base URL to https://<resource>.openai.azure.com/openai/v1/ or https://<resource>.services.ai.azure.com/openai/v1/.",
        aliases=("azure-ai-foundry", "azure-ai"),
        supports_responses=True,
        default_api_mode="responses",
        requires_base_url=True,
    ),
    _spec(
        "google",
        "Google Gemini",
        base_url=None,
        env=("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
        default_model="gemini-2.5-flash",
        docs_url="https://ai.google.dev/gemini-api/docs",
        aliases=("gemini", "google-gemini"),
        api_format="google_genai",
        supports_chat_completions=True,
        tier="native",
        # Multi-route: native via langchain-google-genai (default) or
        # OpenAI-compat shim at generativelanguage.googleapis.com/v1beta/openai.
        # Per-thread toggle lets users trade thought signatures for the
        # documented latency win on the compat path.
        supported_routes=("native", "openai_compat"),
        default_route="native",
        openai_compat_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    _spec(
        "google-vertex",
        "Google Vertex AI",
        base_url=None,
        env=("GOOGLE_VERTEX_ACCESS_TOKEN",),
        base_url_env=("GOOGLE_VERTEX_OPENAI_BASE_URL",),
        default_model="google/gemini-2.0-flash-001",
        docs_url="https://cloud.google.com/vertex-ai/generative-ai/docs/start/openai",
        notes="Requires a Google Cloud OAuth access token as the OpenAI api_key; Nymeria does not refresh ADC tokens for this provider yet.",
        aliases=("vertex", "vertex-ai", "google-vertex-ai"),
        requires_base_url=True,
        notes_for_user="Nymeria does not refresh Google Cloud ADC tokens automatically. For Gemini reasoning, prefer the Google Gemini provider.",
    ),
    _spec(
        "bedrock",
        "AWS Bedrock",
        base_url=None,
        env=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"),
        base_url_env=("AWS_BEDROCK_ENDPOINT_URL",),
        default_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        docs_url="https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html",
        notes="Uses boto3 default credential chain (env, ~/.aws/credentials, IAM role). Set AWS_REGION (or AWS_DEFAULT_REGION) before connecting.",
        aliases=("aws-bedrock", "aws"),
        api_format="bedrock_converse",
        supports_chat_completions=False,
        requires_api_key=False,
        tier="native",
    ),
    _spec(
        "xai",
        "xAI",
        base_url="https://api.x.ai/v1",
        env=("XAI_API_KEY",),
        default_model="grok-4",
        docs_url="https://docs.x.ai/docs/api-reference",
        aliases=("x-ai", "x.ai", "grok"),
        supports_responses=True,
        default_api_mode="responses",
        notes_for_user="Grok 4 / 4.1 reasoning is not surfaced beyond the initial Thinking token (langchain issue #35224).",
    ),
    _spec(
        "groq",
        "Groq",
        base_url="https://api.groq.com/openai/v1",
        env=("GROQ_API_KEY",),
        default_model="llama-3.3-70b-versatile",
        docs_url="https://console.groq.com/docs/responses-api",
        supports_responses=True,
        default_api_mode="responses",
        notes_for_user="Responses API (beta) is the default and round-trips reasoning on gpt-oss-20b/120b and qwen3-32b. Stateless: history is replayed each turn.",
    ),
    _spec(
        "deepseek",
        "DeepSeek",
        base_url="https://api.deepseek.com",
        env=("DEEPSEEK_API_KEY",),
        default_model="deepseek-chat",
        docs_url="https://api-docs.deepseek.com/",
        aliases=("deep-seek",),
        notes="DeepSeek also accepts https://api.deepseek.com/v1 as a compatibility alias.",
        notes_for_user="Reasoning round-trips over Chat Completions: reasoning_content is echoed back on tool-call turns (V4 thinking) and stripped on non-tool turns (deepseek-reasoner). Do not force tool_choice on V4 thinking (DeepSeek 400s; DeepSeek-V3#1376).",
    ),
    _spec(
        "mistral",
        "Mistral AI",
        base_url="https://api.mistral.ai/v1",
        env=("MISTRAL_API_KEY",),
        default_model="mistral-large-latest",
        docs_url="https://docs.mistral.ai/api/",
        notes_for_user="Magistral chain-of-thought ships in main content with no native reasoning block; round-trip across tool calls is not guaranteed.",
    ),
    _spec(
        "cohere",
        "Cohere",
        base_url="https://api.cohere.ai/compatibility/v1",
        env=("COHERE_API_KEY",),
        default_model="command-a-03-2025",
        docs_url="https://docs.cohere.com/v2/docs/compatibility-api",
        notes="OpenAI-compatible compatibility API; native Cohere APIs are separate.",
    ),
    _spec(
        "togetherai",
        "Together AI",
        base_url="https://api.together.ai/v1",
        env=("TOGETHER_API_KEY",),
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        docs_url="https://docs.together.ai/docs/inference/openai-compatibility",
        aliases=("together", "together-ai"),
        notes_for_user="When a reasoning model is selected, reasoning_content round-trips over Chat Completions on tool-call turns (Together returns it as reasoning and it is replayed as reasoning_content; stripped on plain turns). Enabling thinking is per-model; GLM-5 preserved thinking needs chat_template_kwargs clear_thinking=false.",
    ),
    _spec(
        "fireworks-ai",
        "Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        env=("FIREWORKS_API_KEY",),
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        docs_url="https://docs.fireworks.ai/guides/reasoning",
        aliases=("fireworks",),
        notes_for_user="When reasoning is enabled, reasoning_content round-trips over Chat Completions via reasoning_history=preserved (Qwen3, GPT-OSS, Kimi, MiniMax reasoning models).",
    ),
    _spec(
        "perplexity",
        "Perplexity",
        base_url="https://api.perplexity.ai",
        env=("PERPLEXITY_API_KEY",),
        default_model="sonar-pro",
        docs_url="https://docs.perplexity.ai/guides/chat-completions-sdk",
    ),
    _spec(
        "cerebras",
        "Cerebras",
        base_url="https://api.cerebras.ai/v1",
        env=("CEREBRAS_API_KEY",),
        default_model="gpt-oss-120b",
        docs_url="https://inference-docs.cerebras.ai/resources/openai",
    ),
    _spec(
        "sambanova",
        "SambaNova",
        base_url="https://api.sambanova.ai/v1",
        env=("SAMBANOVA_API_KEY",),
        docs_url="https://docs.sambanova.ai/docs/en/features/responses",
        supports_responses=True,
        default_api_mode="responses",
        notes_for_user="Responses API (GA) is the default and round-trips reasoning on gpt-oss-120b / MiniMax-M2.7. Stateless; avoid forcing tool_choice on gpt-oss-120b.",
    ),
    _spec(
        "nvidia",
        "NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        env=("NVIDIA_API_KEY",),
        default_model="nvidia/nemotron-3-super-120b-a12b",
        docs_url="https://docs.nvidia.com/nim/large-language-models/latest/reference/api-reference.html",
        aliases=("nim", "nvidia-nim", "build-nvidia"),
        supports_responses=True,
        default_api_mode="responses",
        notes_for_user="Responses API (GA on the hosted endpoint) is the default and round-trips reasoning (Nemotron, DeepSeek, Qwen). Self-hosted NIM needs a recent version with /v1/responses.",
    ),
    _spec(
        "huggingface",
        "Hugging Face Inference Providers",
        base_url="https://router.huggingface.co/v1",
        env=("HF_TOKEN", "HUGGINGFACE_API_KEY"),
        default_model="deepseek-ai/DeepSeek-R1",
        docs_url="https://huggingface.co/docs/inference-providers/index",
        aliases=("hf", "hugging-face", "huggingface-hub"),
        notes_for_user="Reasoning and tool-call behavior depend on the upstream Hugging Face Inference Provider routing.",
    ),
    _spec(
        "deepinfra",
        "DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        env=("DEEPINFRA_API_KEY",),
        docs_url="https://deepinfra.com/docs/openai_api",
        aliases=("deep-infra",),
    ),
    _spec(
        "moonshotai",
        "Moonshot AI / Kimi",
        base_url="https://api.moonshot.ai/v1",
        env=("MOONSHOT_API_KEY",),
        default_model="kimi-k2.5",
        docs_url="https://platform.kimi.ai/docs/api/overview",
        aliases=("moonshot", "kimi"),
        notes_for_user="When reasoning is enabled, reasoning_content round-trips over Chat Completions via thinking.keep=all (Kimi K2 Thinking / K2.6).",
    ),
    _spec(
        "moonshotai-cn",
        "Moonshot AI / Kimi China",
        base_url="https://api.moonshot.cn/v1",
        env=("MOONSHOT_API_KEY",),
        default_model="kimi-k2.5",
        docs_url="https://platform.moonshot.cn/docs/",
    ),
    _spec(
        "aihubmix",
        "AIHubMix",
        base_url="https://aihubmix.com/v1",
        env=("AIHUBMIX_API_KEY",),
        docs_url="https://docs.aihubmix.com/en/index",
        notes="OpenAI-compatible routing gateway; https://api.aihubmix.com is the documented backup host.",
        notes_for_user="Reasoning round-trips over Chat Completions via OpenRouter-style reasoning_details (signatures preserved); interleaved thinking auto-enables for Claude.",
        tier="gateway",
    ),
    _spec(
        "alibaba",
        "Alibaba Cloud Model Studio",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env=("DASHSCOPE_API_KEY", "ALIBABA_API_KEY"),
        default_model="qwen-plus",
        docs_url="https://www.alibabacloud.com/help/model-studio/use-qwen-by-calling-api",
        aliases=("dashscope", "qwen", "modelstudio", "alibaba-cloud"),
        notes_for_user="When reasoning is enabled, enable_thinking turns on Qwen3.x thinking and reasoning_content round-trips over Chat Completions on tool-call turns (stripped on plain turns; tool_choice stays auto/none while thinking).",
    ),
    _spec(
        "alibaba-cn",
        "Alibaba Cloud Model Studio China",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env=("DASHSCOPE_API_KEY", "ALIBABA_API_KEY"),
        default_model="qwen-plus",
        docs_url="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        notes_for_user="When reasoning is enabled, enable_thinking turns on Qwen3.x thinking and reasoning_content round-trips over Chat Completions on tool-call turns.",
    ),
    _spec(
        "alibaba-coding-plan",
        "Alibaba Cloud Coding Plan",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        env=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_env=("ALIBABA_CODING_PLAN_BASE_URL",),
        default_model="qwen3.5-plus",
        docs_url="https://www.alibabacloud.com/help/en/model-studio/other-tools-coding-plan",
        notes="Coding Plan API keys are intended for interactive coding tools; use the dedicated Coding Plan key tier.",
        aliases=("alibaba-coding", "alibaba_coding", "dashscope-coding", "qwen-coding", "qwen-coding-plan"),
        notes_for_user="When reasoning is enabled, enable_thinking turns on Qwen3.x thinking and reasoning_content round-trips over Chat Completions on tool-call turns.",
    ),
    _spec(
        "alibaba-coding-plan-cn",
        "Alibaba Cloud Coding Plan China",
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        env=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_env=("ALIBABA_CODING_PLAN_CN_BASE_URL",),
        default_model="qwen3.5-plus",
        docs_url="https://help.aliyun.com/zh/model-studio/coding-plan",
        notes="China-region Coding Plan endpoint.",
        notes_for_user="When reasoning is enabled, enable_thinking turns on Qwen3.x thinking and reasoning_content round-trips over Chat Completions on tool-call turns.",
    ),
    _spec(
        "qwen-oauth",
        "Qwen Portal",
        base_url="https://portal.qwen.ai/v1",
        env=("QWEN_API_KEY",),
        base_url_env=("QWEN_BASE_URL",),
        default_model="qwen3.5-plus",
        docs_url="https://docs.qwencloud.com/coding-plan/tools/cline",
        notes="Hermes reference provider for Qwen portal/OAuth flows. Prefer Alibaba Coding Plan for normal API-key setup.",
        aliases=("qwen-portal", "qwen-cli"),
        notes_for_user="When reasoning is enabled, enable_thinking turns on Qwen3.x thinking and reasoning_content round-trips over Chat Completions on tool-call turns.",
    ),
    _spec(
        "zai",
        "Z.ai",
        base_url="https://api.z.ai/api/paas/v4",
        env=("ZAI_API_KEY", "Z_AI_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"),
        default_model="glm-4.7",
        docs_url="https://docs.z.ai/",
        aliases=("z.ai", "z-ai", "glm"),
    ),
    _spec(
        "zhipuai",
        "Zhipu AI BigModel",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        env=("ZHIPU_API_KEY", "GLM_API_KEY"),
        default_model="glm-4-plus",
        docs_url="https://open.bigmodel.cn/dev/api",
        aliases=("zhipu", "bigmodel"),
    ),
    _spec(
        "qianfan",
        "Baidu Qianfan",
        base_url="https://qianfan.baidubce.com/v2",
        env=("QIANFAN_API_KEY",),
        default_model="deepseek-v3.2",
        docs_url="https://cloud.baidu.com/doc/WENXINWORKSHOP/",
        aliases=("baidu", "baidu-qianfan"),
    ),
    _spec(
        "stepfun",
        "StepFun",
        base_url="https://api.stepfun.ai/v1",
        env=("STEPFUN_API_KEY",),
        base_url_env=("STEPFUN_BASE_URL",),
        default_model="step-3.5-flash",
        docs_url="https://platform.stepfun.ai/docs/en/guides/developer/openai",
        aliases=("step",),
    ),
    _spec(
        "stepfun-plan",
        "StepFun Step Plan",
        base_url="https://api.stepfun.ai/step_plan/v1",
        env=("STEPFUN_API_KEY",),
        base_url_env=("STEPFUN_PLAN_BASE_URL",),
        default_model="step-3.5-flash",
        docs_url="https://docs.openclaw.ai/providers/stepfun",
        notes="OpenClaw/Hermes coding-plan endpoint. Standard StepFun uses https://api.stepfun.ai/v1.",
        aliases=("stepfun-coding-plan", "step-plan"),
    ),
    _spec(
        "volcengine",
        "Volcengine Ark",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        env=("VOLCANO_ENGINE_API_KEY", "ARK_API_KEY", "VOLCENGINE_API_KEY"),
        default_model="doubao-seed-1-8-251228",
        docs_url="https://www.volcengine.com/docs/82379",
        aliases=("doubao", "ark"),
    ),
    _spec(
        "volcengine-coding-plan",
        "Volcengine Ark Coding Plan",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        env=("VOLCANO_ENGINE_API_KEY", "ARK_API_KEY", "VOLCENGINE_API_KEY"),
        base_url_env=("VOLCENGINE_CODING_BASE_URL", "ARK_CODING_BASE_URL"),
        default_model="ark-code-latest",
        docs_url="https://docs.openclaw.ai/providers/volcengine",
        notes="OpenClaw coding-plan endpoint for Doubao/Ark coding models.",
        aliases=("doubao-coding", "ark-coding", "volcengine-coding"),
    ),
    _spec(
        "byteplus",
        "BytePlus ModelArk",
        base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
        env=("BYTEPLUS_API_KEY", "ARK_API_KEY"),
        base_url_env=("BYTEPLUS_BASE_URL",),
        default_model="seed-1-8-251228",
        docs_url="https://docs.byteplus.com/en/docs/modelark/1330626",
        notes="BytePlus ModelArk OpenAI-compatible endpoint for international regions.",
        aliases=("byteplus-ark",),
    ),
    _spec(
        "byteplus-coding-plan",
        "BytePlus ModelArk Coding Plan",
        base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        env=("BYTEPLUS_API_KEY", "ARK_API_KEY"),
        base_url_env=("BYTEPLUS_CODING_BASE_URL",),
        default_model="ark-code-latest",
        docs_url="https://docs.byteplus.com/en/docs/ModelArk/2188959",
        notes="OpenClaw-compatible BytePlus coding endpoint; do not use the standard ModelArk base URL for coding-plan tools.",
        aliases=("byteplus-coding", "byteplus-ark-coding"),
    ),
    _spec(
        "tencent-tokenhub",
        "Tencent TokenHub",
        base_url="https://tokenhub.tencentmaas.com/v1",
        env=("TENCENT_TOKENHUB_API_KEY",),
        docs_url="https://cloud.tencent.com/document/product/1729",
        aliases=("tencent", "tokenhub", "tencent-cloud"),
    ),
    _spec(
        "novita-ai",
        "Novita AI",
        base_url="https://api.novita.ai/openai",
        env=("NOVITA_API_KEY",),
        docs_url="https://novita.ai/docs/api-reference/model-api",
        aliases=("novita",),
        notes_for_user="When a reasoning model is selected, reasoning_content round-trips over Chat Completions on tool-call turns (Novita does not auto-carry reasoning, so it is replayed from history; stripped on plain turns).",
    ),
    _spec(
        "siliconflow",
        "SiliconFlow",
        base_url="https://api.siliconflow.com/v1",
        env=("SILICONFLOW_API_KEY",),
        docs_url="https://docs.siliconflow.com/",
    ),
    _spec(
        "siliconflow-cn",
        "SiliconFlow China",
        base_url="https://api.siliconflow.cn/v1",
        env=("SILICONFLOW_CN_API_KEY", "SILICONFLOW_API_KEY"),
        docs_url="https://docs.siliconflow.cn/",
    ),
    _spec(
        "arcee",
        "Arcee AI",
        base_url="https://api.arcee.ai/api/v1",
        env=("ARCEEAI_API_KEY", "ARCEE_API_KEY"),
        default_model="trinity-large-thinking",
        docs_url="https://docs.arcee.ai/",
    ),
    _spec(
        "chutes",
        "Chutes",
        base_url="https://llm.chutes.ai/v1",
        env=("CHUTES_API_KEY", "CHUTES_OAUTH_TOKEN"),
        default_model="zai-org/GLM-4.7-TEE",
        docs_url="https://chutes.ai/docs",
    ),
    _spec(
        "venice",
        "Venice AI",
        base_url="https://api.venice.ai/api/v1",
        env=("VENICE_API_KEY",),
        default_model="kimi-k2-5",
        docs_url="https://docs.venice.ai/",
    ),
    _spec(
        "requesty",
        "Requesty",
        base_url="https://router.requesty.ai/v1",
        env=("REQUESTY_API_KEY",),
        docs_url="https://docs.requesty.ai/",
        tier="gateway",
        notes_for_user="For Claude models the Chat Completions path exposes reasoning as a plain reasoning_content string (signature dropped); set this thread's route to Anthropic Messages for native thinking via the gateway /v1/messages endpoint.",
        supported_routes=("openai_compat", "anthropic_messages"),
        anthropic_native_for_claude=True,
        anthropic_messages_base_url="https://router.requesty.ai",
    ),
    _spec(
        "poe",
        "Poe",
        base_url="https://api.poe.com/v1",
        env=("POE_API_KEY",),
        docs_url="https://developer.poe.com/server-bots/accessing-other-bots-on-poe",
        tier="gateway",
        notes_for_user="Chat Completions exposes no reasoning for Claude (Poe scopes thinking to its Anthropic endpoint); set this thread's route to Anthropic Messages for native thinking. Only official Anthropic models are callable this way, not custom Poe bots.",
        supported_routes=("openai_compat", "anthropic_messages"),
        anthropic_native_for_claude=True,
        anthropic_messages_base_url="https://api.poe.com",
    ),
    _spec(
        "github-models",
        "GitHub Models",
        base_url="https://models.github.ai/inference",
        env=("GITHUB_TOKEN",),
        docs_url="https://docs.github.com/github-models/prototyping-with-ai-models",
        aliases=("github",),
    ),
    _spec(
        "github-copilot",
        "GitHub Copilot",
        base_url="https://api.githubcopilot.com",
        env=("COPILOT_GITHUB_TOKEN", "GITHUB_COPILOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        docs_url="https://docs.github.com/copilot",
        notes="Reference-provider support from models.dev/Hermes. Requires a Copilot bearer token; a normal GitHub PAT may not be sufficient.",
        aliases=("copilot", "github-copilot-chat"),
    ),
    _spec(
        "cloudflare-workers-ai",
        "Cloudflare Workers AI",
        base_url="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        env=("CLOUDFLARE_API_KEY", "CLOUDFLARE_API_TOKEN"),
        base_url_env=("CLOUDFLARE_WORKERS_AI_BASE_URL",),
        docs_url="https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/",
        notes="Default URL requires CLOUDFLARE_ACCOUNT_ID expansion.",
    ),
    _spec(
        "cloudflare-ai-gateway",
        "Cloudflare AI Gateway",
        base_url=None,
        env=("CLOUDFLARE_API_TOKEN",),
        base_url_env=("CLOUDFLARE_AI_GATEWAY_BASE_URL",),
        docs_url="https://developers.cloudflare.com/ai-gateway/",
        requires_base_url=True,
        tier="gateway",
    ),
    _spec(
        "vercel",
        "Vercel AI Gateway",
        base_url="https://ai-gateway.vercel.sh/v1",
        env=("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"),
        docs_url="https://vercel.com/docs/ai-gateway/sdks-and-apis/responses",
        aliases=("vercel-ai-gateway", "ai-gateway"),
        supports_responses=True,
        default_api_mode="responses",
        notes_for_user="Defaults to the Responses API. Reasoning also round-trips over Chat Completions via OpenRouter-style reasoning_details (higher-confidence, signature-preserving) if you switch the thread to chat_completions.",
        tier="gateway",
    ),
    _spec(
        "opencode",
        "OpenCode Zen",
        base_url="https://opencode.ai/zen/v1",
        env=("OPENCODE_API_KEY",),
        docs_url="https://opencode.ai/docs",
        aliases=("opencode-zen", "zen"),
        tier="gateway",
        notes_for_user="For Claude models the Chat Completions path exposes reasoning as a plain string (signature dropped); set this thread's route to Anthropic Messages for native thinking via the gateway /v1/messages endpoint.",
        supported_routes=("openai_compat", "anthropic_messages"),
        anthropic_native_for_claude=True,
        anthropic_messages_base_url="https://opencode.ai/zen",
    ),
    _spec(
        "opencode-go",
        "OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        env=("OPENCODE_API_KEY",),
        docs_url="https://opencode.ai/docs",
        tier="gateway",
    ),
    _spec(
        "kilocode",
        "Kilo Code Gateway",
        base_url="https://api.kilo.ai/api/gateway",
        env=("KILOCODE_API_KEY", "KILO_API_KEY"),
        default_model="kilo/auto",
        docs_url="https://kilocode.ai/docs",
        aliases=("kilo", "kilo-code"),
        tier="gateway",
    ),
    _spec(
        "gmi",
        "GMI Cloud",
        base_url="https://api.gmi-serving.com/v1",
        env=("GMI_API_KEY",),
        base_url_env=("GMI_BASE_URL",),
        docs_url="https://docs.gmicloud.ai/inference-engine/api-reference/llm-api-reference",
        notes="OpenAI-compatible multi-model endpoint. Some accounts require an X-Organization-ID header, which Nymeria does not expose yet.",
        aliases=("gmi-cloud", "gmicloud"),
    ),
    _spec(
        "nous",
        "Nous Research",
        base_url="https://inference.nousresearch.com/v1",
        env=("NOUS_API_KEY",),
        base_url_env=("NOUS_BASE_URL",),
        default_model="hermes-3-405b",
        docs_url="https://portal.nousresearch.com/help",
        notes="Hermes reference provider. Some accounts use OAuth/device-code credentials rather than static API keys.",
        aliases=("nous-portal", "nousresearch"),
    ),
    _spec(
        "v0",
        "Vercel v0",
        base_url="https://api.v0.dev/v1",
        env=("V0_API_KEY",),
        default_model="v0-1.5-md",
        docs_url="https://vercel.com/docs/v0/api",
        notes="OpenAI-compatible coding/design model API; requires a v0 plan with API access.",
    ),
    _spec(
        "ollama",
        "Ollama local",
        base_url="http://localhost:11434",
        env=("OLLAMA_API_KEY",),
        base_url_env=("OLLAMA_BASE_URL",),
        docs_url="https://github.com/ollama/ollama/blob/main/docs/api.md",
        notes="Defaults to Ollama's native /api/chat protocol for reasoning round-trip. Use provider_route=openai_compat for /v1/chat/completions compatibility.",
        aliases=("ollama-native", "ollama_native"),
        api_format="ollama_native",
        supports_chat_completions=True,
        requires_api_key=False,
        tier="native",
        supported_routes=("native", "openai_compat"),
        default_route="native",
        openai_compat_base_url="http://localhost:11434/v1",
    ),
    _spec(
        "ollama-cloud",
        "Ollama Cloud",
        base_url="https://ollama.com/v1",
        env=("OLLAMA_API_KEY",),
        docs_url="https://ollama.com/blog/openai-compatibility",
    ),
    _spec(
        "lmstudio",
        "LM Studio",
        base_url="http://127.0.0.1:1234/v1",
        env=("LMSTUDIO_API_KEY", "LM_API_KEY"),
        base_url_env=("LMSTUDIO_BASE_URL", "LM_BASE_URL"),
        docs_url="https://lmstudio.ai/docs/app/api/endpoints/openai",
        aliases=("lm-studio", "lm_studio"),
        requires_api_key=False,
    ),
    _spec(
        "llamacpp",
        "llama.cpp server",
        base_url="http://localhost:8080/v1",
        env=("LLAMACPP_API_KEY",),
        base_url_env=("LLAMACPP_BASE_URL", "LLAMA_CPP_BASE_URL"),
        docs_url="https://github.com/ggml-org/llama.cpp/tree/master/tools/server",
        aliases=("llama.cpp", "llama-cpp"),
        requires_api_key=False,
    ),
    _spec(
        "vllm",
        "vLLM OpenAI server",
        base_url="http://localhost:8000/v1",
        env=("VLLM_API_KEY",),
        base_url_env=("VLLM_BASE_URL",),
        docs_url="https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
        requires_api_key=False,
    ),
    _spec(
        "localai",
        "LocalAI",
        base_url="http://localhost:8080/v1",
        env=("LOCALAI_API_KEY",),
        base_url_env=("LOCALAI_BASE_URL",),
        docs_url="https://localai.io/features/openai-functions/",
        requires_api_key=False,
    ),
    _spec(
        "litellm",
        "LiteLLM Proxy",
        base_url="http://localhost:4000",
        env=("LITELLM_API_KEY",),
        base_url_env=("LITELLM_BASE_URL",),
        docs_url="https://docs.litellm.ai/docs/proxy/user_keys",
        requires_api_key=False,
        tier="gateway",
        notes_for_user="Reasoning round-trips over Chat Completions: the normalized reasoning_content is echoed on tool-call turns and stripped on plain turns. For Claude models, the Chat Completions path drops the signed thinking blocks; set this thread's route to Anthropic Messages to get native thinking via the proxy /v1/messages endpoint.",
        supported_routes=("openai_compat", "anthropic_messages"),
        anthropic_native_for_claude=True,
    ),
    _spec(
        "tgi",
        "Hugging Face TGI",
        base_url="http://localhost:8080/v1",
        env=("TGI_API_KEY",),
        base_url_env=("TGI_BASE_URL", "TEXT_GENERATION_INFERENCE_BASE_URL"),
        docs_url="https://huggingface.co/docs/text-generation-inference/en/basic_tutorials/using_guidance",
        requires_api_key=False,
    ),
)


# models.dev long-tail OpenAI-compatible providers.  They all use the same
# OpenAI-compatible runtime path.  Keep these compact: detailed caveats live in
# docs/chat_completions_providers.md.
_LONG_TAIL_SPECS: tuple[LLMProviderSpec, ...] = (
    _spec("302ai", "302.AI", base_url="https://api.302.ai/v1", env=("302AI_API_KEY",)),
    _spec("abacus", "Abacus", base_url="https://routellm.abacus.ai/v1", env=("ABACUS_API_KEY",)),
    _spec("abliteration-ai", "abliteration.ai", base_url="https://api.abliteration.ai/v1", env=("ABLIT_KEY",)),
    _spec("ambient", "Ambient", base_url="https://api.ambient.xyz/v1", env=("AMBIENT_API_KEY",)),
    _spec("auriko", "Auriko", base_url="https://api.auriko.ai/v1", env=("AURIKO_API_KEY",)),
    _spec("bailing", "Bailing", base_url="https://api.tbox.cn/api/llm/v1", env=("BAILING_API_TOKEN",)),
    _spec(
        "baseten",
        "Baseten",
        base_url="https://inference.baseten.co/v1",
        env=("BASETEN_API_KEY",),
        notes_for_user="Reasoning round-trips over Chat Completions: reasoning_content is echoed back on tool-call turns (DeepSeek V4 / GPT-OSS 400 otherwise) and stripped on non-tool turns. Enabling thinking is per-model (reasoning_effort or chat_template_args); no blanket toggle is set.",
    ),
    _spec("berget", "Berget.AI", base_url="https://api.berget.ai/v1", env=("BERGET_API_KEY",)),
    _spec("clarifai", "Clarifai", base_url="https://api.clarifai.com/v2/ext/openai/v1", env=("CLARIFAI_PAT",)),
    _spec("claudinio", "Claudinio", base_url="https://api.claudin.io/v1", env=("CLAUDINIO_API_KEY",)),
    _spec("cloudferro-sherlock", "CloudFerro Sherlock", base_url="https://api-sherlock.cloudferro.com/openai/v1", env=("CLOUDFERRO_SHERLOCK_API_KEY",)),
    _spec("cortecs", "Cortecs", base_url="https://api.cortecs.ai/v1", env=("CORTECS_API_KEY",)),
    _spec("databricks", "Databricks", base_url="https://${DATABRICKS_HOST}/ai-gateway/mlflow/v1", env=("DATABRICKS_TOKEN",), base_url_env=("DATABRICKS_OPENAI_BASE_URL",)),
    _spec("digitalocean", "DigitalOcean", base_url="https://inference.do-ai.run/v1", env=("DIGITALOCEAN_ACCESS_TOKEN",)),
    _spec("dinference", "DInference", base_url="https://api.dinference.com/v1", env=("DINFERENCE_API_KEY",)),
    _spec("drun", "D.Run", base_url="https://chat.d.run/v1", env=("DRUN_API_KEY",)),
    _spec("evroc", "evroc", base_url="https://models.think.evroc.com/v1", env=("EVROC_API_KEY",)),
    _spec(
        "fastrouter",
        "FastRouter",
        base_url="https://go.fastrouter.ai/api/v1",
        env=("FASTROUTER_API_KEY",),
        tier="gateway",
        notes_for_user="For Claude models the Chat Completions path exposes reasoning as a plain reasoning string (signature dropped); set this thread's route to Anthropic Messages for native thinking via the gateway /v1/messages endpoint.",
        supported_routes=("openai_compat", "anthropic_messages"),
        anthropic_native_for_claude=True,
        anthropic_messages_base_url="https://api.fastrouter.ai",
    ),
    _spec("firepass", "Fireworks FirePass", base_url="https://api.fireworks.ai/inference/v1", env=("FIREPASS_API_KEY",)),
    _spec("friendli", "Friendli", base_url="https://api.friendli.ai/serverless/v1", env=("FRIENDLI_TOKEN",)),
    _spec("frogbot", "FrogBot", base_url="https://app.frogbot.ai/api/v1", env=("FROGBOT_API_KEY",)),
    _spec("helicone", "Helicone AI Gateway", base_url="https://ai-gateway.helicone.ai/v1", env=("HELICONE_API_KEY",), tier="gateway"),
    _spec("hpc-ai", "HPC-AI", base_url="https://api.hpc-ai.com/inference/v1", env=("HPC_AI_API_KEY",)),
    _spec("iflowcn", "iFlow", base_url="https://apis.iflow.cn/v1", env=("IFLOW_API_KEY",)),
    _spec("inception", "Inception", base_url="https://api.inceptionlabs.ai/v1", env=("INCEPTION_API_KEY",)),
    _spec("inference", "Inference.net", base_url="https://inference.net/v1", env=("INFERENCE_API_KEY",)),
    _spec("io-net", "IO.NET", base_url="https://api.intelligence.io.solutions/api/v1", env=("IOINTELLIGENCE_API_KEY",)),
    _spec("jiekou", "Jiekou.AI", base_url="https://api.jiekou.ai/openai", env=("JIEKOU_API_KEY",)),
    _spec("kuae-cloud-coding-plan", "KUAE Cloud Coding Plan", base_url="https://coding-plan-endpoint.kuaecloud.net/v1", env=("KUAE_API_KEY",)),
    _spec("llama", "Meta Llama API", base_url="https://api.llama.com/compat/v1", env=("LLAMA_API_KEY",)),
    _spec("llmgateway", "LLM Gateway", base_url="https://api.llmgateway.io/v1", env=("LLMGATEWAY_API_KEY",), tier="gateway"),
    _spec("lucidquery", "LucidQuery AI", base_url="https://lucidquery.com/api/v1", env=("LUCIDQUERY_API_KEY",)),
    _spec("meganova", "Meganova", base_url="https://api.meganova.ai/v1", env=("MEGANOVA_API_KEY",)),
    _spec("mixlayer", "Mixlayer", base_url="https://models.mixlayer.ai/v1", env=("MIXLAYER_API_KEY",)),
    _spec("moark", "Moark", base_url="https://moark.com/v1", env=("MOARK_API_KEY",)),
    _spec("modelscope", "ModelScope", base_url="https://api-inference.modelscope.cn/v1", env=("MODELSCOPE_API_KEY",)),
    _spec("morph", "Morph", base_url="https://api.morphllm.com/v1", env=("MORPH_API_KEY",)),
    _spec("nano-gpt", "NanoGPT", base_url="https://nano-gpt.com/api/v1", env=("NANO_GPT_API_KEY",)),
    _spec("nebius", "Nebius Token Factory", base_url="https://api.tokenfactory.nebius.com/v1", env=("NEBIUS_API_KEY",)),
    _spec("neuralwatt", "Neuralwatt", base_url="https://api.neuralwatt.com/v1", env=("NEURALWATT_API_KEY",)),
    _spec("nova", "Amazon Nova", base_url="https://api.nova.amazon.com/v1", env=("NOVA_API_KEY",)),
    _spec("ovhcloud", "OVHcloud AI Endpoints", base_url="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", env=("OVHCLOUD_API_KEY",)),
    _spec("perplexity-agent", "Perplexity Agent", base_url="https://api.perplexity.ai/v1", env=("PERPLEXITY_API_KEY",)),
    _spec("privatemode-ai", "Privatemode AI", base_url="http://localhost:8080/v1", env=("PRIVATEMODE_API_KEY",), base_url_env=("PRIVATEMODE_ENDPOINT",)),
    _spec("qihang-ai", "QiHang", base_url="https://api.qhaigc.net/v1", env=("QIHANG_API_KEY",)),
    _spec("qiniu-ai", "Qiniu", base_url="https://api.qnaigc.com/v1", env=("QINIU_API_KEY",)),
    _spec("regolo-ai", "Regolo AI", base_url="https://api.regolo.ai/v1", env=("REGOLO_API_KEY",)),
    _spec("sarvam", "Sarvam AI", base_url="https://api.sarvam.ai/v1", env=("SARVAM_API_KEY",)),
    _spec("scaleway", "Scaleway", base_url="https://api.scaleway.ai/v1", env=("SCALEWAY_API_KEY",)),
    _spec("stackit", "STACKIT", base_url="https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1", env=("STACKIT_API_KEY",)),
    _spec("submodel", "submodel", base_url="https://llm.submodel.ai/v1", env=("SUBMODEL_INSTAGEN_ACCESS_KEY",)),
    _spec("synthetic", "Synthetic", base_url="https://api.synthetic.new/openai/v1", env=("SYNTHETIC_API_KEY",)),
    _spec("tencent-coding-plan", "Tencent Coding Plan", base_url="https://api.lkeap.cloud.tencent.com/coding/v3", env=("TENCENT_CODING_PLAN_API_KEY",)),
    _spec("the-grid-ai", "The Grid AI", base_url="https://api.thegrid.ai/v1", env=("THEGRIDAI_API_KEY",)),
    _spec("upstage", "Upstage", base_url="https://api.upstage.ai/v1/solar", env=("UPSTAGE_API_KEY",)),
    _spec("vivgrid", "Vivgrid", base_url="https://api.vivgrid.com/v1", env=("VIVGRID_API_KEY",)),
    _spec("vultr", "Vultr", base_url="https://api.vultrinference.com/v1", env=("VULTR_API_KEY",)),
    _spec("wafer.ai", "Wafer", base_url="https://pass.wafer.ai/v1", env=("WAFER_API_KEY",)),
    _spec("wandb", "Weights & Biases Inference", base_url="https://api.inference.wandb.ai/v1", env=("WANDB_API_KEY",)),
    _spec("xiaomi", "Xiaomi MiMo", base_url="https://api.xiaomimimo.com/v1", env=("XIAOMI_API_KEY",), default_model="mimo-v2-flash"),
    _spec("xiaomi-token-plan-ams", "Xiaomi Token Plan Europe", base_url="https://token-plan-ams.xiaomimimo.com/v1", env=("XIAOMI_API_KEY",)),
    _spec("xiaomi-token-plan-cn", "Xiaomi Token Plan China", base_url="https://token-plan-cn.xiaomimimo.com/v1", env=("XIAOMI_API_KEY",)),
    _spec("xiaomi-token-plan-sgp", "Xiaomi Token Plan Singapore", base_url="https://token-plan-sgp.xiaomimimo.com/v1", env=("XIAOMI_API_KEY",)),
    _spec("xpersona", "Xpersona", base_url="https://xpersona.co/v1", env=("XPERSONA_API_KEY",)),
    _spec("zai-coding-plan", "Z.ai Coding Plan", base_url="https://api.z.ai/api/coding/paas/v4", env=("ZHIPU_API_KEY", "ZAI_API_KEY")),
    _spec("zhipuai-coding-plan", "Zhipu AI Coding Plan", base_url="https://open.bigmodel.cn/api/coding/paas/v4", env=("ZHIPU_API_KEY",)),
    _spec(
        "zenmux",
        "ZenMux",
        base_url="https://zenmux.ai/api/v1",
        env=("ZENMUX_API_KEY",),
        tier="gateway",
        notes_for_user="For Claude models the Chat Completions path exposes reasoning as a plain reasoning string (signature dropped); set this thread's route to Anthropic Messages for native thinking via the gateway /v1/messages endpoint.",
        supported_routes=("openai_compat", "anthropic_messages"),
        anthropic_native_for_claude=True,
        anthropic_messages_base_url="https://zenmux.ai/api/anthropic",
    ),
)


ALL_LLM_PROVIDERS: dict[str, LLMProviderSpec] = {
    spec.id: spec for spec in (*_PROVIDER_SPECS, *_LONG_TAIL_SPECS)
}

_ALIASES: dict[str, str] = {}
for _provider in ALL_LLM_PROVIDERS.values():
    for _alias in _provider.aliases:
        _ALIASES[_alias.lower()] = _provider.id

# Preserve legacy/provider-family aliases used by other tools.
_ALIASES.update(
    {
        "openai-compatible": "openai",
        "custom-openai": "openai",
        "azure": "azure-openai",
        "fireworks": "fireworks-ai",
        "together": "togetherai",
        "novita": "novita-ai",
        "moonshot": "moonshotai",
        "qwen": "alibaba",
        "dashscope": "alibaba",
        "hf": "huggingface",
        "github": "github-models",
        "local": "openai",
    }
)


def normalize_llm_provider(provider: str | None) -> str:
    """Normalize provider IDs while preserving unknown custom IDs."""
    key = (provider or "").strip().lower()
    return _ALIASES.get(key, key)


def get_llm_provider_spec(provider: str | None) -> LLMProviderSpec | None:
    """Return a provider spec by canonical ID or alias."""
    return ALL_LLM_PROVIDERS.get(normalize_llm_provider(provider))


def is_known_llm_provider(provider: str | None) -> bool:
    return get_llm_provider_spec(provider) is not None


def is_openai_compatible_provider(provider: str | None) -> bool:
    spec = get_llm_provider_spec(provider)
    return bool(spec and spec.api_format == "openai_chat")


def normalize_provider_route(route: Any) -> ProviderRoute | None:
    """Normalize a provider route value, returning None for blanks/unknowns."""
    value = str(route or "").strip().lower()
    if value in _PROVIDER_ROUTE_VALUES:
        return cast(ProviderRoute, value)
    return None


def provider_supported_routes(provider: str | None) -> tuple[ProviderRoute, ...]:
    """Return adapter routes supported by a provider."""
    spec = get_llm_provider_spec(provider)
    if spec:
        return spec.supported_routes
    return ("openai_compat",)


def provider_supports_route(
    provider: str | None,
    route: ProviderRoute | str | None,
) -> bool:
    """Return True if a provider advertises the requested adapter route."""
    normalized = normalize_provider_route(route)
    if normalized is None:
        return False
    return normalized in provider_supported_routes(provider)


def provider_default_route(provider: str | None) -> ProviderRoute:
    """Return the provider's default adapter route."""
    spec = get_llm_provider_spec(provider)
    if spec:
        return spec.default_route
    return "openai_compat"


# Dedup set for the one-shot downgrade warning. Keyed on (provider, requested
# route) so a user who saves an unsupported override sees one warning, not one
# per chat turn.
_DOWNGRADED_ROUTE_WARNED: set[tuple[str, str]] = set()


def resolve_provider_route(
    provider: str | None,
    *,
    route_override: Any = None,
    global_route: Any = None,
) -> ProviderRoute:
    """Resolve thread override, global default, then provider default route.

    When a candidate is recognised (a valid ``ProviderRoute`` value) but the
    provider does not advertise it in ``supported_routes``, the candidate is
    silently downgraded to the provider's ``default_route`` and a one-shot
    warning is logged so users notice mis-configured route saves without
    spamming the log on every chat turn.
    """
    canonical_provider = normalize_llm_provider(provider) or "unknown"
    for candidate, scope in (
        (route_override, "thread"),
        (global_route, "global"),
    ):
        route = normalize_provider_route(candidate)
        if route is None:
            continue
        if provider_supports_route(provider, route):
            return route
        # Recognised but unsupported route — log once per (provider, route).
        warn_key = (canonical_provider, route)
        if warn_key not in _DOWNGRADED_ROUTE_WARNED:
            _DOWNGRADED_ROUTE_WARNED.add(warn_key)
            supported = provider_supported_routes(provider)
            logger.warning(
                "[LLM] Provider %r does not support %s provider_route=%r "
                "(supported: %s). Falling back to default route %r.",
                canonical_provider,
                scope,
                route,
                ", ".join(supported) or "none",
                provider_default_route(provider),
            )
    return provider_default_route(provider)


def provider_supports_responses(provider: str | None) -> bool:
    spec = get_llm_provider_spec(provider)
    return bool(spec and spec.supports_responses)


def provider_requires_api_key(provider: str | None) -> bool:
    spec = get_llm_provider_spec(provider)
    return True if spec is None else spec.requires_api_key


def is_provider_verified(provider: str | None) -> bool:
    """Return True if the provider has a known tier (native or gateway).

    Back-compat shim over the new `tier` taxonomy. Unverified providers return
    False so existing call sites (e.g., the startup warning in
    `vendor/react_agent/providers.py::_warn_if_unverified_provider`) continue
    to behave correctly.
    """
    spec = get_llm_provider_spec(provider)
    return bool(spec and spec.verified)


def get_provider_tier(provider: str | None) -> ProviderTier:
    """Return the tier classification for a provider (or 'unverified')."""
    spec = get_llm_provider_spec(provider)
    return spec.tier if spec else "unverified"


def provider_default_api_mode(provider: str | None) -> ApiMode:
    spec = get_llm_provider_spec(provider)
    return spec.default_api_mode if spec else "chat_completions"


def list_llm_provider_specs() -> list[LLMProviderSpec]:
    """Return provider specs sorted for UI/doc consumers."""
    return sorted(ALL_LLM_PROVIDERS.values(), key=lambda spec: spec.label.lower())


def _read_env_files(project_root: Path | None = None) -> dict[str, str]:
    if project_root is None:
        try:
            from .settings import get_env_file_paths

            paths = get_env_file_paths()
        except Exception:
            paths = ()
    else:
        paths = tuple(project_root / name for name in (".env", "config.env", ".env.docker"))

    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            if dotenv_values is not None:
                parsed = dotenv_values(path)
                for key, value in parsed.items():
                    if key and value not in (None, ""):
                        values[key] = str(value)
                continue

            for line in path.read_text(encoding="utf-8").splitlines():
                clean = line.strip()
                if not clean or clean.startswith("#") or "=" not in clean:
                    continue
                key, value = clean.split("=", 1)
                value = value.strip().strip("\"'")
                if key.strip() and value:
                    values[key.strip()] = value
        except UnicodeDecodeError:
            continue

    return values


def _setting_value_for_env_var(settings: Any, env_var: str) -> str | None:
    """Resolve a Settings attribute from its env-var name via the plain convention.

    The attribute name is ``env_var.lower()``. This resolver is used only for LLM
    provider key/base-url env vars, whose Settings field names follow that
    convention exactly, so no per-name override table is needed. Fields whose env
    var diverges from ``field.upper()`` (the S3 credentials, see
    ``_env_overrides.FIELD_ENV_OVERRIDES``) are intentionally NOT resolved here:
    they are not provider keys, and ``getattr(settings, env_var.lower())`` returns
    ``None`` for them (e.g. there is no ``aws_access_key_id`` attribute).
    """
    attr = env_var.lower()
    value = getattr(settings, attr, None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def resolve_env_value(
    env_vars: Iterable[str],
    *,
    settings: Any | None = None,
    project_root: Path | None = None,
) -> str | None:
    """Resolve an env var from process env, Settings, or Nymeria dotenv files."""
    names = tuple(name for name in env_vars if name)
    if not names:
        return None

    if settings is not None:
        for name in names:
            value = _setting_value_for_env_var(settings, name)
            if value:
                return value

    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()

    env_file_values = _read_env_files(
        project_root or getattr(settings, "project_root", None)
    )
    for name in names:
        value = env_file_values.get(name)
        if value:
            return value.strip()
    return None


_ENV_TEMPLATE_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def expand_env_templates(
    value: str | None,
    *,
    settings: Any | None = None,
    project_root: Path | None = None,
) -> str | None:
    """Expand ${ENV_VAR} placeholders using process env, Settings, or dotenv."""
    if not value:
        return value

    def replacement(match: re.Match[str]) -> str:
        resolved = resolve_env_value(
            (match.group(1),),
            settings=settings,
            project_root=project_root,
        )
        return resolved or match.group(0)

    return _ENV_TEMPLATE_PATTERN.sub(replacement, value)


def resolve_provider_base_url(
    provider: str | None,
    *,
    configured_base_url: str | None = None,
    provider_route: ProviderRoute | str | None = None,
    settings: Any | None = None,
    project_root: Path | None = None,
    include_default: bool = True,
) -> str | None:
    """Resolve effective base URL for a provider."""
    if configured_base_url:
        base_url = configured_base_url.strip().rstrip("/")
        route = normalize_provider_route(provider_route)
        if normalize_llm_provider(provider) == "ollama" and route == "openai_compat":
            return _ollama_openai_compat_base_url(base_url)
        return base_url

    spec = get_llm_provider_spec(provider)
    if spec is None:
        return None

    env_base = resolve_env_value(
        spec.base_url_env_vars,
        settings=settings,
        project_root=project_root,
    )
    route = normalize_provider_route(provider_route)
    default_base_url = spec.default_base_url
    if route == "openai_compat" and spec.openai_compat_base_url:
        default_base_url = spec.openai_compat_base_url
    elif route == "anthropic_messages" and spec.anthropic_messages_base_url:
        default_base_url = spec.anthropic_messages_base_url
    base_url = env_base or (default_base_url if include_default else None)
    expanded = expand_env_templates(
        base_url,
        settings=settings,
        project_root=project_root,
    )
    if not expanded:
        return None
    resolved = expanded.strip().rstrip("/")
    if normalize_llm_provider(provider) == "ollama" and route == "openai_compat":
        return _ollama_openai_compat_base_url(resolved)
    return resolved


def _ollama_openai_compat_base_url(base_url: str) -> str:
    """Convert an Ollama root URL to the OpenAI-compatible /v1 endpoint."""
    clean = base_url.strip().rstrip("/")
    if clean.endswith("/v1"):
        return clean
    return f"{clean}/v1"


def cliproxy_base_url_for_provider(
    provider: str | None,
    main_base_url: str | None,
) -> str | None:
    """Derive a provider's CLIProxy endpoint from the global LLM base URL.

    CLIProxy serves the Anthropic OAuth path at the container root and the
    OpenAI-compatible path at ``/v1`` on the same host, so a cross-provider
    reference must derive the matching URL from the global base URL rather than
    falling through to the provider's public default (which would bill direct
    and reject the proxy key). Mirrors the derivation in
    ``core/agent_llm_config.base_url_for_provider``. Returns ``None`` when the
    global base URL is unset or is not a CLIProxy URL.
    """
    url = (main_base_url or "").rstrip("/")
    if not url or ("cli-proxy" not in url and "cliproxy" not in url):
        return None
    p = normalize_llm_provider(provider)
    if p == "anthropic":
        return url[:-3] if url.endswith("/v1") else url
    if p == "openai":
        return url if url.endswith("/v1") else f"{url}/v1"
    return None


def resolve_provider_api_key(
    provider: str | None,
    *,
    configured_api_key: str | None = None,
    settings: Any | None = None,
    project_root: Path | None = None,
) -> str | None:
    """Resolve effective API key for a provider.

    ``LLM_API_KEY`` is a generic escape hatch for any OpenAI-compatible
    provider; provider-specific env vars still win when present.
    """
    if configured_api_key:
        return configured_api_key

    spec = get_llm_provider_spec(provider)
    provider_vars = spec.api_key_env_vars if spec else ()
    key = resolve_env_value(
        (*provider_vars, "LLM_API_KEY"),
        settings=settings,
        project_root=project_root,
    )
    return key


def provider_requires_base_url(provider: str | None) -> bool:
    spec = get_llm_provider_spec(provider)
    return bool(spec and spec.requires_base_url)

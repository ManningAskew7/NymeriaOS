"""Provider registry for LLM API compatibility.

Nymeria has historically treated most OpenAI-compatible services as the
``openai`` provider plus ``LLM_BASE_URL``.  This registry keeps the same runtime
adapter while giving first-class names, default endpoints, key env vars, and
documentation links to providers that speak the OpenAI Chat Completions shape.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover - python-dotenv is expected via pydantic.
    dotenv_values = None


ApiMode = str


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
) -> LLMProviderSpec:
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
        "google",
        "Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env=("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
        default_model="gemini-2.5-flash",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
        aliases=("gemini", "google-gemini"),
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
    ),
    _spec(
        "groq",
        "Groq",
        base_url="https://api.groq.com/openai/v1",
        env=("GROQ_API_KEY",),
        default_model="llama-3.3-70b-versatile",
        docs_url="https://console.groq.com/docs/openai",
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
    ),
    _spec(
        "mistral",
        "Mistral AI",
        base_url="https://api.mistral.ai/v1",
        env=("MISTRAL_API_KEY",),
        default_model="mistral-large-latest",
        docs_url="https://docs.mistral.ai/api/",
    ),
    _spec(
        "togetherai",
        "Together AI",
        base_url="https://api.together.ai/v1",
        env=("TOGETHER_API_KEY",),
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        docs_url="https://docs.together.ai/docs/inference/openai-compatibility",
        aliases=("together", "together-ai"),
    ),
    _spec(
        "fireworks-ai",
        "Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        env=("FIREWORKS_API_KEY",),
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        docs_url="https://docs.fireworks.ai/api-reference/post-chatcompletions",
        aliases=("fireworks",),
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
        docs_url="https://docs.sambanova.ai/cloud/api-reference/endpoints/chat",
    ),
    _spec(
        "nvidia",
        "NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        env=("NVIDIA_API_KEY",),
        default_model="nvidia/nemotron-3-super-120b-a12b",
        docs_url="https://docs.nvidia.com/nim/",
        aliases=("nim", "nvidia-nim", "build-nvidia"),
    ),
    _spec(
        "huggingface",
        "Hugging Face Inference Providers",
        base_url="https://router.huggingface.co/v1",
        env=("HF_TOKEN", "HUGGINGFACE_API_KEY"),
        default_model="deepseek-ai/DeepSeek-R1",
        docs_url="https://huggingface.co/docs/inference-providers/index",
        aliases=("hf", "hugging-face", "huggingface-hub"),
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
        "alibaba",
        "Alibaba Cloud Model Studio",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env=("DASHSCOPE_API_KEY", "ALIBABA_API_KEY"),
        default_model="qwen-plus",
        docs_url="https://www.alibabacloud.com/help/model-studio/use-qwen-by-calling-api",
        aliases=("dashscope", "qwen", "modelstudio", "alibaba-cloud"),
    ),
    _spec(
        "alibaba-cn",
        "Alibaba Cloud Model Studio China",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env=("DASHSCOPE_API_KEY", "ALIBABA_API_KEY"),
        default_model="qwen-plus",
        docs_url="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
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
        "volcengine",
        "Volcengine Ark",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        env=("VOLCANO_ENGINE_API_KEY", "ARK_API_KEY", "VOLCENGINE_API_KEY"),
        default_model="doubao-seed-1-8-251228",
        docs_url="https://www.volcengine.com/docs/82379",
        aliases=("doubao", "ark"),
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
    ),
    _spec(
        "poe",
        "Poe",
        base_url="https://api.poe.com/v1",
        env=("POE_API_KEY",),
        docs_url="https://developer.poe.com/server-bots/accessing-other-bots-on-poe",
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
    ),
    _spec(
        "vercel",
        "Vercel AI Gateway",
        base_url="https://ai-gateway.vercel.sh/v1",
        env=("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"),
        docs_url="https://vercel.com/docs/ai-gateway",
        aliases=("vercel-ai-gateway", "ai-gateway"),
    ),
    _spec(
        "opencode",
        "OpenCode Zen",
        base_url="https://opencode.ai/zen/v1",
        env=("OPENCODE_API_KEY",),
        docs_url="https://opencode.ai/docs",
        aliases=("opencode-zen", "zen"),
    ),
    _spec(
        "opencode-go",
        "OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        env=("OPENCODE_API_KEY",),
        docs_url="https://opencode.ai/docs",
    ),
    _spec(
        "kilocode",
        "Kilo Code Gateway",
        base_url="https://api.kilo.ai/api/gateway",
        env=("KILOCODE_API_KEY", "KILO_API_KEY"),
        default_model="kilo/auto",
        docs_url="https://kilocode.ai/docs",
        aliases=("kilo", "kilo-code"),
    ),
    _spec(
        "ollama",
        "Ollama local",
        base_url="http://localhost:11434/v1",
        env=("OLLAMA_API_KEY",),
        base_url_env=("OLLAMA_BASE_URL",),
        docs_url="https://github.com/ollama/ollama/blob/main/docs/openai.md",
        notes="Local Ollama usually does not require an API key; set LLM_API_KEY=no-key-required if validation is strict.",
        requires_api_key=False,
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
    _spec("baseten", "Baseten", base_url="https://inference.baseten.co/v1", env=("BASETEN_API_KEY",)),
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
    _spec("fastrouter", "FastRouter", base_url="https://go.fastrouter.ai/api/v1", env=("FASTROUTER_API_KEY",)),
    _spec("firepass", "Fireworks FirePass", base_url="https://api.fireworks.ai/inference/v1", env=("FIREPASS_API_KEY",)),
    _spec("friendli", "Friendli", base_url="https://api.friendli.ai/serverless/v1", env=("FRIENDLI_TOKEN",)),
    _spec("frogbot", "FrogBot", base_url="https://app.frogbot.ai/api/v1", env=("FROGBOT_API_KEY",)),
    _spec("helicone", "Helicone AI Gateway", base_url="https://ai-gateway.helicone.ai/v1", env=("HELICONE_API_KEY",)),
    _spec("hpc-ai", "HPC-AI", base_url="https://api.hpc-ai.com/inference/v1", env=("HPC_AI_API_KEY",)),
    _spec("iflowcn", "iFlow", base_url="https://apis.iflow.cn/v1", env=("IFLOW_API_KEY",)),
    _spec("inception", "Inception", base_url="https://api.inceptionlabs.ai/v1", env=("INCEPTION_API_KEY",)),
    _spec("inference", "Inference.net", base_url="https://inference.net/v1", env=("INFERENCE_API_KEY",)),
    _spec("io-net", "IO.NET", base_url="https://api.intelligence.io.solutions/api/v1", env=("IOINTELLIGENCE_API_KEY",)),
    _spec("jiekou", "Jiekou.AI", base_url="https://api.jiekou.ai/openai", env=("JIEKOU_API_KEY",)),
    _spec("kuae-cloud-coding-plan", "KUAE Cloud Coding Plan", base_url="https://coding-plan-endpoint.kuaecloud.net/v1", env=("KUAE_API_KEY",)),
    _spec("llama", "Meta Llama API", base_url="https://api.llama.com/compat/v1", env=("LLAMA_API_KEY",)),
    _spec("llmgateway", "LLM Gateway", base_url="https://api.llmgateway.io/v1", env=("LLMGATEWAY_API_KEY",)),
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
    _spec("zenmux", "ZenMux", base_url="https://zenmux.ai/api/v1", env=("ZENMUX_API_KEY",)),
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


def provider_supports_responses(provider: str | None) -> bool:
    spec = get_llm_provider_spec(provider)
    return bool(spec and spec.supports_responses)


def provider_requires_api_key(provider: str | None) -> bool:
    spec = get_llm_provider_spec(provider)
    return True if spec is None else spec.requires_api_key


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

    return values


def _setting_value_for_env_var(settings: Any, env_var: str) -> str | None:
    """Resolve known Settings attributes that use nontrivial field names."""
    attr_map = {
        "OPENAI_API_KEY": "openai_api_key",
        "OPENROUTER_API_KEY": "openrouter_api_key",
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "ANTHROPIC_DIRECT_API_KEY": "anthropic_direct_api_key",
        "GEMINI_API_KEY": "gemini_api_key",
        "PERPLEXITY_API_KEY": "perplexity_api_key",
        "GITHUB_TOKEN": "github_token",
    }
    attr = attr_map.get(env_var) or env_var.lower()
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
    settings: Any | None = None,
    project_root: Path | None = None,
    include_default: bool = True,
) -> str | None:
    """Resolve effective base URL for a provider."""
    if configured_base_url:
        return configured_base_url.strip().rstrip("/")

    spec = get_llm_provider_spec(provider)
    if spec is None:
        return None

    env_base = resolve_env_value(
        spec.base_url_env_vars,
        settings=settings,
        project_root=project_root,
    )
    base_url = env_base or (spec.default_base_url if include_default else None)
    expanded = expand_env_templates(
        base_url,
        settings=settings,
        project_root=project_root,
    )
    return expanded.strip().rstrip("/") if expanded else None


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

"""LLM 提供商注册表 — 提供商元数据的唯一真实来源。

本模块维护了 OpenHarness 支持的所有 LLM 提供商的元数据注册表。
每个提供商以 :class:`ProviderSpec` 数据类定义，包含名称、关键词、
环境变量、后端类型、检测规则等元数据。

添加新提供商的方法：
  1. 在 :data:`PROVIDERS` 元组中添加一个 :class:`ProviderSpec` 即可。
  检测、展示和配置均从此注册表自动推导。

注册表的顺序很重要 — 它控制匹配优先级。网关和云提供商优先，
标准提供商按关键词匹配，本地/特殊提供商最后。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """单个 LLM 提供商的元数据定义。

    描述了提供商的身份、路由、自动检测信号和分类标志。
    backend_type 取值说明：
      - ``"anthropic"`` — 使用 Anthropic SDK（claude-* 模型的默认选择）
      - ``"openai_compat"`` — 使用 OpenAI 兼容的 REST API
      - ``"copilot"`` — 使用 GitHub Copilot OAuth 流程

    Attributes:
        name: 规范名称，如 ``"dashscope"``。
        keywords: 用于模型名检测的关键词元组（小写），如 ``("qwen", "dashscope")``。
        env_key: 主要的 API Key 环境变量名。
        display_name: 在状态/诊断中显示的名称，若为空则自动从 name 生成。
        backend_type: 后端类型（``"anthropic"`` | ``"openai_compat"`` | ``"copilot"``）。
        default_base_url: 该提供商的默认 Base URL。
        detect_by_key_prefix: 通过 API Key 前缀匹配，如 ``"sk-or-"``。
        detect_by_base_keyword: 通过 Base URL 中的子串匹配。
        is_gateway: 是否为网关型提供商（可路由任何模型，如 OpenRouter）。
        is_local: 是否为本地部署（如 vLLM、Ollama）。
        is_oauth: 是否使用 OAuth 认证（而非 API Key）。
    """

    # Identity
    name: str  # canonical name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name substrings for detection (lowercase)
    env_key: str  # primary API key environment variable
    display_name: str = ""  # shown in status / diagnostics

    # Routing
    backend_type: str = "openai_compat"  # "anthropic" | "openai_compat" | "copilot"
    default_base_url: str = ""  # fallback base URL for this provider

    # Auto-detection signals
    detect_by_key_prefix: str = ""  # match api_key prefix, e.g. "sk-or-"
    detect_by_base_keyword: str = ""  # match substring in base_url

    # Classification flags
    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix, …)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    is_oauth: bool = False  # uses OAuth instead of API key

    @property
    def label(self) -> str:
        """返回提供商的可读标签。

        若设置了 ``display_name`` 则直接使用，否则将 ``name`` 转为标题格式。

        Returns:
            提供商的显示标签字符串。
        """
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS registry — order = detection priority.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === GitHub Copilot (OAuth, detected by api_format="copilot") ============
    ProviderSpec(
        name="github_copilot",
        keywords=("copilot",),
        env_key="",
        display_name="GitHub Copilot",
        backend_type="copilot",
        default_base_url="",
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        is_gateway=False,
        is_local=False,
        is_oauth=True,
    ),
    # === Gateways (detected by api_key prefix / base_url keyword) ============
    # OpenRouter: global gateway, keys start with "sk-or-"
    ProviderSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        backend_type="openai_compat",
        default_base_url="https://openrouter.ai/api/v1",
        detect_by_key_prefix="sk-or-",
        detect_by_base_keyword="openrouter",
        is_gateway=True,
        is_local=False,
        is_oauth=False,
    ),
    # AiHubMix: OpenAI-compatible gateway
    ProviderSpec(
        name="aihubmix",
        keywords=("aihubmix",),
        env_key="OPENAI_API_KEY",
        display_name="AiHubMix",
        backend_type="openai_compat",
        default_base_url="https://aihubmix.com/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="aihubmix",
        is_gateway=True,
        is_local=False,
        is_oauth=False,
    ),
    # SiliconFlow (硅基流动): OpenAI-compatible gateway
    ProviderSpec(
        name="siliconflow",
        keywords=("siliconflow",),
        env_key="OPENAI_API_KEY",
        display_name="SiliconFlow",
        backend_type="openai_compat",
        default_base_url="https://api.siliconflow.cn/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="siliconflow",
        is_gateway=True,
        is_local=False,
        is_oauth=False,
    ),
    # VolcEngine (火山引擎 / Ark): OpenAI-compatible gateway
    ProviderSpec(
        name="volcengine",
        keywords=("volcengine", "volces", "ark"),
        env_key="OPENAI_API_KEY",
        display_name="VolcEngine",
        backend_type="openai_compat",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        detect_by_key_prefix="",
        detect_by_base_keyword="volces",
        is_gateway=True,
        is_local=False,
        is_oauth=False,
    ),
    # === Standard cloud providers (matched by model-name keyword) ============
    # Anthropic: native SDK for claude-* models
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        backend_type="anthropic",
        default_base_url="",
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # OpenAI: gpt-* models
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt", "o1", "o3", "o4"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        backend_type="openai_compat",
        default_base_url="",
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # DeepSeek
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        backend_type="openai_compat",
        default_base_url="https://api.deepseek.com/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="deepseek",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Google Gemini
    ProviderSpec(
        name="gemini",
        keywords=("gemini",),
        env_key="GEMINI_API_KEY",
        display_name="Gemini",
        backend_type="openai_compat",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        detect_by_key_prefix="",
        detect_by_base_keyword="googleapis",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # DashScope (Qwen / 阿里云)
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        backend_type="openai_compat",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="dashscope",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Moonshot / Kimi
    ProviderSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        backend_type="openai_compat",
        default_base_url="https://api.moonshot.ai/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="moonshot",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # MiniMax
    ProviderSpec(
        name="minimax",
        keywords=("minimax",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        backend_type="openai_compat",
        default_base_url="https://api.minimax.io/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="minimax",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Zhipu AI / GLM
    ProviderSpec(
        name="zhipu",
        keywords=("zhipu", "glm", "chatglm"),
        env_key="ZHIPUAI_API_KEY",
        display_name="Zhipu AI",
        backend_type="openai_compat",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        detect_by_key_prefix="",
        detect_by_base_keyword="bigmodel",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Groq
    ProviderSpec(
        name="groq",
        keywords=("groq",),
        env_key="GROQ_API_KEY",
        display_name="Groq",
        backend_type="openai_compat",
        default_base_url="https://api.groq.com/openai/v1",
        detect_by_key_prefix="gsk_",
        detect_by_base_keyword="groq",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Mistral
    ProviderSpec(
        name="mistral",
        keywords=("mistral", "mixtral", "codestral"),
        env_key="MISTRAL_API_KEY",
        display_name="Mistral",
        backend_type="openai_compat",
        default_base_url="https://api.mistral.ai/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="mistral",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # StepFun (阶跃星辰)
    ProviderSpec(
        name="stepfun",
        keywords=("step-", "stepfun"),
        env_key="STEPFUN_API_KEY",
        display_name="StepFun",
        backend_type="openai_compat",
        default_base_url="https://api.stepfun.com/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="stepfun",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Baidu / ERNIE
    ProviderSpec(
        name="baidu",
        keywords=("ernie", "baidu"),
        env_key="QIANFAN_ACCESS_KEY",
        display_name="Baidu",
        backend_type="openai_compat",
        default_base_url="https://qianfan.baidubce.com/v2",
        detect_by_key_prefix="",
        detect_by_base_keyword="baidubce",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # === Cloud platform providers (detected by base_url) ====================
    # AWS Bedrock
    ProviderSpec(
        name="bedrock",
        keywords=("bedrock",),
        env_key="AWS_ACCESS_KEY_ID",
        display_name="AWS Bedrock",
        backend_type="openai_compat",
        default_base_url="",
        detect_by_key_prefix="",
        detect_by_base_keyword="bedrock",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # Google Vertex AI
    ProviderSpec(
        name="vertex",
        keywords=("vertex",),
        env_key="GOOGLE_APPLICATION_CREDENTIALS",
        display_name="Vertex AI",
        backend_type="openai_compat",
        default_base_url="",
        detect_by_key_prefix="",
        detect_by_base_keyword="aiplatform",
        is_gateway=False,
        is_local=False,
        is_oauth=False,
    ),
    # === Local deployments (matched by keyword or base_url) =================
    # Ollama
    ProviderSpec(
        name="ollama",
        keywords=("ollama",),
        env_key="",
        display_name="Ollama",
        backend_type="openai_compat",
        default_base_url="http://localhost:11434/v1",
        detect_by_key_prefix="",
        detect_by_base_keyword="localhost:11434",
        is_gateway=False,
        is_local=True,
        is_oauth=False,
    ),
    # vLLM / any OpenAI-compatible local server
    ProviderSpec(
        name="vllm",
        keywords=("vllm",),
        env_key="",
        display_name="vLLM/Local",
        backend_type="openai_compat",
        default_base_url="",
        detect_by_key_prefix="",
        detect_by_base_keyword="",
        is_gateway=False,
        is_local=True,
        is_oauth=False,
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """通过规范名称查找提供商规格。

    在注册表中按名称精确匹配查找。

    Args:
        name: 提供商的规范名称（如 ``"dashscope"``）。

    Returns:
        匹配的 :class:`ProviderSpec` 对象，若未找到则返回 ``None``。
    """
    for spec in PROVIDERS:
        if spec.name == name:
            return spec
    return None


def _match_by_model(model: str) -> ProviderSpec | None:
    """通过模型名关键词匹配标准/网关提供商（不区分大小写）。

    首先尝试模型名中的提供商前缀精确匹配（如 ``deepseek/...`` → deepseek），
    然后回退到关键词扫描。仅匹配非本地、非 OAuth 的标准提供商和网关。

    Args:
        model: 模型标识字符串。

    Returns:
        匹配的 :class:`ProviderSpec` 对象，若未找到则返回 ``None``。
    """
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
    normalized_prefix = model_prefix.replace("-", "_")

    std_specs = [s for s in PROVIDERS if not s.is_local and not s.is_oauth]

    # Prefer an explicit provider-prefix match (e.g. "deepseek/..." → deepseek spec)
    for spec in std_specs:
        if model_prefix and normalized_prefix == spec.name:
            return spec

    # Fall back to keyword scan
    for spec in std_specs:
        if any(
            kw in model_lower or kw.replace("-", "_") in model_normalized
            for kw in spec.keywords
        ):
            return spec
    return None


def detect_provider_from_registry(
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ProviderSpec | None:
    """根据给定输入检测最佳匹配的提供商规格。

    检测优先级：
      1. API Key 前缀匹配（如 ``"sk-or-"`` → OpenRouter）
      2. Base URL 关键词匹配（如 URL 中含 ``"aihubmix"`` → AiHubMix）
      3. 模型名关键词匹配（如 ``"qwen"`` → DashScope）

    Args:
        model: 模型标识字符串。
        api_key: API 密钥，用于前缀匹配。
        base_url: API 端点 URL，用于关键词匹配。

    Returns:
        最佳匹配的 :class:`ProviderSpec` 对象，若均不匹配则返回 ``None``。
    """
    # 1. api_key prefix
    if api_key:
        for spec in PROVIDERS:
            if spec.detect_by_key_prefix and api_key.startswith(spec.detect_by_key_prefix):
                return spec

    # 2. base_url keyword
    if base_url:
        base_lower = base_url.lower()
        for spec in PROVIDERS:
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in base_lower:
                return spec

    # 3. model keyword
    if model:
        return _match_by_model(model)

    return None

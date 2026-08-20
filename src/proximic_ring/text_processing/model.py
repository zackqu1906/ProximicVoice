from __future__ import annotations

from dataclasses import dataclass


INPUT_MODE_DICTATION = "dictation"
INPUT_MODE_EDIT = "edit"
# Backward-compatible import name for older scripts.  The former instruction
# lane is now the one-shot edit lane; the Agent lane will be added separately.
INPUT_MODE_INSTRUCTION = INPUT_MODE_EDIT
INPUT_MODES = frozenset({INPUT_MODE_DICTATION, INPUT_MODE_EDIT})
LLM_PROVIDER_LOCAL = "local"
LLM_PROVIDER_OPENAI = "openai"
LLM_PROVIDER_VOLCENGINE = "volcengine"
LLM_PROVIDERS = frozenset(
    {LLM_PROVIDER_LOCAL, LLM_PROVIDER_OPENAI, LLM_PROVIDER_VOLCENGINE}
)
DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_MODEL = "doubao-seed-2-0-lite-260215"
DEFAULT_ARK_DEEPSEEK_MODEL = "deepseek-v4-flash-260425"
DEFAULT_ARK_API_KEY_ENV = "ARK_API_KEY"
MAX_EDIT_TARGET_CHARS = 5000


def normalize_input_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode == "instruction":
        return INPUT_MODE_EDIT
    return mode if mode in INPUT_MODES else INPUT_MODE_DICTATION


def normalize_llm_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in LLM_PROVIDERS else LLM_PROVIDER_LOCAL


def validate_edit_target_text(value: str) -> str:
    """Reject captures that cannot safely fit in one local edit request."""

    text = str(value or "")
    if not text.strip():
        raise ValueError("修改模式需要已有的目标文本")
    if len(text) > MAX_EDIT_TARGET_CHARS:
        raise ValueError(
            f"读取到 {len(text)} 个字符，超过单次修改上限 "
            f"{MAX_EDIT_TARGET_CHARS}；可能误选了整个页面，也可能文本本身过长。"
            "本次内容未发送给大模型，请重新点击目标文本框后重试"
        )
    return text


@dataclass(frozen=True)
class LLMSettings:
    """Configuration for an OpenAI-compatible chat-completions endpoint."""

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    timeout_s: float = 30.0
    provider: str = LLM_PROVIDER_OPENAI
    local_server_path: str = ""
    local_model_path: str = ""
    local_auto_start: bool = False
    local_context_size: int = 8192
    local_reasoning: str = "off"

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.base_url.strip():
            raise ValueError("大模型 API 地址不能为空")
        if not self.model.strip():
            raise ValueError("大模型名称不能为空")
        if self.timeout_s <= 0:
            raise ValueError("大模型请求超时必须大于 0 秒")
        if normalize_llm_provider(self.provider) == LLM_PROVIDER_LOCAL:
            if self.api_key_env.strip():
                raise ValueError("本地模型不应配置 API Key 环境变量")
            if self.local_auto_start and not self.local_server_path.strip():
                raise ValueError("本地模型服务程序路径不能为空")
            if self.local_auto_start and not self.local_model_path.strip():
                raise ValueError("本地 GGUF 模型路径不能为空")
            if self.local_context_size < 512:
                raise ValueError("本地模型上下文长度不能小于 512")
        elif (
            normalize_llm_provider(self.provider) == LLM_PROVIDER_VOLCENGINE
            and not self.api_key_env.strip()
        ):
            raise ValueError("火山方舟需要配置 API Key 环境变量名")


@dataclass(frozen=True)
class TextProcessingRequest:
    request_id: int
    session_id: int
    mode: str
    raw_text: str
    settings: LLMSettings
    target_text: str = ""


@dataclass(frozen=True)
class TextProcessingResult:
    request_id: int
    session_id: int
    mode: str
    raw_text: str
    final_text: str
    latency_s: float
    used_llm: bool
    target_text: str = ""
    error: str | None = None
    model_output: str = ""

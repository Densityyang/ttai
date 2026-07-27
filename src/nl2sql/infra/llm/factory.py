"""LLM 工厂"""

from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.core.settings import get_settings


@lru_cache(maxsize=16)
def _build_llm(
    model_name: str,
    openai_api_key: str,
    openai_base_url: str | None,
    temperature: float,
    streaming: bool,
) -> ChatOpenAI:
    extra_body: dict[str, bool] | None = None
    base_url = openai_base_url or ""
    # DashScope 的 Qwen 思考模型与 json_object/tool_choice 强制模式存在兼容限制，
    # 统一在模型层关闭思考模式，避免运行时出现 400 InvalidParameter。
    if "dashscope.aliyuncs.com" in base_url and "qwen" in model_name.lower():
        extra_body = {"enable_thinking": False}

    return ChatOpenAI(
        model=model_name,
        api_key=SecretStr(openai_api_key),
        base_url=openai_base_url,
        temperature=temperature,
        streaming=streaming,
        extra_body=extra_body,
    )


def get_llm(
    model_name: str | None = None,
    openai_base_url: str | None = None,
    temperature: float = 0,
    streaming: bool = True,
) -> ChatOpenAI:
    """获取可缓存复用的大语言模型实例。"""
    settings = get_settings()
    if not settings.openai_api_key.strip():
        raise ValueError("OPENAI_API_KEY 未设置")

    resolved_model_name = model_name or settings.model_name
    resolved_base_url = openai_base_url if openai_base_url is not None else settings.openai_base_url

    return _build_llm(
        model_name=resolved_model_name,
        openai_api_key=settings.openai_api_key,
        openai_base_url=resolved_base_url,
        temperature=temperature,
        streaming=streaming,
    )

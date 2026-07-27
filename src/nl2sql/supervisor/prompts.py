"""Supervisor Agent 提示词模板（Phase 4: KV-Cache 友好重构）。

架构：
- 静态前缀（不变）由 prompt_builder 管理 → LLM 可复用 KV-Cache
- 动态后缀（每次请求追加）仅包含时间、用户信息、会话上下文

保留旧接口 get_supervisor_prompt() 兼容现有调用方。
"""

from src.nl2sql.infra.context.prompt_builder import (
    build_system_prompt,
    inject_session_summary,
)


def get_supervisor_prompt(
    language: str = "zh",
    current_time: str = "",
    user_info: str = "",
    extra_context: str = "",
    session_summary: str = "",
) -> str:
    """获取 Supervisor 系统提示词（KV-Cache 友好版本）。

    Args:
        language: 语言代码，目前支持 'zh'（中文）
        current_time: 当前系统时间
        user_info: 用户身份信息
        extra_context: 额外动态上下文（路由提示等）
        session_summary: 会话摘要（超长对话时注入）

    Returns:
        格式化后的系统提示词
    """
    prompt = build_system_prompt(
        current_time=current_time,
        user_info=user_info,
        extra_context=extra_context,
    )

    if session_summary:
        prompt = inject_session_summary(prompt, session_summary)

    return prompt

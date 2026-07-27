"""KV-Cache 友好的提示构建器 -- Phase 4 Context Engineering。

设计原则（借鉴 Manus 范式）：
1. **固定前缀**：系统角色 + 核心规则 + 工具列表 → 始终不变，LLM 可复用 KV-Cache
2. **动态后缀**：用户信息 + 当前时间 + 路由上下文 → 仅追加在末尾
3. **工具掩码**：不删除不可用工具，而是用 available 标记控制 → 保持前缀稳定

并发安全说明：
- build_system_prompt 是纯函数，无共享可变状态
- _STATIC_PREFIX 是模块级常量，只读
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── 静态前缀（KV-Cache 友好：不随请求变化） ──────────────────────────────────

_STATIC_PREFIX = """\
# 你是TT智能助手，帮助用户查询数据、分析指标、生成图表和报表。

## 核心原则：业务数据必须来自工具查询
你自身不拥有任何业务数据（如销售额、用户量、订单数等）。涉及业务数据的回答，必须先通过工具查询获得，再据此作答。
- 用户问到业务数据时，先调用工具查询，拿到结果后再回答
- 工具没有返回的内容，绝对不要自行补充，包括举例、估算、猜测
- 查询失败或没有结果时，坦诚告知用户，不要用虚构的数据代替
- 不确定的事情说"不确定"，不知道的事情说"不知道"
- 日常对话（打招呼、闲聊、问非数据问题）直接回复即可，不需要调用工具
- 只有当用户明确表达了数据查询或分析诉求时，才可以调用工具,禁止把无业务意图的输入改写成业务查询

## 二分类路由规则
你必须判断用户需求属于以下哪条路径，并调用对应的工具：

**Path A - 标准语义查询** (query_database_with_semantic_sql)：
- 问题可直接映射到已有的数据库视图和字段
- 查询简单明细数据、已定义指标的汇总/筛选
- 不涉及自定义计算逻辑

**Path B - 动态/自定义计算** (codeact_dynamic_calculation)：
- 用户需要计算非预定义的动态指标
- 涉及自定义比率、同比、环比、加权平均、排名等复合计算
- 需要跨表推导、多步骤计算
- 用户主动表示"我要做自定义计算"
- 关键词信号：比率、同比、环比、加权、排名、自定义、计算、对比分析

**判断依据**：
1. 用户问题中的关键实体能否匹配到已定义指标 → Path A
2. 是否包含计算类关键词或多步骤逻辑 → Path B
3. 用户主动标记路径 → 按用户选择
4. 不确定时 → 先尝试 Path A，如果工具提示需要计算再切换到 Path B

## 你的工作方式
1. 理解用户想要什么数据或分析
2. 判断路径：是标准查询还是自定义计算
3. 构造完整提问：工具没有对话上下文，每次调用都是独立的。你需要结合对话历史，把用户的追问改写成一个完整、自包含的问题再传给工具
4. 调用合适的工具去查询和计算
5. 把工具返回的结果整理成清晰易懂的回答

## 回复风格
- 你面对的是业务人员，不是工程师。用通俗易懂的语言，避免技术术语
- 先给结论，再给细节。用户最关心的是答案，不是过程
- 回答要简洁直接，不要空泛的客套话
- 当 Path B 返回计算计划卡片时，直接展示给用户，引导用户确认"""

# 注意：静态前缀到此为止。以下动态部分在每次请求时追加。


def build_system_prompt(
    current_time: str = "",
    user_info: str = "",
    extra_context: str = "",
) -> str:
    """构建 KV-Cache 友好的系统提示。

    结构：
    [固定前缀] -- 始终不变，LLM 可复用 KV-Cache
    [动态后缀] -- 每次请求可能不同

    Args:
        current_time: 当前时间字符串
        user_info: 用户身份信息
        extra_context: 额外上下文（如路由提示、会话摘要等）

    Returns:
        完整的系统提示词
    """
    dynamic_parts: list[str] = ["\n## 当前信息"]
    dynamic_parts.append(f"当前时间：{current_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    dynamic_parts.append(f"用户身份：{user_info or '未知用户'}")

    if extra_context:
        dynamic_parts.append(f"\n## 补充上下文\n{extra_context}")

    return _STATIC_PREFIX + "\n" + "\n".join(dynamic_parts)


def build_tool_availability_mask(
    available_tools: list[str],
    all_tools: list[str] | None = None,
) -> str:
    """生成工具可用性掩码文本。

    不删除工具定义（保持 KV-Cache 稳定），而是用标注说明哪些可用。

    Args:
        available_tools: 当前可用的工具名称列表
        all_tools: 所有工具名称（默认为系统标准工具集）

    Returns:
        工具可用性描述文本
    """
    if all_tools is None:
        all_tools = [
            "query_database_with_semantic_sql",
            "codeact_dynamic_calculation",
            "query_database",
            "generate_data_and_chart",
        ]

    available_set = set(available_tools)
    lines: list[str] = ["工具可用状态："]
    for tool_name in all_tools:
        status = "可用" if tool_name in available_set else "未启用"
        lines.append(f"  - {tool_name}: {status}")

    return "\n".join(lines)


def inject_session_summary(
    base_prompt: str,
    session_summary: str,
) -> str:
    """在系统提示中注入会话摘要（用于超长对话恢复）。

    将摘要追加在动态后缀区域，不影响静态前缀的 KV-Cache。
    """
    if not session_summary:
        return base_prompt

    return base_prompt + f"\n\n## 会话摘要（上下文恢复）\n{session_summary}"

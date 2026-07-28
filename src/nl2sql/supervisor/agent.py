"""Supervisor Agent - 协调无状态子 Agent

Supervisor 负责维护对话记忆和上下文压缩,
子 Agent(nl2sql、chart)每次调用都是无状态的干净上下文。
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRetryMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.structured_output import AutoStrategy
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver

from src.core.observer import create_monitored_config
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.gateway import get_legacy_model
from src.nl2sql.infra.runtime.registry import (
    get_or_create_codeact_graph,
    get_or_create_dynamic_calc_graph,
    get_or_create_gen_data_agent,
    get_or_create_semantic_sql_graph,
    get_or_create_sql_graph,
)
from src.nl2sql.supervisor.prompts import get_supervisor_prompt
from src.nl2sql.supervisor.schemas import SupervisorResponse

logger = logging.getLogger(__name__)

_SUPERVISOR_SYSTEM_MSG_NAME = "tt_supervisor_system"


class DynamicSystemPromptMiddleware(AgentMiddleware[Any, Any]):
    """按请求动态注入 Supervisor 系统提示词。"""

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        del runtime
        messages_raw = state.get("messages")
        if not isinstance(messages_raw, list):
            return None

        messages = [msg for msg in messages_raw if isinstance(msg, BaseMessage)]
        system_message = self._build_system_message()

        if (
            messages
            and isinstance(messages[0], SystemMessage)
            and messages[0].name == _SUPERVISOR_SYSTEM_MSG_NAME
        ):
            messages[0] = system_message
            return {"messages": messages}

        return {"messages": [system_message, *messages]}

    def _build_system_message(self) -> SystemMessage:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_info = self._read_user_info_from_configurable()
        prompt = get_supervisor_prompt(
            language="zh",
            current_time=now_str,
            user_info=user_info,
        )
        return SystemMessage(content=prompt, name=_SUPERVISOR_SYSTEM_MSG_NAME)

    @staticmethod
    def _read_user_info_from_configurable() -> str:
        config = var_child_runnable_config.get()
        if not isinstance(config, dict):
            return "未知用户"

        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            return "未知用户"

        user_id = configurable.get("auth_user_id")
        telephone = configurable.get("auth_user_telephone")
        roles_raw = configurable.get("auth_user_roles")
        roles = roles_raw if isinstance(roles_raw, list) else []
        roles_text = ", ".join(str(role) for role in roles) if roles else "无"

        if user_id is None and telephone is None and not roles:
            return "未知用户"

        return f"user_id={user_id}, telephone={telephone}, roles={roles_text}"


def _get_current_thread_id() -> str | None:
    config = var_child_runnable_config.get()
    if not isinstance(config, dict):
        return None

    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None

    thread_id = configurable.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    return None


@tool
async def query_database(question: str) -> str:
    """执行标准数据库查询（NL2SQL）。

    调用条件：
    - 用户要查询业务数据、统计指标、明细列表或对比结果。
    - 用户问题可直接落到 SQL（包含指标、对象、筛选条件、时间范围中的至少一项）。

    禁止调用：
    - 闲聊、解释系统行为、改写文案、代码问题等非数据查询请求。
    - 仅要求图表样式设计但未提出数据查询目标。

    参数约束：
    - question 必须保留用户原始业务语义，不擅自补充不存在的过滤条件。

    备注：
    - 当前未在 Supervisor 中启用，仅保留用于对照测试/回归验证。

    返回约束：
    - 返回查询结果文本；失败时返回可读错误信息。
    """
    if not isinstance(question, str):
        return "请提供需要查询的具体问题。"
    normalized_question = question.strip()
    if not normalized_question:
        return "请提供需要查询的具体问题。"

    return await _invoke_nl2sql_agent(normalized_question)


@tool
async def generate_data_and_chart(question: str) -> str:
    """获取图表所需数据并产出分析结果。

    调用条件：
    - 用户明确要求图表、可视化、趋势展示、分布展示或“做一个看板/报表”。
    - 需要先查询数据再组织为可视化输入。

    禁止调用：
    - 用户只要纯文本答案且不需要图表或可视化表达。
    - 问题与数据库查询无关。

    参数约束：
    - question 必须包含可查询的业务目标；不虚构字段、表或口径。

    备注：
    - 当前未在 Supervisor 中启用，仅保留用于图表链路测试。

    返回约束：
    - 返回原始数据与分析文本，供 Supervisor 组装可视化内容块。
    """
    if not isinstance(question, str) or not question.strip():
        return "请提供需要查询的具体问题。"

    normalized_question = question.strip()
    return await _invoke_gen_data_agent(normalized_question)


@tool
async def query_database_with_semantic_sql(question: str) -> str:
    """使用语义层 SQL Agent 执行数据库查询。

    调用条件：
    - 用户需求属于标准数据查询或统计分析。
    - 问题可直接映射到已有的数据库视图和字段。

    禁止调用：
    - 与数据查询无关的请求。
    - 需要自定义计算逻辑、比率计算、排名分析、趋势拟合等非标准聚合的请求（应使用 dynamic_metric_calculation）。

    参数约束：
    - question 需是明确的数据问题；不得改写核心业务语义。

    返回约束：
    - 返回语义 SQL 链路的最终文本结果或可读错误信息。
    """
    if not isinstance(question, str) or not question.strip():
        return "请提供需要查询的具体问题。"

    normalized_question = question.strip()
    return await _invoke_semantic_sql_agent(normalized_question)


@tool
async def dynamic_metric_calculation(question: str) -> str:
    """执行动态指标计算（SQL 取数 + 代码计算）。

    调用条件：
    - 用户需要计算非预定义的动态指标（如自定义比率、排名、趋势分析、复合计算）。
    - 问题涉及多步骤计算逻辑，不能用单条 SQL 直接完成。
    - 用户描述了取数来源和计算规则，但语义层没有对应的预定义指标。

    禁止调用：
    - 简单的数据查询（应使用 query_database_with_semantic_sql）。
    - 与数据无关的请求。

    参数约束：
    - question 必须包含可识别的计算目标和数据来源描述。

    返回约束：
    - 返回计算结果文本或可读错误信息。
    """
    if not isinstance(question, str) or not question.strip():
        return "请提供需要计算的具体问题。"

    normalized_question = question.strip()
    return await _invoke_dynamic_calc_agent(normalized_question)


async def _invoke_sql_runnable(
    *,
    normalized_question: str,
    runnable: Any,
    run_name: str,
) -> str:
    """执行指定 SQL runnable 并提取最终文本结果。"""
    config = get_agent_config()

    invoke_config = create_monitored_config(
        session_id=_get_current_thread_id(),
        base_config={"recursion_limit": config.graph_recursion_limit},
        run_name=run_name,
    )

    try:
        result = await asyncio.wait_for(
            runnable.ainvoke(
                {"messages": [{"role": "user", "content": normalized_question}]},
                config=invoke_config,
            ),
            timeout=config.graph_timeout,
        )
    except TimeoutError:
        logger.warning(
            "SQL 查询超时: run_name=%s, question=%s",
            run_name,
            normalized_question,
        )
        return f"查询超时（{config.graph_timeout}秒），请简化问题或稍后重试。"
    except Exception as exc:
        logger.exception(
            "SQL 查询异常: run_name=%s, question=%s",
            run_name,
            normalized_question,
        )
        return f"查询失败: {exc!s}"

    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list) or not messages:
        return "未获取到可用的查询结果，请稍后重试。"

    content = getattr(messages[-1], "content", None)
    if isinstance(content, str):
        return content
    return "未获取到可用的查询结果，请稍后重试。"


async def _invoke_gen_data_agent(normalized_question: str) -> str:
    runnable = await get_or_create_gen_data_agent()
    return await _invoke_sql_runnable(
        normalized_question=normalized_question,
        runnable=runnable,
        run_name="gen_data_agent",
    )


async def _invoke_nl2sql_agent(normalized_question: str) -> str:
    runnable = await get_or_create_sql_graph()
    return await _invoke_sql_runnable(
        normalized_question=normalized_question,
        runnable=runnable,
        run_name="sql_agent",
    )


async def _invoke_semantic_sql_agent(normalized_question: str) -> str:
    runnable = await get_or_create_semantic_sql_graph()
    return await _invoke_sql_runnable(
        normalized_question=normalized_question,
        runnable=runnable,
        run_name="semantic_sql_agent",
    )


async def _invoke_dynamic_calc_agent(normalized_question: str) -> str:
    runnable = await get_or_create_dynamic_calc_graph()
    return await _invoke_sql_runnable(
        normalized_question=normalized_question,
        runnable=runnable,
        run_name="dynamic_calc_agent",
    )


@tool
async def codeact_dynamic_calculation(question: str) -> str:
    """执行 HITL 确认 + CodeAct 动态指标计算（新版）。

    调用条件：
    - 用户需要计算非预定义的动态指标（如自定义比率、排名、趋势分析、复合计算）。
    - 问题涉及多步骤计算逻辑，不能用单条 SQL 直接完成。
    - 用户描述了取数来源和计算规则，但语义层没有对应的预定义指标。
    - 用户主动选择"我要做自定义计算"。
    - 问题中包含"比率""同比""环比""加权""排名""自定义"等计算类关键词。

    禁止调用：
    - 简单的数据查询（应使用 query_database_with_semantic_sql）。
    - 与数据无关的请求。

    参数约束：
    - question 必须包含可识别的计算目标和数据来源描述。

    返回约束：
    - 首先返回计算计划卡片供用户确认。
    - 确认后返回计算结果文本或可读错误信息。
    """
    if not isinstance(question, str) or not question.strip():
        return "请提供需要计算的具体问题。"

    normalized_question = question.strip()
    return await _invoke_codeact_agent(normalized_question)


async def _invoke_codeact_agent(normalized_question: str) -> str:
    config = get_agent_config()
    available, reason = config.codeact_capability()
    if not available:
        return f"Dynamic calculation is unavailable: {reason}."
    if config.codeact_mode == "trusted-template":
        runnable = await get_or_create_dynamic_calc_graph()
        run_name = "trusted_template_calculation"
    else:
        runnable = await get_or_create_codeact_graph()
        run_name = "codeact_engine"
    return await _invoke_sql_runnable(
        normalized_question=normalized_question,
        runnable=runnable,
        run_name=run_name,
    )


async def create_supervisor(checkpointer: BaseCheckpointSaver) -> Any:
    """创建 Supervisor Agent

    Args:
        checkpointer: 会话持久化后端

    Supervisor 负责：
    1. 意图识别和任务路由
    2. 对话历史维护和压缩
    3. 调用子 Agent 工具
    """
    logger.info("开始创建 Supervisor Agent")
    llm = get_legacy_model()

    agent_config = get_agent_config()
    # 二分类路由：Path A (标准语义查询) + Path B (HITL + CodeAct 动态计算)
    active_tools: list[Any] = [query_database_with_semantic_sql]
    if agent_config.codeact_capability()[0]:
        active_tools.append(codeact_dynamic_calculation)

    supervisor = create_agent(
        model=llm,
        tools=active_tools,
        checkpointer=checkpointer,
        middleware=[
            # 对话历史压缩：token 超过 4000 时触发，保留最近 20 条消息
            SummarizationMiddleware(
                model=llm,
                trigger=("tokens", 4000),
                keep=("messages", 20),
            ),
            # LLM 调用重试：处理临时网络故障
            ModelRetryMiddleware(
                max_retries=3,
                backoff_factor=2.0,
                initial_delay=1.0,
            ),
            # Tool 调用重试：处理工具临时故障
            ToolRetryMiddleware(
                max_retries=2,
                backoff_factor=1.5,
                initial_delay=0.5,
            ),
            DynamicSystemPromptMiddleware(),
        ],
        response_format=AutoStrategy(SupervisorResponse),
        # response_format=ProviderStrategy(SupervisorResponse),
        name="supervisor",
    )
    logger.info("Supervisor Agent 创建成功")
    return supervisor

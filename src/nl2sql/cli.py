"""NL2SQL 命令行工具

通过 Supervisor Agent 交互，支持多轮对话记忆。
"""

import argparse
import asyncio
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from prompt_toolkit import PromptSession


def _sync_rag_on_startup() -> None:
    """启动时同步 RAG 索引"""
    from src.nl2sql.infra.store.qa_rag import sync_qa_index

    try:
        print("开始检查 RAG 索引状态...")
        result = sync_qa_index()

        if result.changed:
            print(
                f"✓ RAG 索引已更新: {result.total_qas} 条 QA, "
                f"{result.total_chunks} 个分片 | {result.reason}"
            )
        else:
            print(
                f"✓ RAG 索引已是最新: {result.total_qas} 条 QA, "
                f"{result.total_chunks} 个分片 | {result.reason}"
            )
    except ImportError as e:
        print("✗ RAG 索引同步失败：缺少 FAISS 依赖。")
        print("请先安装 `faiss-cpu` 后再启动。")
        print(f"详细错误: {e}")
        raise
    except Exception as e:
        print(f"✗ RAG 索引同步失败: {e}")
        raise


def main() -> None:
    """交互式命令行"""
    parser = argparse.ArgumentParser(description="NL2SQL 命令行工具 (Supervisor 版本)")
    parser.add_argument(
        "--thread-id",
        default=None,
        help="会话线程 ID（相同 thread_id 可复用对话上下文）",
    )
    args = parser.parse_args()

    # 启动时自动同步 RAG 索引
    _sync_rag_on_startup()

    thread_id = args.thread_id or str(uuid4())

    print("=" * 60)
    print("NL2SQL 命令行工具 (Supervisor 版本)")
    print("=" * 60)
    print(f"Thread ID: {thread_id}")
    print("输入 'exit' 退出\n")

    session: PromptSession[str] = PromptSession()

    async def run_interactive():
        from src.core.observer import create_monitored_config
        from src.nl2sql.config.settings import get_agent_config
        from src.nl2sql.infra.memory.checkpointer import get_checkpointer_manager
        from src.nl2sql.supervisor.agent import create_supervisor

        # 初始化 checkpointer
        checkpointer_manager = get_checkpointer_manager()
        await checkpointer_manager.init()

        try:
            supervisor = await create_supervisor(checkpointer_manager.checkpointer)
            agent_config = get_agent_config()

            while True:
                try:
                    question = session.prompt("问题> ").strip()

                    if question.lower() in {"exit", "quit", "q"}:
                        print("再见!")
                        break

                    if not question:
                        continue

                    print()
                    final_answer: str | None = None

                    config = create_monitored_config(
                        session_id=thread_id,
                        base_config={
                            "configurable": {"thread_id": thread_id},
                            "recursion_limit": agent_config.graph_recursion_limit,
                        },
                        run_name="nl2sql",
                    )
                    async for event in supervisor.astream_events(
                        {"messages": [HumanMessage(content=question)]},
                        config=config,
                        version="v2",
                    ):
                        if event["event"] == "on_chat_model_stream":
                            chunk = event["data"]["chunk"]
                            if hasattr(chunk, "content") and chunk.content:
                                print(chunk.content, end="", flush=True)
                                final_answer = (final_answer or "") + chunk.content

                        # 捕获最终响应
                        if event["event"] == "on_chain_end" and event["name"] == "nl2sql":
                            if final_answer is None:
                                result = event["data"]["output"]
                                if isinstance(result, dict) and "messages" in result:
                                    messages = result["messages"]
                                    if messages:
                                        last_msg = messages[-1]
                                        if isinstance(last_msg, AIMessage):
                                            final_answer = str(last_msg.content)

                    if final_answer:
                        print(f"\n{'=' * 60}\n")

                except KeyboardInterrupt:
                    print("\n\n再见!")
                    break
                except Exception as e:
                    print(f"\n异常: {e}\n")
        finally:
            await checkpointer_manager.close()

    asyncio.run(run_interactive())


if __name__ == "__main__":
    main()

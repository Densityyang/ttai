"""tt-ai 服务入口模块

提供开发环境、生产环境和 CLI 三种启动方式。
"""

import time

import typer
import uvicorn

from src.core.settings import get_settings

shell_app = typer.Typer()


def create_app():
    """创建 FastAPI 应用实例"""
    from fastapi import FastAPI
    from starlette.middleware.cors import CORSMiddleware

    from src.nl2sql.api import lifespan

    settings = get_settings()

    app = FastAPI(
        title="tt-ai",
        description="NL2SQL AI 服务 - 自然语言转 SQL 查询",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.auth_enabled else None,
        redoc_url="/redoc" if settings.auth_enabled else None,
        openapi_url="/openapi.json" if settings.auth_enabled else None,
    )

    # 跨域配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


@shell_app.command()
def dev(
    host: str = typer.Option(default="0.0.0.0", help="监听主机 IP"),
    port: int | None = typer.Option(default=None, help="监听端口"),
    reload_dirs: list[str] | None = typer.Option(
        default=None, help="热重载监听目录，可多次传入"
    ),
):
    """开发环境启动（热重载）"""
    settings = get_settings()
    actual_port = port if port is not None else settings.api_port

    uvicorn.run(
        app="main:create_app",
        host=host,
        port=actual_port,
        lifespan="on",
        factory=True,
        reload=True,
        reload_dirs=reload_dirs,
    )


@shell_app.command()
def prod(
    host: str = typer.Option(default="0.0.0.0", help="监听主机 IP"),
    port: int | None = typer.Option(default=None, help="监听端口"),
    workers: int = typer.Option(default=1, help="Worker 进程数"),
):
    """生产环境启动

    使用 uvicorn 的多 worker 模式运行，适合生产环境部署。
    """
    settings = get_settings()
    actual_port = port if port is not None else settings.api_port

    uvicorn.run(
        app="main:create_app",
        host=host,
        port=actual_port,
        lifespan="on",
        factory=True,
        workers=workers,
    )


@shell_app.command()
def cli(
    thread_id: str | None = typer.Option(default=None, help="会话线程 ID"),
    sync_rag: bool = typer.Option(default=False, help="同步 RAG 索引后退出"),
    force: bool = typer.Option(default=False, help="强制重建 RAG 索引"),
):
    """启动交互式命令行工具

    通过 Supervisor Agent 交互，支持多轮对话记忆。
    相同 thread_id 可复用对话上下文。
    """
    from uuid import uuid4

    if sync_rag:
        from src.nl2sql.infra.store.qa_rag import sync_qa_index

        try:
            result = sync_qa_index(force=force)
        except ImportError as e:
            print("RAG 索引同步失败：缺少 FAISS 依赖。")
            print("请先安装 `faiss-cpu`，再执行 --sync-rag。")
            print(f"详细错误: {e}")
            raise typer.Exit(1)

        print("=" * 60)
        print("RAG 索引同步结果")
        print("=" * 60)
        print(f"变更: {'是' if result.changed else '否'}")
        print(f"QA 条目数: {result.total_qas}")
        print(f"分片数: {result.total_chunks}")
        print(f"说明: {result.reason}")
        raise typer.Exit(0)

    actual_thread_id = thread_id or str(uuid4())

    print("=" * 60)
    print("NL2SQL 命令行工具 (Supervisor 版本)")
    print("=" * 60)
    print(f"Thread ID: {actual_thread_id}")
    print("输入 'exit' 退出\n")

    # 使用 prompt_toolkit 的 asyncio 集成
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_input
    from prompt_toolkit.output import create_output

    async def run_interactive():
        from langchain_core.messages import AIMessage, HumanMessage

        from src.core.observer import create_monitored_config
        from src.nl2sql.config.settings import get_agent_config
        from src.nl2sql.infra.memory.checkpointer import get_checkpointer_manager
        from src.nl2sql.infra.runtime.registry import warmup_runtime
        from src.nl2sql.infra.store.database import get_db_manager
        from src.nl2sql.supervisor.agent import create_supervisor

        agent_config = get_agent_config()

        # 初始化数据库连接和预热 runtime
        await get_db_manager(schema=agent_config.nl2sql_db_schema)
        await warmup_runtime()

        # 初始化 checkpointer
        checkpointer_manager = get_checkpointer_manager()
        await checkpointer_manager.init()

        try:
            supervisor = await create_supervisor(checkpointer_manager.checkpointer)

            # 在 asyncio 环境中创建 prompt_toolkit session
            with create_app_session(input=create_input(), output=create_output()):
                session: PromptSession[str] = PromptSession()

                while True:
                    try:
                        question = await session.prompt_async("问题> ")
                        question = question.strip()

                        if question.lower() in {"exit", "quit", "q"}:
                            print("再见!")
                            break

                        if not question:
                            continue

                        print()
                        start_time = time.perf_counter()
                        final_answer: str | None = None

                        config = create_monitored_config(
                            session_id=actual_thread_id,
                            base_config={
                                "configurable": {"thread_id": actual_thread_id},
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
                                    if isinstance(chunk.content, str):
                                        print(chunk.content, end="", flush=True)
                                        final_answer = (final_answer or "") + chunk.content

                            # 捕获最终响应
                            if event["event"] == "on_chain_end":
                                output = event.get("data", {}).get("output")
                                if isinstance(output, dict) and "structured_response" in output:
                                    structured = output["structured_response"]
                                    if structured and hasattr(structured, "blocks") and structured.blocks:
                                        # 直接输出结构化响应块，避免额外渲染器模块依赖
                                        rendered_blocks: list[object] = []
                                        for block in structured.blocks:
                                            if hasattr(block, "model_dump"):
                                                rendered_blocks.append(block.model_dump())
                                            elif hasattr(block, "dict"):
                                                rendered_blocks.append(block.dict())  # type: ignore[reportUnknownMemberType]
                                            else:
                                                rendered_blocks.append(block)

                                        if rendered_blocks:
                                            # 如果前面没有大模型直接输出的纯文本，此处打印各区块
                                            if final_answer is None:
                                                final_answer = ""
                                            import json

                                            block_output = json.dumps(rendered_blocks, ensure_ascii=False, indent=2)
                                            print(f"\n[结构化响应块]:\n{block_output}", end="", flush=True)
                                            final_answer += f"\n[结构化响应块]:\n{block_output}"

                                if final_answer is None and event["name"] == "nl2sql":
                                    result = event.get("data", {}).get("output")
                                    if isinstance(result, dict) and "messages" in result:
                                        messages = result["messages"]
                                        if messages:
                                            last_msg = messages[-1]
                                            if isinstance(last_msg, AIMessage):
                                                final_answer = str(last_msg.content)
                                                print(final_answer, end="", flush=True)

                        if final_answer:
                            elapsed = time.perf_counter() - start_time
                            print(f"\n(耗时 {elapsed:.2f}s)")
                            print(f"{'=' * 60}\n")

                    except KeyboardInterrupt:
                        print("\n\n再见!")
                        break
                    except Exception as e:
                        print(f"\n异常: {e}\n")
                        import traceback
                        traceback.print_exc()
        finally:
            await checkpointer_manager.close()
            from src.nl2sql.infra.store.database import close_db_manager
            await close_db_manager()

    import asyncio

    asyncio.run(run_interactive())


if __name__ == "__main__":
    shell_app()

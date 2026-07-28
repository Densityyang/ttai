"""tt-ai ??????

???????????? CLI ???????
"""

import time

import typer
import uvicorn

from src.core.settings import get_settings

shell_app = typer.Typer()


def create_app():
    """?? FastAPI ????"""
    from fastapi import FastAPI
    from starlette.middleware.cors import CORSMiddleware

    from src.nl2sql.api import lifespan
    from src.nl2sql.v2 import register_v1_gone_routes, register_v2_routes

    settings = get_settings()

    app = FastAPI(
        title="tt-ai",
        description="NL2SQL AI ?? - ????? SQL ??",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.auth_enabled else None,
        redoc_url="/redoc" if settings.auth_enabled else None,
        openapi_url="/openapi.json" if settings.auth_enabled else None,
    )
    register_v2_routes(app)
    register_v1_gone_routes(app)

    # ????
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        from src.nl2sql.infra.llm.gateway import model_gateway_available

        model_ready = model_gateway_available()
        container = getattr(app.state, "container", None)
        audit_ready = settings.service_mode != "product" or bool(
            getattr(container, "audit_available", False)
        )
        ready = (not settings.model_required or model_ready) and audit_ready
        payload = {
            "status": "ready" if ready else "not_ready",
            "service_mode": settings.service_mode,
            "components": {
                "model": "ready" if model_ready else "unavailable",
                "control_audit": "ready" if audit_ready else "unavailable",
            },
        }
        if ready:
            return payload
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=payload)

    return app


@shell_app.command()
def dev(
    host: str = typer.Option(default="0.0.0.0", help="???? IP"),
    port: int | None = typer.Option(default=None, help="????"),
    reload_dirs: list[str] | None = typer.Option(
        default=None, help="?????????????"
    ),
):
    """???????????"""
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
    host: str = typer.Option(default="0.0.0.0", help="???? IP"),
    port: int | None = typer.Option(default=None, help="????"),
    workers: int = typer.Option(default=1, help="Worker ???"),
):
    """??????

    ?? uvicorn ?? worker ??????????????
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
    thread_id: str | None = typer.Option(default=None, help="???? ID"),
    sync_rag: bool = typer.Option(default=False, help="?? RAG ?????"),
    force: bool = typer.Option(default=False, help="???? RAG ??"),
):
    """??????????

    ?? Supervisor Agent ????????????
    ?? thread_id ?????????
    """
    from uuid import uuid4

    if sync_rag:
        from src.nl2sql.infra.store.qa_rag import sync_qa_index

        try:
            result = sync_qa_index(force=force)
        except ImportError as e:
            print("RAG ????????? FAISS ???")
            print("???? `faiss-cpu`???? --sync-rag?")
            print(f"????: {e}")
            raise typer.Exit(1)

        print("=" * 60)
        print("RAG ??????")
        print("=" * 60)
        print(f"??: {'?' if result.changed else '?'}")
        print(f"QA ???: {result.total_qas}")
        print(f"???: {result.total_chunks}")
        print(f"??: {result.reason}")
        raise typer.Exit(0)

    actual_thread_id = thread_id or str(uuid4())

    print("=" * 60)
    print("NL2SQL ????? (Supervisor ??)")
    print("=" * 60)
    print(f"Thread ID: {actual_thread_id}")
    print("?? 'exit' ??\n")

    # ?? prompt_toolkit ? asyncio ??
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

        # ??????????? runtime
        await get_db_manager(schema=agent_config.nl2sql_db_schema)
        await warmup_runtime()

        # ??? checkpointer
        checkpointer_manager = get_checkpointer_manager()
        await checkpointer_manager.init()

        try:
            supervisor = await create_supervisor(checkpointer_manager.checkpointer)

            # ? asyncio ????? prompt_toolkit session
            with create_app_session(input=create_input(), output=create_output()):
                session: PromptSession[str] = PromptSession()

                while True:
                    try:
                        question = await session.prompt_async("??> ")
                        question = question.strip()

                        if question.lower() in {"exit", "quit", "q"}:
                            print("??!")
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

                            # ??????
                            if event["event"] == "on_chain_end":
                                output = event.get("data", {}).get("output")
                                if isinstance(output, dict) and "structured_response" in output:
                                    structured = output["structured_response"]
                                    if structured and hasattr(structured, "blocks") and structured.blocks:
                                        # ??????????????????????
                                        rendered_blocks: list[object] = []
                                        for block in structured.blocks:
                                            if hasattr(block, "model_dump"):
                                                rendered_blocks.append(block.model_dump())
                                            elif hasattr(block, "dict"):
                                                rendered_blocks.append(block.dict())  # type: ignore[reportUnknownMemberType]
                                            else:
                                                rendered_blocks.append(block)

                                        if rendered_blocks:
                                            # ?????????????????????????
                                            if final_answer is None:
                                                final_answer = ""
                                            import json

                                            block_output = json.dumps(rendered_blocks, ensure_ascii=False, indent=2)
                                            print(f"\n[??????]:\n{block_output}", end="", flush=True)
                                            final_answer += f"\n[??????]:\n{block_output}"

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
                            print(f"\n(?? {elapsed:.2f}s)")
                            print(f"{'=' * 60}\n")

                    except KeyboardInterrupt:
                        print("\n\n??!")
                        break
                    except Exception as e:
                        print(f"\n??: {e}\n")
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

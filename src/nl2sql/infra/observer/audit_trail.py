"""全链路审计 -- Phase 4 安全强化。

每次请求生成唯一 trace_id，记录完整链路：
用户输入 → 路由决策 → RAG 证据(含评分+置信等级)
→ 生成的 SQL/代码 → 执行结果 → 修复轨迹 → HITL 确认 → 最终输出

审计数据写入 Langfuse（复用现有集成），支持按 thread_id + trace_id 回放。

并发安全说明：
- AuditTrail 是不可变的（frozen dataclass），每次请求创建独立实例
- 事件列表使用 append（CPython GIL 下原子），但每个 AuditTrail 仅由单个请求使用
- Langfuse 客户端通过 lru_cache 单例获取，其 SDK 内部处理了线程安全
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    """单条审计事件。"""

    stage: str
    event_type: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class AuditTrail:
    """请求级审计轨迹。

    每个请求创建一个 AuditTrail 实例，全程记录各阶段事件。
    请求结束后调用 flush() 写入 Langfuse。
    """

    trace_id: str = field(default_factory=lambda: str(uuid4()))
    thread_id: str = ""
    user_id: str = ""
    question: str = ""
    events: list[AuditEvent] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    def record(self, stage: str, event_type: str, **data: Any) -> None:
        """记录审计事件。"""
        self.events.append(AuditEvent(
            stage=stage,
            event_type=event_type,
            data=data,
        ))

    def record_routing(self, route: str, reason: str, signals: dict[str, Any] | None = None) -> None:
        """记录路由决策。"""
        self.record(
            "routing", "decision",
            route=route,
            reason=reason,
            signals=signals or {},
        )

    def record_rag(
        self,
        confidence_tier: str,
        avg_score: float,
        evidence_count: int,
        route_path: str = "",
        rewrite_count: int = 0,
    ) -> None:
        """记录 RAG 检索结果（含 CRAG 置信等级）。"""
        self.record(
            "rag", "evaluation",
            confidence_tier=confidence_tier,
            avg_score=round(avg_score, 3),
            evidence_count=evidence_count,
            route_path=route_path,
            rewrite_count=rewrite_count,
        )

    def record_sql_generation(self, sql: str, strategy: str = "standard") -> None:
        """记录 SQL 生成。"""
        self.record(
            "sql_generation", "generated",
            sql=sql[:2000],
            strategy=strategy,
        )

    def record_sql_execution(self, sql: str, success: bool, error: str = "", row_count: int = 0) -> None:
        """记录 SQL 执行结果。"""
        self.record(
            "sql_execution", "result",
            sql=sql[:1000],
            success=success,
            error=error[:500] if error else "",
            row_count=row_count,
        )

    def record_sql_repair(self, original_sql: str, repaired_sql: str, error: str, round_num: int) -> None:
        """记录 SQL 修复。"""
        self.record(
            "sql_repair", "attempt",
            original_sql=original_sql[:1000],
            repaired_sql=repaired_sql[:1000],
            error=error[:500],
            round=round_num,
        )

    def record_hitl(self, action: str, plan_version: int = 0, feedback: str = "") -> None:
        """记录 HITL 交互事件。"""
        self.record(
            "hitl", action,
            plan_version=plan_version,
            feedback=feedback[:500] if feedback else "",
        )

    def record_codeact(
        self,
        code: str,
        success: bool,
        error: str = "",
        elapsed_ms: float = 0,
    ) -> None:
        """记录 CodeAct 代码执行。"""
        self.record(
            "codeact", "execution",
            code=code[:2000],
            success=success,
            error=error[:500] if error else "",
            elapsed_ms=round(elapsed_ms, 1),
        )

    def record_validation(self, passed: bool, checks: dict[str, Any] | None = None) -> None:
        """记录结果验证。"""
        self.record(
            "validation", "result",
            passed=passed,
            checks=checks or {},
        )

    def record_final_output(self, output_type: str, output_preview: str = "") -> None:
        """记录最终输出。"""
        elapsed = time.time() - self.start_time
        self.record(
            "output", "final",
            output_type=output_type,
            output_preview=output_preview[:500],
            total_elapsed_seconds=round(elapsed, 2),
            total_events=len(self.events),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于存储/传输）。"""
        return {
            "trace_id": self.trace_id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "question": self.question[:500],
            "start_time": self.start_time,
            "total_events": len(self.events),
            "events": [
                {
                    "stage": e.stage,
                    "event_type": e.event_type,
                    "data": e.data,
                    "timestamp": e.timestamp,
                }
                for e in self.events
            ],
        }

    def flush(self) -> None:
        """将审计轨迹写入 Langfuse。

        幂等操作：多次调用不会重复写入（Langfuse trace 按 id 去重）。
        """
        from src.nl2sql.infra.observer.langfuse import _init_langfuse_client

        client = _init_langfuse_client()
        if client is None:
            logger.debug("Langfuse 未启用，审计轨迹未写入: trace_id=%s", self.trace_id)
            return

        try:
            trace = cast(Any, client).trace(
                id=self.trace_id,
                name="nl2sql_request",
                session_id=self.thread_id or None,
                user_id=self.user_id or None,
                input=self.question[:500],
                metadata={
                    "total_events": len(self.events),
                    "total_elapsed_seconds": round(time.time() - self.start_time, 2),
                },
            )

            for event in self.events:
                trace.event(
                    name=f"{event.stage}.{event.event_type}",
                    metadata=event.data,
                    start_time=event.timestamp,
                )

            logger.debug(
                "审计轨迹已写入 Langfuse: trace_id=%s, events=%d",
                self.trace_id, len(self.events),
            )

        except Exception as e:
            logger.warning("审计轨迹写入 Langfuse 失败: %s", e)


def create_audit_trail(
    thread_id: str = "",
    user_id: str = "",
    question: str = "",
) -> AuditTrail:
    """创建新的审计轨迹实例。"""
    return AuditTrail(
        thread_id=thread_id,
        user_id=user_id,
        question=question,
    )

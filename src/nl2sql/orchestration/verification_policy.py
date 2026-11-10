"""Shared structural policy for terminal typed-result verification."""

from __future__ import annotations

from src.nl2sql.contracts import ExecutionPlan, VerifyStep


def verify_execution_plan_policy(
    execution_plan: ExecutionPlan,
) -> tuple[str, ...]:
    """Return stable failures for the mandatory terminal VerifyStep contract."""

    verify_steps = [
        step for step in execution_plan.steps if isinstance(step, VerifyStep)
    ]
    if not verify_steps:
        return ("execution_plan_verify_missing",)
    if len(verify_steps) > 1:
        return ("execution_plan_verify_duplicate",)

    verify = verify_steps[0]
    failures: list[str] = []
    if "typed_result_present" not in verify.invariant_ids:
        failures.append("execution_plan_verify_invariant_missing")
    if any(verify.step_id in step.depends_on for step in execution_plan.steps):
        failures.append("execution_plan_verify_not_terminal")

    dependency_ids = set(verify.depends_on)
    referenced_ids = {reference.split(".", 1)[0] for reference in verify.input_refs}
    if dependency_ids != referenced_ids:
        failures.append("execution_plan_verify_coverage_incomplete")

    steps_by_id = {step.step_id: step for step in execution_plan.steps}
    ancestors: set[str] = set()
    pending = list(verify.depends_on)
    while pending:
        step_id = pending.pop()
        if step_id in ancestors:
            continue
        ancestors.add(step_id)
        step = steps_by_id.get(step_id)
        if step is None:
            failures.append("execution_plan_verify_coverage_incomplete")
            continue
        pending.extend(step.depends_on)

    required_steps = {
        step.step_id for step in execution_plan.steps if not isinstance(step, VerifyStep)
    }
    if not required_steps.issubset(ancestors):
        failures.append("execution_plan_verify_coverage_incomplete")
    return tuple(dict.fromkeys(failures))


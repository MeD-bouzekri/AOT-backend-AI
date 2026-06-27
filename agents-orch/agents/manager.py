"""
manager.py - department manager (Level 2 supervisor).

A manager receives its mandate from the Planner, runs the workers the plan assigned to its
department, and synthesizes a DepartmentResult. Workers run through the generic
ConfigurableAgent. The manager attributes each task (assigned_by = manager) so the dashboard
can draw the tree and show who assigned what.

For the demo a manager runs its selected workers in sequence and rolls up the results. In
live mode it could reason (ReAct) about ordering; the structure supports that without
changing callers.
"""

from __future__ import annotations

from schemas import AgentSpec, DepartmentResult, WorkerResult, CompanyContext
from agents.configurable import run_worker


def run_department(
    department: str,
    worker_names: list[str],
    context: dict,
    ctx: CompanyContext,
    emit=None,
) -> DepartmentResult:
    """Run all assigned workers for one department and roll up the result.

    `emit(StepEvent-like dict)` is an optional callback so the graph/gateway can stream
    progress; manager.py itself stays transport-agnostic.
    """
    specs = {c.name: c for c in ctx.capabilities if c.level == "worker"}
    manager_name = f"{department}_manager"

    results: list[WorkerResult] = []
    for wname in worker_names:
        spec = specs.get(wname)
        if spec is None or spec.department != department:
            continue  # not this department's worker

        if emit:
            emit({"department": department, "level": "worker", "agent": wname,
                  "assigned_by": manager_name, "phase": f"{spec.role}", "status": "running"})

        result = run_worker(spec, context)
        results.append(result)

        if emit:
            emit({"department": department, "level": "worker", "agent": wname,
                  "assigned_by": manager_name, "phase": "done", "status": result.status,
                  "output": result.output, "reasoning": result.reasoning,
                  "tools_used": [t.strip() for t in (result.tool_used or "").split(",") if t.strip()]})

    status = "done" if all(r.status == "done" for r in results) else "error"
    reasoning = (
        f"{manager_name} assigned {len(results)} task(s): "
        f"{', '.join(r.worker for r in results)}."
    )
    return DepartmentResult(
        department=department, manager_reasoning=reasoning,
        workers=results, status=status,
    )


def workers_for_department(department: str, plan_workers: list[str],
                           ctx: CompanyContext) -> list[str]:
    """Filter the plan's worker list down to a given department's workers."""
    specs = {c.name: c.department for c in ctx.capabilities if c.level == "worker"}
    return [w for w in plan_workers if specs.get(w) == department]

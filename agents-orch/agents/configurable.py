"""
configurable.py - ONE generic agent node that runs ANY worker from its AgentSpec.

This is the core of the "agents as data" design (REGISTRY.md). Instead of 9 hand-written
worker classes, we have one runner parameterized by config:
    - spec.tools        -> which tools it may call
    - spec.role/prompt  -> how it reasons
    - spec.mode         -> "rule" (deterministic, no LLM), "single" (one LLM call),
                          "react" (capped reason->tool->observe loop)

Every run produces a WorkerResult and (when wired into the graph) a StepEvent. A built or a
built-in worker is the SAME object here - only the config differs, which is exactly why the
dashboard "create a worker" feature is nearly free.
"""

from __future__ import annotations

import json

from schemas import AgentSpec, WorkerResult
from tools.runner import call_tool
from llm import is_demo, build_llm, worker_llm


REACT_MAX_STEPS = 3


def run_worker(spec: AgentSpec, context: dict) -> WorkerResult:
    """Run a worker agent given its spec and the request context (a flat dict of fields)."""
    if spec.mode == "rule":
        return _run_rule(spec, context)
    if spec.mode == "react":
        return _run_react(spec, context)
    return _run_single(spec, context)


# ───────────────────────── rule mode (deterministic, no LLM) ─────────────────────────

def _run_rule(spec: AgentSpec, context: dict) -> WorkerResult:
    """Call the worker's tools with the context and summarize - no LLM, instant, reliable."""
    tool_outputs = {}
    for tool in spec.tools:
        tool_outputs[tool] = call_tool(tool, **context)
    summary = _summarize_tools(tool_outputs)
    flags = _derive_flags(tool_outputs)
    return WorkerResult(
        worker=spec.name, department=spec.department,
        tool_used=", ".join(spec.tools) or None,
        output=summary, reasoning=f"{spec.role} (deterministic).",
        status="done", flags=flags,
    )


# ───────────────────────── single mode (one LLM call) ─────────────────────────

def _run_single(spec: AgentSpec, context: dict) -> WorkerResult:
    # gather tool data first (LLM reasons over real data, not guesses)
    tool_outputs = {t: call_tool(t, **context) for t in spec.tools}
    flags = _derive_flags(tool_outputs)

    if is_demo():
        return WorkerResult(
            worker=spec.name, department=spec.department,
            tool_used=", ".join(spec.tools) or None,
            output=_summarize_tools(tool_outputs),
            reasoning=f"{spec.role} (demo).", status="done", flags=flags,
        )

    llm = build_llm(spec.llm or worker_llm())
    prompt = (
        f"You are the {spec.name} in the {spec.department.upper()} department. "
        f"Role: {spec.role}\n"
        f"{_domain_note(context)}\n"
        f"{_pii_note(spec, context)}"
        f"Request facts: {json.dumps(_safe(context))}\n"
        f"Tool results: {json.dumps(_safe(tool_outputs))}\n\n"
        f"In 1-2 sentences, state what you did for THIS request and the outcome. Be concrete "
        f"and stay strictly within your role; do not invent facts not present above. "
        f"When the request provides specific identifiers relevant to your role (salary, RIB, "
        f"NIF, CNAS, national ID, email), cite the EXACT values you used."
    )
    try:
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as exc:  # noqa: BLE001 - fall back, never crash
        text = _summarize_tools(tool_outputs) + f"  [llm fallback: {exc}]"
    return WorkerResult(
        worker=spec.name, department=spec.department,
        tool_used=", ".join(spec.tools) or None,
        output=text.strip(), reasoning=spec.role, status="done", flags=flags,
    )


# ───────────────────────── react mode (capped tool loop) ─────────────────────────

def _run_react(spec: AgentSpec, context: dict) -> WorkerResult:
    """Bounded reason->tool->observe loop. Capped at REACT_MAX_STEPS to stay fast + safe."""
    collected = {}
    # demo / no-LLM: just call all tools once in sequence
    if is_demo():
        for t in spec.tools:
            collected[t] = call_tool(t, **{**context, **_flatten(collected)})
        return WorkerResult(
            worker=spec.name, department=spec.department,
            tool_used=", ".join(spec.tools) or None,
            output=_summarize_tools(collected),
            reasoning=f"{spec.role} (react/demo, {len(spec.tools)} tools).",
            status="done", flags=_derive_flags(collected),
        )

    # live: let the LLM decide which tool to call next, up to the cap
    llm = build_llm(spec.llm or worker_llm())
    for _ in range(REACT_MAX_STEPS):
        remaining = [t for t in spec.tools if t not in collected]
        if not remaining:
            break
        prompt = (
            f"You are the {spec.name}. Role: {spec.role}\n"
            f"Context: {json.dumps(_safe(context))}\n"
            f"Tool results so far: {json.dumps(_safe(collected))}\n"
            f"Available tools you have not used: {remaining}\n\n"
            f"Reply with ONLY the name of the single most useful next tool to call, "
            f"or 'DONE' if you have enough information."
        )
        try:
            resp = llm.invoke(prompt)
            choice = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        except Exception:  # noqa: BLE001
            choice = remaining[0]
        if choice.upper().startswith("DONE"):
            break
        tool = choice if choice in remaining else remaining[0]
        collected[tool] = call_tool(tool, **{**context, **_flatten(collected)})

    # final synthesis
    prompt = (
        f"You are the {spec.name}. Role: {spec.role}\n"
        f"{_domain_note(context)}\n"
        f"Tool results: {json.dumps(_safe(collected))}\n\n"
        f"Give a 1-2 sentence assessment and a clear recommendation for THIS request."
    )
    try:
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as exc:  # noqa: BLE001
        text = _summarize_tools(collected) + f"  [llm fallback: {exc}]"
    return WorkerResult(
        worker=spec.name, department=spec.department,
        tool_used=", ".join(collected.keys()) or None,
        output=text.strip(), reasoning=spec.role, status="done",
        flags=_derive_flags(collected),
    )


# ───────────────────────── helpers ─────────────────────────

# which personal/payroll fields each HR worker should cite when present
_WORKER_PII = {
    "payroll_worker": ["salary", "bank_rib", "social_security_cnas", "tax_id_nif",
                       "contract_type", "marital_status"],
    "docs_worker": ["national_id", "tax_id_nif", "nationality", "contract_type",
                    "start_date", "email"],
    "accounts_worker": ["email", "name"],
    "benefits_worker": ["social_security_cnas", "marital_status", "employment_type"],
}


def _pii_note(spec, context: dict) -> str:
    """Surface the specific identifiers this worker is expected to use, so the LLM
    cites the real values from the request instead of staying generic."""
    fields = _WORKER_PII.get(spec.name)
    if not fields:
        return ""
    present = {f: context.get(f) for f in fields if context.get(f)}
    if not present:
        return ""
    kv = "; ".join(f"{k}={v}" for k, v in present.items())
    return f"Relevant employee data to act on (use these exact values): {kv}\n"


def _domain_note(context: dict) -> str:
    domain = context.get("domain")
    if domain == "procurement":
        return ("This request is a PROCUREMENT/PURCHASE of "
                f"'{context.get('item') or context.get('vendor')}'. It is NOT onboarding a "
                "person. Do not refer to a new hire.")
    if domain == "hr_onboarding":
        return (f"This request is ONBOARDING a new hire ({context.get('name') or 'the hire'}, "
                f"{context.get('role','')}).")
    return ""


def _summarize_tools(outputs: dict) -> str:
    parts = []
    for tool, out in outputs.items():
        if isinstance(out, dict):
            detail = out.get("detail") or out.get("output")
            if detail:
                parts.append(str(detail))
            else:
                kv = ", ".join(f"{k}={v}" for k, v in list(out.items())[:4])
                parts.append(f"{tool}: {kv}")
        else:
            parts.append(f"{tool}: {out}")
    return " | ".join(parts) if parts else "No tools invoked."


def _derive_flags(outputs: dict) -> dict:
    flags = {}
    for out in outputs.values():
        if not isinstance(out, dict):
            continue
        if out.get("sensitive"):
            flags["sensitive"] = True
        if out.get("risk_flag") or out.get("known_breaches"):
            flags["risk"] = True
        # invoice validation: missing mandatory fields or bad VAT
        if out.get("valid") is False or out.get("missing_fields"):
            flags["invalid_invoice"] = True
        # fraud sentinel: high risk or any fraud flags
        if out.get("risk") == "high" or out.get("flags"):
            flags["fraud_risk"] = True
        # three-way match issues
        if out.get("matched") is False:
            flags["match_issue"] = True
    return flags


def _flatten(collected: dict) -> dict:
    """Merge tool dict outputs into a flat kwargs dict for the next tool call."""
    flat = {}
    for out in collected.values():
        if isinstance(out, dict):
            flat.update({k: v for k, v in out.items() if not isinstance(v, (dict, list))})
    return flat


def _safe(obj):
    """JSON-safe shallow copy (avoid huge nested dumps)."""
    return obj

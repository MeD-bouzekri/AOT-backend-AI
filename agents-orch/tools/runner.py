"""
runner.py - call any tool by name, built-in OR user-built.

Built-in tools  -> real Python functions in builtin.BUILTIN_TOOLS.
User-built tools -> config (ToolSpec) with a `mock_response` template. The runner returns the
                   template, optionally filled with the call args. NO code is generated or
                   executed for user-built tools - this is the safe, declarative path that
                   makes the dashboard "create a tool" feature possible.

The user-tool registry is loaded from data/registry_tools.json (created via the dashboard);
if the file is absent, only built-in tools are available.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.builtin import BUILTIN_TOOLS

_USER_TOOLS_FILE = Path(__file__).parent.parent / "data" / "registry_tools.json"


def _load_user_tools() -> dict[str, dict]:
    if not _USER_TOOLS_FILE.exists():
        return {}
    try:
        specs = json.loads(_USER_TOOLS_FILE.read_text(encoding="utf-8"))
        return {s["name"]: s for s in specs}
    except Exception:  # noqa: BLE001
        return {}


def list_tools() -> list[str]:
    return list(BUILTIN_TOOLS.keys()) + list(_load_user_tools().keys())


def call_tool(tool_name: str, /, **kwargs: Any) -> dict:
    """Run a tool by name. Returns a dict result and never raises for an unknown tool.

    `tool_name` is positional-only so it can never collide with a context field named
    'name' (e.g. a new hire's name) passed via **kwargs.
    """
    name = tool_name
    # map the request's person name to `person` so it reaches the tools without colliding
    # with the positional tool_name; then remove the raw `name` key.
    if "name" in kwargs and "person" not in kwargs:
        kwargs["person"] = kwargs["name"]
    kwargs.pop("name", None)
    # 1) built-in
    fn = BUILTIN_TOOLS.get(name)
    if fn is not None:
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a tool error must not crash the run
            return {"status": "error", "tool": name, "detail": str(exc)}

    # 2) user-built (declarative mock, no code execution)
    user = _load_user_tools().get(name)
    if user is not None:
        template = user.get("mock_response") or {}
        # shallow-fill {placeholders} in string values from kwargs
        filled = {
            k: (v.format(**kwargs) if isinstance(v, str) else v)
            for k, v in template.items()
        }
        return {"status": "ok", "tool": name, "simulated": True, **filled}

    # 3) unknown
    return {"status": "error", "tool": name, "detail": f"Unknown tool '{name}'"}


if __name__ == "__main__":
    print("Available tools:", list_tools())
    print(call_tool("credit_check", vendor="Slack"))
    print(call_tool("grant_access", scope=["production"], user="priya@acme.com"))
    print(call_tool("doc_catalog", employment_type="contractor"))
    print(call_tool("nonexistent"))

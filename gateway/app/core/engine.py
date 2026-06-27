"""
Bridge to the orchestration engine in agents-orch/.

The gateway never reimplements engine logic - it imports the compiled LangGraph app and
helper modules. agents-orch is not an installed package, so we add it to sys.path once here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parents[3] / "agents-orch"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

# engine modules
import graph as _graph                              # noqa: E402
import memory as _memory                            # noqa: E402
import sentinel as _sentinel                        # noqa: E402
import llm as _llm                                  # noqa: E402
from context_loader import load_company_context     # noqa: E402,F401
from tools.runner import list_tools                 # noqa: E402,F401
from tools import builtin as _builtin                # noqa: E402
import schemas as _schemas                          # noqa: E402

# re-export the surface the gateway uses
GRAPH = _graph.GRAPH
OrchState = _graph.OrchState
run_graph = _graph.run                              # run(raw_text, run_id) -> OrchState

recall = _memory.recall
remember = _memory.remember
make_record = _memory.make_record
feedback = _memory.feedback
memory_stats = _memory.stats

sentinel_scan = _sentinel.scan

validate_ollama = _llm.validate_ollama
list_ollama_models = _llm.list_ollama_models

reload_config = _builtin.reload_config
schemas = _schemas

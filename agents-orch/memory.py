"""
memory.py — Institutional Memory (the "learns over time" feature).

Stores a compact record of every completed run and lets the Planner retrieve similar past
cases to make wiser decisions ("last time a contractor asked for prod access, Security
blocked it"; "this vendor split invoices before").

Design avoids heavy local embedding models (which caused MemoryError via torch/onnx):
    - PRIMARY: Chroma with Gemini embeddings (cloud — no local model, no torch).
    - FALLBACK: a JSON store with keyword/overlap similarity (zero deps, always works).

Public API:
    remember(record)            -> persist a finished run
    recall(query, k=3)          -> list of similar past records
    feedback(run_id, outcome)   -> attach the real outcome/lesson to a remembered run
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone

_DATA = Path(__file__).parent / "data"
_JSON_STORE = _DATA / "memory_store.json"
_CHROMA_DIR = _DATA / "chroma"

_USE_CHROMA = os.getenv("MEMORY_BACKEND", "auto")   # auto | chroma | json


# ───────────────────────── record shape ─────────────────────────

def make_record(*, run_id: str, domain: str, summary: str, request: str,
                outcome: str, rule_id: str | None = None,
                department: str | None = None, lesson: str = "") -> dict:
    return {
        "run_id": run_id,
        "domain": domain,
        "summary": summary,
        "request": request,
        "outcome": outcome,                 # done | frozen | denied | awaiting_human
        "rule_id": rule_id,                 # e.g. SEC-04 / DZ-INV-02 if blocked
        "department": department,
        "lesson": lesson,                   # filled later via feedback()
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ───────────────────────── Chroma backend (Gemini embeddings) ─────────────────────────

_collection = None


def _get_chroma():
    global _collection
    if _collection is not None:
        return _collection
    import chromadb
    from chromadb.utils import embedding_functions
    ef = embedding_functions.GoogleGenerativeAiEmbeddingFunction(
        api_key=os.getenv("GOOGLE_API_KEY"), model_name="models/text-embedding-004"
    )
    client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
    _collection = client.get_or_create_collection("institutional_memory", embedding_function=ef)
    return _collection


def _chroma_available() -> bool:
    if _USE_CHROMA == "json":
        return False
    if not os.getenv("GOOGLE_API_KEY"):
        return False
    try:
        import chromadb  # noqa: F401
        _get_chroma()
        return True
    except Exception:  # noqa: BLE001
        return False


# ───────────────────────── JSON fallback ─────────────────────────

def _load_json() -> list[dict]:
    if _JSON_STORE.exists():
        try:
            return json.loads(_JSON_STORE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def _save_json(records: list[dict]) -> None:
    _DATA.mkdir(exist_ok=True)
    _JSON_STORE.write_text(json.dumps(records, indent=2), encoding="utf-8")


def _similarity(a: str, b: str) -> float:
    """Cheap token-overlap similarity for the JSON fallback."""
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ───────────────────────── public API ─────────────────────────

def remember(record: dict) -> None:
    """Persist a finished run to memory (both backends keep the JSON mirror)."""
    records = _load_json()
    records = [r for r in records if r.get("run_id") != record.get("run_id")]
    records.append(record)
    _save_json(records)

    if _chroma_available():
        try:
            col = _get_chroma()
            doc = f"{record['domain']} | {record['summary']} | {record['request']}"
            col.upsert(ids=[record["run_id"]], documents=[doc],
                       metadatas=[{k: (v if v is not None else "")
                                   for k, v in record.items() if k != "request"}])
        except Exception:  # noqa: BLE001
            pass  # JSON mirror already saved


def recall(query: str, k: int = 3) -> list[dict]:
    """Return up to k past records most similar to the query."""
    if _chroma_available():
        try:
            col = _get_chroma()
            res = col.query(query_texts=[query], n_results=k)
            metas = (res.get("metadatas") or [[]])[0]
            if metas:
                return metas
        except Exception:  # noqa: BLE001
            pass
    # JSON fallback: rank by token overlap
    records = _load_json()
    ranked = sorted(records, key=lambda r: _similarity(query, r.get("request", "")
                                                        + " " + r.get("summary", "")),
                    reverse=True)
    return ranked[:k]


def feedback(run_id: str, outcome: str | None = None, lesson: str = "") -> None:
    """Attach the real-world outcome/lesson to a remembered run (the feedback loop)."""
    records = _load_json()
    for r in records:
        if r.get("run_id") == run_id:
            if outcome:
                r["outcome"] = outcome
            if lesson:
                r["lesson"] = lesson
            r["feedback_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            break
    _save_json(records)
    if _chroma_available():
        try:
            col = _get_chroma()
            col.update(ids=[run_id], metadatas=[{"outcome": outcome or "", "lesson": lesson}])
        except Exception:  # noqa: BLE001
            pass


def stats() -> dict:
    records = _load_json()
    by_outcome: dict[str, int] = {}
    for r in records:
        by_outcome[r.get("outcome", "?")] = by_outcome.get(r.get("outcome", "?"), 0) + 1
    return {"total": len(records), "by_outcome": by_outcome,
            "backend": "chroma" if _chroma_available() else "json"}


if __name__ == "__main__":
    remember(make_record(run_id="t1", domain="invoice_ap",
                         summary="TechSupplies 485k DZD", request="invoice TechSupplies 485000 split",
                         outcome="frozen", rule_id="DZ-INV-02", department="finance",
                         lesson="Vendor splits invoices below 500k threshold."))
    remember(make_record(run_id="t2", domain="hr_onboarding",
                         summary="contractor prod access", request="onboard contractor production access",
                         outcome="frozen", rule_id="SEC-04", department="it"))
    print("stats:", stats())
    print("recall('vendor split invoice'):")
    for r in recall("vendor split invoice payment", k=2):
        print("  -", r.get("summary"), "->", r.get("outcome"), r.get("lesson", ""))

"""
llm.py - the LLM factory.

One factory turns any LLMConfig into a unified chat model. Three providers:
    claude  -> langchain_anthropic.ChatAnthropic   (default)
    gemini  -> langchain_google_genai.ChatGoogleGenerativeAI
    ollama  -> langchain_ollama.ChatOllama          (local/self-hosted)

Agent code never changes; only the config. Plus:
    - MODE=demo  -> agents short-circuit to canned outputs (zero cost, zero flake on stage)
    - validate_ollama() -> check a URL + that the model exists, before saving a config
"""

from __future__ import annotations

import os
import sys
from typing import Optional

# langchain_core lazily imports `transformers`/`torch` for optional token counting we never
# use. Loading torch can exhaust memory on some machines. Block those heavy imports so the
# Gemini/Anthropic clients import lean. (Safe: only the optional tokenizer path is affected.)
for _heavy in ("torch", "transformers"):
    sys.modules.setdefault(_heavy, None)  # type: ignore[assignment]

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()                               # load .env (GOOGLE_API_KEY, MODE, ...)
except ImportError:
    pass

from schemas import LLMConfig, OllamaValidation

# Provider imports are lazy so a missing optional dep never breaks startup.

MODE = os.getenv("MODE", "demo").lower()        # "demo" | "live"

# Ollama is the default brain - local/self-hosted, so sensitive data never leaves the
# company. The admin sets the server URL and model name (per agent, or these defaults).
_OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_PLANNER_MODEL = os.getenv("PLANNER_MODEL", "llama3.1:8b")
_WORKER_MODEL = os.getenv("WORKER_MODEL", "llama3.1:8b")
DEFAULT_PLANNER_LLM = LLMConfig(provider="ollama", model=_PLANNER_MODEL,
                                base_url=_OLLAMA_URL, temperature=0.1)
DEFAULT_WORKER_LLM = LLMConfig(provider="ollama", model=_WORKER_MODEL,
                               base_url=_OLLAMA_URL, temperature=0.2)


def _company_default_llm() -> Optional[dict]:
    """Read the admin-saved default LLM from company_config.json, if any.

    Set via PUT /api/settings/default-llm (validated against the server's
    /api/tags before saving). Returns None when unset -> callers fall back to
    the env-driven DEFAULT_*_LLM above.
    """
    import json
    from pathlib import Path
    try:
        path = Path(__file__).parent / "data" / "company_config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        dl = cfg.get("default_llm")
        if dl and dl.get("base_url") and dl.get("model"):
            return dl
    except Exception:  # noqa: BLE001 - never let config IO break a run
        pass
    return None


def planner_llm() -> LLMConfig:
    """Planner LLM: admin-saved default (if set) else the env default."""
    dl = _company_default_llm()
    if dl:
        return DEFAULT_PLANNER_LLM.model_copy(
            update={"base_url": dl["base_url"], "model": dl["model"]})
    return DEFAULT_PLANNER_LLM


def worker_llm() -> LLMConfig:
    """Worker LLM: admin-saved default (if set) else the env default."""
    dl = _company_default_llm()
    if dl:
        return DEFAULT_WORKER_LLM.model_copy(
            update={"base_url": dl["base_url"], "model": dl["model"]})
    return DEFAULT_WORKER_LLM


# ───────────────────────── secret resolution ─────────────────────────

def _resolve_key(cfg: LLMConfig) -> Optional[str]:
    """Resolve an api_key_ref (a secret NAME) to the actual key from the environment.

    Never store raw keys in the config. For the demo we read from env vars named after the
    ref, or fall back to the provider's conventional env var.
    """
    if cfg.api_key_ref:
        val = os.getenv(cfg.api_key_ref)
        if val:
            return val
    if cfg.provider == "claude":
        return os.getenv("ANTHROPIC_API_KEY")
    if cfg.provider == "gemini":
        return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    return None  # ollama needs no key


# ───────────────────────── rate-limit handling ─────────────────────────

import time
import threading

_LAST_CALL = [0.0]
_MIN_INTERVAL = float(os.getenv("LLM_MIN_INTERVAL", "1.0"))   # seconds between calls
_THROTTLE_LOCK = threading.Lock()


def _throttle():
    """Space out calls to stay under free-tier rate limits."""
    with _THROTTLE_LOCK:
        wait = _MIN_INTERVAL - (time.monotonic() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.monotonic()


class _RetryingModel:
    """Wraps a chat model: throttles calls and retries on 429/RESOURCE_EXHAUSTED."""
    def __init__(self, model, max_retries: int = 3):
        self._model = model
        self._max_retries = max_retries

    def with_structured_output(self, schema):
        self._model = self._model.with_structured_output(schema)
        return self

    def invoke(self, *args, **kwargs):
        delay = 2.0
        for attempt in range(self._max_retries + 1):
            _throttle()
            try:
                return self._model.invoke(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                is_rate = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
                if is_rate and attempt < self._max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise


# ───────────────────────── the factory ─────────────────────────

def build_llm(cfg: Optional[LLMConfig] = None):
    """Return a LangChain chat model for the given config (default: Claude worker model).

    In MODE=demo this is still constructed lazily but agents are expected to bypass it via
    their own demo path; build_llm only fully initializes a client in live mode.
    """
    cfg = cfg or DEFAULT_WORKER_LLM

    if cfg.provider == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            api_key=_resolve_key(cfg),
        )

    if cfg.provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        model = ChatGoogleGenerativeAI(
            model=cfg.model,
            temperature=cfg.temperature,
            max_output_tokens=cfg.max_tokens,
            google_api_key=_resolve_key(cfg),
        )
        return _RetryingModel(model)

    if cfg.provider == "ollama":
        from langchain_ollama import ChatOllama
        if not cfg.base_url:
            raise ValueError("Ollama provider requires base_url")
        return ChatOllama(
            model=cfg.model,
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            num_predict=cfg.max_tokens,
        )

    raise ValueError(f"Unknown provider: {cfg.provider}")


# ───────────────────────── Ollama validation ─────────────────────────

async def validate_ollama(base_url: str, model: str) -> OllamaValidation:
    """Check that an Ollama server is reachable and that `model` exists on it.

    Calls GET {base_url}/api/tags and verifies `model` is in the returned model names.
    Used by the admin LLM-config form before saving.
    """
    base_url = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        return OllamaValidation(ok=False, error=f"Ollama not reachable at {base_url}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return OllamaValidation(ok=False, error=f"Unexpected error: {exc}")

    names = [m.get("name", "") for m in data.get("models", [])]
    if model not in names:
        return OllamaValidation(
            ok=False,
            available=names,
            error=f"Model '{model}' not found on this server.",
        )
    return OllamaValidation(ok=True, available=names)


async def list_ollama_models(base_url: str) -> list[str]:
    """Return available model names from an Ollama server (for a dropdown)."""
    result = await validate_ollama(base_url, model="__list_only__")
    return result.available


# ───────────────────────── demo helper ─────────────────────────

def is_demo() -> bool:
    return MODE == "demo"

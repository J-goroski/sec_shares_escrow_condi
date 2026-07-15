"""
local_llm.py — EMBEDDED serverless LLM backend (llama-cpp-python).

Loads a plain ``.gguf`` weights file from ``analysis/models/`` directly into
the Python process.  Nothing to install as a service, nothing to start,
nothing to connect to — the alternative to running an Ollama server.

Model discovery (first hit wins):
    1. env ``LLM_GGUF_PATH``  — explicit path to a .gguf file
    2. the newest ``*.gguf`` in ``analysis/models/``
       (fetch one with:  python analysis/models/download_model.py)

Structured output uses llama.cpp's JSON-schema-constrained grammar, the same
guarantee the Ollama path gives: the reply *cannot* be schema-invalid JSON.

This module is not imported unless the gateway (``ollama_client``) routes to
it, and ``llama_cpp`` itself is imported lazily, so the dependency stays
optional (``pip install llama-cpp-python``).
"""

from __future__ import annotations

import glob
import json
import os
import threading
from typing import Optional

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

DEFAULT_NUM_CTX = 8192
DEFAULT_MAX_TOKENS = 1024

_lock = threading.Lock()          # llama.cpp contexts are not thread-safe
_llama = None                     # lazy singleton
_llama_path: Optional[str] = None


def discover_gguf() -> Optional[str]:
    """The weights file the embedded backend would load, or None."""
    explicit = os.environ.get("LLM_GGUF_PATH")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    hits = sorted(glob.glob(os.path.join(MODELS_DIR, "*.gguf")),
                  key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def _llama_cpp_installed() -> bool:
    try:
        import llama_cpp  # noqa: F401
        return True
    except ImportError:
        return False


def is_available() -> bool:
    """True when llama-cpp-python is installed AND a .gguf file is present."""
    return _llama_cpp_installed() and discover_gguf() is not None


def model_name() -> Optional[str]:
    path = discover_gguf()
    return os.path.basename(path) if path else None


def _get_llama():
    """Load the model once (a 2B Q4 model takes a few seconds and ~2 GB RAM)."""
    global _llama, _llama_path
    path = discover_gguf()
    if path is None:
        raise RuntimeError(
            "no .gguf model found - run: python analysis/models/download_model.py")
    with _lock:
        if _llama is None or _llama_path != path:
            from llama_cpp import Llama
            _llama = Llama(model_path=path, n_ctx=DEFAULT_NUM_CTX,
                           verbose=False)
            _llama_path = path
    return _llama


def _messages(prompt: str, system: str | None) -> list[dict]:
    # Fold any system prompt into the user turn: several small-model chat
    # templates (gemma-2 among them) raise "System role not supported", and
    # for instruction-following extraction the folded form is equivalent.
    if system:
        return [{"role": "user", "content": f"{system}\n\n{prompt}"}]
    return [{"role": "user", "content": prompt}]


def generate_json(
    prompt: str,
    schema: dict,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """One schema-constrained generation; returns the parsed JSON object."""
    llm = _get_llama()
    with _lock:
        out = llm.create_chat_completion(
            messages=_messages(prompt, system),
            response_format={"type": "json_object", "schema": schema},
            temperature=temperature,
            max_tokens=max_tokens,
        )
    content = out["choices"][0]["message"]["content"] or ""
    return json.loads(content)


def generate_text(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Plain (unconstrained) generation."""
    llm = _get_llama()
    with _lock:
        out = llm.create_chat_completion(
            messages=_messages(prompt, system),
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return out["choices"][0]["message"]["content"] or ""

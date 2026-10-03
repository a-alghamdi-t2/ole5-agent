"""Groq reachability. Is the key valid, and does the model we name exist?"""

from __future__ import annotations

from typing import Any

from groq import Groq

from ole5.config import get_settings

TIMEOUT = 20


def get_client() -> Groq:
    """A configured client. Raises if no key is set."""
    s = get_settings()
    if not s.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not set")
    return Groq(api_key=s.groq_api_key.get_secret_value(), timeout=TIMEOUT)


def ping() -> dict[str, Any]:
    """List the models this key can reach and check ours is among them."""
    s = get_settings()
    client = get_client()

    model_ids = sorted(m.id for m in client.models.list().data)
    found = s.groq_model in model_ids

    result: dict[str, Any] = {
        "models_available": len(model_ids),
        "model": s.groq_model,
        "model_found": found,
    }

    if not found:
        # The schema document writes the model as 'gpt-oss-120b'; Groq serves it
        # under a vendor prefix. Rather than guess which, show the near misses.
        near = [m for m in model_ids if "gpt-oss" in m or "120b" in m]
        result["close_matches"] = near or model_ids[:10]

    return result
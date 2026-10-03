"""Retrieval, over RAGent2.

One call to find(), their agentic retrieval: it reformulates the question,
searches several times, and grades each passage DIRECT or INFERENTIAL with a
reason. That grading is most of a verdict already, so this file maps rather
than reasons -- no second model call.

Chosen over search() after measuring both. find() was no slower and returns the
grading we would otherwise have had to produce ourselves.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from ole5.config import get_settings
from ole5.knowledge.contracts import Evidence, Retrieval, Verdict
from ole5.logging import get_logger

log = get_logger(__name__)

UNRELIABLE_STOPS = {"timeout", "error", "model_failed"}


class KnowledgeUnavailable(RuntimeError):
    """RAGent2 is not installed or not configured."""


@lru_cache(maxsize=1)
def _tenant() -> Any:
    """The tenant-scoped document interface. Built once; it caches models."""
    s = get_settings()
    if not s.groq_api_key:
        raise KnowledgeUnavailable("GROQ_API_KEY is not set")

    try:
        from ragent2 import Ragent
        from ragent2.config import Settings as RagSettings
    except ImportError as exc:
        raise KnowledgeUnavailable(
            "ragent2 is not installed; it comes from the private feed"
        ) from exc

    rag = Ragent(settings=RagSettings(
        groq_api_key=s.groq_api_key.get_secret_value(),
        qdrant_url=s.qdrant_url,
        docling_serve_api_url=s.docling_url,
        candidate_pool_size=s.kb_candidate_pool,
        hybrid_prefetch_limit=s.kb_candidate_pool,
        top_k=s.kb_top_k,
        tool_call_repair_attempts=s.kb_tool_repair_attempts,
        max_search_rounds=s.kb_max_rounds,
        embedding_service_timeout=s.kb_embed_timeout,
    ))
    log.info("knowledge ready", extra={"tenant": s.ragent_tenant,
                                       "pool": s.kb_candidate_pool})
    return rag.tenant(s.ragent_tenant)


def _to_evidence(chunk: Any) -> Evidence:
    meta = getattr(chunk, "metadata", None) or {}
    return Evidence(
        text=getattr(chunk, "text", "") or "",
        document=getattr(chunk, "document_name", None) or meta.get("document_name") or "unknown",
        section=meta.get("section"),
        source_file=meta.get("source_file"),
        relation=(getattr(chunk, "relation", None) or "UNKNOWN").upper(),
        score=int(getattr(chunk, "score", 0) or 0),
        reason=getattr(chunk, "reason", "") or "",
    )


def _verdict(evidence: list[Evidence]) -> Verdict:
    """Their grading, mapped to ours.

    DIRECT means a passage answers the question. INFERENTIAL means it is on the
    subject without answering -- worth showing a reviewer, not worth replying
    from, which is why it is partial rather than answerable.
    """
    if any(e.relation == "DIRECT" for e in evidence):
        return Verdict.ANSWERABLE
    if evidence:
        return Verdict.PARTIAL
    return Verdict.NOT_IN_KB


def retrieve(question: str) -> Retrieval:
    """Ask the knowledge base. Never raises: a failure is a verdict.

    Retrieval breaking must not stop a ticket being drafted. It becomes
    NOT_IN_KB with the error recorded, the orchestrator routes, and a human sees
    why -- which is the same outcome as an unanswerable question, and the safe
    one.
    """
    started = time.perf_counter()

    try:
        result = _tenant().find(question)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        log.exception("retrieval failed", extra={"ms": elapsed})
        return Retrieval(
            query=question,
            verdict=Verdict.NOT_IN_KB,
            elapsed_ms=elapsed,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    evidence = [_to_evidence(c) for c in (getattr(result, "chunks", None) or ())]
    diagnostics = getattr(result, "diagnostics", None)

    retrieval = Retrieval(
        query=question,
        verdict=_verdict(evidence),
        evidence=evidence,
        queries_tried=list(getattr(diagnostics, "queries_tried", ()) or ()),
        stop_reason=getattr(diagnostics, "stop_reason", None),
        warnings=list(getattr(result, "warnings", ()) or ()),
        elapsed_ms=elapsed,
    )

    returned = {e.document for e in evidence}
    trace = getattr(diagnostics, "trace", ()) or ()
    seen: dict[str, str] = {}
    for t in trace:
        name = getattr(t, "document_name", None)
        if not name or getattr(t, "returned", False) or name in returned:
            continue
        # IGNORE means graded and rejected; None means never graded at all.
        seen[name] = getattr(t, "relation", None) or "ungraded"
    retrieval.considered = sorted(f"{n} ({r.lower()})" for n, r in seen.items())

    if not evidence and (retrieval.stop_reason in UNRELIABLE_STOPS or retrieval.warnings):
        retrieval.error = retrieval.error or f"retrieval incomplete: {retrieval.stop_reason}"
        log.warning("retrieval incomplete", extra={"stop": retrieval.stop_reason,
                                                   "query": question[:80]})

    log.info(
        "retrieved",
        extra={"verdict": retrieval.verdict.value, "chunks": len(evidence),
               "direct": len(retrieval.direct), "ms": elapsed,
               "stop": retrieval.stop_reason},
    )

    if retrieval.stop_reason == "timeout":
        # Distinct from finding nothing: it ran out of time before grading what
        # it had. Worth seeing in the logs, because it looks like NOT_IN_KB.
        log.warning("retrieval timed out", extra={"query": question[:80]})

    return retrieval
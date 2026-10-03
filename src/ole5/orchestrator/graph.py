"""The decision, as a graph.

classify -> decide -> validate.

classify gives the model the ticket and one tool. It reads the ticket, works out
what is actually being asked -- a body is a forwarded chain with signatures and
pleasantries, and the question is somewhere inside it -- and searches in its own
words. Up to four searches, because a ticket can ask more than one thing.

decide is a second call rather than the same one. Tool calling and JSON mode
together are unreliable, so the tool loop finishes first and the conversation is
then replayed with an instruction to decide.

validate is code. It checks the decision against the option lists and the rules
the model does not get to bend.
"""

from __future__ import annotations

import contextvars
import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from groq import BadRequestError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph

from ole5 import options
from ole5.config import get_settings
from ole5.intake.context import TicketContext
from ole5.knowledge.contracts import Retrieval
from ole5.knowledge.retriever import retrieve
from ole5.logging import get_logger
from ole5.orchestrator import validate
from ole5.orchestrator.contracts import Decision
from ole5.orchestrator.state import OrchestratorState

log = get_logger(__name__)

PROMPT_VER = "orchestrator-v1"

# A cap, so a bad prompt cannot loop. Four because a ticket can ask several
# things, and each needs its own search: one question per search retrieves far
# better than all of them at once.
MAX_KB_CALLS = 4

# How much of a passage to hand back to the model. Enough to write a reply from,
# short enough that four searches do not fill the context.
PASSAGE_CHARS = 1200


class DecisionFailed(RuntimeError):
    """The agent could not produce a decision. Carries what it did before
    failing, so the audit log can show it."""

    def __init__(self, message: str, steps: list | None = None):
        super().__init__(message)
        self.steps = steps or []


@lru_cache(maxsize=1)
def _prompt_base() -> str:
    prompt_path = get_settings().prompts_dir / "orchestrator" / "system.md"
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the trace: what the agent did, step by step, for the audit log
# ---------------------------------------------------------------------------
#
# Collected during run() and written to the audit log with the draft, so that
# for any decision the log answers "what did it look at, and what did it get
# back?" -- every search and its result, every nudge the code gave it, what
# the model proposed before the validator touched it. A context variable, so
# the tools can add to it without it being threaded through every call.

_TRACE: contextvars.ContextVar[list | None] = contextvars.ContextVar("orchestrator_trace",
                                                                    default=None)


def _trace(step: str, **data: Any) -> None:
    steps = _TRACE.get()
    if steps is not None:
        steps.append({"step": step, **data})


def _scopes_block() -> str:
    """The team scopes, whole, in the prompt -- read on every decision, rather
    than left to a tool the model may not call. Rebuilt per ticket, like the
    options, so an upload counts from the next ticket on."""
    from ole5.knowledge.team_docs import scopes_text

    text = scopes_text()
    if not text:
        return ""
    return ("\n\n## Team scopes\n\n"
            "What each team handles and where the lines between them fall, kept "
            "by the teams themselves. Choose the queue by this.\n\n" + text)


def _options_block() -> str:
    """How to choose each field, and the values to choose from, as they stand
    on the Configuration page.

    Built per ticket rather than cached with the prompt: a value or guidance
    changed in the console must reach the next decision without a restart.
    This is the only place the agent learns how to choose these fields --
    system.md says how to work, not what to pick.
    """
    how = options.guidance()
    lines = ["", "## Choosing the fields", "",
             "Choose each of these from its list, exactly as written. For each field: "
             "how to choose it, then each value and when it fits."]
    for kind in ("service", "queue", "next_state", "type", "subtype",
                 "priority", "sla"):
        lines += ["", f"### {kind}"]
        if how.get(kind):
            lines += ["", how[kind].strip()]
        lines += ["", "One of:"]
        for o in options.described(kind):
            lines.append(f"  {o.value}" + (f" — {o.description}" if o.description else ""))
    return "\n".join(lines)


def _system_prompt() -> str:
    return _prompt_base() + "\n" + _options_block() + _scopes_block()


def _llm(temperature: float = 0) -> ChatGroq:
    s = get_settings()
    if not s.groq_api_key:
        raise DecisionFailed("GROQ_API_KEY is not set")
    return ChatGroq(
        model=s.groq_model,
        api_key=s.groq_api_key.get_secret_value(),
        temperature=temperature,
    )


def _make_kb_tool(sink: list[Retrieval]):
    """The knowledge base as a tool, appending every result to a sink.

    The sink is what validate and the audit trail read. What the model gets back
    is a trimmed view of the same thing.
    """

    @tool
    def search_knowledge_base(question: str) -> str:
        """Search the OLE5 knowledge base.

        Ask one clear question in your own words, not the customer's raw text.
        Search again with different wording if the first result is thin, and
        once per question if the ticket asks several things.
        """
        result = retrieve(question)
        sink.append(result)
        _trace("search_knowledge_base", query=question,
               verdict=result.verdict.value if result.verdict else None,
               evidence=[{"document": e.document, "section": e.section,
                          "relation": e.relation} for e in result.evidence],
               error=(result.error or None) and result.error[:300])
        if result.error:
            return json.dumps({
                "status": "search_failed",
                "detail": result.error[:300],
                "note": "The knowledge base could not be reached. This does NOT "
                        "mean the answer is absent. Do not treat it as not covered.",
            }, ensure_ascii=False)
        return json.dumps(
            {
                "verdict": result.verdict.value,
                "evidence": [
                    {
                        "document": e.document,
                        "section": e.section,
                        "relation": e.relation,
                        "why": e.reason,
                        "text": e.text[:PASSAGE_CHARS],
                    }
                    for e in result.evidence
                ],
                "error": result.error,
            },
            ensure_ascii=False,
        )

    return search_knowledge_base


def node_classify(state: OrchestratorState) -> dict:
    """Read the ticket, search as needed."""
    retrievals: list[Retrieval] = []
    kb_tool = _make_kb_tool(retrievals)
    # One tool. Past tickets are not offered as a tool: the similar-tickets
    # check finds them for every draft anyway, and a tool leaves it to the
    # model whether to look. Whether they go into the prompt instead is being
    # measured first.
    tools = [kb_tool]
    by_name = {t.name: t for t in tools}
    llm = _llm().bind_tools(tools)

    convo: list[Any] = [
        SystemMessage(content=_system_prompt()),
        HumanMessage(content=state["context"].render()),
    ]

    calls = 0
    while calls < MAX_KB_CALLS:
        reply: AIMessage | None = None
        for attempt in (1, 2):
            try:
                # A malformed tool call from a deterministic model repeats
                # identically; nudging the temperature is what breaks it out.
                client = llm if attempt == 1 else _llm(temperature=0.3).bind_tools(tools)
                reply = client.invoke(convo)
                break
            except BadRequestError as exc:
                _trace("retry", reason="malformed tool call", attempt=attempt,
                       error=str(exc)[:300])
                log.warning("tool call malformed", extra={"attempt": attempt,
                                                          "error": str(exc)[:300],
                                                          "kb_calls": calls})

        if reply is None:
            break

        convo.append(reply)
        if not reply.tool_calls:
            break

        for call in reply.tool_calls:
            fn = by_name.get(call["name"])
            if fn is None:
                _trace("unknown_tool", name=call["name"])
                convo.append(ToolMessage(content=f"No tool named {call['name']}.",
                                         tool_call_id=call["id"]))
                continue
            convo.append(ToolMessage(content=fn.invoke(call["args"]),
                                     tool_call_id=call["id"]))
            if call["name"] == "search_knowledge_base":
                calls += 1

    if calls == 0:
        _trace("nudge", reason="no knowledge-base search yet; asked to search or say why not")
        # Routing without searching is a shortcut the model takes when allowed:
        # answering unsearched is overruled, routing unsearched is not. It is
        # sometimes right -- an account question has no knowledge-base answer --
        # so ask rather than forbid.
        convo.append(HumanMessage(content=(
            "You have not searched the knowledge base. If this is an account or "
            "case-state question that no knowledge base could answer, say so in "
            "one line and decide. Otherwise search now."
        )))
        try:
            reply = llm.invoke(convo)
            convo.append(reply)
            for call in reply.tool_calls or []:
                fn = by_name.get(call["name"])
                if fn is None:
                    continue
                convo.append(ToolMessage(content=fn.invoke(call["args"]),
                                         tool_call_id=call["id"]))
                if call["name"] == "search_knowledge_base":
                    calls += 1
        except BadRequestError as exc:
            log.warning("search nudge failed", extra={"error": str(exc)[:300]})

    if calls >= MAX_KB_CALLS:
        _trace("nudge", reason=f"search budget of {MAX_KB_CALLS} used up; told to decide")
        convo.append(HumanMessage(content=(
            "You have used all your searches. Decide with what you have."
        )))

    log.info("classified", extra={"ticket": state["context"].ticket_number,
                                  "searches": calls,
                                  "evidence": sum(len(r.evidence) for r in retrievals)})
    return {"retrievals": retrievals, "messages_so_far": convo}


def node_decide(state: OrchestratorState) -> dict:
    """Turn the conversation into a decision."""
    llm = _llm().bind(response_format={"type": "json_object"})
    schema = json.dumps(Decision.model_json_schema(), ensure_ascii=False)

    messages = list(state["messages_so_far"])
    messages.append(HumanMessage(content=(
        "Now give your decision for this ticket.\n\n"
        "Reply with a single JSON object and nothing else -- no explanation, no "
        "markdown fences. It must match this schema:\n\n" + schema
    )))

    for attempt in (1, 2):
        try:
            reply = llm.invoke(messages)
            text = (reply.content or "").strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
            decision = Decision.model_validate_json(text.strip())
            _trace("proposed", decision=decision.model_dump(mode="json"))
            return {"decision": decision}
        except Exception as exc:
            _trace("retry", reason="decision was not valid JSON for the schema",
                   attempt=attempt, error=str(exc)[:300])
            log.warning("decision invalid", extra={"attempt": attempt,
                                                   "error": str(exc)[:300]})
            messages.append(HumanMessage(content=(
                f"That was not valid. The problem was: {str(exc)[:400]}. "
                "Return JSON only, with every required field."
            )))

    return {"failure": "the model did not return a usable decision"}


def node_validate(state: OrchestratorState) -> dict:
    """Apply the rules that are not the model's to bend."""
    if state.get("failure"):
        return {}
    decision, corrections = validate.apply(state["decision"], state.get("retrievals", []))
    _trace("validated", corrections=corrections)
    return {"decision": decision, "corrections": corrections}


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _graph():
    builder = StateGraph(OrchestratorState)
    builder.add_node("classify", node_classify)
    builder.add_node("decide", node_decide)
    builder.add_node("validate", node_validate)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "decide")
    builder.add_edge("decide", "validate")
    builder.add_edge("validate", END)
    return builder.compile()


def run(context: TicketContext) -> tuple[Decision, list[Retrieval], list[str], dict]:
    """Decide one ticket.

    Returns the validated decision, every search it ran, what code had to
    correct, and metadata for the audit trail.
    """
    started = time.perf_counter()
    steps: list = []
    token = _TRACE.set(steps)
    try:
        from ole5.knowledge.team_docs import current_scopes

        scopes = current_scopes()
        _trace("started", ticket=context.ticket_number, model=get_settings().groq_model,
               prompt_ver=PROMPT_VER, team_scopes_version=scopes["id"] if scopes else None)
    except Exception:
        _trace("started", ticket=context.ticket_number, team_scopes_version="unknown")
    try:
        final = _graph().invoke({"context": context})
    except options.OptionsUnavailable as exc:
        # The poller already handles DecisionFailed: logged, counted, and the
        # ticket is picked up again on the next pass.
        _TRACE.reset(token)
        raise DecisionFailed(str(exc), steps) from exc
    except Exception:
        _TRACE.reset(token)
        raise
    elapsed = int((time.perf_counter() - started) * 1000)

    if final.get("failure"):
        _TRACE.reset(token)
        raise DecisionFailed(final["failure"], steps)

    decision: Decision = final["decision"]
    retrievals: list[Retrieval] = final.get("retrievals", [])
    corrections: list[str] = final.get("corrections", [])

    meta = {
        "model": get_settings().groq_model,
        "prompt_ver": PROMPT_VER,
        "latency_ms": elapsed,
        "searches": len(retrievals),
        "corrections": corrections,
        "trace": steps,
    }
    _TRACE.reset(token)

    log.info("decided", extra={"ticket": context.ticket_number,
                               "action": decision.action.value,
                               "queue": decision.queue,
                               "confidence": decision.confidence.value,
                               "searches": len(retrievals),
                               "corrections": len(corrections),
                               "ms": elapsed})
    return decision, retrievals, corrections, meta

"""The rules the model does not get to bend.

The prompt asks for these; this file guarantees them. A model that returns an
invalid queue, or a reply with no evidence behind it, is corrected here rather
than trusted.

Corrections are recorded, not silent: every one is returned so the reviewer and
the audit log can see what the model actually said.
"""

from __future__ import annotations

import re

from ole5 import options
from ole5.knowledge.contracts import Retrieval, Verdict, all_evidence, strongest
from ole5.logging import get_logger
from ole5.orchestrator.contracts import Action, Decision, ReplyKind

log = get_logger(__name__)

DEFAULT_SERVICE = "OLE5 / Ole5 New"
FALLBACK_QUEUE = "Ole5 New::Product Support"
DEFAULT_TYPE = "Default"
DEFAULT_SUBTYPE = "Other"
DEFAULT_PRIORITY = "1. Low"
DEFAULT_SLA = "SLA - Low (Ole5 + Ole5 New + Availo)"
ROUTED_STATE = "Waiting for Concerned Department"


def _snap(value: str | None, kind: str, default: str,
          field: str, corrections: list[str]) -> str:
    """Force a field onto its list. Case and spacing are forgiven; meaning is not.

    The list is whatever is active in the Configuration page right now. Every
    default passed in is a locked option, so it cannot have been disabled.
    """
    allowed = options.active(kind)
    if value:
        # The prompt lists an option with its description as "value -- text",
        # and the model sometimes copies the whole line back ("Junk -- Spam and
        # unrelated mail only..."). The description is ours, not a guess, so
        # the value before it is taken. Only a separator with spaces around
        # it is cut: "Ole5 New::Product" and "5 Critical" stay whole.
        candidates = [value.strip()]
        head = re.split(r"\s+[—–-]\s+", value.strip(), maxsplit=1)[0].strip()
        if head and head != candidates[0]:
            candidates.append(head)
        for candidate in candidates:
            for option in allowed:
                if candidate.casefold() == option.casefold():
                    return option
        # Priorities carry their OTRS numbering ("2. Medium", "5 Critical"),
        # and the model sometimes drops it and writes "Medium". Accepted when
        # the name without its number points to exactly one option; otherwise
        # it is a guess, and the default applies as before.
        def bare(text: str) -> str:
            return re.sub(r"^\s*\d+\s*\.?\s*", "", text).strip().casefold()
        for candidate in candidates:
            hits = [o for o in allowed if bare(o) == bare(candidate) and bare(candidate)]
            if len(hits) == 1:
                return hits[0]
    corrections.append(f"{field}: {value!r} is not an option, using {default!r}")
    return default


def apply(decision: Decision, retrievals: list[Retrieval]) -> tuple[Decision, list[str]]:
    """Return the decision as it will be stored, and what had to be changed.

    Judged against every search together, not the last one: a ticket asking two
    things gets two searches, and one that found nothing must not veto one that
    did.
    """
    corrections: list[str] = []

    # service is theirs -- their Service field is the product. The fallback only
    # fires on a malformed value, not on genuine doubt: doubt is carried by
    # confidence. OLE5 because it is the common case and a reviewer sees every
    # draft before anything is written.
    service = decision.service.value if hasattr(decision.service, "value") else decision.service
    decision.service = _snap(service, "service", DEFAULT_SERVICE, "service", corrections)

    decision.queue = _snap(decision.queue, "queue", FALLBACK_QUEUE, "queue", corrections)
    decision.type = _snap(decision.type, "type", DEFAULT_TYPE, "type", corrections)
    decision.subtype = _snap(decision.subtype, "subtype", DEFAULT_SUBTYPE, "subtype", corrections)
    decision.priority = _snap(decision.priority, "priority", DEFAULT_PRIORITY, "priority", corrections)
    decision.sla = _snap(decision.sla, "sla", DEFAULT_SLA, "sla", corrections)
    decision.next_state = _snap(decision.next_state, "next_state", "open", "next_state", corrections)

    # A closed state is the agent's to propose. It used to be forbidden here;
    # the team chose to allow it, since a reviewer approves every draft and
    # junk is closed as a matter of course. The prompt says when it fits.

    verdict = strongest(retrievals) if retrievals else Verdict.NOT_IN_KB
    evidence = all_evidence(retrievals)

    # No evidence, no assertion -- and inferential evidence is not evidence for
    # this purpose.
    #
    # The rule applies to answering and not to asking. A reply that tells a
    # customer something has to come from a passage that says it; a reply that
    # asks for the error message asserts nothing.
    #
    # The inferential case was allowed for a while, on the reasoning that the
    # grader was unstable and the model could judge sufficiency itself. It
    # cannot. Of 200 replayed tickets, 33 answers rested on inferential evidence
    # alone, and every one examined invented a procedure: menu paths that do not
    # exist, a 90 day limit nobody set, and a customer told the system was
    # reachable from outside the Kingdom when it is not. Given thin evidence the
    # model reconstructs something plausible rather than declining, and it reads
    # exactly like documentation.
    if decision.action == Action.ANSWER:
        asking = decision.reply_kind == ReplyKind.ASK_MORE

        if not asking:
            if not retrievals:
                corrections.append("answer without searching the knowledge base: "
                                   "forced to route")
                decision.action = Action.ROUTE
            elif not evidence:
                corrections.append("answer with no evidence: forced to route")
                decision.action = Action.ROUTE
            elif verdict == Verdict.PARTIAL:
                corrections.append("answer on inferential evidence only: "
                                   "forced to route")
                decision.action = Action.ROUTE
            elif all(r.error for r in retrievals):
                corrections.append("answer despite every search failing: "
                                   "forced to route")
                decision.action = Action.ROUTE

    if decision.action == Action.ROUTE:
        if decision.reply_body or decision.reply_kind:
            corrections.append("reply discarded: the action is route")
        decision.reply_body = None
        decision.reply_kind = None
        if decision.next_state == "open":
            decision.next_state = ROUTED_STATE
    else:
        if not decision.reply_body or not decision.reply_body.strip():
            corrections.append("answer with an empty reply: forced to route")
            decision.action = Action.ROUTE
            decision.reply_kind = None
            decision.reply_body = None
            decision.next_state = ROUTED_STATE
        elif decision.reply_kind is None:
            decision.reply_kind = ReplyKind.ANSWER
            corrections.append("reply_kind was missing, assumed answer")

    # ask_more without saying what is missing is not a question.
    if decision.reply_kind == ReplyKind.ASK_MORE and not decision.missing:
        corrections.append("ask_more with nothing listed as missing")

    if corrections:
        log.warning("decision corrected", extra={"count": len(corrections),
                                                 "corrections": "; ".join(corrections)})
    return decision, corrections

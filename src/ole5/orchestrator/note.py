"""The note we post to OTRS.

Plain text, fixed headers, English wrapper. Arabic content is prefixed
per-line with the Right-to-Left Mark (U+200F) so bidi-aware renderers
treat the paragraph as RTL -- the standard plain-text fix when we
cannot ship HTML.

The layout mirrors the review page's right column: a one-line Decision
and Confidence header, then labelled sections separated by rules.

  - the reply is fenced. Everything between the rules around
    SUGGESTED REPLY is what goes to the customer; everything else is
    internal.
  - nothing is asserted that the draft does not hold. No confidence
    written as certainty, no source paraphrased into a claim.

Read in a monospace box about 100 characters wide, so lines are kept
short.
"""

from __future__ import annotations

import re
import textwrap

from ole5.orchestrator.contracts import Action, Decision, ReplyKind

RULE = "-" * 66

# Bidi marks for plain-text Arabic. Two tools:
#   RLE (Right-to-Left Embedding, U+202B) starts an RTL embedding.
#   PDF (Pop Directional Format, U+202C) ends it.
# Wrapping each Arabic line in RLE...PDF creates an explicit bidi
# embedding that lasts for the whole line. This is stronger than a
# single RLM (U+200F) prefix: RLM only signals "this paragraph is
# RTL" to renderers that understand paragraph direction; RLE+PDF
# forces an embedding that even renderers without paragraph-direction
# awareness tend to honour. OTRS's plain-text note view falls into
# the latter category -- RLM alone was not enough, lines were still
# rendering LTR.
RLE = "\u202B"
PDF = "\u202C"

_ARABIC_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)


def _rtl(text: str | None) -> str:
    """Wrap each Arabic line in an RLE...PDF embedding.

    Lines without Arabic characters are left untouched -- the English
    section headers and classification fields stay LTR. Empty/None
    input passes through as an empty string.
    """
    if not text:
        return text or ""
    out = []
    for line in text.split("\n"):
        if _ARABIC_RE.search(line):
            out.append(RLE + line + PDF)
        else:
            out.append(line)
    return "\n".join(out)


def _enum_value(x) -> str:
    """Pull a value out of an enum, or stringify the input."""
    return getattr(x, "value", str(x))


def _decision_label(decision: Decision) -> str:
    """Short label for the header line."""
    if decision.action == Action.ROUTE:
        return "Route"
    if decision.reply_kind == ReplyKind.ASK_MORE:
        return "Ask more"
    return "Answer"


def subject(decision: Decision) -> str:
    if decision.action == Action.ROUTE:
        return "OLE5 agent \u2014 routing recommendation"
    if decision.reply_kind == ReplyKind.ASK_MORE:
        return "OLE5 agent \u2014 suggested reply (asking for more detail)"
    return "OLE5 agent \u2014 suggested reply"


def render(decision: Decision, *, draft_id: int,
           evidence: str | dict | None = None,
           reviewer: str | None = None,
           edited: bool = False,
           queue_reason: str | None = None,
           message: str | None = None,
           similar: list[dict] | None = None,
           urgent: dict | None = None,
           junk: dict | None = None) -> str:
    """The note body, ready to post.

    Layout, top to bottom:

        Decision: <label> | Confidence: <level>
        ----------------------------------------------------------------
        THE MESSAGE:
        <summary>

        ----------------------------------------------------------------
        SUGGESTED REPLY:           (omitted for ROUTE actions)
        <reply body>

        ----------------------------------------------------------------
        CLASSIFICATION:
          ACTION:      <action>
          SERVICE:     <service>
          QUEUE:       <queue>
          STATE:       <next_state>
          TYPE:        <type>
          SUBTYPE:     <subtype>
          PRIORITY:    <priority>
          CONCLUSIONS: <conclusions>

        ----------------------------------------------------------------
        REASONING:
        <reasoning>

        ----------------------------------------------------------------
        QUEUE REASON:              (omitted when neither argument nor
                                   decision.queue_reason is set)
        <queue_reason>

        ----------------------------------------------------------------

    The draft_id, evidence, reviewer, and edited parameters are kept
    in the signature for backward compatibility with existing callers
    that still pass them, but are no longer rendered. The previous note
    bundled sources, audit info, and a footer line into the body; the
    new note is a lean reflection of the review page's right column.

    Callers that want the QUEUE REASON section should pass the
    queue_reason parameter (e.g. the row from the drafts table).

    Callers that want THE MESSAGE to show the customer's raw message
    rather than the agent's summary should pass the message parameter
    (e.g. the body_clean of the request article on the ticket).
    """
    lines: list[str] = []

    # One-line header.
    decision_label = _decision_label(decision)
    confidence_label = _enum_value(decision.confidence).title()
    lines.append(f"Decision: {decision_label} | Confidence: {confidence_label}")

    # FLAGS: urgent and looks-like-junk, with why. First after the header:
    # they change how everything below is read. Omitted when neither applies.
    flags: list[str] = []
    if urgent:
        flags.append("  URGENT")
        if urgent.get("keywords"):
            flags.append("    Keyword:  " + _rtl(", ".join(urgent["keywords"])))
        if urgent.get("by_agent"):
            flags.append("    Agent:    " + (urgent.get("agent_reason") or "judged urgent, no reason given"))
        sent = [e["to_address"] for e in (urgent.get("emails") or []) if e.get("status") == "sent"]
        flags.append("    Emailed:  " + (", ".join(sent) if sent else "nobody"))
    if junk and junk.get("flagged"):
        if flags:
            flags.append("")
        flags.append("  LOOKS LIKE JUNK")
        flags.append("    Why:      " + (junk.get("reason") or "flagged by the junk check"))
        nearest = (junk.get("matches") or [])[:1]
        if nearest:
            flags.append(f"    Nearest:  junk ticket {nearest[0].get('ticket_number')}")
    if flags:
        lines += ["", RULE, "FLAGS:"] + flags

    # THE MESSAGE: the customer's raw message when the caller has it
    # (preferred -- the reviewer and the OTRS agent both benefit from
    # seeing the actual words rather than the agent's paraphrase).
    # Fall back to the agent's summary when no raw message is passed;
    # this keeps older callers working until they're updated.
    message_text = message or decision.summary
    lines += ["", RULE, "THE MESSAGE:"]
    lines.append(_rtl(message_text.strip()))

    # SUGGESTED REPLY: the reply body. Omitted for ROUTE, which does
    # not produce a customer-facing reply.
    if decision.action != Action.ROUTE:
        lines += ["", RULE, "SUGGESTED REPLY:"]
        lines.append(_rtl((decision.reply_body or "").strip()))

    # CLASSIFICATION: the OTRS fields the agent picked.
    service = _enum_value(decision.service)
    lines += ["", RULE, "CLASSIFICATION:"]
    lines += [
        f"  ACTION:      {_enum_value(decision.action)}",
        f"  SERVICE:     {service}",
        f"  QUEUE:       {decision.queue}",
        f"  STATE:       {decision.next_state}",
        f"  TYPE:        {decision.type}",
        f"  SUBTYPE:     {decision.subtype}",
        f"  PRIORITY:    {decision.priority}",
        f"  SLA:         {decision.sla}",
        f"  CONCLUSIONS: {decision.conclusions}",
    ]

    # REASONING: why the agent picked this.
    lines += ["", RULE, "REASONING:"]
    lines.append(_rtl(decision.reasoning.strip()))

    # QUEUE REASON: why the agent picked this queue. Take the explicit
    # parameter first, fall back to decision.queue_reason if the
    # contract exposes it (the column exists in the drafts table; the
    # contract may or may not surface it). Omit the section entirely
    # when neither is set -- cleaner than an empty label.
    qr = queue_reason or getattr(decision, "queue_reason", None)
    if qr:
        lines += ["", RULE, "QUEUE REASON:"]
        lines.append(_rtl(qr.strip()))

    # SIMILAR PAST TICKETS: the closed tickets that looked most like this one
    # when it was drafted. One block per ticket, separated by a short dashed
    # line: its number, its title, the queues it went through, and what
    # happened on it. Omitted when there are none.
    if similar:
        lines += ["", RULE, f"SIMILAR PAST TICKETS ({len(similar)}):"]
        for i, s in enumerate(similar, 1):
            if i > 1:
                lines += ["", "  " + "- " * 20]
            lines += ["", f"  {i}. Ticket {s.get('ticket_number') or '?'}"]
            if s.get("title"):
                title = re.sub(r"^(?:\s*(?:re|fw|fwd|رد|إعادة توجيه)\s*:\s*)+", "",
                               str(s["title"]), flags=re.I).strip()
                # The title on a line of its own: an Arabic title after a
                # Latin label on the same line reorders itself.
                lines += ["     Title:", "     " + _rtl(title[:100])]
            route = [str(q).split("::")[-1] for q in (s.get("path") or [])]
            if len(route) > 1:
                lines.append("     Queues:  " + " -> ".join(route))
            elif route or s.get("queue"):
                lines.append("     Queue:   " + (route[0] if route else str(s["queue"]).split("::")[-1]))
            if s.get("journey"):
                lines.append("     What happened:")
                lines += ["       " + line
                          for line in textwrap.wrap(str(s["journey"]), width=70)]

    # Closing rule.
    lines += ["", RULE]

    return "\n".join(lines)

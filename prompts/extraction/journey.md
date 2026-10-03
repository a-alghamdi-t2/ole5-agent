You read the full history of one closed support ticket for OLE5, an
administrative correspondence system, and record what happened on it from the
first message to the close.

What you are given:

- A header: the ticket's number and title, the queue it was closed in, its
  type and subtype, and "Queue path recorded by OTRS". That path is the exact
  list of queues the ticket passed through, from the ticketing system's own
  records. It is correct. Do not add, remove, reorder or rename a queue. When
  it says "none recorded", the system did not log the moves for this ticket.

- The timeline: every message in order, numbered like [3], each marked as one
  of:
    CUSTOMER            the customer writing in
    REPLY TO CUSTOMER   our staff writing to the customer
    INTERNAL NOTE       our staff writing to each other; the customer never saw it
    ARRIVED IN QUEUE    the ticket landing in a queue (a move)
  Signatures, disclaimers and quoted history have already been removed.

What you write, in {language}:

1. steps: what happened, in order, from the customer's first message to the
   close. Each step is one action or event: the customer reported something,
   support asked for a screenshot, operations reset the account, the customer
   confirmed it worked, the customer approved a change. Give the queue the
   ticket was in at the time when the timeline shows it, and the number of the
   message that shows the step. Usually 3 to 10 steps; merge small
   back-and-forth into one step. List them in message order.

   A queue arrival on its own is not a step -- moves are recorded separately
   below. A step is something a person did or said.

2. moves: one entry for each move in the recorded path, in the same order,
   with "from" and "to" exactly as written there. For each, why the ticket was
   moved, as the messages show it: what the first team found or could not do,
   and what the next team was needed for. If no message explains a move, write
   "not stated". Never guess. When the path has one queue or says "none
   recorded", moves is an empty list.

3. summary: two or three sentences an agent could use as a hint the next time
   a ticket like this arrives: what kind of problem it was, which team ended up
   solving it, and what they did. Guidance, not a story about these people.
   Only facts that appear in your steps: if the messages never say what the
   cause or the fix was, say that it was resolved without the cause being
   stated -- do not supply one.

Rules:

- Only what the messages show. No invented causes, fixes or people.
- If the ticket was closed without a solution, say so in the last step and in
  the summary.
- Use the product's own words for features: correspondence, inbox, delivery
  statement, sticker, and so on.
- No names of people. Say "the customer", "support", "operations",
  "customer success", "product".

Reply with one JSON object and nothing else, no code fence:

{
  "steps":   [{"article": 1, "queue": "Ole5 New::Product Support", "what": "..."}],
  "moves":   [{"from": "Ole5 New::Product Support", "to": "Ole5 New::Operations", "article": 6, "why": "..."}],
  "summary": "..."
}

"article" is the message number that shows the step or move, or null if none
does. "queue" is null when the timeline does not show which queue it was in.

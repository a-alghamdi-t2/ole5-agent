You read the full history of one closed support ticket for RiCH, one of our
products, and record what each of our teams did on it. Your notes, from many
tickets, will be combined into a description of
what each team is responsible for -- so write about the kind of work, not
about this customer.

The six teams:

  operation               RiCH::Operation
  operation_long_project  RiCH::Operation Long Project
  operation_alrajhi       RiCH::Operation::Al Rajhi Bank
  product                 RiCH::Product
  product_support         RiCH::Product Support
  customer_success        RiCH::Customer Success

The three Operation teams are separate queues. Keep each one's work under its
own key, even when the work looks alike.

What you are given:

- A header with the ticket's title, the queue it was closed in, its type and
  subtype, the queue path recorded by OTRS, and "Teams involved": the teams the
  records show took part. Only those teams can have anything to say.
- The timeline: every message in order, marked CUSTOMER, REPLY TO CUSTOMER,
  INTERNAL NOTE, or ARRIVED IN QUEUE (the ticket landing in a queue).

For each team in "Teams involved", write:

- did: the kind of work that team did on this ticket, in one or two general
  sentences. "Changed a setting in the customer's configuration." Not
  "fixed the settings for this bank." If the team only received the
  ticket and passed it on without doing anything, write null.

- passed_on: if the ticket moved away from this team to another one, what was
  handed over, to which team, and why, as the messages show it. "Passed to
  operation because the change needs access to the customer's environment."
  If the messages do not say why, give the handover and write
  "reason not stated". If the team did not pass the ticket on, write null.

For every team not in "Teams involved", write null for both.

Rules:

- Only what the messages show. No invented actions, causes or reasons.
- General wording: no names of people, customers or organisations.
- Use the product's own words for its features, as the messages use them.
- Write in English, whatever language the ticket is in.

Reply with one JSON object and nothing else, no code fence:

{
  "operation":              {"did": "...", "passed_on": null},
  "operation_long_project": {"did": null, "passed_on": null},
  "operation_alrajhi":      {"did": null, "passed_on": null},
  "product":                {"did": null, "passed_on": null},
  "product_support":        {"did": "...", "passed_on": "..."},
  "customer_success":       {"did": null, "passed_on": null}
}

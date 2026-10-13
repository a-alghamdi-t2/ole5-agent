# OLE5

The first product. These are the defaults in `config.py`, so OLE5's `.env`
needs none of them. Another product sets each one in its own `.env`.

| Setting | OLE5's value | What it is |
|---|---|---|
| `PRODUCT` | `ole5` | This folder's name |
| `PRODUCT_NAME` | `OLE5` | In the OTRS note headings ("OLE5 agent — suggested reply"), the OTRS history, and the search tool's description the agent reads |
| `OTRS_INTAKE_QUEUE` | `Support` | Where new tickets are picked up |
| `HISTORY_QUEUES` | `Ole5 New::Operations,Ole5 New::Product,Ole5 New::Product Support,Ole5 New::Customer Success,Junk` | Whose closed tickets the weekly job adds to the history |
| `TEAM_QUEUES` | `Ole5 New::Operations=operations,Ole5 New::Product=product,Ole5 New::Product Support=product_support,Ole5 New::Customer Success=customer_success` | Each team's queue, and the short name its work is stored under |
| `RAGENT_TENANT` | `ole5` | The knowledge base index |
| `PRECEDENT_TENANT` | `ole5-precedent` | The similar-tickets index |
| `JUNK_TENANT` | `ole5-junk` | The junk index |
| `FALLBACK_SERVICE` | `OLE5 / Ole5 New` | Used when the agent returns a value that is not on the Configuration page. Each must be a locked value there |
| `FALLBACK_QUEUE` | `Ole5 New::Product Support` | |
| `FALLBACK_SLA` | `SLA - Low (Ole5 + Ole5 New + Availo)` | |
| `QUEUE_PREFIX` | `Ole5 New::` | Left off queue names where the console shows them short |
| `SUBTYPE_PREFIX` | `OLE5::` | Left off subtype names the same way |
| `SESSION_COOKIE` | `ole5_session` | The sign-in cookie. Two products on one host need different names |
| `OWN_SERVICES` | unset (later `OLE5 / Ole5 New`) | The Service values that are this product's. Set it in both products' `.env` once two agents read the same intake queue: each console then lists the other product's drafts in their own section, with a Not ours button. Unset, every draft is listed together |
| `JUNK_THRESHOLD`, `JUNK_FLOOR`, `JUNK_MARGIN`, `SIMILAR_MIN_SCORE` | as calibrated | Measured on OLE5's tickets. Recalibrate for another product |
| `WEEKLY_REPORT_RECIPIENTS` | the OLE5 team | Who gets the weekly report |

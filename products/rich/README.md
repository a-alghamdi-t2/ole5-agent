# RiCH

Runs from this repo with its own `.env`, database and indexes. OLE5's values
are the defaults in `config.py`; these are RiCH's, set in its `.env`.

| Setting | RiCH's value |
|---|---|
| `PRODUCT` | `rich` |
| `PRODUCT_NAME` | `RiCH` |
| `HISTORY_QUEUES` | `RiCH::Customer Success,RiCH::Operation,RiCH::Operation Long Project,RiCH::Operation::Al Rajhi Bank,RiCH::Product,RiCH::Product Support` |
| `TEAM_QUEUES` | `RiCH::Operation=operation,RiCH::Operation Long Project=operation_long_project,RiCH::Operation::Al Rajhi Bank=operation_alrajhi,RiCH::Product=product,RiCH::Product Support=product_support,RiCH::Customer Success=customer_success` |
| `RAGENT_TENANT` | `rich` |
| `PRECEDENT_TENANT` | `rich-precedent` |
| `JUNK_TENANT` | `ole5-junk`: shared |
| `JUNK_SYNC` | `false`: OLE5 keeps the junk index up to date, RiCH only reads it |
| `QDRANT_URL`, `DOCLING_URL` | OLE5's: one Qdrant holds both products' indexes, under their own names |
| `QUEUE_PREFIX` | `RiCH::` |
| `SESSION_COOKIE` | `rich_session` |
| `OWN_SERVICES` | RiCH's Service value(s), once both agents read Support |
| `FALLBACK_SERVICE`, `FALLBACK_QUEUE`, `FALLBACK_SLA`, `SUBTYPE_PREFIX` | from `import_history.py values` |

Junk is one queue for every product, so there is one junk index, OLE5's.
RiCH does not load the Junk queue into its history. Its junk check still
compares against the shared index; only the sender rule (a domain that has
sent nothing but junk) needs the Junk tickets in its own database, and is off
until they are loaded.

The three Operation queues are kept as three teams. If they turn out to be one
team, map them to the same key in `TEAM_QUEUES` and change
`prompts/extraction/scopes.md` to match.

## Building the history

```
python scripts/import_history.py count
python scripts/import_history.py run
python scripts/import_history.py values --out values.txt
python scripts/search_text.py boilerplate
python scripts/search_text.py fill
python scripts/export_timelines.py
python scripts/extract_journeys.py
python scripts/extract_scopes.py
python scripts/extract_scopes.py --export scopes        # for writing the team scopes
python scripts/extract_journeys.py --export journeys    # for writing the knowledge base
```

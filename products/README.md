# Products

The code is the same for every product. What differs lives in two places:
this folder, and the product's `.env`. Each product runs as its own copy of
the app, with its own database.

```
products/
  ole5/
    README.md      OLE5's settings, for reference
    prompts/       the instructions given to the AI models
      orchestrator/system.md
      extraction/journey.md
      extraction/scopes.md
```

## Adding a product

1. **Prompts.** Copy `products/ole5/` to `products/<name>/` and edit the
   prompts. `system.md` is mostly general: change the intake queue's name if it
   differs. The two extraction prompts name the product and its teams: rewrite
   those parts.

2. **`.env`.** Set `PRODUCT=<name>` and every setting in the table in
   `products/ole5/README.md`, with the new product's values. Anything left at
   OLE5's value shows up as a warning when the app starts.

3. **A copy of the app.** Its own folder (a clone of this repo), database,
   Qdrant store and ports. The same `docker-compose.yml` serves every product;
   these lines in the product's `.env` keep it apart from OLE5's:

   ```
   COMPOSE_PROJECT_NAME=rich-agent     # also names its database volume
   QDRANT_VOLUME=rich-agent_qdrant     # create it once: docker volume create rich-agent_qdrant
   CONSOLE_PORT=8421
   DB_PORT=5433
   QDRANT_PORT=6335
   QDRANT_GRPC_PORT=6336
   DOCLING_PORT=8093
   ```

   Unset, each is OLE5's. Never edit `docker-compose.yml` in a product's
   folder: a changed file stops `git pull` there.

4. **The Configuration page.** A new database starts with OLE5's values. Write
   the product's own in a file (see `scripts/configuration.py` for the format),
   then load it once:

   ```
   python scripts/configuration.py load products/<name>/configuration.json --yes
   ```

5. **In the console.** Upload the knowledge base and the team scopes, and add
   the urgent keywords and recipients.

6. **The history.** Export the product's closed tickets, summarise them, then
   build the similar-tickets and junk indexes and calibrate the junk check
   (`scripts/`). Then turn on `WEEKLY_ENABLED`.

7. **Test on staging** with the Testing page, then turn on `POLLER_ENABLED`.

## Two products, one intake queue

When two products' tickets arrive in the same queue (OLE5 and RiCH both in
Support), both agents read it and draft every ticket. Set `OWN_SERVICES` in
each product's `.env` to the Service values that are its own:

```
OWN_SERVICES=OLE5 / Ole5 New      # OLE5's .env
OWN_SERVICES=RiCH                 # RiCH's .env
```

Each console then lists the drafts the agent thinks are the other product's
in their own section at the bottom of Review ("Probably RiCH"), outside the
badge and the sound. A reviewer approves one if it is theirs after all, or
marks it **Not ours**: nothing is written to OTRS, it is not counted as a
rejection, and the ticket is not drafted again unless the customer writes
again. Whichever console approves a ticket first moves it out of the queue;
the other console's draft is then withdrawn by itself.

Turn it on in both at the same time: with only one side on, a ticket marked
Not ours there has nobody to pick it up.

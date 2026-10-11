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

3. **A copy of the app.** Its own database, its own Qdrant store, its own port.
   With Docker, that means its own `docker-compose.yml` with a different
   project name, volumes and ports.

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

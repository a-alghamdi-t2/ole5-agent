# OLE5 Support Agent

This app reads new support tickets from OTRS, decides what should happen to each one, and prepares a draft. A person reviews the draft in a web console and approves or rejects it. Nothing is written to OTRS until someone approves.

## How it works

1. Every minute, the app checks the `Support` queue in OTRS for new tickets.
2. For each new ticket, the agent (an AI model) reads it, searches the knowledge base, and decides: answer it, ask the customer for more details, or route it to a team.
3. Three checks run on the draft: is it urgent, does it look like junk, and which past tickets are similar.
4. If it is urgent, the people on the urgent list get an email straight away.
5. A reviewer opens the draft in the console, changes anything that is wrong, and approves it.
6. On approval, the app writes the decision to OTRS: the queue, state, type, priority, SLA, and an internal note.

Every Friday at 4:00 AM (Riyadh time), the app also adds that week's closed tickets to its history and emails a weekly report.

## The containers

Everything runs with Docker Compose. There are four containers:

| Container | What it does | Port |
|---|---|---|
| `ole5` | The app itself: the console, the agent, and the background jobs | 8420 (open to the network) |
| `db` | PostgreSQL. Holds tickets, drafts, reviews, settings, and the history | 5432 (this machine only) |
| `qdrant` | The search index for the knowledge base, similar tickets, and junk | 6333, 6334 (this machine only) |
| `docling` | Turns uploaded documents (PDF, Word) into text for the knowledge base | 8092 (this machine only) |

Only the console (port 8420) can be reached from other computers. The other three are only reachable from the machine they run on.

## Folders and files

```
src/ole5/          the app's code
migrations/        database changes, applied automatically when the app starts
prompts/           the instructions given to the AI models
scripts/           tools you run by hand for maintenance
data/uploads/      documents uploaded to the knowledge base (do not delete)
.env               all settings and passwords (never share or commit this)
Dockerfile         how the app's image is built
docker-compose.yml how the four containers run together
entrypoint.sh      runs when the app container starts
pyproject.toml     the Python packages the app needs
yoyo.ini           settings for the migration tool
```

Inside `src/ole5/`:

| Folder | What is in it |
|---|---|
| `intake/` | Fetches new tickets from OTRS and cleans the text (removes signatures, quoted emails, and so on) |
| `orchestrator/` | The agent: the prompt, the decision, the checks on the decision, and the OTRS note |
| `review/` | What happens when a draft is approved or rejected |
| `history/` | Past tickets: similar tickets, the junk check, journeys, and the weekly job |
| `knowledge/` | The knowledge base, team scopes, and urgent keywords |
| `notify/` | Emails: urgent alerts and the weekly report |
| `web/` | The console: pages, sign-in, and the API behind them |
| `clients/` | The connection to OTRS |
| `db/` | The database connection, migrations, and the audit log |

A few single files are worth knowing too: `config.py` reads the settings from `.env`, `options.py` handles the lists on the Configuration page, and `runtime.py` runs the background jobs.

Inside `prompts/`:

- `orchestrator/system.md` tells the agent how to work: how to search, when not to assert something, and how to write a reply. What to choose for each field (queue, priority, SLA and so on) is not in here. That is set on the Configuration page in the console.
- `extraction/` has the prompts used to summarise past tickets.

## Setting it up on the server

You need Docker and Docker Compose on the server.

Along with the code, you will be given four things. Put all of them in the project folder, next to `docker-compose.yml`:

| What | What it is |
|---|---|
| `data/` | The uploaded knowledge base documents. Keep it as a folder named `data` |
| `.env` | The settings and passwords. Keep it private |
| `pg.tgz` | A packed copy of the database |
| `qdrant.tgz` | A packed copy of the search index |

The two `.tgz` files hold everything, including customer messages, so handle them carefully.

Then, in the project folder:

1. Create the two volumes the app keeps its data in, and unpack the files into them:

   ```
   docker volume create ole5-agent_postgres_data
   docker volume create deploy_qdrant_storage
   docker run --rm -v ole5-agent_postgres_data:/v -v $PWD:/b alpine tar xzf /b/pg.tgz -C /v
   docker run --rm -v deploy_qdrant_storage:/v -v $PWD:/b alpine tar xzf /b/qdrant.tgz -C /v
   ```

2. Build and start:

   ```
   docker compose build
   docker compose up -d
   ```

   The first build takes a long time because it downloads the AI models. Later builds are fast unless `pyproject.toml` changes.

3. Check that it started:

   ```
   docker compose logs ole5 --tail 30
   ```

   You should see `schema ready` and `loop started` for the poller, outbox, and weekly jobs.

4. Check that the data arrived:

   ```
   docker compose exec ole5 python scripts/similar.py status
   docker compose exec ole5 python scripts/junk.py status
   ```

   Both should say "in step".

5. Open the console at `http://<server address>:8420` and sign in.

Once everything works, delete `pg.tgz` and `qdrant.tgz`. The data is now in the volumes, and the files are only a copy of it.

## Updating

After changing the code:

```
docker compose build ole5
docker compose up -d --force-recreate ole5
```

After changing only `.env`, a restart is enough:

```
docker compose up -d --force-recreate ole5
```

Database changes in `migrations/` are applied by themselves when the app starts.

## Important settings in .env

| Setting | What it is |
|---|---|
| `OTRS_BASE_URL`, `OTRS_USER`, `OTRS_PASSWORD` | The OTRS the app reads new tickets from and writes approvals to |
| `HISTORY_OTRS_*` | Optional. Where the weekly job reads closed tickets from, if it is a different OTRS |
| `GROQ_API_KEY` | The key for the AI models |
| `SMTP_*`, `MAIL_*` | The mail server for urgent emails and the weekly report |
| `CONSOLE_URL` | The console's address. The emails link to it |
| `REGISTRATION_ALLOWLIST` | Who can create an account, for example `@t2.sa` |
| `WEEKLY_REPORT_RECIPIENTS` | Who gets the weekly report, separated by commas |
| `DRY_RUN` | `true` means nothing is written to OTRS and no emails are sent |
| `PIP_EXTRA_INDEX_URL` | Where the build downloads the private RAGent2 package from. Needed to build |

## Useful scripts

Run them inside the app container, like this:

```
docker compose exec ole5 python scripts/<name>.py <command>
```

| Script | What for |
|---|---|
| `weekly.py` | Check, run, or view the weekly update (`status`, `run`, `show 3`) |
| `junk.py` | The junk index (`status`, `sync`, `calibrate`) |
| `similar.py` | The similar tickets index (`status`, `sync`) |
| `reset_password.py` | Give someone a temporary password if they forgot theirs |
| `otrs_probe.py` | See what is waiting in the Support queue |
| `search_text.py`, `export_timelines.py`, `extract_journeys.py`, `extract_scopes.py` | Rebuild parts of the history. Only needed if the cleaning rules or prompts change |

## Backups

The database holds things that cannot be recreated: reviews, the audit log, and the ticket history. Back it up regularly:

```
docker compose exec db pg_dump -U ole5 -d ole5 -Fc -f /tmp/ole5.dump
docker compose cp db:/tmp/ole5.dump ./backup.dump
```

Keep the backup somewhere other than this server. The search index in Qdrant can always be rebuilt from the database, so it does not need a backup.

## If something looks wrong

- **No drafts appear.** Check the logs (`docker compose logs ole5 --tail 50`) and run `otrs_probe.py` to see if anything is waiting in the Support queue.
- **An approval did not reach OTRS.** Look at the `otrs_outbox` and `audit_log` tables on the Tables page. The error from OTRS is recorded there.
- **The weekly report did not arrive.** Run `weekly.py status` to see if the run failed and why.

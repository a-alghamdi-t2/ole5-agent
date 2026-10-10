"""The console server.

    uvicorn ole5.web.app:app --reload --port 8420 --app-dir src

Four pages, one stream, one entry point for tickets.

v1's console could only read, which was right when the agent was the only thing
allowed to act. A review page cannot keep that rule -- approving is the point --
so the narrower one is that every write here is a reviewer's own decision,
carrying their name and going through ole5.review.approve.

Push, not poll from the browser. The server watches a fingerprint query and
sends only when it moves, so a page that is open all day is one quiet
connection rather than a request every two seconds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from contextlib import asynccontextmanager

from ole5 import options as agent_options
from ole5.history import junk, precedent, similar
from ole5.knowledge import team_docs
from ole5.notify import urgent
from ole5 import runtime
from ole5.config import get_settings
from ole5.db import audit, postgres, migrate
from ole5.intake import context as context_builder, store as intake_store
from ole5.intake.contracts import IngestRequest
from ole5.logging import get_logger, setup_logging
from ole5.review import approve as review
from ole5.web import auth, queries

setup_logging()
log = get_logger(__name__)

STATIC = Path(__file__).parent / "static"

POLL_SECONDS = 2.0
KEEPALIVE_SECONDS = 20.0

ALLOWED_UPLOADS = {".pdf", ".txt", ".md", ".docx", ".pptx", ".xlsx", ".csv"}

@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Everything the process does, started and stopped in one place.

    Migrations first: the loops query tables that must exist. A failure here
    stops the server rather than leaving it serving 500s against a schema that
    is not there.
    """
    setup_logging()
    settings = get_settings()

    for warning in settings.warnings():
        log.warning("config", extra={"note": warning})

    if settings.migrate_on_start:
        applied = await run_in_threadpool(migrate.apply)
        log.info("schema ready", extra={"applied": applied})

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    runtime.start()

    try:
        yield
    finally:
        await runtime.stop()
        postgres.close_pool()


app = FastAPI(title="OLE5 console", docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

from ole5.web import testing  # testing page
app.include_router(testing.router)  # testing page






# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/favicon.ico")
def favicon() -> Response:
    # The tab icon cannot follow the console's theme -- the browser draws the
    # tab. The original logo if it is still there, else the black one.
    for name in ("logo.png", "logo-black.png"):
        path = STATIC / name
        if path.exists():
            return FileResponse(path, media_type="image/png")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# who is here
# ---------------------------------------------------------------------------

@app.get("/api/me")
async def me(request: Request) -> dict:
    """Who is signed in, and whether anyone has registered at all.

    The sign-up form opens only while there are no reviewers -- the first person
    creates their own account, everyone after that is added by someone who is
    already in.
    """
    token = request.cookies.get(auth.COOKIE)
    reviewer = await run_in_threadpool(auth.reviewer_for, token)
    registered = await run_in_threadpool(auth.anyone_registered)
    return {"reviewer": reviewer, "anyone_registered": registered}


@app.post("/api/login")
async def login(response: Response, email: str = Form(...),
                password: str = Form(...)) -> dict:
    try:
        token = await run_in_threadpool(auth.login, email, password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    settings = get_settings()
    response.set_cookie(
        auth.COOKIE, token, httponly=True, samesite="lax",
        max_age=settings.session_hours * 3600,
        secure=settings.env == "prod",
    )
    reviewer = await run_in_threadpool(auth.reviewer_for, token)
    return {"reviewer": reviewer}


@app.post("/api/register")
async def register(email: str = Form(...), password: str = Form(...),
                   display_name: str = Form(default="")) -> dict:
    """Step 1 of sign-up: email a 6-digit code to the address. No account is
    created yet -- a mistyped or invented address never gets past this."""
    try:
        await run_in_threadpool(auth.start_signup, email, password, display_name or None)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"code_sent": True, "email": email.strip().lower()}


@app.post("/api/register/verify")
async def register_verify(response: Response, email: str = Form(...),
                          code: str = Form(...)) -> dict:
    """Step 2: the code from the email creates the account and signs in."""
    try:
        reviewer_id = await run_in_threadpool(auth.finish_signup, email, code)
        token = await run_in_threadpool(auth.session_for, reviewer_id)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record(actor="reviewer", action="registered", reasoning=email.strip().lower(),
                 evidence={"reviewer_id": reviewer_id, "confirmed_by": "email code"})
    settings = get_settings()
    response.set_cookie(
        auth.COOKIE, token, httponly=True, samesite="lax",
        max_age=settings.session_hours * 3600,
        secure=settings.env == "prod",
    )
    reviewer = await run_in_threadpool(auth.reviewer_for, token)
    return {"reviewer": reviewer}


@app.post("/api/password/forgot")
async def password_forgot(email: str = Form(...)) -> dict:
    """Step 1 of a reset: email a code if the address has an account. The same
    answer either way, so the form cannot reveal who has one."""
    try:
        await run_in_threadpool(auth.start_reset, email)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"code_sent": True}


@app.post("/api/password/reset")
async def password_reset(response: Response, email: str = Form(...), code: str = Form(...),
                         new_password: str = Form(...)) -> dict:
    """Step 2: the code and a new password. Every other sign-in ends; this
    browser is signed in."""
    try:
        reviewer_id = await run_in_threadpool(auth.finish_reset, email, code, new_password)
        token = await run_in_threadpool(auth.session_for, reviewer_id)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record(actor="reviewer", action="password_reset", reasoning=email.strip().lower(),
                 evidence={"reviewer_id": reviewer_id, "confirmed_by": "email code"})
    settings = get_settings()
    response.set_cookie(
        auth.COOKIE, token, httponly=True, samesite="lax",
        max_age=settings.session_hours * 3600,
        secure=settings.env == "prod",
    )
    reviewer = await run_in_threadpool(auth.reviewer_for, token)
    return {"reviewer": reviewer}


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(auth.COOKIE)
    if token:
        await run_in_threadpool(auth.logout, token)
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.get("/api/options")
async def options(_: dict = Depends(auth.require)) -> dict:
    """The values OTRS will accept. Dropdowns, not free text.

    A reviewer typing a queue name by hand would be a write that fails at the
    outbox, which they would find out about minutes later. These are the same
    lists the validator holds the model to: whatever is active on the
    Configuration page.
    """
    try:
        lists = await run_in_threadpool(agent_options.all_active)
    except agent_options.OptionsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        **lists,
        "action": ["answer", "route"],
        "reply_kind": ["answer", "ask_more"],
    }


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# team scopes and urgent keywords (the Knowledge page's other two parts)
# ---------------------------------------------------------------------------

@app.get("/api/team-scopes")
async def get_team_scopes(_: dict = Depends(auth.require)) -> dict:
    """The document in use, or nothing when none has been uploaded."""
    row = await run_in_threadpool(team_docs.current_scopes)
    return {"scopes": queries.jsonable(row) if row else None}


@app.post("/api/team-scopes")
async def upload_team_scopes(file: UploadFile = File(...),
                             reviewer: dict = Depends(auth.require)) -> dict:
    """Replace the team scopes with an uploaded file. The previous version is
    kept; this one is used from the next decision on."""
    raw = await file.read()
    try:
        row = await run_in_threadpool(team_docs.upload_scopes, raw, file.filename,
                                      reviewer["id"])
    except team_docs.KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"scopes": queries.jsonable(row)}


@app.get("/api/team-scopes/download")
async def download_team_scopes(_: dict = Depends(auth.require)) -> Response:
    row = await run_in_threadpool(team_docs.current_scopes)
    if row is None:
        raise HTTPException(status_code=404, detail="no team scopes uploaded yet")
    name = row["filename"] or "team_scopes.md"
    return PlainTextResponse(row["content"], media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/urgent-keywords")
async def get_keywords(_: dict = Depends(auth.require)) -> list:
    return queries.jsonable(await run_in_threadpool(team_docs.keywords))


@app.post("/api/urgent-keywords")
async def add_keyword(payload: dict, reviewer: dict = Depends(auth.require)) -> dict:
    try:
        return await run_in_threadpool(team_docs.add_keyword, payload.get("keyword") or "",
                                       reviewer["id"])
    except team_docs.KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/urgent-keywords/{keyword_id}")
async def remove_keyword(keyword_id: int, reviewer: dict = Depends(auth.require)) -> dict:
    try:
        await run_in_threadpool(team_docs.remove_keyword, keyword_id, reviewer["id"])
    except team_docs.KnowledgeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"removed": keyword_id}


@app.get("/api/past-tickets/{ticket_number}")
async def past_ticket(ticket_number: str, _: dict = Depends(auth.require)) -> dict:
    """One closed ticket, whole: for the viewer behind a similar ticket or a
    junk match on the review page."""
    row = await run_in_threadpool(similar.past_ticket, ticket_number)
    if row is None:
        raise HTTPException(status_code=404, detail="no such closed ticket")
    return queries.jsonable(row)


@app.get("/api/urgent-recipients")
async def get_recipients(_: dict = Depends(auth.require)) -> list:
    return queries.jsonable(await run_in_threadpool(urgent.recipients))


@app.post("/api/urgent-recipients")
async def add_recipient(payload: dict, reviewer: dict = Depends(auth.require)) -> dict:
    try:
        return await run_in_threadpool(urgent.add_recipient, payload.get("email") or "",
                                       payload.get("name"), reviewer["id"])
    except urgent.UrgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/urgent-recipients/{recipient_id}")
async def remove_recipient(recipient_id: int, reviewer: dict = Depends(auth.require)) -> dict:
    try:
        await run_in_threadpool(urgent.remove_recipient, recipient_id, reviewer["id"])
    except urgent.UrgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"removed": recipient_id}


@app.get("/api/config/options")
async def config_options(_: dict = Depends(auth.require)) -> dict:
    """Every option, disabled ones included, for the Configuration page."""
    try:
        rows = await run_in_threadpool(agent_options.everything)
    except agent_options.OptionsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    how = await run_in_threadpool(agent_options.guidance)
    return {"kinds": [{"kind": k, "label": agent_options.LABELS[k]}
                      for k in agent_options.KINDS],
            "options": rows,
            "guidance": how}


@app.put("/api/config/guidance/{kind}")
async def set_guidance(kind: str, payload: dict,
                       reviewer: dict = Depends(auth.require)) -> dict:
    """How the agent should choose this field. Required; used from the next
    ticket on."""
    try:
        return await run_in_threadpool(agent_options.set_guidance, kind,
                                       payload.get("guidance"), reviewer["id"])
    except agent_options.OptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/config/options")
async def add_option(payload: dict,
                     reviewer: dict = Depends(auth.require)) -> dict:
    """Add a value. Nothing checks it against OTRS -- there is no call on their
    webservice that lists queues or SLAs -- so the page warns before adding."""
    try:
        return await run_in_threadpool(
            agent_options.add,
            payload.get("kind") or "", payload.get("value") or "",
            payload.get("description"), reviewer["id"],
        )
    except agent_options.OptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/config/options/{option_id}")
async def update_option(option_id: int, payload: dict,
                        reviewer: dict = Depends(auth.require)) -> dict:
    """Enable, disable, or change the description. Values are not renamed."""
    try:
        return await run_in_threadpool(
            lambda: agent_options.update(
                option_id, reviewer["id"],
                active_flag=payload.get("active"),
                description=payload.get("description"),
                set_description="description" in payload,
            )
        )
    except agent_options.OptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/drafts")
async def drafts(_: dict = Depends(auth.require)) -> list[dict]:
    return await run_in_threadpool(queries.pending_drafts)


@app.get("/api/drafts/{draft_id}")
async def draft(draft_id: int, _: dict = Depends(auth.require)) -> dict:
    found = await run_in_threadpool(queries.draft, draft_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such draft")
    found["history"] = await run_in_threadpool(queries.draft_history,
                                               found["ticket_id"])
    found["similar"] = await run_in_threadpool(similar.for_draft, draft_id)
    found["junk"] = await run_in_threadpool(junk.for_draft, draft_id)
    found["urgent"] = await run_in_threadpool(urgent.for_draft, draft_id)
    # So the page can say "not built yet" rather than "none found".
    found["similar_index"] = await run_in_threadpool(precedent.index_size) > 0
    return found


@app.post("/api/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, payload: dict,
                        reviewer: dict = Depends(auth.require)) -> dict:
    """Approve, with or without changes.

    Anything in `final` overrides the agent's value; a justification is required
    the moment something differs.
    """
    try:
        result = await run_in_threadpool(
            review.approve, draft_id, reviewer["id"],
            final=payload.get("final") or {},
            justification=payload.get("justification"),
            comment=payload.get("comment"),
        )
    except review.NotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Send in the background. Approving should finish the job -- a reviewer
    # expects the ticket to move -- but their click must not wait on OTRS, and
    # a failure leaves the row queued for the Dashboard to retry.
    asyncio.create_task(run_in_threadpool(review.send_pending))

    return {"draft_id": result.draft_id, "review_id": result.review_id,
            "changed_fields": result.changed_fields}


@app.post("/api/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int, payload: dict,
                       reviewer: dict = Depends(auth.require)) -> dict:
    try:
        review_id = await run_in_threadpool(
            review.reject, draft_id, reviewer["id"],
            payload.get("justification") or "", payload.get("comment"),
        )
    except review.NotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"draft_id": draft_id, "review_id": review_id}


@app.post("/api/outbox/send")
async def send_outbox(_: dict = Depends(auth.require)) -> dict:
    """Write approved drafts to OTRS. Refuses while DRY_RUN is on."""
    sent, failed = await run_in_threadpool(review.send_pending)
    return {"sent": sent, "failed": failed, "dry_run": get_settings().dry_run}


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
async def dashboard(_: dict = Depends(auth.require)) -> dict:
    return await run_in_threadpool(queries.dashboard)


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------

@app.get("/api/documents")
async def documents(_: dict = Depends(auth.require)) -> list[dict]:
    return await run_in_threadpool(queries.documents)


@app.post("/api/documents")
async def upload(file: UploadFile = File(...),
                 reviewer: dict = Depends(auth.require)) -> dict:
    """Take a file and index it.

    Indexing is slow -- chunking is model-driven, so a large document is
    minutes -- and must not be done in the request. The row is written as
    pending and a background task does the work; the page polls the status.

    The content hash is unique, so the same file cannot be indexed twice. That
    is a normal thing for someone to do, so it is a result rather than an error.
    """
    settings = get_settings()

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_UPLOADS:
        raise HTTPException(
            status_code=415,
            detail=f"{suffix or 'that file type'} cannot be indexed. "
                   f"Accepted: {', '.join(sorted(ALLOWED_UPLOADS))}",
        )

    raw = await file.read()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"larger than {settings.max_upload_mb} MB")

    digest = hashlib.sha256(raw).hexdigest()
    existing = await run_in_threadpool(
        postgres.query_one,
        "SELECT id, filename, status FROM documents WHERE content_hash = %s",
        (digest,),
    )
    if existing:
        return {"duplicate": True, "document": queries.jsonable(existing)}

    # A folder per hash rather than a prefix on the name: RAGent2 records the
    # filename it is given, and that name is what a reviewer sees under "based
    # on" when the agent answers from this document.
    folder = settings.upload_dir / digest[:16]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file.filename
    path.write_bytes(raw)

    row = await run_in_threadpool(
        postgres.query_one,
        """
        INSERT INTO documents (filename, content_hash, size_bytes, uploaded_by)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (file.filename, digest, len(raw), reviewer["id"]),
    )
    document_id = row["id"]

    asyncio.create_task(_index(document_id, path))
    log.info("upload accepted", extra={"document": document_id,
                                       "file": file.filename})
    return {"duplicate": False, "document_id": document_id, "status": "pending"}


async def _index(document_id: int, path: Path) -> None:
    """Index one uploaded file. Records the outcome either way."""
    from ole5.knowledge.ingest import add_document

    outcome = await add_document(path)
    if outcome.ok and not outcome.duplicate:
        await run_in_threadpool(
            postgres.execute,
            """
            UPDATE documents SET status = 'indexed', ragent_doc_id = %s,
                   chunk_count = %s, indexed_at = now(), error = NULL
            WHERE id = %s
            """,
            (outcome.doc_id, outcome.chunk_count, document_id),
        )
    elif outcome.duplicate:
        await run_in_threadpool(
            postgres.execute,
            """
            UPDATE documents SET status = 'indexed', indexed_at = now(),
                   error = 'already in the knowledge base'
            WHERE id = %s
            """,
            (document_id,),
        )
    else:
        await run_in_threadpool(
            postgres.execute,
            "UPDATE documents SET status = 'failed', error = %s WHERE id = %s",
            ((outcome.error or "")[:500], document_id),
        )


@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: int,
                            _: dict = Depends(auth.require)) -> FileResponse:
    """The file as it was uploaded, under its original name."""
    row = await run_in_threadpool(
        postgres.query_one,
        "SELECT filename, content_hash FROM documents WHERE id = %s",
        (document_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such document")

    # The same place the upload wrote it: a folder per content hash.
    upload_dir = get_settings().upload_dir.resolve()
    path = (upload_dir / row["content_hash"][:16] / row["filename"]).resolve()
    if upload_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404,
                            detail="the file is no longer on the server")
    return FileResponse(path, filename=row["filename"])


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: int,
                          _: dict = Depends(auth.require)) -> dict:
    """Remove a document everywhere it exists.

    Refuses while the document is still indexing. Deleting mid-index used to
    leave the chunks behind: the row went, and the background task then finished
    and wrote into RAGent2 with nothing left to record what it was. A large PDF
    takes minutes, so that window is real, not theoretical.
    """
    from ole5.knowledge.ingest import remove_document

    row = await run_in_threadpool(
        postgres.query_one,
        "SELECT ragent_doc_id, filename, content_hash, status FROM documents WHERE id = %s",
        (document_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such document")

    if row["status"] == "pending":
        raise HTTPException(
            status_code=409,
            detail="still indexing; wait for it to finish before deleting",
        )

    removed = await run_in_threadpool(remove_document, row["ragent_doc_id"],
                                      content_hash=row["content_hash"])
    if not removed:
        # Keep the row and the file. Deleting them while chunks remain is how
        # the knowledge base came to answer from documents nobody could see:
        # the page showed nothing, the agent still retrieved from it, and there
        # was no row left to retry the removal from.
        raise HTTPException(
            status_code=502,
            detail="the document could not be removed from the knowledge base; "
                   "it is still listed so you can try again",
        )

    # One folder per content hash, named by its first 16 characters -- the same
    # construction the upload used.
    settings = get_settings()
    folder = settings.upload_dir / row["content_hash"][:16]
    removed_file = False
    try:
        resolved = folder.resolve()
        # Never rmtree outside the upload directory. The hash is ours and cannot
        # contain a traversal, but a recursive delete is not something to leave
        # one bug away from walking upward.
        if resolved.is_dir() and settings.upload_dir.resolve() in resolved.parents:
            shutil.rmtree(resolved)
            removed_file = True
    except Exception:
        log.exception("could not remove uploaded file",
                      extra={"document": document_id, "folder": str(folder)})

    await run_in_threadpool(postgres.execute,
                            "DELETE FROM documents WHERE id = %s", (document_id,))

    log.info("document deleted", extra={"document": document_id,
                                        "file": row["filename"],
                                        "file_removed": removed_file})
    return {"deleted": True, "file_removed": removed_file}

# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

@app.get("/api/tables")
async def tables(_: dict = Depends(auth.require)) -> list[dict]:
    return await run_in_threadpool(queries.table_index)


@app.get("/api/tables/{table}")
async def table(table: str, limit: int = Query(50, le=200), offset: int = 0,
                search: str | None = None,
                _: dict = Depends(auth.require)) -> dict:
    try:
        return await run_in_threadpool(queries.table_rows, table,
                                       limit=limit, offset=offset, search=search)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/tables/{table}/{row_id}")
async def table_row(table: str, row_id: int,
                    _: dict = Depends(auth.require)) -> dict:
    try:
        row = await run_in_threadpool(queries.table_row, table, row_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="no such row")
    return row


# ---------------------------------------------------------------------------
# the stream
# ---------------------------------------------------------------------------

@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events. One line when something the console shows has moved.

    The payload is not sent down the wire -- only a nudge. The page refetches
    whatever it is looking at, which keeps this endpoint cheap and means a new
    page needs no change here.
    """
    async def events():
        last = None
        quiet = 0.0
        while True:
            if await request.is_disconnected():
                break
            try:
                current = await run_in_threadpool(queries.fingerprint)
            except Exception:
                log.exception("stream fingerprint failed")
                await asyncio.sleep(POLL_SECONDS)
                continue

            if current != last:
                last = current
                quiet = 0.0
                yield f"data: {json.dumps({'changed': True})}\n\n"
            else:
                quiet += POLL_SECONDS
                if quiet >= KEEPALIVE_SECONDS:
                    quiet = 0.0
                    yield ": keepalive\n\n"

            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# the entry point for tickets
# ---------------------------------------------------------------------------

@app.post("/ingest")
async def ingest(body: IngestRequest) -> JSONResponse:
    """Take one ticket in the TicketGet shape and mirror it.

    Not authenticated: it is how a ticket arrives, and the poller uses the same
    path. Nothing here decides anything.
    """
    result = await run_in_threadpool(intake_store.upsert, body.ticket)
    return JSONResponse(
        status_code=201 if result.is_new else 200,
        content={"ticket_id": result.ticket_id,
                 "ticket_number": result.ticket_number,
                 "new": result.is_new,
                 "articles_inserted": result.articles_inserted,
                 "articles_already_known": result.articles_skipped},
    )


@app.get("/tickets/{ticket_id}/context", response_class=PlainTextResponse)
async def ticket_context(ticket_id: int,
                         _: dict = Depends(auth.require)) -> str:
    """What the orchestrator reads. Here for eyes, not for machines."""
    context = await run_in_threadpool(context_builder.build, ticket_id)
    return context.render()


@app.get("/api/health")
async def health() -> dict:
    """Enough to tell whether the one process is doing all of its jobs."""
    settings = get_settings()
    return {
        "ok": await run_in_threadpool(postgres.ping),
        "dry_run": settings.dry_run,
        "otrs_configured": settings.otrs_configured,
        "loops": runtime.status(),
    }

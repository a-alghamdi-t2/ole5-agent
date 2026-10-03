"""Their OTRS, over the Generic Interface.

Everything above this file is unaware that OTRS is remote. Nothing else in the
codebase builds a URL or holds their credentials.

Their setup, learned by calling it rather than from documentation:

  - the webservice is TicketAPI, and the operation is the last path segment
  - there is no session: UserLogin and Password go in the body of every request
  - the certificate is self-signed on a bare IP, so verification is off
  - an error arrives as HTTP 200 with an Error object in the body, so checking
    the status code is not enough
  - TicketGet wraps the ticket in a list, even for one ticket
  - TicketArticleCreate is not routed on this webservice, but TicketUpdate
    accepts an Article -- so a note, a queue move and a classification are one
    request, and there is no half-written state to recover from
  - Service is rejected on staging even though production tickets display one,
    so we do not write it
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ole5.config import get_settings
from ole5.intake.contracts import IncomingTicket
from ole5.logging import get_logger

log = get_logger(__name__)

RETRIES = 2
RETRY_WAIT = 2.0


class OtrsError(RuntimeError):
    """Exception raised when the API returns an error or is unreachable."""

    def __init__(self, operation: str, code: str | None, message: str):
        self.operation = operation
        self.code = code
        super().__init__(f"{operation}: {code or 'error'}: {message}")


class OtrsNotConfigured(RuntimeError):
    pass


class OtrsClient:
    def __init__(self, *, base_url: str | None = None, user: str | None = None,
                 password: str | None = None) -> None:
        """The OTRS in the settings, or another one given here -- the weekly
        history update may read a different OTRS from the one the agent
        drafts on (staging drafting, production history)."""
        s = get_settings()
        if base_url or user or password:
            if not (base_url and user and password):
                raise OtrsNotConfigured("an OTRS given here needs a URL, a user and a password")
            self._base = f"{base_url.rstrip('/')}/{s.otrs_webservice}"
            self._auth = {"UserLogin": user, "Password": password}
        else:
            if not s.otrs_configured:
                raise OtrsNotConfigured(
                    "OTRS_BASE_URL, OTRS_USER and OTRS_PASSWORD must all be set"
                )
            self._base = f"{s.otrs_base_url}/{s.otrs_webservice}"
            self._auth = {
                "UserLogin": s.otrs_user,
                "Password": s.otrs_password.get_secret_value(),
            }
        # Self-signed certificate on an IP address. Staging is not reachable
        # any other way; production should be checked before it is pointed at.
        self._http = httpx.Client(timeout=s.otrs_timeout, verify=False)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OtrsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- the one place a request is made -------------------------------

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/{operation}"
        body = {**self._auth, **payload}
        started = time.perf_counter()

        last: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            try:
                response = self._http.post(url, json=body)
                response.raise_for_status()
                data = response.json()
                break
            except Exception as exc:
                last = exc
                log.warning("otrs call failed", extra={"operation": operation,
                                                       "attempt": attempt,
                                                       "error": str(exc)[:200]})
                if attempt < RETRIES:
                    time.sleep(RETRY_WAIT)
        else:
            raise OtrsError(operation, None, str(last))

        elapsed = int((time.perf_counter() - started) * 1000)

        if isinstance(data, dict) and "Error" in data:
            err = data["Error"]
            raise OtrsError(operation, err.get("ErrorCode"),
                            err.get("ErrorMessage", ""))

        log.debug("otrs call", extra={"operation": operation, "ms": elapsed})
        return data

    # ---- reading -------------------------------------------------------

    def search(self, *, queues: list[str] | None = None,
               states: list[str] | None = None,
               limit: int | None = None) -> list[str]:
        """Returns matching TicketIDs or an empty list. Filtering is strict, 
           preventing data floods from unknown queues.
        """
        payload: dict[str, Any] = {}
        if queues:
            payload["Queues"] = queues
        if states:
            payload["States"] = states
        if limit:
            payload["Limit"] = limit

        data = self._call("TicketSearch", payload)
        return [str(i) for i in (data.get("TicketID") or [])]

    def get(self, ticket_id: str, *, articles: bool = True) -> IncomingTicket:
        """Fetches a single ticket or raises OtrsError if missing."""
        payload: dict[str, Any] = {
            "TicketID": str(ticket_id),
            "DynamicFields": 1,
            "Extended": 1,
        }
        if articles:
            payload["AllArticles"] = 1

        data = self._call("TicketGet", payload)
        tickets = data.get("Ticket") or []
        if not tickets:
            raise OtrsError("TicketGet", None, f"no ticket {ticket_id}")
        return IncomingTicket.model_validate(tickets[0])

    def intake(self, *, states: list[str] | None = None,
               limit: int | None = None) -> list[str]:
        """Returns untouched tickets waiting in the intake queue (defaults to "new" state)."""
        s = get_settings()
        return self.search(queues=[s.otrs_intake_queue],
                           states=states or ["new"], limit=limit)

    # ---- writing -------------------------------------------------------

    def apply_decision(
        self,
        ticket_id: str,
        *,
        note_subject: str,
        note_body: str,
        queue: str | None = None,
        state: str | None = None,
        type_: str | None = None,
        priority: str | None = None,
        subtype: str | None = None,
        conclusions: str | None = None,
    ) -> str:
        """Posts a customer-invisible note and updates the ticket in one request, 
           returning the ArticleID. Service and SLA are written separately, by
           set_service_sla, so a refusal there cannot undo this.
        """
        ticket: dict[str, Any] = {}
        if queue:
            ticket["Queue"] = queue
        if state:
            ticket["State"] = state
        if type_:
            ticket["Type"] = type_
        if priority:
            ticket["Priority"] = priority

        dynamic = [
            {"Name": name, "Value": value}
            for name, value in (("SubType", subtype), ("Conclusions", conclusions))
            if value
        ]

        payload: dict[str, Any] = {
            "TicketID": str(ticket_id),
            "Article": {
                "Subject": note_subject,
                "Body": note_body,
                "ContentType": "text/plain; charset=utf-8",
                "SenderType": "agent",
                "IsVisibleForCustomer": 0,
                "ArticleTypeID": 9,
                "HistoryType": "AddNote",
                "HistoryComment": "OLE5 agent",
            },
        }
        if ticket:
            payload["Ticket"] = ticket
        if dynamic:
            payload["DynamicField"] = dynamic

        data = self._call("TicketUpdate", payload)
        article_id = data.get("ArticleID")
        if not article_id:
            raise OtrsError("TicketUpdate", None, f"no ArticleID returned: {data}")

        log.info("decision applied", extra={"ticket": ticket_id,
                                            "article": article_id,
                                            "queue": queue, "state": state})
        return str(article_id)

    def set_service_sla(self, ticket_id: str, *, service: str | None,
                        sla: str | None) -> None:
        """Set the ticket's Service and SLA, in a request of their own.

        OTRS accepts an SLA only on a ticket that has a service, and only one
        of that service's SLAs, so the two are written together. Kept apart
        from apply_decision on purpose: if OTRS refuses the pair -- a service
        name it does not know, a service not allowed for this customer, an SLA
        not attached to it -- the note, queue and state are already written
        and stay written. Raises OtrsError with OTRS's own message.
        """
        ticket: dict[str, Any] = {}
        if service:
            ticket["Service"] = service
        if sla:
            ticket["SLA"] = sla
        if not ticket:
            return
        self._call("TicketUpdate", {"TicketID": str(ticket_id), "Ticket": ticket})
        log.info("service and sla set", extra={"ticket": ticket_id,
                                               "service": service, "sla": sla})

    def create(self, *, title: str, queue: str, customer_user: str,
               body: str, subject: str | None = None,
               sender: str | None = None, state: str = "new",
               priority: str = "1. Low",
               type_: str = "Default") -> tuple[str, str]:
        """Creates staging test tickets since the agent doesn't create production tickets. 
           Returns the TicketID and TicketNumber.
        """
        data = self._call("TicketCreate", {
            "Ticket": {
                "Title": title,
                "Queue": queue,
                "State": state,
                "Priority": priority,
                "Type": type_,
                "CustomerUser": customer_user,
            },
            "Article": {
                "Subject": subject or title,
                "Body": body,
                "ContentType": "text/plain; charset=utf-8",
                "SenderType": "customer",
                "IsVisibleForCustomer": 1,
                "From": sender or customer_user,
                "HistoryType": "EmailCustomer",
                "HistoryComment": "created for testing by the OLE5 agent",
            },
        })
        ticket_id = data.get("TicketID")
        if not ticket_id:
            raise OtrsError("TicketCreate", None, f"no TicketID returned: {data}")
        number = str(data.get("TicketNumber"))
        log.info("ticket created", extra={"ticket": ticket_id, "number": number})
        return str(ticket_id), number


def reachable() -> tuple[bool, str]:
    """Safe health check that never raises exceptions."""
    try:
        with OtrsClient() as otrs:
            waiting = otrs.intake()
            return True, f"{len(waiting)} waiting in the intake queue"
    except OtrsNotConfigured as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
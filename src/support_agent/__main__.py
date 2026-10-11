"""The one command.

    python -m support_agent

Serves the console, polls the intake queue, and drains the outbox, in one
process. Everything is configured through .env; nothing here takes arguments.
"""

from __future__ import annotations

import uvicorn

from support_agent.config import get_settings
from support_agent.logging import setup_logging


def main() -> int:
    setup_logging()
    s = get_settings()
    # One worker, always. The background loops are not safe to run twice, and
    # a second worker would be a second poller deciding the same tickets.
    uvicorn.run(
        "support_agent.web.app:app",
        host=s.host,
        port=s.port,
        workers=1,
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
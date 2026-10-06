"""Send one test email the way the app sends urgent alerts and sign-up codes.

    python scripts/mail_test.py someone@t2.sa             # send, short result
    python scripts/mail_test.py someone@t2.sa --verbose   # also show the SMTP conversation

Shows which mail settings are in use, sends one message (even under DRY_RUN),
and says in plain words what went wrong if it fails. "Sent" means the mail
server accepted it. If it then does not arrive, it was dropped or put in
quarantine after that point: check the junk folder, then ask IT.
"""

from __future__ import annotations

import argparse
import smtplib
import socket
import ssl
import sys
from datetime import datetime

from ole5.config import get_settings
from ole5.notify import mail


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("to")
    ap.add_argument("--verbose", action="store_true", help="print the SMTP conversation")
    args = ap.parse_args()

    s = get_settings()
    print(f"server:    {s.smtp_host}:{s.smtp_port}")
    print(f"login as:  {s.mail_username or '(no login)'}"
          + ("   password set" if s.mail_password else "   NO PASSWORD SET"))
    print(f"from:      {s.mail_from or s.mail_username}")
    print(f"to:        {args.to}")
    print(f"DRY_RUN:   {s.dry_run}" + ("   (urgent emails are NOT sent while this is on)" if s.dry_run else ""))
    print()

    if not s.smtp_host:
        sys.exit("SMTP_HOST is not set in .env, so nothing can be sent.")

    msg = mail.compose(args.to, "Support Agent mail test",
                       f"This is a test from the Support Agent, sent {datetime.now():%Y-%m-%d %H:%M}.\n\n"
                       "If you can read this, urgent alerts and sign-up codes reach this address.")
    try:
        mail.deliver(msg, ignore_dry_run=True, debug=args.verbose)
    except smtplib.SMTPAuthenticationError as exc:
        sys.exit(f"FAILED: the server refused the user name or password.\n  {exc}\n"
                 "Check MAIL_USERNAME and MAIL_PASSWORD. On Microsoft 365, SMTP sign-in "
                 "(SMTP AUTH) must also be allowed for this mailbox, which IT turns on.")
    except smtplib.SMTPSenderRefused as exc:
        sys.exit(f"FAILED: the server will not send as {msg['From']}.\n  {exc}\n"
                 "MAIL_FROM must be the mailbox you log in with, or one it may send as.")
    except smtplib.SMTPRecipientsRefused as exc:
        sys.exit(f"FAILED: the server refused the address {args.to}.\n  {exc}")
    except smtplib.SMTPServerDisconnected as exc:
        sys.exit(f"FAILED: {s.smtp_host} answered, then closed the connection mid-way.\n  {exc}\n"
                 "Most often a refused login (check MAIL_USERNAME and MAIL_PASSWORD, and that "
                 "SMTP sign-in is allowed for the mailbox), or a sender it will not accept.")
    except ssl.SSLError as exc:
        sys.exit(f"FAILED: reached {s.smtp_host}, but the secure connection failed.\n  {exc}\n"
                 "Usually SMTP_HOST does not match the server's certificate (use its name, "
                 "not an IP address), or the port is wrong: 587 for STARTTLS, 465 for SSL.")
    except (socket.timeout, TimeoutError, ConnectionRefusedError, OSError) as exc:
        sys.exit(f"FAILED: could not reach {s.smtp_host}:{s.smtp_port}.\n  {exc}\n"
                 "The host or port is wrong, or a firewall blocks this machine from it.")
    except smtplib.SMTPException as exc:
        sys.exit(f"FAILED: {type(exc).__name__}: {exc}")

    print(f"SENT. The mail server accepted the message for {args.to}.")
    print("If it does not arrive within a few minutes, check the junk folder; "
          "if it is not there either, the receiving side dropped it: ask IT.")


if __name__ == "__main__":
    main()

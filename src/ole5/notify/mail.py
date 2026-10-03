"""Sending an email over SMTP, with Arabic laid out right to left.

Taken from the earlier project's sender, with its direction handling intact:
Arabic-heavy text gets a right-to-left mark on each line of the plain part and
an HTML alternative with dir="rtl", so Outlook and Gmail show it the right way
round. The settings are the same: SMTP_HOST, SMTP_PORT, MAIL_FROM,
MAIL_USERNAME, MAIL_PASSWORD.

Follows DRY_RUN, as before: with it on, the message is composed and nothing is
sent. With no SMTP_HOST configured, nothing is sent either -- the caller
records it as skipped rather than failing.
"""

from __future__ import annotations

import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from ole5.config import get_settings
from ole5.logging import get_logger

log = get_logger(__name__)

TIMEOUT = 30

_ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
RLM = "\u200f"  # RIGHT-TO-LEFT MARK


class NotConfigured(RuntimeError):
    """No mail server in the settings."""


def _mostly_arabic(text: str) -> bool:
    return len(_ARABIC_RE.findall(text)) > len(_LATIN_RE.findall(text))


def _apply_direction(body: str) -> str:
    """Right-to-left marks on each line of predominantly Arabic text."""
    if not _mostly_arabic(body):
        return body
    return "\n".join(RLM + line if line.strip() else line for line in body.splitlines())


def compose(to_address: str, subject: str, body: str) -> EmailMessage:
    s = get_settings()
    sender = s.mail_from or s.mail_username or "support-agent@localhost"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_address
    msg["Subject"] = subject
    if s.mail_username and sender != s.mail_username:
        msg["Reply-To"] = sender
    msg["Date"] = formatdate(localtime=True)
    domain = sender.split("@")[-1] if "@" in sender else "t2.sa"
    msg["Message-ID"] = make_msgid(domain=domain)

    msg.set_content(_apply_direction(body), subtype="plain", charset="utf-8")
    if _mostly_arabic(body):
        html_body = html.escape(body).replace("\n", "<br>\n")
        msg.add_alternative(
            '<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head>\n'
            '<body dir="rtl"><pre style="direction:rtl; unicode-bidi:plaintext; '
            'white-space:pre-wrap; word-wrap:break-word;">\n'
            f"{html_body}\n</pre></body></html>",
            subtype="html",
        )
    return msg


def deliver(msg: EmailMessage) -> bool:
    """Send a composed message. True if sent, False under DRY_RUN.
    Raises NotConfigured without a mail server, and SMTP errors as they come."""
    s = get_settings()
    if s.dry_run:
        log.info("email suppressed by DRY_RUN", extra={"to": msg["To"], "subject": msg["Subject"]})
        return False
    if not s.smtp_host:
        raise NotConfigured("SMTP_HOST is not set")

    context = ssl.create_default_context()
    if s.smtp_port == 465:
        conn: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port,
                                              timeout=TIMEOUT, context=context)
    else:
        conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=TIMEOUT)
    try:
        conn.ehlo()
        if s.smtp_port != 465:
            conn.starttls(context=context)
            conn.ehlo()  # STARTTLS resets the advertised feature list
        if s.mail_username and s.mail_password:
            conn.login(s.mail_username, s.mail_password.get_secret_value())
        conn.send_message(msg)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    log.info("email sent", extra={"to": msg["To"], "subject": msg["Subject"]})
    return True

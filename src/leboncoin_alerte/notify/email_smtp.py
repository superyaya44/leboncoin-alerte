"""Email via SMTP (STARTTLS si port 587, SSL si 465)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ..config import EmailConfig
from ..model import Listing


def send(config: EmailConfig, search_name: str, listing: Listing) -> bool:
    msg = EmailMessage()
    msg["Subject"] = f"[LBC {search_name}] {listing.title}"
    msg["From"] = config.from_addr or config.username
    msg["To"] = ", ".join(config.to)
    msg.set_content(listing.format())

    if config.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=20)
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20)
        server.starttls()
    try:
        server.login(config.username, config.password)
        server.send_message(msg)
        return True
    finally:
        server.quit()

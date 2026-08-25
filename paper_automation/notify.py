"""Optional completion notification (spec section 28).

Every channel is opt-in and every failure to notify is swallowed with a log line —
a broken webhook must never fail an otherwise successful run.
"""

import json
import logging
import smtplib
from email.message import EmailMessage

from .config import Config
from .models import ProcessingState, RunReport

log = logging.getLogger(__name__)


def _subject(run: RunReport) -> str:
    failed = sum(1 for r in run.results if r.state is ProcessingState.FAILED)
    flagged = sum(
        1 for r in run.results if r.state is ProcessingState.REQUIRES_HUMAN_REVIEW
    )
    status = "OK" if not (failed or flagged) else f"{failed} failed, {flagged} flagged"
    return f"Paper automation {run.month}: {status}"


def _email(cfg: Config, body: str, run: RunReport) -> None:
    notify = cfg.notify
    if not (notify.smtp_host and notify.email_from and notify.email_to):
        log.warning("Email notification enabled but not fully configured; skipping")
        return
    message = EmailMessage()
    message["Subject"] = _subject(run)
    message["From"] = notify.email_from
    message["To"] = ", ".join(notify.email_to)
    message.set_content(body)

    with smtplib.SMTP(notify.smtp_host, notify.smtp_port, timeout=60) as server:
        server.starttls()
        if notify.smtp_user:
            server.login(notify.smtp_user, notify.smtp_password)
        server.send_message(message)


def _post(url: str, payload: dict) -> None:
    import requests

    response = requests.post(url, json=payload, timeout=60)
    response.raise_for_status()


def _telegram(cfg: Config, body: str, run: RunReport) -> None:
    notify = cfg.notify
    if not (notify.telegram_bot_token and notify.telegram_chat_id):
        log.warning("Telegram notification enabled but not configured; skipping")
        return
    _post(
        f"https://api.telegram.org/bot{notify.telegram_bot_token}/sendMessage",
        {
            "chat_id": notify.telegram_chat_id,
            "text": f"{_subject(run)}\n\n```\n{body}\n```",
            "parse_mode": "Markdown",
        },
    )


def _slack(cfg: Config, body: str, run: RunReport) -> None:
    if not cfg.notify.slack_webhook_url:
        log.warning("Slack notification enabled but no webhook configured; skipping")
        return
    _post(cfg.notify.slack_webhook_url, {"text": f"*{_subject(run)}*\n```{body}```"})


def _teams(cfg: Config, body: str, run: RunReport) -> None:
    if not cfg.notify.teams_webhook_url:
        log.warning("Teams notification enabled but no webhook configured; skipping")
        return
    _post(
        cfg.notify.teams_webhook_url,
        {"title": _subject(run), "text": f"<pre>{body}</pre>"},
    )


_CHANNELS = {"email": _email, "telegram": _telegram, "slack": _slack, "teams": _teams}


def send(cfg: Config, body: str, run: RunReport) -> None:
    for channel in cfg.notify.channels:
        handler = _CHANNELS.get(channel.lower())
        if handler is None:
            log.warning("Unknown notification channel: %s", channel)
            continue
        try:
            handler(cfg, body, run)
            log.info("Notification sent via %s", channel)
        except Exception as exc:
            log.error("Notification via %s failed: %s", channel, exc)

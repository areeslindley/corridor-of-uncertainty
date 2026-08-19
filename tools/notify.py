#!/usr/bin/env python3
"""
Corridor of Uncertainty — run notifier (Gmail SMTP).
====================================================

Sends one email per weekly run: what was added, what was skipped and why, and
what broke. Configured entirely by environment variables so no secret ever
touches the repository.

    COU_SMTP_HOST      default smtp.gmail.com
    COU_SMTP_PORT      default 587 (STARTTLS)
    COU_SMTP_USER      e.g. arees05@gmail.com
    COU_SMTP_PASSWORD  a Google *app password*, not the account password
    COU_MAIL_TO        comma-separated recipients (defaults to COU_SMTP_USER)
    COU_MAIL_FROM      defaults to COU_SMTP_USER

Google requires 2-Step Verification on the account before app passwords can be
minted (myaccount.google.com -> Security -> App passwords). A normal account
password will be rejected.

Notification failure never fails the run — the job's real output is the commit.
"""

from __future__ import annotations

import html
import logging
import os
import re
import smtplib
import socket
from email.message import EmailMessage

log = logging.getLogger("cou.notify")

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587


class NotifyConfigError(RuntimeError):
    pass


ENV_FILE_HINT = "~/.config/corridor-of-uncertainty/cou.env"


def _config() -> dict:
    user = os.environ.get("COU_SMTP_USER", "").strip()
    # Google displays app passwords in four space-separated groups ("abcd efgh
    # ijkl mnop"). Pasted verbatim, the spaces reach smtplib and authentication
    # fails with an opaque 535. Strip all internal whitespace.
    password = re.sub(r"\s+", "", os.environ.get("COU_SMTP_PASSWORD", ""))

    missing = [
        name
        for name, value in (("COU_SMTP_USER", user), ("COU_SMTP_PASSWORD", password))
        if not value
    ]
    if missing:
        raise NotifyConfigError(
            f"{' and '.join(missing)} not set. Expected them in {ENV_FILE_HINT} "
            "(copy tools/cou.env.example there, then `set -a; source` it)."
        )
    if len(password) != 16:
        log.warning(
            "COU_SMTP_PASSWORD is %d characters; Google app passwords are 16. "
            "Is this the account password rather than an app password?",
            len(password),
        )

    to = os.environ.get("COU_MAIL_TO", user).strip()
    return {
        "host": os.environ.get("COU_SMTP_HOST", DEFAULT_HOST),
        "port": int(os.environ.get("COU_SMTP_PORT", DEFAULT_PORT)),
        "user": user,
        "password": password,
        "from": os.environ.get("COU_MAIL_FROM", user).strip(),
        "to": [addr.strip() for addr in to.split(",") if addr.strip()],
    }


def send(subject: str, body_text: str, body_html: str | None = None) -> bool:
    """Send one message. Returns True on success; never raises on send failure."""
    try:
        cfg = _config()
    except NotifyConfigError as exc:
        log.warning("Email not sent: %s", exc)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        log.error("Failed to send notification email: %s", exc)
        return False

    log.info("Notification sent to %s", ", ".join(cfg["to"]))
    return True


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(report: dict) -> tuple[str, str, str]:
    """
    Turn a run report dict into ``(subject, text_body, html_body)``.

    Expected keys: status, host, started, duration, added, skipped, failed,
    warnings, pushed, log_tail.
    """
    added = report.get("added", [])
    skipped = report.get("skipped", [])
    failed = report.get("failed", [])
    warnings = report.get("warnings", [])
    status = report.get("status", "unknown")

    if status == "error":
        subject = f"[CoU] Weekly update FAILED — {len(failed)} error(s)"
    elif added:
        nums = ", ".join(str(a["episode_number"]) for a in added)
        subject = f"[CoU] Weekly update: added episode {nums}"
    else:
        subject = "[CoU] Weekly update: no new episodes"

    lines = [
        "Corridor of Uncertainty — weekly site update",
        "=" * 46,
        f"Status    : {status}",
        f"Host      : {report.get('host', '?')}",
        f"Started   : {report.get('started', '?')}",
        f"Duration  : {report.get('duration', '?')}",
        f"Pushed    : {'yes' if report.get('pushed') else 'no'}",
        "",
    ]

    if added:
        lines.append(f"ADDED ({len(added)})")
        for a in added:
            lines.append(f"  * Episode {a['episode_number']:>3} — {a['title']}")
            lines.append(f"      {a['url']}")
            for w in a.get("validation_warnings", []):
                lines.append(f"      ! {w}")
        lines.append("")

    if failed:
        lines.append(f"FAILED ({len(failed)})")
        for f in failed:
            lines.append(f"  * {f.get('video_id', '?')} — {f.get('title', '')}")
            lines.append(f"      {f.get('error', '')}")
        lines.append("")

    if skipped:
        lines.append(f"SKIPPED ({len(skipped)})")
        for s in skipped[:25]:
            lines.append(f"  * {s.get('title', '')[:70]} — {s.get('reason', '')}")
        if len(skipped) > 25:
            lines.append(f"  ... and {len(skipped) - 25} more")
        lines.append("")

    if warnings:
        lines.append(f"WARNINGS ({len(warnings)})")
        lines.extend(f"  - {w}" for w in warnings[:25])
        lines.append("")

    if report.get("log_tail"):
        lines.append("LOG TAIL")
        lines.append("-" * 46)
        lines.append(report["log_tail"])

    text = "\n".join(lines)

    colour = {"ok": "#2e7d32", "no_change": "#616161", "error": "#c62828"}.get(
        status, "#616161"
    )
    html_body = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,Helvetica,sans-serif;"
        "font-size:14px;color:#1a1a1a\">"
        f"<h2 style=\"color:{colour};margin-bottom:0.2em\">{html.escape(subject)}</h2>"
        f"<pre style=\"background:#f6f6f6;border:1px solid #e0e0e0;border-radius:6px;"
        f"padding:1em;white-space:pre-wrap;font-size:12.5px\">{html.escape(text)}</pre>"
        "</body></html>"
    )

    return subject, text, html_body


def send_report(report: dict) -> bool:
    subject, text, html_body = render_report(report)
    return send(subject, text, html_body)


def check() -> bool:
    """Report configuration state without sending anything."""
    try:
        cfg = _config()
    except NotifyConfigError as exc:
        print(f"NOT CONFIGURED: {exc}")
        return False
    print("Configuration looks complete:")
    print(f"  host : {cfg['host']}:{cfg['port']}")
    print(f"  user : {cfg['user']}")
    print(f"  from : {cfg['from']}")
    print(f"  to   : {', '.join(cfg['to'])}")
    print(f"  pass : {len(cfg['password'])} characters (not shown)")
    return True


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if "--check" in sys.argv:
        raise SystemExit(0 if check() else 1)

    if not check():
        raise SystemExit(1)
    ok = send(
        "[CoU] Notifier test",
        "If you are reading this, SMTP configuration on this host is working.",
    )
    raise SystemExit(0 if ok else 1)

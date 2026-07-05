"""Job-completion email notifications (Pro).

Configured with one env var::

    BATCHROOM_SMTP_URL=smtp://user:pass@mail.example.com:587?tls=starttls&from=batchroom@example.com

``tls``: ``starttls`` (default for :587), ``ssl`` (default for :465) or
``none``. Recipients: the submitting user's email when multi-user is on,
else ``BATCHROOM_NOTIFY_EMAIL``. Sending happens on a daemon thread and can
never fail a job.
"""

from __future__ import annotations

import logging
import smtplib
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("batchroom.email")


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    tls: str  # starttls | ssl | none
    sender: str

    @classmethod
    def from_url(cls, url: str) -> SmtpConfig | None:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("smtp", "smtps"):
                raise ValueError(f"unsupported scheme '{parsed.scheme}'")
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
            default_tls = "ssl" if (parsed.scheme == "smtps" or port == 465) else "starttls"
            tls = query.get("tls", default_tls).lower()
            if tls not in ("starttls", "ssl", "none"):
                raise ValueError(f"invalid tls mode '{tls}'")
            username = unquote(parsed.username) if parsed.username else None
            sender = query.get("from") or username
            if not parsed.hostname or not sender:
                raise ValueError("smtp url needs a host and a 'from' (or username)")
            return cls(
                host=parsed.hostname,
                port=port,
                username=username,
                password=unquote(parsed.password) if parsed.password else None,
                tls=tls,
                sender=sender,
            )
        except ValueError as exc:
            log.error("invalid BATCHROOM_SMTP_URL: %s", exc)
            return None


class EmailNotifier:
    def __init__(self, config: SmtpConfig):
        self.config = config

    # ------------------------------------------------------------------
    def notify_job_finished(self, to: str, job: dict, manifest: dict) -> None:
        """Fire-and-forget from worker threads."""
        threading.Thread(
            target=self._notify_sync, args=(to, job, manifest),
            name="batchroom-email", daemon=True,
        ).start()

    def _notify_sync(self, to: str, job: dict, manifest: dict) -> None:
        try:
            self._deliver(self._build_message(to, job, manifest))
            log.info("notification sent to %s for %s", to, job.get("id"))
        except Exception as exc:  # never let email problems matter
            log.warning("email notification failed for %s: %s", job.get("id"), exc)

    # ------------------------------------------------------------------
    def _build_message(self, to: str, job: dict, manifest: dict) -> EmailMessage:
        counts = manifest.get("counts", {})
        status = job.get("status", "done")
        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = to
        message["Subject"] = (
            f"[Batchroom] {job.get('id')} {status}: "
            f"{counts.get('done', 0)} ok, {counts.get('failed', 0)} failed"
        )
        lines = [
            f"Job:      {job.get('id')}",
            f"Recipe:   {manifest.get('recipe', {}).get('name', job.get('recipe'))}",
            f"Status:   {status}",
            f"Items:    {counts.get('total', 0)} total — {counts.get('done', 0)} ok, "
            f"{counts.get('failed', 0)} failed, {counts.get('cancelled', 0)} cancelled",
            f"Started:  {manifest.get('started_at', '')}",
            f"Finished: {manifest.get('finished_at', '')}",
            f"Output:   {job.get('output_dir', '')}",
            "",
            "Failed items:" if counts.get("failed") else "",
        ]
        for item in manifest.get("items", []):
            if item.get("status") == "failed":
                lines.append(f"  - {item.get('source')}: {item.get('error')}")
        lines += ["", "— Batchroom (self-hosted; this mail came from your own server)"]
        message.set_content("\n".join(line for line in lines if line is not None))
        return message

    def _deliver(self, message: EmailMessage) -> None:
        cfg = self.config
        if cfg.tls == "ssl":
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=20) as smtp:
                self._login_and_send(smtp, message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as smtp:
                if cfg.tls == "starttls":
                    smtp.starttls()
                self._login_and_send(smtp, message)

    def _login_and_send(self, smtp, message: EmailMessage) -> None:
        if self.config.username and self.config.password:
            smtp.login(self.config.username, self.config.password)
        smtp.send_message(message)

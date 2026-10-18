"""Webhook delivery: POST the job manifest on completion, with retries."""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("batchroom.webhooks")

ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 5.0, 15.0)


async def deliver_webhook(url: str, manifest: dict) -> bool:
    """POST manifest JSON. Retries with backoff; failures are logged, never fatal."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"batchroom/{manifest.get('app_version', '0')}",
        "X-Batchroom-Event": "job.finished",
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        for attempt in range(ATTEMPTS):
            try:
                response = await client.post(url, json=manifest, headers=headers)
                if 200 <= response.status_code < 300:
                    log.info(
                        "webhook delivered for %s -> %s (%d)",
                        manifest.get("job_id"), url, response.status_code,
                    )
                    return True
                log.warning(
                    "webhook attempt %d for %s got HTTP %d",
                    attempt + 1, manifest.get("job_id"), response.status_code,
                )
            except httpx.InvalidURL as exc:  # not an HTTPError; retrying cannot help
                log.error("webhook URL %s for %s is invalid: %s", url, manifest.get("job_id"), exc)
                return False
            except httpx.HTTPError as exc:
                log.warning(
                    "webhook attempt %d for %s failed: %s",
                    attempt + 1, manifest.get("job_id"), exc,
                )
            if attempt < ATTEMPTS - 1:
                await asyncio.sleep(BACKOFF_SECONDS[attempt])
    log.error("webhook delivery gave up for %s -> %s", manifest.get("job_id"), url)
    return False

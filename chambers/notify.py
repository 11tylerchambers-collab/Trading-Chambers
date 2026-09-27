"""Telegram messages and alerts (PHASE1A §6).

`.env` gains TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Without them messaging is disabled with one
startup warning and nothing else changes: every message is still written to the `alerts` table with
status `disabled`, so the dashboard shows it. Identical alerts (same key) are rate-limited to one per
15 minutes; the extras are recorded as `suppressed`. The limit is kept in the database, so it also holds
across processes (the engine and the watchdog).

Sending uses the Bot API over HTTPS with the standard library (no new dependency). Long messages are
split at 4000 characters. A failed send is recorded as `failed` with the error; it never raises into
the engine.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Callable, Optional

from .clock import ET

log = logging.getLogger("chambers.notify")

RATE_LIMIT = timedelta(minutes=15)
MAX_LEN = 4000
API = "https://api.telegram.org/bot{token}/sendMessage"


def telegram_sender(token: str, chat_id: str, timeout: float = 10.0) -> Callable[[str], None]:
    def send(text: str) -> None:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(API.format(token=token), data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
            if not body.get("ok", False):
                raise RuntimeError(f"telegram: {body.get('description', body)}")
    return send


class Notifier:
    def __init__(self, store, token: Optional[str] = None, chat_id: Optional[str] = None, dry_run: bool = False,
                 sender: Optional[Callable[[str], None]] = None, now_fn: Optional[Callable[[], datetime]] = None,
                 echo: Callable[[str], None] = print):
        self.store = store
        self.dry_run = dry_run
        self.enabled = bool(sender) or bool(token and chat_id)
        self._sender = sender or (telegram_sender(token, chat_id) if token and chat_id else None)
        self._now = now_fn or (lambda: datetime.now(ET))
        self._echo = echo
        if not self.enabled and not dry_run:
            log.warning("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env): "
                        "messages and alerts are recorded in the database only")

    @classmethod
    def from_env(cls, store, **kw) -> "Notifier":
        return cls(store, os.environ.get("TELEGRAM_BOT_TOKEN") or None, os.environ.get("TELEGRAM_CHAT_ID") or None,
                   **kw)

    def send(self, kind: str, key: str, text: str, now: Optional[datetime] = None) -> str:
        now = now or self._now()
        text = text.strip()
        if self.dry_run:
            self._echo(text)
            status, err = "dry_run", None
        elif not self.enabled:
            status, err = "disabled", None
        else:
            status, err = "sent", None
            try:
                for i in range(0, len(text), MAX_LEN):
                    self._sender(text[i:i + MAX_LEN])
            except Exception as e:
                status, err = "failed", f"{type(e).__name__}: {e}"
                log.warning("telegram send failed: %s", err)
        try:
            self.store.write_alert(now, kind, key, text, status, err)
        except Exception:
            log.exception("could not record message")
        return status

    def alert(self, key: str, text: str, now: Optional[datetime] = None) -> str:
        """An immediate alert. Identical alerts (same key) at most once per 15 minutes."""
        now = now or self._now()
        last = self.store.last_alert(key)
        if last is not None and now - datetime.fromisoformat(last["ts"]) < RATE_LIMIT:
            self.store.write_alert(now, "alert", key, text, "suppressed")
            return "suppressed"
        return self.send("alert", key, text, now)

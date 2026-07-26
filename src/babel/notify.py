"""Alerts for the two events a log line will not reach a human in time for.

Deliberately narrow. A notifier that also reports routine progress is one the
operator learns to swipe away, and then the disk fills unnoticed anyway.

Nothing here may raise. A crawl running for weeks must not die because Telegram
had a bad minute; the alert is a courtesy, the crawl is the job.
"""

import logging
import time
from typing import Protocol

import aiohttp

log = logging.getLogger("babel.notify")


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    """Used when no credentials are configured. Logs and moves on."""

    async def send(self, text: str) -> None:
        log.info("notification (no telegram configured): %s", text)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            self._url, json={"chat_id": self._chat_id, "text": text}
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"telegram returned {resp.status}: {await resp.text()}")


class Throttled:
    """Wraps a notifier so a persistent condition alerts once, not every cycle.

    The disk-full check runs on a loop; without this the operator gets a message
    every few minutes until they act, which is indistinguishable from spam.
    """

    def __init__(self, inner: Notifier, interval_sec: float, now=time.monotonic) -> None:
        self._inner = inner
        self._interval = interval_sec
        self._now = now
        self._last: dict[str, float] = {}

    async def send_once(self, key: str, text: str) -> None:
        now = self._now()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return
        try:
            await self._inner.send(text)
        except Exception:  # noqa: BLE001 — an alert failing must never stop the crawl
            log.exception("could not deliver notification: %s", text)
            return
        # Recorded only on success. Stamping before the send would treat a
        # Telegram hiccup exactly like a delivered message and silence the
        # condition for the whole interval — the opposite of what an alert that
        # exists to escalate should do. A persistently broken transport retries
        # each cycle instead, which is cheap and leaves a log line every time.
        self._last[key] = now


def build_notifier(settings) -> Notifier:
    if settings.bot_token and settings.chat_id:
        return TelegramNotifier(settings.bot_token, settings.chat_id)
    return NullNotifier()

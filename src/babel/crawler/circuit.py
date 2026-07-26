"""Stop asking a host that has stopped answering.

The retry cooldown in `claim_pending_images` defers a row that already failed.
It does nothing about the next row from the same host, which has never been
tried and so enters the very next batch. Measured live: i.postimg.cc started
stalling for the full request timeout and then failing, after we had pulled
hundreds of images from it. Every batch refilled with fresh postimg rows at 20s
each, and image capture fell to 0.04 images/second while every other host in the
queue was answering in under a second.

So this is per host and in memory: the worker is one long-running process, and a
host's health is a fact about now, not something worth persisting. Nothing here
touches a row's status — a skipped image is not a failed image, it is one we
chose not to ask about yet.
"""

import time

from babel.crawler.images import url_host


class HostCircuit:
    """Consecutive failures per host; past a threshold the host is left alone.

    Consecutive rather than cumulative, because a host that mostly works and
    drops the occasional request is a normal host and must not be shut out.
    """

    def __init__(self, threshold: int, open_sec: float, now=time.monotonic) -> None:
        self._threshold = threshold
        self._open_sec = open_sec
        self._now = now
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def record_success(self, url: str) -> None:
        self._failures.pop(url_host(url), None)

    def record_failure(self, url: str) -> None:
        host = url_host(url)
        self._failures[host] = self._failures.get(host, 0) + 1
        if self._failures[host] >= self._threshold:
            self._opened_at[host] = self._now()

    def is_open(self, url: str) -> bool:
        """Whether this host is currently being left alone.

        The claim already excludes open hosts, but a batch is chosen once and
        worked through afterwards: without this the rest of an in-flight batch
        still hammers a host that opened partway through it, which at 50 rows and
        a 20-second stall each is most of the damage.
        """
        return url_host(url) in self._opened_at

    def open_hosts(self) -> list[str]:
        """Hosts to leave out of the next claim, and a chance to expire the rest.

        Expiring here rather than on a timer keeps the whole thing to one call
        per cycle. A host that comes back gets its failure count cleared too, so
        the first failure after reopening does not immediately re-open it — that
        would ban a host permanently after one bad spell.
        """
        now = self._now()
        for host, opened in list(self._opened_at.items()):
            if now - opened > self._open_sec:
                del self._opened_at[host]
                self._failures.pop(host, None)
        return sorted(self._opened_at)

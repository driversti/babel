"""_watch_egress: a leak must stop the crawl immediately, a flaky IP-info
provider must not.

check_ip_leak raises IpLeak on a confirmed leak and a bare RuntimeError when
every provider failed to answer after its own internal retries (see
babel.vpn's module docstring — those providers rate-limit, and Gluetun's DNS
has been seen to block some outright). Only the second case is tolerated,
and only up to MAX_CONSECUTIVE_LOOKUP_FAILURES in a row.
"""

import pytest

from babel.cli import MAX_CONSECUTIVE_LOOKUP_FAILURES, _watch_egress
from babel.vpn import IpLeak


class _StopError(Exception):
    """Ends the watch loop deterministically in a test, without waiting on
    real time or an unbounded `while True`. Not IpLeak and not a
    RuntimeError, so _watch_egress cannot mistake it for either case and it
    always propagates straight out."""


async def test_leak_exits_immediately():
    async def check():
        raise IpLeak("egress is home country")

    with pytest.raises(IpLeak):
        await _watch_egress(check, interval_sec=0)


async def test_four_failures_then_success_do_not_exit_and_reset_the_counter():
    calls = {"n": 0}

    async def check():
        calls["n"] += 1
        n = calls["n"]
        if n <= 4:
            raise RuntimeError("provider down")
        if n == 5:
            return  # a clean check resets the consecutive-failure count
        if n <= 9:
            raise RuntimeError("provider down")
        # Reaching a 10th call proves the loop survived 4 failures, a reset,
        # and 4 more failures (8 total) without ever hitting the 5-in-a-row
        # threshold — which it would have if the reset hadn't happened.
        raise _StopError

    with pytest.raises(_StopError):
        await _watch_egress(check, interval_sec=0)
    assert calls["n"] == 10


async def test_five_consecutive_failures_exit():
    calls = {"n": 0}

    async def check():
        calls["n"] += 1
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError) as exc_info:
        await _watch_egress(check, interval_sec=0)

    assert not isinstance(exc_info.value, IpLeak)
    assert calls["n"] == MAX_CONSECUTIVE_LOOKUP_FAILURES

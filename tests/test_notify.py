import logging

from babel.config import Settings
from babel.notify import NullNotifier, TelegramNotifier, Throttled, build_notifier


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_null_notifier_accepts_everything_silently():
    await NullNotifier().send("anything")  # must not raise


def test_build_returns_null_when_unconfigured():
    assert isinstance(build_notifier(Settings(_env_file=None)), NullNotifier)


def test_build_returns_telegram_when_configured():
    settings = Settings(_env_file=None, bot_token="t", chat_id="c")
    assert isinstance(build_notifier(settings), TelegramNotifier)


async def test_throttle_suppresses_a_repeat_within_the_interval():
    recorder = Recorder()
    throttled = Throttled(recorder, interval_sec=3600, now=lambda: 0.0)
    await throttled.send_once("disk", "disk is full")
    await throttled.send_once("disk", "disk is full")
    assert recorder.sent == ["disk is full"]


async def test_throttle_allows_a_repeat_after_the_interval():
    recorder = Recorder()
    clock = {"t": 0.0}
    throttled = Throttled(recorder, interval_sec=100, now=lambda: clock["t"])
    await throttled.send_once("disk", "first")
    clock["t"] = 101.0
    await throttled.send_once("disk", "second")
    assert recorder.sent == ["first", "second"]


async def test_throttle_keys_are_independent():
    recorder = Recorder()
    throttled = Throttled(recorder, interval_sec=3600, now=lambda: 0.0)
    await throttled.send_once("disk", "disk")
    await throttled.send_once("leak", "leak")
    assert recorder.sent == ["disk", "leak"]


async def test_a_failing_transport_never_propagates():
    calls = {"n": 0}

    class Broken:
        async def send(self, text: str) -> None:
            calls["n"] += 1
            raise RuntimeError("telegram is down")

    throttled = Throttled(Broken(), interval_sec=1, now=lambda: 0.0)
    await throttled.send_once("k", "text")  # must not raise
    assert calls["n"] == 1  # swallowed, not skipped


async def test_a_failed_send_is_retried_rather_than_throttled():
    # The alert exists to escalate. Treating a transport failure as a delivered
    # message would silence a full disk for the whole interval.
    calls = {"n": 0}

    class Broken:
        async def send(self, text: str) -> None:
            calls["n"] += 1
            raise RuntimeError("telegram is down")

    throttled = Throttled(Broken(), interval_sec=3600, now=lambda: 0.0)
    await throttled.send_once("disk", "full")
    await throttled.send_once("disk", "full")
    assert calls["n"] == 2


async def test_build_falls_back_when_only_one_credential_is_set():
    assert isinstance(build_notifier(Settings(_env_file=None, bot_token="t")), NullNotifier)
    assert isinstance(build_notifier(Settings(_env_file=None, chat_id="c")), NullNotifier)


def test_half_configured_telegram_warns(caplog):
    """Filling one of the two and not the other is the likely operator mistake,
    and the result is silence — no alerts, and nothing saying so. Both alerts
    exist to escalate off-host, so a half-configuration must be loud."""
    with caplog.at_level(logging.WARNING, logger="babel.notify"):
        build_notifier(Settings(_env_file=None, bot_token="t"))
    assert "chat_id" in caplog.text.lower()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="babel.notify"):
        build_notifier(Settings(_env_file=None, chat_id="c"))
    assert "bot_token" in caplog.text.lower()


def test_no_telegram_at_all_does_not_warn(caplog):
    """Running without Telegram is a legitimate choice, not a mistake."""
    with caplog.at_level(logging.WARNING, logger="babel.notify"):
        build_notifier(Settings(_env_file=None))
    assert caplog.text == ""

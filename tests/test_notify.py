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
    class Broken:
        async def send(self, text: str) -> None:
            raise RuntimeError("telegram is down")

    throttled = Throttled(Broken(), interval_sec=1, now=lambda: 0.0)
    await throttled.send_once("k", "text")  # must not raise

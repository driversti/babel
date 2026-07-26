"""A host that has stopped answering must stop being asked.

Measured live: i.postimg.cc began stalling 20s per request and then failing, after
we had pulled hundreds of images from it. 66 failures against 109 successes, while
every other host in the queue answered in 0.2-1.0s. Because the drain is
newest-article-first and the newest articles' images were concentrated there, all
but a fraction of every batch was postimg, at 20s each: 0.04 images/second.

The retry cooldown does not cover this. It defers rows that already failed, and
each batch brought in fresh postimg rows that had never been tried.
"""

from babel.crawler.circuit import HostCircuit


def _circuit(now, threshold=3, open_sec=900.0):
    return HostCircuit(threshold=threshold, open_sec=open_sec, now=now)


def test_a_healthy_host_is_never_skipped():
    c = _circuit(lambda: 0.0)
    assert c.open_hosts() == []
    c.record_success("https://a.example/x.png")
    assert c.open_hosts() == []


def test_it_takes_repeated_failures_to_open():
    """One timeout is a bad moment, not a broken host. Opening on a single
    failure would knock out a host over the sort of blip that retrying fixes."""
    c = _circuit(lambda: 0.0, threshold=3)
    c.record_failure("https://a.example/1.png")
    c.record_failure("https://a.example/2.png")
    assert c.open_hosts() == []
    c.record_failure("https://a.example/3.png")
    assert c.open_hosts() == ["a.example"]


def test_a_success_clears_the_run():
    """Consecutive failures, not cumulative. A host that mostly works but drops
    the occasional request should never open."""
    c = _circuit(lambda: 0.0, threshold=3)
    c.record_failure("https://a.example/1.png")
    c.record_failure("https://a.example/2.png")
    c.record_success("https://a.example/3.png")
    c.record_failure("https://a.example/4.png")
    c.record_failure("https://a.example/5.png")
    assert c.open_hosts() == []


def test_one_host_opening_does_not_affect_another():
    c = _circuit(lambda: 0.0, threshold=2)
    c.record_failure("https://a.example/1.png")
    c.record_failure("https://a.example/2.png")
    c.record_failure("https://b.example/1.png")
    assert c.open_hosts() == ["a.example"]


def test_it_closes_again_after_the_cooldown():
    """Open forever would be worse than the problem: postimg was answering
    normally an hour before it started stalling, and holds images nothing else
    has a copy of."""
    clock = {"t": 0.0}
    c = _circuit(lambda: clock["t"], threshold=1, open_sec=900.0)
    c.record_failure("https://a.example/1.png")
    assert c.open_hosts() == ["a.example"]

    clock["t"] = 899.0
    assert c.open_hosts() == ["a.example"]
    clock["t"] = 901.0
    assert c.open_hosts() == []


def test_a_reopened_host_is_given_a_clean_run():
    """Otherwise the first failure after the cooldown re-opens it immediately and
    the host is effectively banned for good."""
    clock = {"t": 0.0}
    c = _circuit(lambda: clock["t"], threshold=3, open_sec=100.0)
    for i in range(3):
        c.record_failure(f"https://a.example/{i}.png")
    assert c.open_hosts() == ["a.example"]

    clock["t"] = 200.0
    assert c.open_hosts() == []
    c.record_failure("https://a.example/x.png")
    assert c.open_hosts() == [], "one failure after reopening must not re-open it"


def test_is_open_answers_for_a_single_url():
    """The claim excludes open hosts, but a batch is chosen once and worked
    through afterwards, so the worker needs a per-item answer too."""
    c = _circuit(lambda: 0.0, threshold=2)
    assert not c.is_open("https://a.example/1.png")
    c.record_failure("https://a.example/1.png")
    assert not c.is_open("https://a.example/2.png")
    c.record_failure("https://a.example/2.png")
    assert c.is_open("https://a.example/3.png")
    assert not c.is_open("https://b.example/1.png")

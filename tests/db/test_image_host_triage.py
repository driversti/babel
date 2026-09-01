"""Writing off a dead image host in one move.

The backlog after the walk is 3M+ 'pending' rows, a large share of them on hosts
that stopped existing years ago (tinypic closed in 2019). Left alone the worker
fetches each of those five times before giving up. `image_host_backlog` is the
report an operator reads to spot them; `kill_image_host` is the reverse of
`requeue_images_by_host` — it moves a host's queued rows to 'dead' so the worker
never dials them again. The guard is lifetime 'ok': a host we have ever fetched
from is not written off without --force.
"""

import pytest

from babel.db import repo


async def _image(pg, article_id, position, url, status, attempts=0):
    await pg.execute(
        """INSERT INTO articles (id, title, body, author_id, author_name, e_day, published_at)
           VALUES ($1, 't', 'b', 1, 'a', 6822, now()) ON CONFLICT (id) DO NOTHING""",
        article_id,
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, attempts)
           VALUES ($1, $2, $3, $4, $5)""",
        article_id, position, url, status, attempts,
    )


async def _status(pg, article_id, position):
    row = await pg.fetchrow(
        "SELECT status, attempts FROM article_images WHERE article_id=$1 AND position=$2",
        article_id, position,
    )
    return row["status"], row["attempts"]


# --- image_host_backlog -----------------------------------------------------


async def test_backlog_counts_each_status_per_host(pg):
    await _image(pg, 1, 0, "https://i49.tinypic.com/a.jpg", "pending")
    await _image(pg, 1, 1, "https://i49.tinypic.com/b.jpg", "error", attempts=5)
    await _image(pg, 1, 2, "https://i49.tinypic.com/c.jpg", "dead")
    await _image(pg, 2, 0, "https://i.imgur.com/d.jpg", "pending")
    await _image(pg, 2, 1, "https://i.imgur.com/e.jpg", "ok", attempts=1)

    rows = {r.host: r for r in await repo.image_host_backlog(pg, min_rows=1)}

    assert rows["i49.tinypic.com"].pending == 1
    assert rows["i49.tinypic.com"].error == 1
    assert rows["i49.tinypic.com"].dead == 1
    assert rows["i49.tinypic.com"].ok == 0
    assert rows["i.imgur.com"].pending == 1
    assert rows["i.imgur.com"].ok == 1


async def test_backlog_hides_hosts_below_the_threshold(pg):
    await _image(pg, 1, 0, "https://big.example/a.jpg", "pending")
    await _image(pg, 1, 1, "https://big.example/b.jpg", "pending")
    await _image(pg, 1, 2, "https://small.example/c.jpg", "pending")

    hosts = [r.host for r in await repo.image_host_backlog(pg, min_rows=2)]

    assert hosts == ["big.example"]


async def test_backlog_threshold_counts_only_pending_and_error(pg):
    """A host with one queued row and a thousand stored ones is not a backlog."""
    await _image(pg, 1, 0, "https://mostly-done.example/a.jpg", "pending")
    for i in range(5):
        await _image(pg, 1, i + 1, f"https://mostly-done.example/ok{i}.jpg", "ok")

    assert await repo.image_host_backlog(pg, min_rows=2) == []


async def test_backlog_is_ordered_by_queue_depth_descending(pg):
    await _image(pg, 1, 0, "https://small.example/a.jpg", "pending")
    await _image(pg, 2, 0, "https://big.example/a.jpg", "pending")
    await _image(pg, 2, 1, "https://big.example/b.jpg", "error")
    await _image(pg, 2, 2, "https://big.example/c.jpg", "pending")

    hosts = [r.host for r in await repo.image_host_backlog(pg, min_rows=1)]

    assert hosts == ["big.example", "small.example"]


# --- sample_url_for_host --------------------------------------------------


async def test_sample_url_returns_a_queued_url_for_the_host(pg):
    await _image(pg, 1, 0, "https://host.example/only.jpg", "pending")

    assert await repo.sample_url_for_host(pg, "host.example") == "https://host.example/only.jpg"


async def test_sample_url_prefers_the_newest_article(pg):
    await _image(pg, 10, 0, "https://host.example/old.jpg", "pending")
    await _image(pg, 99, 0, "https://host.example/new.jpg", "pending")

    assert await repo.sample_url_for_host(pg, "host.example") == "https://host.example/new.jpg"


async def test_sample_url_ignores_settled_rows(pg):
    await _image(pg, 1, 0, "https://host.example/stored.jpg", "ok")
    await _image(pg, 1, 1, "https://host.example/gone.jpg", "dead")

    assert await repo.sample_url_for_host(pg, "host.example") is None


async def test_sample_url_is_none_for_an_unknown_host(pg):
    assert await repo.sample_url_for_host(pg, "nobody.example") is None


# --- kill_image_host ------------------------------------------------------


async def test_kill_moves_pending_and_error_to_dead(pg):
    await _image(pg, 1, 0, "https://i49.tinypic.com/a.jpg", "pending")
    await _image(pg, 1, 1, "https://i49.tinypic.com/b.jpg", "error", attempts=5)

    assert await repo.kill_image_host(pg, "i49.tinypic.com") == 2
    assert (await _status(pg, 1, 0))[0] == "dead"
    assert (await _status(pg, 1, 1))[0] == "dead"


async def test_kill_leaves_attempts_untouched_as_a_record(pg):
    await _image(pg, 1, 0, "https://i49.tinypic.com/a.jpg", "error", attempts=3)

    await repo.kill_image_host(pg, "i49.tinypic.com")

    assert (await _status(pg, 1, 0)) == ("dead", 3)


async def test_kill_never_touches_a_stored_image(pg):
    await _image(pg, 1, 0, "https://i49.tinypic.com/a.jpg", "ok", attempts=1)
    await _image(pg, 1, 1, "https://i49.tinypic.com/b.jpg", "pending")

    with pytest.raises(repo.HostHasLiveImages):
        await repo.kill_image_host(pg, "i49.tinypic.com")


async def test_kill_refuses_a_host_we_have_fetched_from_before(pg):
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpg", "ok")
    await _image(pg, 1, 1, "https://i.imgur.com/b.jpg", "pending")

    with pytest.raises(repo.HostHasLiveImages) as excinfo:
        await repo.kill_image_host(pg, "i.imgur.com")

    assert excinfo.value.ok_count == 1
    assert (await _status(pg, 1, 1))[0] == "pending", "nothing is written off on a refusal"


async def test_kill_with_force_overrides_the_guard(pg):
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpg", "ok")
    await _image(pg, 1, 1, "https://i.imgur.com/b.jpg", "pending")

    assert await repo.kill_image_host(pg, "i.imgur.com", force=True) == 1
    assert (await _status(pg, 1, 1))[0] == "dead"
    assert (await _status(pg, 1, 0))[0] == "ok", "the stored image still stands"


async def test_kill_leaves_other_hosts_alone(pg):
    await _image(pg, 1, 0, "https://i49.tinypic.com/a.jpg", "pending")
    await _image(pg, 1, 1, "https://keep.example/b.jpg", "pending")

    await repo.kill_image_host(pg, "i49.tinypic.com")

    assert (await _status(pg, 1, 1))[0] == "pending"


async def test_kill_matching_ignores_case_port_and_userinfo(pg):
    await _image(pg, 1, 0, "https://I49.TinyPic.com/a.jpg", "pending")
    await _image(pg, 1, 1, "https://i49.tinypic.com:8080/b.jpg", "pending")
    await _image(pg, 1, 2, "https://user@i49.tinypic.com/c.jpg", "pending")

    assert await repo.kill_image_host(pg, "I49.TINYPIC.com") == 3


async def test_kill_does_not_match_a_host_that_is_a_suffix(pg):
    await _image(pg, 1, 0, "https://tinypic.com/a.jpg", "pending")
    await _image(pg, 1, 1, "https://i49.tinypic.com/b.jpg", "pending")

    assert await repo.kill_image_host(pg, "tinypic.com") == 1
    assert (await _status(pg, 1, 1))[0] == "pending"


async def test_kill_then_requeue_is_a_round_trip(pg):
    """A mistaken kill is undone by the recovery path that already exists."""
    await _image(pg, 1, 0, "https://i49.tinypic.com/a.jpg", "pending")

    await repo.kill_image_host(pg, "i49.tinypic.com")
    assert (await _status(pg, 1, 0))[0] == "dead"

    assert await repo.requeue_images_by_host(pg, "i49.tinypic.com") == 1
    assert await _status(pg, 1, 0) == ("pending", 0)

"""Requeueing images a host lied about.

Every false 'dead' in the first live run arrived as a batch from one host, each
for its own reason: imgur rate-limited us, giphy and postimg served landing
pages, screencast mislabelled live PNGs as octet-stream. Each was fixed in code,
but the rows already written stayed 'dead' — and 'dead' is never reclaimed. This
is the way back for the next one, which there will be.
"""

from babel.db import repo


async def _image(pg, article_id: int, position: int, url: str, status: str, attempts: int = 5):
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


async def _status(pg, article_id: int, position: int) -> tuple[str, int]:
    row = await pg.fetchrow(
        "SELECT status, attempts FROM article_images WHERE article_id=$1 AND position=$2",
        article_id, position,
    )
    return row["status"], row["attempts"]


async def test_requeues_dead_rows_for_the_named_host(pg):
    await _image(pg, 1, 0, "http://content.screencast.com/users/x/a.png", "dead")

    assert await repo.requeue_images_by_host(pg, "content.screencast.com") == 1
    assert await _status(pg, 1, 0) == ("pending", 0), "attempts must reset, or the ceiling holds"


async def test_requeues_error_rows_too(pg):
    """An 'error' row that burned through the ceiling is just as stuck as a 'dead' one."""
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "error", attempts=5)

    assert await repo.requeue_images_by_host(pg, "i.imgur.com") == 1
    assert await _status(pg, 1, 0) == ("pending", 0)


async def test_leaves_other_hosts_alone(pg):
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "dead")
    await _image(pg, 1, 1, "https://other.example/b.png", "dead")

    assert await repo.requeue_images_by_host(pg, "i.imgur.com") == 1
    assert (await _status(pg, 1, 1))[0] == "dead"


async def test_never_touches_a_stored_image(pg):
    """Requeueing an 'ok' row would discard a good capture and refetch it for nothing."""
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "ok", attempts=1)

    assert await repo.requeue_images_by_host(pg, "i.imgur.com") == 0
    assert await _status(pg, 1, 0) == ("ok", 1)


async def test_does_not_re_queue_what_is_already_queued(pg):
    """A 'pending' row is already in the queue; counting it would overstate the recovery."""
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "pending", attempts=0)

    assert await repo.requeue_images_by_host(pg, "i.imgur.com") == 0


async def test_matches_a_protocol_relative_url(pg):
    """Older articles embed //host/path, which normalise_url expands at fetch time
    but which is stored verbatim."""
    await _image(pg, 1, 0, "//i.imgur.com/a.jpeg", "dead")

    assert await repo.requeue_images_by_host(pg, "i.imgur.com") == 1


async def test_matching_ignores_case_port_and_userinfo(pg):
    await _image(pg, 1, 0, "https://I.Imgur.COM/a.jpeg", "dead")
    await _image(pg, 1, 1, "https://i.imgur.com:8080/b.jpeg", "dead")
    await _image(pg, 1, 2, "https://user@i.imgur.com/c.jpeg", "dead")

    assert await repo.requeue_images_by_host(pg, "I.IMGUR.com") == 3


async def test_a_host_that_is_a_suffix_of_another_does_not_match_it(pg):
    """'imgur.com' must not sweep up 'i.imgur.com' — the operator named one host."""
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "dead")
    await _image(pg, 1, 1, "https://imgur.com/b.jpeg", "dead")

    assert await repo.requeue_images_by_host(pg, "imgur.com") == 1
    assert (await _status(pg, 1, 0))[0] == "dead"


async def test_unknown_host_changes_nothing(pg):
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "dead")

    assert await repo.requeue_images_by_host(pg, "nope.example") == 0


async def test_stuck_image_hosts_ranks_hosts_by_how_many_are_stuck(pg):
    """The operator has a host name from a log line, or nothing at all. This is
    what makes the command usable without one."""
    await _image(pg, 1, 0, "https://i.imgur.com/a.jpeg", "dead")
    await _image(pg, 1, 1, "https://i.imgur.com/b.jpeg", "dead")
    await _image(pg, 1, 2, "https://other.example/c.png", "error")
    await _image(pg, 1, 3, "https://fine.example/d.png", "ok")

    assert await repo.stuck_image_hosts(pg, limit=10) == [
        ("i.imgur.com", 2),
        ("other.example", 1),
    ]

import datetime

from babel.db import repo
from babel.models import Article

UTC = datetime.UTC


def _article(article_id: int, body: str = "body", title: str = "title") -> Article:
    return Article(
        id=article_id, title=title, body=body, body_raw=None,
        author_id=1, author_name="a", country="Poland",
        published_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        e_day=6616, comment_count=0, comments=(), images=(),
    )


async def test_saving_an_article_queues_it_for_embedding(pg):
    await repo.save_article(pg, _article(10))
    pending = await repo.claim_pending_embeddings(pg, 10)
    assert [p.article_id for p in pending] == [10]
    assert pending[0].title == "title"
    assert pending[0].body == "body"


async def test_the_queue_is_newest_first(pg):
    for article_id in (10, 30, 20):
        await repo.save_article(pg, _article(article_id))
    pending = await repo.claim_pending_embeddings(pg, 10)
    assert [p.article_id for p in pending] == [30, 20, 10]


async def test_an_embedded_article_leaves_the_queue(pg):
    await repo.save_article(pg, _article(10))
    vector = repo.vector_literal([0.1] * repo.EMBED_DIM)
    await repo.save_embeddings(pg, "test/model", [(10, vector)])
    assert await repo.claim_pending_embeddings(pg, 10) == ()
    stored = await pg.fetchval("SELECT model FROM article_embeddings WHERE article_id = 10")
    assert stored == "test/model"


async def test_a_hidden_article_is_not_offered(pg):
    await repo.save_article(pg, _article(10))
    await pg.execute("UPDATE articles SET hidden_at = now() WHERE id = 10")
    assert await repo.claim_pending_embeddings(pg, 10) == ()


async def test_re_collecting_with_a_changed_body_clears_the_vector(pg):
    """A sweep that rewrites the text must not leave the old meaning behind."""
    await repo.save_article(pg, _article(10, body="first"))
    await repo.save_embeddings(pg, "test/model", [(10, repo.vector_literal([0.1] * repo.EMBED_DIM))])
    await repo.save_article(pg, _article(10, body="entirely different text"))
    assert [p.article_id for p in await repo.claim_pending_embeddings(pg, 10)] == [10]


async def test_re_collecting_with_an_unchanged_body_keeps_the_vector(pg):
    """Otherwise every sweep drops the whole archive out of search for hours."""
    await repo.save_article(pg, _article(10, body="same"))
    await repo.save_embeddings(pg, "test/model", [(10, repo.vector_literal([0.1] * repo.EMBED_DIM))])
    await repo.save_article(pg, _article(10, body="same", title="a new title"))
    assert await repo.claim_pending_embeddings(pg, 10) == ()


async def test_the_seeding_statement_queues_articles_that_predate_the_migration(pg):
    """The gap the deploy order is supposed to prevent, closed by hand.

    Same shape as the one `refetch --to` left: rows written by an image that
    did not know about this table get no queue row, and nothing revisits them.
    """
    await repo.save_article(pg, _article(10))
    await pg.execute("DELETE FROM article_embeddings")
    assert await repo.claim_pending_embeddings(pg, 10) == ()
    await pg.execute(
        "INSERT INTO article_embeddings (article_id) SELECT id FROM articles "
        "ON CONFLICT DO NOTHING"
    )
    assert [p.article_id for p in await repo.claim_pending_embeddings(pg, 10)] == [10]

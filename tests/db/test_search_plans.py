import datetime

from babel.db import repo, search

UTC = datetime.UTC


async def _populated(conn, rows=500):
    """Vectors that differ from each other, deterministically.

    Identical vectors would build a degenerate HNSW graph, which is both slow
    to construct and useless for proving the planner will use it.
    """
    await conn.execute(
        """INSERT INTO articles (id, title, body, author_name, country,
                                 published_at, comment_count)
           SELECT g, 'T' || g, 'body', 'author', 'Poland',
                  timestamptz '2026-01-01' + (g || ' seconds')::interval, 0
             FROM generate_series(1, $1) g
           ON CONFLICT (id) DO NOTHING""",
        rows,
    )
    # halfvec's dimension is a type modifier, and Postgres requires type
    # modifiers to be a constant or identifier at parse time, not a bound
    # parameter — asyncpg raises PostgresSyntaxError ("type modifiers must be
    # simple constants or identifiers") if $2 is used there directly. $2 stays
    # bound for the inner generate_series, an ordinary integer argument; only
    # the cast's dimension is interpolated as a literal.
    await conn.execute(
        f"""INSERT INTO article_embeddings (article_id, embedding, model)
           SELECT g,
                  (SELECT ('[' || string_agg(sin(g * 0.37 + i)::real::text, ',') || ']')
                     FROM generate_series(1, $2) i)::halfvec({repo.EMBED_DIM}),
                  'test/model'
             FROM generate_series(1, $1) g
           ON CONFLICT (article_id) DO UPDATE
               SET embedding = EXCLUDED.embedding, model = EXCLUDED.model""",
        rows, repo.EMBED_DIM,
    )
    await conn.execute(search.HNSW_INDEX_SQL)
    await conn.execute("ANALYZE articles")
    await conn.execute("ANALYZE article_embeddings")


def _probe():
    return [0.1] * repo.EMBED_DIM


async def test_the_index_and_the_query_quantise_identically(pg):
    """The invariant that makes the index usable at all.

    Both strings come from the same helper, so this fails the moment someone
    edits one of them alone — which is the change that would silently turn
    every search into a sequential scan over 2.8M rows.
    """
    expression = search.quantised("embedding")
    assert expression in search.HNSW_INDEX_SQL
    assert expression in search.build_search_query()


async def test_the_search_uses_the_hnsw_index(pg):
    await _populated(pg)
    sql = search.build_search_query()
    async with pg.transaction():
        # Forced, not hoped for. At any row count a test can afford, the
        # planner may reasonably prefer a scan; what this test exists to prove
        # is that the ORDER BY expression *matches* the index, which is what
        # breaks when someone edits one side of it.
        await pg.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            r[0] for r in await pg.fetch(
                f"EXPLAIN {sql}", repo.vector_literal(_probe()), 50, 10
            )
        )
    assert "article_embeddings_bin_idx" in plan


async def test_set_local_actually_takes_effect(pg):
    """SET LOCAL outside a transaction is a no-op: Postgres emits a WARNING and
    asyncpg does not raise it, so ef_search would silently stay at 40 while the
    inner query asks for 500 candidates. This reads the setting back rather
    than trusting that issuing it worked."""
    await _populated(pg, rows=50)
    seen = await search.search_articles_with_settings_probe(pg, _probe(), candidates=123, limit=5)
    assert seen == "123"


async def test_results_are_ordered_by_similarity_to_the_probe(pg):
    await _populated(pg, rows=200)
    target = await pg.fetchval("SELECT embedding FROM article_embeddings WHERE article_id = 77")
    vector = [float(v) for v in str(target).strip("[]").split(",")]
    rows = await search.search_articles(pg, vector, candidates=100, limit=5)
    assert rows[0].id == 77
    assert rows[0].score > rows[-1].score


async def test_a_pending_row_is_never_returned(pg):
    await _populated(pg, rows=200)
    await pg.execute("UPDATE article_embeddings SET embedding = NULL WHERE article_id = 77")
    rows = await search.search_articles(pg, _probe(), candidates=200, limit=200)
    assert 77 not in [r.id for r in rows]


async def test_a_hidden_article_is_never_returned(pg):
    await _populated(pg, rows=200)
    await pg.execute("UPDATE articles SET hidden_at = now() WHERE id = 77")
    rows = await search.search_articles(pg, _probe(), candidates=200, limit=200)
    assert 77 not in [r.id for r in rows]


async def test_the_pending_claim_uses_its_partial_index(pg):
    """The bug this project has already shipped twice.

    CLAUDE.md's finding M3 records `claim_retryable` and `claim_pending_images`
    both being one edit away from losing their partial index — turn the status
    literal into a bound parameter and Postgres can no longer prove the index
    applicable, so the plan degrades to a scan of a table heading for 2.8M rows.
    Nothing errors; the queue just gets slower every week.

    Task 3 added a third such index and no guard for it, which is what this
    closes. It takes its SQL from `repo._CLAIM_PENDING_EMBEDDINGS` rather than a
    pasted copy — reaching for the private name deliberately, because a test that
    EXPLAINs its own copy of the statement proves only that Postgres *can* use a
    partial index, which is precisely the mistake M3 names.
    """
    await _populated(pg, rows=500)
    # Half the corpus back into the queue. On an empty queue the partial index
    # is empty too, every plan costs about the same, and the choice carries no
    # information.
    await pg.execute("UPDATE article_embeddings SET embedding = NULL WHERE article_id % 2 = 0")
    await pg.execute("ANALYZE article_embeddings")
    plan = "\n".join(
        r[0] for r in await pg.fetch(f"EXPLAIN {repo._CLAIM_PENDING_EMBEDDINGS}", 32)
    )
    assert "article_embeddings_pending_idx" in plan

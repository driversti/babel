"""Does this actually do the thing it was built for?

Every other test in this suite uses a fake encoder, because the real one needs
CUDA and 2.3 GB of weights. That means nothing else in the suite can fail when
cross-language retrieval stops working — the property the whole project exists
for is, by construction, untested everywhere else.

Opt in with `uv run pytest -m live`, against a populated database and a running
embed service. Not part of the default run and not part of CI.
"""

import os

import asyncpg
import pytest

from babel.config import Settings
from babel.db import repo, search
from babel.embed.client import EmbedClient

pytestmark = pytest.mark.live

# Queries in one language, and an article that should come back for each. Each
# query is a different language from the article it targets, and each of the
# eight target ids below was chosen by reading the real archived body first —
# not by guessing from the title, and not by round-tripping through the
# embeddings themselves, which would only prove the model agrees with itself.
#
# Found on the live archive 2026-07-31, read-only, one topical article per
# language actually present in the corpus:
#   SELECT id, country, title, length(body) FROM articles
#   WHERE country = 'Serbia' AND title ILIKE '%izbor%' ORDER BY id DESC LIMIT 20;
# (and the equivalent keyword per country/topic — election, war, economy — for
# the other seven languages).
CASES = [
    # (query, language of the query, expected article id, article's own language)
    # 2797020 (sr): a Serbian congress candidate's platform against "traitor"
    # labelling by the ruling party, urging voters to pick substance over
    # tribalism this election.
    ("congressional election campaign against political corruption", "en", 2797020),
    # 2797081 (pl): "W IMIENIU RZĄDU JEDNOŚCI NARODÓW..." — a Polish-language
    # call to arms as Finnish troops enter Norrland to break a Ukrainian
    # occupation there.
    ("Финландия обявява война за освобождение на Норрланд", "bg", 2797081),
    # 2794826 (hu): a Hungarian economic write-up of company/manager taxation
    # rates and how they are computed.
    ("análisis de impuestos sobre empresas y gerentes", "es", 2794826),
    # 2762609 (bg): a Bulgarian commentary on the Russia-Ukraine war, Bakhmut
    # casualties, mobilisation and who is "stuck in it" indirectly.
    ("orosz-ukrán háború és a bahmuti veszteségek", "hu", 2762609),
    # 2796804 (es): a Spanish monthly economy bulletin — austerity, budget
    # control, pensions.
    ("laporan ekonomi bulanan tentang penghematan dan pensiun", "id", 2796804),
    # 2796905 (id): an Indonesian economic write-up on the Aircraft Weapon
    # industry's potential in the Maluku Islands region.
    ("پتانسیل اقتصادی صنعت اسلحه هوایی در جزایر مالوکو", "fa", 2796905),
    # 2776557 (fa): a Persian proposal for a military participation incentive
    # scheme, with a bonus multiplier for low-level accounts.
    ("plan zachęt do udziału w wojnach dla słabszych kont", "pl", 2776557),
    # 2797058 (en): an eUK Ministry of Defence war update — containing a
    # "botserver", AEGIS tactics, coalition thanks.
    ("izveštaj o ratu i suzbijanju botserver naloga", "sr", 2797058),
]


@pytest.fixture
async def live_conn():
    dsn = os.environ.get("EVAL_DATABASE_URL")
    if not dsn:
        pytest.skip("EVAL_DATABASE_URL is not set")
    conn = await asyncpg.connect(dsn)
    yield conn
    await conn.close()


async def test_recall_at_20_across_languages(live_conn):
    filled = [c for c in CASES if c[2] is not None]
    if not filled:
        pytest.fail("CASES has no expected article ids — fill them in from the archive first")

    settings = Settings()
    client = EmbedClient(
        settings.embed_service_url, model=settings.embed_model,
        dim=repo.EMBED_DIM, timeout_sec=30.0,
    )

    hits = 0
    for query, lang, expected in filled:
        vector = (await client.embed([query]))[0]
        rows = await search.search_articles(live_conn, vector, limit=20)
        found = expected in [r.id for r in rows]
        print(f"{lang:>3} {query[:40]:<42} {'hit' if found else 'MISS'}")
        hits += found

    recall = hits / len(filled)
    # 0.8 rather than 1.0: this is a retrieval system, and a hand-built set of
    # this size cannot distinguish a real regression from one awkward query.
    # The number that matters is the one printed above when it drops.
    assert recall >= 0.8, f"recall@20 was {recall:.2f} across {len(filled)} queries"

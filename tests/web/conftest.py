import httpx
import pytest
import pytest_asyncio

from babel.config import Settings
from babel.db.repo import EMBED_DIM
from babel.web.app import create_app


@pytest.fixture
def image_root(tmp_path):
    return tmp_path


class StubEmbedder:
    """Stands in for EmbedClient. Tests mutate `error` and `vector` in place.

    Mutable rather than constructed per test, because the app is built once by
    the `client` fixture and the same instance has to be reachable from the
    test that wants it to fail.
    """

    def __init__(self):
        self.vector = [0.1] * EMBED_DIM
        self.error = None
        self.calls = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [self.vector]


@pytest.fixture
def embedder():
    return StubEmbedder()


@pytest_asyncio.fixture
async def client(pool, image_root, embedder):
    """The app driven over ASGI, with the per-test pool and embedder injected.

    The DSNs below are never dialled — the injected pool is the database. They
    are still two different strings, because open_pool refuses to start when
    they are equal and that refusal is production behaviour worth not
    accidentally disabling in the fixture.
    """
    settings = Settings(
        database_url="postgresql://babel@unused/babel",
        web_database_url="postgresql://babel_web@unused/babel",
        contact="archive@example.invalid",
        image_root=str(image_root),
        embed_service_url="http://embed.invalid",
        embed_model="test/model",
    )
    app = create_app(settings, pool=pool, embedder=embedder)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        yield c

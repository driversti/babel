import httpx
import pytest
import pytest_asyncio

from babel.config import Settings
from babel.web.app import create_app


@pytest.fixture
def image_root(tmp_path):
    return tmp_path


@pytest_asyncio.fixture
async def client(pool, image_root):
    """The app driven over ASGI, with the per-test pool injected.

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
    )
    app = create_app(settings, pool=pool)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        yield c

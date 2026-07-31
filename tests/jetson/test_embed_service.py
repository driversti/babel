import httpx
import pytest
import pytest_asyncio
from embed_service.app import create_app


class FakeEncoder:
    """Deterministic, and deliberately not unit-length.

    The service's job is to hand back what the encoder produced; normalising or
    otherwise fixing up a vector here would hide an encoder fault from the
    client, which is the one component positioned to notice it.
    """

    model_id = "fake/model"
    dim = 4
    cuda = False

    def __init__(self):
        self.calls = []

    async def encode(self, texts):
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0, 2.0, 3.0] for t in texts]


@pytest.fixture
def encoder():
    return FakeEncoder()


@pytest_asyncio.fixture
async def client(encoder):
    app = create_app(encoder, max_batch=3, max_input_chars=50)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_embed_returns_a_vector_per_text_and_names_its_model(client):
    resp = await client.post("/embed", json={"texts": ["ab", "cde"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "fake/model"
    assert body["dim"] == 4
    assert body["vectors"] == [[2.0, 1.0, 2.0, 3.0], [3.0, 1.0, 2.0, 3.0]]


async def test_healthz_reports_the_model_and_whether_cuda_is_live(client):
    body = (await client.get("/healthz")).json()
    assert body == {"model": "fake/model", "dim": 4, "cuda": False}


async def test_an_oversized_batch_is_refused_before_the_encoder_runs(client, encoder):
    resp = await client.post("/embed", json={"texts": ["a", "b", "c", "d"]})
    assert resp.status_code == 413
    assert encoder.calls == []


async def test_an_overlong_text_is_refused_before_the_encoder_runs(client, encoder):
    # The tokenizer's max_length bounds the quadratic attention term, but
    # tokenisation itself is linear in characters and runs first — so a
    # megabyte of text is expensive even when 1024 tokens of it survive. This
    # cap is what stops that, and it has to be checked before encode().
    resp = await client.post("/embed", json={"texts": ["x" * 51]})
    assert resp.status_code == 413
    assert encoder.calls == []


async def test_an_empty_batch_is_refused(client, encoder):
    resp = await client.post("/embed", json={"texts": []})
    assert resp.status_code == 400
    assert encoder.calls == []

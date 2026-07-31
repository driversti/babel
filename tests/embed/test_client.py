import pytest

from babel.embed.client import EmbedClient, EmbedError

DIM = 4


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for aiohttp.ClientSession, which is an async context manager
    whose .post() is another one."""

    def __init__(self, response):
        self._response = response
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json, **kwargs):
        self.posted.append((url, json))
        return self._response


def build(payload, status=200, dim=DIM):
    session = FakeSession(FakeResponse(status, payload))
    client = EmbedClient(
        "http://embed.invalid", model="right/model", dim=dim, timeout_sec=1.0,
        session_factory=lambda **kw: session,
    )
    return client, session


def unit(*values):
    """A unit-length vector of length DIM, padded with zeros."""
    return list(values) + [0.0] * (DIM - len(values))


async def test_returns_the_vectors():
    client, session = build(
        {"model": "right/model", "dim": DIM, "vectors": [unit(1.0), unit(0.0, 1.0)]}
    )
    assert await client.embed(["a", "b"]) == [unit(1.0), unit(0.0, 1.0)]
    assert session.posted[0][1] == {"texts": ["a", "b"]}


async def test_a_different_model_is_refused():
    """The silent failure this whole design is arranged around: vectors from
    two models are not comparable and nothing else would ever say so."""
    client, _ = build({"model": "other/model", "dim": DIM, "vectors": [unit(1.0)]})
    with pytest.raises(EmbedError, match="other/model"):
        await client.embed(["a"])


async def test_a_different_dimension_is_refused():
    client, _ = build({"model": "right/model", "dim": 7, "vectors": [unit(1.0)]})
    with pytest.raises(EmbedError, match="7"):
        await client.embed(["a"])


async def test_a_short_vector_is_refused():
    client, _ = build({"model": "right/model", "dim": DIM, "vectors": [[1.0, 0.0]]})
    with pytest.raises(EmbedError, match="length"):
        await client.embed(["a"])


async def test_a_wrong_count_is_refused():
    client, _ = build({"model": "right/model", "dim": DIM, "vectors": [unit(1.0)]})
    with pytest.raises(EmbedError, match="2"):
        await client.embed(["a", "b"])


async def test_a_vector_that_is_not_unit_length_is_refused():
    """A change detector on the encoder, not a correctness guard.

    Magnitude affects neither operator this project uses — measured against
    pgvector 0.8.6, scaling a vector 1000x leaves both binary_quantize and
    cosine distance identical. But bge-m3 normalises by default, so a vector
    that is not unit length means the encoder is no longer doing what this
    system was built against, and that is worth stopping for.
    """
    client, _ = build({"model": "right/model", "dim": DIM, "vectors": [[9.0, 0.0, 0.0, 0.0]]})
    with pytest.raises(EmbedError, match="norm"):
        await client.embed(["a"])


async def test_a_non_200_is_refused():
    client, _ = build({"detail": "too big"}, status=413)
    with pytest.raises(EmbedError, match="413"):
        await client.embed(["a"])


async def test_a_200_carrying_a_json_array_is_refused():
    """A 200 whose body is valid JSON but not an object — a bare list, for
    example — makes `body.get("model")` raise AttributeError, which is in
    neither the worker's nor the search route's except tuple, so it would
    escape both as an unhandled exception. Same class of bug as the
    json.JSONDecodeError path already closed: the service answered, but not
    with anything this client's contract expects.
    """
    client, _ = build([1, 2, 3])
    with pytest.raises(EmbedError, match="object"):
        await client.embed(["a"])


async def test_a_nan_component_is_refused():
    """The bug the whole-branch review found by tracing the path against a
    real pgvector container, not by reasoning: `norm = sqrt(sum(v*v ...))` is
    NaN when any component is NaN, and `abs(nan - 1.0) > NORM_TOLERANCE` is
    **False** — every comparison against NaN is False in IEEE 754 — so the
    existing norm guard lets a NaN vector through with no EmbedError. It then
    reaches `save_embeddings` as `[nan,0,...]` and asyncpg raises
    `DataError: NaN not allowed in halfvec` from a call site outside the
    worker's own try/except, killing the process; `restart: unless-stopped`
    brings it back to claim the same batch and die again, forever, with no
    alert. fp16 inference overflowing to inf and then to NaN through
    F.normalize is not exotic, so this is caught explicitly, before the norm
    check ever runs on it.
    """
    client, _ = build(
        {"model": "right/model", "dim": DIM, "vectors": [[float("nan"), 0.0, 0.0, 0.0]]}
    )
    with pytest.raises(EmbedError, match="finite"):
        await client.embed(["a"])


async def test_an_all_zero_vector_is_refused():
    """Not a new guard: the existing norm check already refuses this, because
    a zero vector's norm (0.0) sits 1.0 away from 1.0, over NORM_TOLERANCE.
    Deferred separately from the NaN finding above, and nothing pinned it
    with a test until now.
    """
    client, _ = build({"model": "right/model", "dim": DIM, "vectors": [[0.0, 0.0, 0.0, 0.0]]})
    with pytest.raises(EmbedError, match="norm"):
        await client.embed(["a"])

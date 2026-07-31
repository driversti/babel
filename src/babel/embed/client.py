"""Talks to the Jetson's embed service, and refuses anything surprising.

Every check here guards a failure that is otherwise silent. A vector of the
right shape from the wrong model produces a search that returns articles,
scores them plausibly, and is wrong — with no exception, no log line and no
failing healthcheck. The only detector is a human reading the results, so the
checks live at the one boundary positioned to see the model id at all.
"""

import math

import aiohttp

# How far a vector's L2 norm may sit from 1.0. Generous: bge-m3 normalises in
# fp16 on the GPU and the JSON round-trip is fp32, so exact equality is not
# available. Tight enough that an unnormalised vector cannot pass.
NORM_TOLERANCE = 0.05


class EmbedError(Exception):
    """The embed service answered, and the answer cannot be trusted."""


class EmbedClient:
    def __init__(
        self, base_url, model, dim, timeout_sec, *, session_factory=aiohttp.ClientSession
    ) -> None:
        self._url = base_url.rstrip("/") + "/embed"
        self._model = model
        self._dim = dim
        self._timeout_sec = timeout_sec
        self._session_factory = session_factory

    async def embed(self, texts: list[str]) -> list[list[float]]:
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)
        async with self._session_factory(timeout=timeout) as session, session.post(
            self._url, json={"texts": texts}
        ) as resp:
            if resp.status != 200:
                raise EmbedError(f"embed service returned {resp.status}: {await resp.text()}")
            body = await resp.json()

        if body.get("model") != self._model:
            raise EmbedError(
                f"embed service reports model {body.get('model')!r}, expected "
                f"{self._model!r} — vectors from two models are not comparable"
            )
        if body.get("dim") != self._dim:
            raise EmbedError(
                f"embed service reports dimension {body.get('dim')}, expected {self._dim}"
            )
        vectors = body.get("vectors") or []
        if len(vectors) != len(texts):
            raise EmbedError(f"asked for {len(texts)} vectors, got {len(vectors)}")
        for i, vec in enumerate(vectors):
            if len(vec) != self._dim:
                raise EmbedError(f"vector {i} has length {len(vec)}, expected {self._dim}")
            norm = math.sqrt(sum(v * v for v in vec))
            if abs(norm - 1.0) > NORM_TOLERANCE:
                raise EmbedError(
                    f"vector {i} has norm {norm:.4f} — the encoder has stopped normalising"
                )
        return vectors

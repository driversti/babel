"""The HTTP contract. No model code lives here.

This service holds no state, no credentials and no database driver, and knows
nothing about eRepublik. That is structural, not incidental: it is reachable
from the LAN and every string it sees originated with an anonymous visitor
typing into a public search box.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class EmbedRequest(BaseModel):
    texts: list[str]


def create_app(encoder, *, max_batch: int, max_input_chars: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"model": encoder.model_id, "dim": encoder.dim, "cuda": encoder.cuda}

    @app.post("/embed")
    async def embed(req: EmbedRequest) -> dict:
        if not req.texts:
            raise HTTPException(400, "texts is empty")
        if len(req.texts) > max_batch:
            raise HTTPException(413, f"batch of {len(req.texts)} exceeds {max_batch}")
        for i, text in enumerate(req.texts):
            if len(text) > max_input_chars:
                raise HTTPException(
                    413, f"text {i} is {len(text)} characters, over the {max_input_chars} cap"
                )
        vectors = await encoder.encode(req.texts)
        return {"model": encoder.model_id, "dim": encoder.dim, "vectors": vectors}

    return app

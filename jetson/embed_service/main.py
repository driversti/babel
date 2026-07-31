import os

import uvicorn

from embed_service.app import create_app
from embed_service.encoder import BgeM3Encoder


def build():
    # MAX_TOKENS and MAX_BATCH default to the measured values from
    # docs/superpowers/specs/2026-07-30-embeddings-design.md ("Still unmeasured"
    # item 1's table and item 2's reasoning) — 2048 and 32 — not generic
    # placeholders. The Dockerfile's ENV and jetson/docker-compose.yml both
    # supply these explicitly today, so this fallback is normally unreached,
    # but it is this process's own last line of defense if either of those is
    # ever bypassed (a bare `docker run`, a simplified compose file, running
    # this module directly). A wrong fallback here fails silently: half the
    # measured token cap truncates real article bodies, and double the
    # measured batch ceiling accepts a request size nothing was ever
    # benchmarked at.
    encoder = BgeM3Encoder(
        model_dir=os.environ.get("MODEL_DIR", "/models/bge-m3"),
        model_id=os.environ.get("MODEL_ID", "BAAI/bge-m3"),
        dim=int(os.environ.get("EMBED_DIM", "1024")),
        max_tokens=int(os.environ.get("MAX_TOKENS", "2048")),
        device=os.environ.get("DEVICE", "cuda"),
    )
    if not encoder.cuda:
        # Refuse rather than fall back to CPU. A silent CPU fallback is a 30x
        # slowdown that looks exactly like success, and the whole reason this
        # process runs on this machine is the GPU.
        raise RuntimeError("CUDA is not available — refusing to serve on CPU")
    return create_app(
        encoder,
        max_batch=int(os.environ.get("MAX_BATCH", "32")),
        max_input_chars=int(os.environ.get("MAX_INPUT_CHARS", "32000")),
    )


if __name__ == "__main__":
    uvicorn.run(build(), host="0.0.0.0", port=8081)  # noqa: S104

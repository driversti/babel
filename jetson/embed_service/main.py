import os

import uvicorn

from embed_service.app import create_app
from embed_service.encoder import BgeM3Encoder


def build():
    encoder = BgeM3Encoder(
        model_dir=os.environ.get("MODEL_DIR", "/models/bge-m3"),
        model_id=os.environ.get("MODEL_ID", "BAAI/bge-m3"),
        dim=int(os.environ.get("EMBED_DIM", "1024")),
        max_tokens=int(os.environ.get("MAX_TOKENS", "1024")),
        device=os.environ.get("DEVICE", "cuda"),
    )
    if not encoder.cuda:
        # Refuse rather than fall back to CPU. A silent CPU fallback is a 30x
        # slowdown that looks exactly like success, and the whole reason this
        # process runs on this machine is the GPU.
        raise RuntimeError("CUDA is not available — refusing to serve on CPU")
    return create_app(
        encoder,
        max_batch=int(os.environ.get("MAX_BATCH", "64")),
        max_input_chars=int(os.environ.get("MAX_INPUT_CHARS", "32000")),
    )


if __name__ == "__main__":
    uvicorn.run(build(), host="0.0.0.0", port=8081)  # noqa: S104

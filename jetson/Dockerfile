# Built on the Jetson itself. There is no cross-compilation and no registry
# push: the only machine that runs this image is the one that builds it, which
# is what retires SPEC.md's "ARM64 is a separate release path" risk instead of
# paying it.
FROM dustynv/l4t-pytorch:r36.4.0

WORKDIR /app

# transformers only — not FlagEmbedding or sentence-transformers. Both wrap the
# same forward pass in a pooling policy chosen by config, and this service must
# be explicit about taking bge-m3's *dense* CLS output: the model also emits a
# sparse head and a multi-vector head, and picking one of those by accident
# yields perfectly valid 1024-dimension vectors that retrieve badly.
#
# --index-url pins this to plain PyPI rather than the base image's default
# index (jetson-ai-lab, for Jetson-specific wheels). All three packages here
# are pure Python with no CUDA content, so there is no reason to source them
# from a Jetson wheel index — unlike torch itself, which is genuinely
# Jetson-specific and already in the base image, untouched by this line.
RUN pip install --no-cache-dir --index-url https://pypi.org/simple \
        "transformers>=4.44,<5" "fastapi>=0.115" "uvicorn>=0.32"

# Only the benchmark at this task. The service package does not exist yet —
# task 2 adds `COPY embed_service ./embed_service`, the EXPOSE and the CMD
# alongside the code they refer to. A COPY of a directory that is not there
# fails the build.
COPY bench.py ./bench.py

ENV MODEL_DIR=/models/bge-m3 \
    MODEL_ID=BAAI/bge-m3 \
    EMBED_DIM=1024 \
    MAX_TOKENS=1024 \
    MAX_BATCH=64 \
    MAX_INPUT_CHARS=32000 \
    DEVICE=cuda

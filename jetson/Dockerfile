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

# MAX_TOKENS=2048 and MAX_BATCH=32 are measured, not chosen defaults — see
# docs/superpowers/specs/2026-07-30-embeddings-design.md, "Still unmeasured"
# item 1's table and item 2's reasoning.
#
# MAX_TOKENS matches EMBED_MAX_TOKENS from that measurement: 1,024 tokens
# still truncates a p90-sized article (measured real token counts were
# 1,040-1,206 against a 4,600-character body), 2,048 does not, and the
# throughput cost between them is negligible (8.6-8.8 → 7.2-8.4 docs/s).
#
# MAX_BATCH is the service's request-size *ceiling*, not the worker's batch
# size (EMBED_BATCH_SIZE=8, chosen separately because batch 8 measured best
# or tied-best throughput at every token cap on this board's shared CPU/GPU
# memory bus). The ceiling does not have to equal the worker's batch size,
# but it should not be a number nothing measured: the original 64 here was
# never benchmarked. The sweep covered 8/16/32 — every batch above 8
# measured equal or worse throughput, but none errored or crashed up to 32 —
# so 32 is set as the ceiling: the largest batch size this Jetson is actually
# known to handle. 64 is dropped rather than carried forward unverified.
ENV MODEL_DIR=/models/bge-m3 \
    MODEL_ID=BAAI/bge-m3 \
    EMBED_DIM=1024 \
    MAX_TOKENS=2048 \
    MAX_BATCH=32 \
    MAX_INPUT_CHARS=32000 \
    DEVICE=cuda

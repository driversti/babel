# Cross-Language Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the public archive a search box that finds articles by meaning, in any language, from a query in any other.

**Architecture:** A stateless `POST /embed` service on the Jetson turns text into 1024-dimension vectors on the GPU. A worker on the x86 box drains a queue of unembedded articles through it and writes `halfvec(1024)` rows into the same Postgres the archive already uses. `babel serve` embeds the reader's query through the same service and answers with one two-stage SQL statement: Hamming distance over binary-quantised vectors to pick 500 candidates, then a re-rank of those against the full vectors.

**Tech Stack:** Python 3.12 (x86) / 3.10 (Jetson container), FastAPI, asyncpg, pgvector 0.8.6, PyTorch fp16 + transformers, `BAAI/bge-m3`, Docker Compose on both hosts.

**Read first:** [the design spec](../specs/2026-07-30-embeddings-design.md). It carries the measurements and the reasoning; this plan does not repeat them.

## Global Constraints

Every task's requirements implicitly include this section.

- **Postgres image is pinned to `pgvector/pgvector:0.8.6-pg17-trixie`.** Not `pg17`, not `-bookworm`. Measured 2026-07-30: prod `db` and this image both report `Debian GLIBC 2.41-12+deb13u3`, `trixie`. A different Debian generation silently changes text collation across the existing archive.
- **pgvector ≥ 0.8** — iterative index scans. Earlier versions filter after the graph walk.
- **Vectors are `halfvec(1024)`.** The dimension is the only thing the schema commits to; changing model later must not change it.
- **`hnsw.ef_search` must be ≥ the inner `LIMIT`,** and both it and `hnsw.iterative_scan = relaxed_order` must be set with `SET LOCAL` **inside an explicit transaction**. Outside one, Postgres warns and asyncpg does not raise.
- **Embed `body`, never `body_raw`.** `body_raw` is untrusted markup.
- **One model id**, configured once, checked by the worker and reported by `/healthz`.
- **Query length is capped twice** — at `web` before the call, and at the service before the tokenizer.
- **Articles only.** Comments are out of scope.
- **`jetson/` code must run on Python 3.10** (the L4T base image's interpreter). No PEP 695 generics, no `type` statements.
- **`uv run ruff check src tests jetson` clean; line length 110.**
- **`uv run pytest` green before every commit.** The suite needs Docker.
- Work happens on branch `feat/phase-3-embeddings`.

## File Structure

**New:**

| Path | Responsibility |
|---|---|
| `jetson/embed_service/app.py` | The HTTP contract: `/embed`, `/healthz`, batch and length caps. No model code. |
| `jetson/embed_service/encoder.py` | Loads `bge-m3`, tokenises, runs the forward pass, normalises. No HTTP. |
| `jetson/embed_service/main.py` | Wires encoder + app, reads env, runs uvicorn. |
| `jetson/bench.py` | Throughput measurement at several token caps. Task 1's deliverable. |
| `jetson/Dockerfile` | arm64, L4T PyTorch base. Built on the Jetson. |
| `jetson/docker-compose.yml` | The Jetson-side stack (one service, one model bind mount). |
| `migrations/008_embeddings.sql` | Extension, table, partial queue index, seeding. |
| `src/babel/embed/__init__.py` | Package marker. |
| `src/babel/embed/client.py` | HTTP client to `/embed`, and every check that makes a bad vector loud. |
| `src/babel/embed/worker.py` | The drain loop. Claim → embed → save → back off. |
| `src/babel/db/search.py` | The read path for search: the quantise expression, the index DDL, the two-stage query. |
| `src/babel/web/templates/search.html` | Results page and the honest-unavailable state. |
| `tests/jetson/test_embed_service.py` | Service contract, with a fake encoder. |
| `tests/embed/test_client.py` | Client checks. |
| `tests/embed/test_worker.py` | Drain loop behaviour. |
| `tests/db/test_embeddings_repo.py` | Claim, save, queue seeding, re-collection reset. |
| `tests/db/test_search_plans.py` | Index usage, `SET LOCAL`, expression agreement. |
| `tests/web/test_search_page.py` | Route, cap, degradation. |
| `tests/eval/test_cross_language.py` | Opt-in acceptance: recall@20 across languages. |

**Modified:**

| Path | Change |
|---|---|
| `src/babel/config.py` | Embed and search settings. |
| `src/babel/db/repo.py` | `save_article` queues the embedding; claim and save functions. |
| `src/babel/cli.py` | `babel embed` command. |
| `src/babel/web/app.py` | `008_embeddings.sql` in `REQUIRED_MIGRATIONS`; `/search` in `ROBOTS_TXT`. |
| `src/babel/web/routes.py` | The `/search` route. |
| `src/babel/web/templates/base.html` | Search box in the header. |
| `tests/conftest.py` | pgvector test container. |
| `pyproject.toml` | `pythonpath`, ruff per-file ignores for `jetson/`. |
| `docker-compose.yml` | `db` image; new `embed` service; `EMBED_SERVICE_URL` for `web`. |
| `.env.example` | New variables. |
| `README.md` | Jetson setup, index build, deploy order, reconcile statement. |
| `CLAUDE.md` | Status; phase 3 commands. |

---

### Task 1: Get bge-m3 onto the Jetson GPU and measure it

No product code and no test cycle — this task produces **numbers**, and those numbers set constants three later tasks depend on. It is first because the spec's sizing rests on an estimate (5–15 articles/second) that nobody has checked.

**Files:**
- Create: `jetson/Dockerfile`
- Create: `jetson/bench.py`
- Create: `jetson/.dockerignore`
- Modify: `docs/superpowers/specs/2026-07-30-embeddings-design.md` (the "Still unmeasured" section)

**Interfaces:**
- Consumes: nothing.
- Produces: measured values for `EMBED_MAX_TOKENS`, `EMBED_BATCH_SIZE`, and a throughput figure, all recorded in the spec. Task 2 uses the Dockerfile.

- [ ] **Step 1: Check what the four running containers do on a Docker restart**

The toolkit install restarts the daemon. Confirm first that nothing on the Jetson comes back down.

```bash
ssh jetson@<jetson-host> 'docker inspect -f "{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}" $(docker ps -q)'
```

Expected: every container reports `always` or `unless-stopped`. If any reports `no`, stop and tell the operator which one — it will not come back.

- [ ] **Step 2: Install the NVIDIA container toolkit**

```bash
ssh -t jetson@<jetson-host> 'sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker'
```

Needs the operator's password. If `nvidia-container-toolkit` is not found, the NVIDIA apt source is missing — `/etc/apt/sources.list.d/nvidia-l4t-apt-source.list` exists on this host, so report the exact apt error rather than guessing at repositories.

- [ ] **Step 3: Verify the GPU is visible from inside a container**

```bash
ssh jetson@<jetson-host> 'docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-jetpack:r36.4.0 nvidia-smi'
```

Expected: an `nvidia-smi` table naming Orin. If this fails, nothing later in this task can work — report the error verbatim and stop.

- [ ] **Step 4: Write the Dockerfile**

```dockerfile
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
RUN pip install --no-cache-dir "transformers>=4.44,<5" "fastapi>=0.115" "uvicorn>=0.32"

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
```

- [ ] **Step 5: Write `jetson/.dockerignore`**

```
__pycache__/
*.pyc
models/
```

- [ ] **Step 6: Write the benchmark**

```python
"""Throughput of bge-m3 on this machine, at several token caps.

Run inside the container, on the Jetson. Prints a table; the numbers go into
the design spec's "Still unmeasured" section, which is what closes it.

Deliberately measures with *real* article text rather than lorem ipsum: token
count per character varies by more than 2x across the scripts in this archive
(Latin against Persian and Cyrillic), and a benchmark on English alone would
report a throughput the corpus never sees.
"""

import argparse
import os
import statistics
import time

import torch
from transformers import AutoModel, AutoTokenizer

# One representative body per script family in the archive, trimmed to roughly
# the corpus p90 of 4,626 characters. Replace the placeholders with real text
# pulled from the archive before trusting the numbers:
#   psql -c "SELECT body FROM articles WHERE length(body) BETWEEN 4000 AND 5000 LIMIT 1"
SAMPLES = {
    "latin": "REPLACE ME with a real ~4600-char Polish or Spanish article body",
    "cyrillic": "REPLACE ME with a real ~4600-char Serbian or Bulgarian article body",
    "persian": "REPLACE ME with a real ~4600-char Persian article body",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.environ.get("MODEL_DIR", "/models/bge-m3"))
    ap.add_argument("--batches", type=int, default=10)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModel.from_pretrained(args.model_dir, torch_dtype=torch.float16)
    model = model.to("cuda").eval()

    print(f"{'tokens':>7} {'batch':>6} {'script':>9} {'docs/s':>8} {'ms/batch':>9}")
    for max_tokens in (512, 1024, 2048):
        for batch_size in (8, 16, 32):
            for name, text in SAMPLES.items():
                texts = [text] * batch_size
                timings = []
                for i in range(args.batches + 2):
                    torch.cuda.synchronize()
                    start = time.perf_counter()
                    with torch.inference_mode():
                        enc = tok(texts, padding=True, truncation=True,
                                  max_length=max_tokens, return_tensors="pt").to("cuda")
                        out = model(**enc).last_hidden_state[:, 0]
                        torch.nn.functional.normalize(out, p=2, dim=-1)
                    torch.cuda.synchronize()
                    # The first two iterations pay for CUDA context setup and
                    # kernel autotuning, which no production batch pays again.
                    if i >= 2:
                        timings.append(time.perf_counter() - start)
                per_batch = statistics.median(timings)
                print(f"{max_tokens:>7} {batch_size:>6} {name:>9} "
                      f"{batch_size / per_batch:>8.1f} {per_batch * 1000:>9.0f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Pull real sample text into the benchmark**

```bash
ssh erepublik@<deploy-host> 'cd ~/babel && docker compose exec -T db psql -U babel -d babel -A -t -c "SELECT left(body, 4600) FROM articles WHERE length(body) BETWEEN 4600 AND 6000 ORDER BY id DESC LIMIT 3"'
```

Paste three bodies of visibly different scripts into `SAMPLES`. If all three come back in the same script, re-query with `WHERE body ~ '[؀-ۿ]'` for Persian and `'[Ѐ-ӿ]'` for Cyrillic.

- [ ] **Step 8: Download the model onto the Jetson**

```bash
ssh jetson@<jetson-host> 'mkdir -p ~/babel-embed/models && docker run --rm -v ~/babel-embed/models:/models python:3.12-slim sh -c "pip install -q huggingface_hub && python -c \"from huggingface_hub import snapshot_download; snapshot_download(\\\"BAAI/bge-m3\\\", local_dir=\\\"/models/bge-m3\\\", allow_patterns=[\\\"*.json\\\",\\\"*.safetensors\\\",\\\"*.model\\\",\\\"tokenizer*\\\"])\""'
```

Expected: ~2.3 GB under `~/babel-embed/models/bge-m3`. Downloaded once, mounted read-only afterwards, so a restart needs no internet.

- [ ] **Step 9: Copy the repo to the Jetson and build**

```bash
ssh jetson@<jetson-host> 'git clone -b feat/phase-3-embeddings <repo-url> ~/babel-embed/src 2>/dev/null || git -C ~/babel-embed/src pull'
ssh jetson@<jetson-host> 'cd ~/babel-embed/src/jetson && docker build -t babel-embed .'
```

- [ ] **Step 10: Run the benchmark**

```bash
ssh jetson@<jetson-host> 'docker run --rm --runtime nvidia -v ~/babel-embed/models:/models:ro babel-embed python3 bench.py'
```

Expected: a table. Watch for `docs/s` collapsing between 1024 and 2048 tokens — if 2048 costs more than ~2.2x of 1024, the attention term has started to dominate and 1024 is the cap.

- [ ] **Step 11: Record the numbers in the spec and commit**

Replace items 1 and 2 of the spec's "Still unmeasured" section with the measured table and the chosen values. State the chosen `EMBED_MAX_TOKENS` and `EMBED_BATCH_SIZE` explicitly — later tasks read them from there.

```bash
git add jetson/ docs/superpowers/specs/2026-07-30-embeddings-design.md
git commit -m "Measure bge-m3 throughput on the Jetson"
```

---

### Task 2: The embed service

**Files:**
- Create: `jetson/embed_service/__init__.py`, `jetson/embed_service/app.py`, `jetson/embed_service/encoder.py`, `jetson/embed_service/main.py`
- Create: `jetson/docker-compose.yml`
- Create: `tests/jetson/__init__.py`, `tests/jetson/test_embed_service.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `jetson/Dockerfile` from Task 1.
- Produces: the HTTP contract every later task depends on —
  `POST /embed` with body `{"texts": [str, ...]}` returning `{"model": str, "dim": int, "vectors": [[float, ...], ...]}`;
  `GET /healthz` returning `{"model": str, "dim": int, "cuda": bool}`.
  `create_app(encoder) -> FastAPI`, where `encoder` has `.model_id: str`, `.dim: int`, `.cuda: bool`, and `async def encode(texts: list[str]) -> list[list[float]]`.

- [ ] **Step 1: Add `jetson` to the test path and relax ruff for it**

In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src", "jetson"]

[tool.ruff.lint.per-file-ignores]
# jetson/ runs on the L4T base image's Python 3.10, so ruff's pyupgrade rules —
# which target this repo's py312 — would suggest syntax that fails there.
"jetson/**" = ["UP"]
```

- [ ] **Step 2: Write the failing test**

`tests/jetson/test_embed_service.py`:

```python
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
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/jetson/test_embed_service.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'embed_service'`

- [ ] **Step 4: Write the app**

`jetson/embed_service/__init__.py` is empty. `jetson/embed_service/app.py`:

```python
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
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/jetson/test_embed_service.py -v`
Expected: 5 passed

- [ ] **Step 6: Write the encoder**

`jetson/embed_service/encoder.py`. Not covered by the suite above — it needs CUDA and a 2.3 GB model, and the dev machine has neither. Task 8's opt-in test is what exercises it.

```python
"""Loads bge-m3 and runs the forward pass. No HTTP.

CLS pooling, then L2 normalisation — that is bge-m3's *dense* representation.
The model also emits a sparse lexical head and a multi-vector ColBERT head;
both would produce plausible-looking output of the wrong meaning, and nothing
downstream could tell. Taking `last_hidden_state[:, 0]` explicitly is the point
of not using a wrapper library that picks a pooling policy from a config file.
"""

import asyncio

import torch
from transformers import AutoModel, AutoTokenizer


class BgeM3Encoder:
    def __init__(self, model_dir, model_id, dim, max_tokens, device="cuda"):
        self.model_id = model_id
        self.dim = dim
        self._max_tokens = max_tokens
        self._device = device
        self._tok = AutoTokenizer.from_pretrained(model_dir)
        self._model = AutoModel.from_pretrained(model_dir, torch_dtype=torch.float16)
        self._model = self._model.to(device).eval()
        # One batch on the GPU at a time. 8 GB is shared between the GPU and
        # everything else on this board, and two concurrent batches at the
        # token cap is the shape that ends in an allocator failure rather than
        # in slower service.
        self._lock = asyncio.Lock()

    @property
    def cuda(self):
        return self._device.startswith("cuda") and torch.cuda.is_available()

    def _encode_sync(self, texts):
        with torch.inference_mode():
            enc = self._tok(
                texts, padding=True, truncation=True,
                max_length=self._max_tokens, return_tensors="pt",
            ).to(self._device)
            out = self._model(**enc).last_hidden_state[:, 0]
            out = torch.nn.functional.normalize(out, p=2, dim=-1)
            return out.float().cpu().tolist()

    async def encode(self, texts):
        # to_thread, because the forward pass is a blocking C call: run inline
        # it would stall the event loop for the whole batch and /healthz would
        # time out under load, which is exactly when it is being asked.
        async with self._lock:
            return await asyncio.to_thread(self._encode_sync, texts)
```

- [ ] **Step 7: Write the entrypoint**

`jetson/embed_service/main.py`:

```python
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
```

- [ ] **Step 7b: Add the service to the Dockerfile**

Task 1 left it out deliberately — the package did not exist then. Append to `jetson/Dockerfile`, after the `COPY bench.py` line:

```dockerfile
COPY embed_service ./embed_service

# python3, not python. Measured inside the built image: the l4t-pytorch base
# provides /usr/bin/python3 (3.10.12) and no `python` on PATH at all, so the
# shorter spelling fails with "executable file not found in $PATH".
EXPOSE 8081
CMD ["python3", "-m", "embed_service.main"]
```

- [ ] **Step 8: Write the Jetson compose file**

`jetson/docker-compose.yml`:

```yaml
services:
  embed:
    build: .
    image: babel-embed
    container_name: babel-embed
    runtime: nvidia
    environment:
      - MODEL_DIR=/models/bge-m3
      - MODEL_ID=BAAI/bge-m3
      - EMBED_DIM=1024
      - MAX_TOKENS=${MAX_TOKENS:-1024}
      - MAX_BATCH=${MAX_BATCH:-64}
      - MAX_INPUT_CHARS=32000
      - DEVICE=cuda
    volumes:
      # Read-only: the service loads the model, it never writes one.
      - ${MODEL_ROOT:-~/babel-embed/models}:/models:ro
    # Bound to the LAN address, not 0.0.0.0. This machine also runs four
    # unrelated containers and sits on a home network; the only client that
    # needs to reach it is the x86 box.
    ports:
      - "${EMBED_BIND:-127.0.0.1}:8081:8081"
    restart: unless-stopped
    healthcheck:
      # python3 for the same reason as the Dockerfile's CMD — the base image
      # has no `python` on PATH.
      test: ["CMD", "python3", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz')"]
      interval: 30s
      timeout: 10s
      start_period: 180s
      retries: 3
```

`start_period` is 180s deliberately: loading 2.3 GB of weights onto the GPU takes far longer than the 60s the crawler's tunnel needs, and a shorter window marks the service unhealthy while it is starting normally.

- [ ] **Step 9: Deploy and verify against the real model**

```bash
ssh jetson@<jetson-host> 'cd ~/babel-embed/src/jetson && docker compose up -d --build'
ssh jetson@<jetson-host> 'sleep 120; curl -s http://<jetson-host>:8081/healthz'
```

Expected: `{"model":"BAAI/bge-m3","dim":1024,"cuda":true}`

Then check that the thing actually does what the whole project is for — a cross-language pair should score far higher than an unrelated pair:

```bash
curl -s -X POST http://<jetson-host>:8081/embed -H 'content-type: application/json' \
  -d '{"texts":["вибори в Сербії","elections in Serbia","recipe for tomato soup"]}' \
  | python3 -c "
import json,sys
v=json.load(sys.stdin)['vectors']
dot=lambda a,b: sum(x*y for x,y in zip(a,b))
print('ua/en  ', round(dot(v[0],v[1]),3))
print('ua/soup', round(dot(v[0],v[2]),3))"
```

Expected: the first number well above the second — order of 0.7+ against 0.3-ish. If they are close, the wrong pooling or the wrong head is in use; re-read `encoder.py` before going further, because every later task builds on this being right.

- [ ] **Step 10: Commit**

```bash
git add jetson/ tests/jetson/ pyproject.toml
git commit -m "Serve bge-m3 embeddings from the Jetson"
```

---

### Task 3: Schema, queue and repository

**Files:**
- Create: `migrations/008_embeddings.sql`
- Create: `tests/db/test_embeddings_repo.py`
- Modify: `src/babel/db/repo.py`, `tests/conftest.py`, `docker-compose.yml`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `repo.EMBED_DIM: int = 1024`
  - `repo.PendingEmbedding` — frozen dataclass with `article_id: int`, `title: str`, `body: str`
  - `async repo.claim_pending_embeddings(conn, limit: int) -> tuple[PendingEmbedding, ...]`
  - `async repo.save_embeddings(conn, model: str, rows: Sequence[tuple[int, str]]) -> None` — each row is `(article_id, vector_literal)`
  - `repo.vector_literal(values: Sequence[float]) -> str`

- [ ] **Step 1: Point the test container at pgvector**

In `tests/conftest.py`, change the image and extend the docstring:

```python
    with PostgresContainer("pgvector/pgvector:0.8.6-pg17-trixie") as container:
```

Add below the existing measurement note:

```python
    # pgvector rather than stock postgres:17, and pinned to the -trixie
    # variant. Prod runs 17.10-1.pgdg13+1 — a trixie build — and both report
    # Debian GLIBC 2.41-12+deb13u3. Testing against a bookworm image would
    # test a different collation than production has.
```

- [ ] **Step 2: Write the failing test**

`tests/db/test_embeddings_repo.py`:

```python
import datetime

from babel.db import repo
from babel.models import Article

UTC = datetime.UTC


def _article(article_id: int, body: str = "body", title: str = "title") -> Article:
    return Article(
        id=article_id, title=title, body=body, body_raw=None,
        author_id=1, author_name="a", country="Poland",
        published_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        e_day=6616, comment_count=0, comments=(), images=(),
    )


async def test_saving_an_article_queues_it_for_embedding(pg):
    await repo.save_article(pg, _article(10))
    pending = await repo.claim_pending_embeddings(pg, 10)
    assert [p.article_id for p in pending] == [10]
    assert pending[0].title == "title"
    assert pending[0].body == "body"


async def test_the_queue_is_newest_first(pg):
    for article_id in (10, 30, 20):
        await repo.save_article(pg, _article(article_id))
    pending = await repo.claim_pending_embeddings(pg, 10)
    assert [p.article_id for p in pending] == [30, 20, 10]


async def test_an_embedded_article_leaves_the_queue(pg):
    await repo.save_article(pg, _article(10))
    vector = repo.vector_literal([0.1] * repo.EMBED_DIM)
    await repo.save_embeddings(pg, "test/model", [(10, vector)])
    assert await repo.claim_pending_embeddings(pg, 10) == ()
    stored = await pg.fetchval("SELECT model FROM article_embeddings WHERE article_id = 10")
    assert stored == "test/model"


async def test_a_hidden_article_is_not_offered(pg):
    await repo.save_article(pg, _article(10))
    await pg.execute("UPDATE articles SET hidden_at = now() WHERE id = 10")
    assert await repo.claim_pending_embeddings(pg, 10) == ()


async def test_re_collecting_with_a_changed_body_clears_the_vector(pg):
    """A sweep that rewrites the text must not leave the old meaning behind."""
    await repo.save_article(pg, _article(10, body="first"))
    await repo.save_embeddings(pg, "test/model", [(10, repo.vector_literal([0.1] * repo.EMBED_DIM))])
    await repo.save_article(pg, _article(10, body="entirely different text"))
    assert [p.article_id for p in await repo.claim_pending_embeddings(pg, 10)] == [10]


async def test_re_collecting_with_an_unchanged_body_keeps_the_vector(pg):
    """Otherwise every sweep drops the whole archive out of search for hours."""
    await repo.save_article(pg, _article(10, body="same"))
    await repo.save_embeddings(pg, "test/model", [(10, repo.vector_literal([0.1] * repo.EMBED_DIM))])
    await repo.save_article(pg, _article(10, body="same", title="a new title"))
    assert await repo.claim_pending_embeddings(pg, 10) == ()


async def test_the_seeding_statement_queues_articles_that_predate_the_migration(pg):
    """The gap the deploy order is supposed to prevent, closed by hand.

    Same shape as the one `refetch --to` left: rows written by an image that
    did not know about this table get no queue row, and nothing revisits them.
    """
    await repo.save_article(pg, _article(10))
    await pg.execute("DELETE FROM article_embeddings")
    assert await repo.claim_pending_embeddings(pg, 10) == ()
    await pg.execute(
        "INSERT INTO article_embeddings (article_id) SELECT id FROM articles "
        "ON CONFLICT DO NOTHING"
    )
    assert [p.article_id for p in await repo.claim_pending_embeddings(pg, 10)] == [10]
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/db/test_embeddings_repo.py -v`
Expected: FAIL — `asyncpg.UndefinedTableError: relation "article_embeddings" does not exist`

- [ ] **Step 4: Write the migration**

`migrations/008_embeddings.sql`:

```sql
-- Vectors, and the queue that fills them.
--
-- APPLY THIS DELIBERATELY, and only after `docker compose build`. Migrations
-- are baked into the image (the Dockerfile COPYs migrations/ and nothing
-- bind-mounts it), so migrating before the rebuild runs the *old* file, reports
-- nothing to do, and exits 0. README has the order.
--
-- Requires the pgvector extension, which stock postgres:17 does not carry. The
-- db service image must be pgvector/pgvector:0.8.6-pg17-trixie — the -trixie
-- variant specifically, matching the 17.10-1.pgdg13+1 already running. Both
-- ship Debian GLIBC 2.41-12+deb13u3; a bookworm image would change collation
-- under every existing text index.
CREATE EXTENSION IF NOT EXISTS vector;

-- A row per article, created at ingest with a NULL vector. This mirrors
-- article_images, and for the same reason: the alternative — no row until
-- there is a vector, and an anti-join for the queue — reads the articles
-- primary key backwards and probes for each row, which is cheap only while the
-- unembedded rows are near the top. They are not. The poller adds ~18 a day at
-- the top and the descending walk adds ~1 a second at the *bottom*, so in
-- steady state the queue lives at the walk frontier and every claim scans past
-- the whole embedded corpus to reach it — growing to 2.8M index entries, paid
-- again every couple of seconds, forever.
--
-- NO status and NO attempts column, unlike article_images. That table needs
-- them because a remote host can permanently refuse a URL and the verdict has
-- to be remembered. Embedding has no such verdict: a failure is always
-- transient — the service is down, the batch timed out — and the answer is
-- always to try again. Retry accounting lives in the worker process, where a
-- restart resets it, which is right for a transient-only failure.
CREATE TABLE article_embeddings (
    article_id bigint PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    embedding  halfvec(1024),
    model      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The queue. Partial, so it shrinks to nothing as the corpus drains and never
-- covers a row that already has a vector.
CREATE INDEX article_embeddings_pending_idx
    ON article_embeddings (article_id DESC) WHERE embedding IS NULL;

-- Everything already collected. ~162,618 rows at the time of writing, all of
-- them narrow, so this is fast — unlike 005's index builds, which is why this
-- one may live in the migration at all.
INSERT INTO article_embeddings (article_id) SELECT id FROM articles
ON CONFLICT DO NOTHING;

-- The HNSW similarity index is deliberately NOT here. CREATE INDEX
-- CONCURRENTLY is illegal inside a transaction and this runner wraps a whole
-- file in one, so an index build at scale is an operator step through psql.
-- README carries the statement and the maintenance_work_mem it needs.
```

- [ ] **Step 5: Add the repository functions**

In `src/babel/db/repo.py`, after `MAX_FETCH_ATTEMPTS`:

```python
# The dimension of every stored vector. It is the only thing the schema commits
# to: switching to a different 1024-dimension encoder costs a re-run of the
# corpus and no migration at all.
EMBED_DIM = 1024
```

And near the dataclasses:

```python
@dataclass(frozen=True, slots=True)
class PendingEmbedding:
    article_id: int
    title: str
    body: str


def vector_literal(values: Sequence[float]) -> str:
    """A vector as pgvector's text form, for a `$n::halfvec(1024)` cast.

    Passed as text rather than through a registered asyncpg codec. A codec
    would have to be installed on every connection the pool hands out — the web
    pool included, which connects as a role that may not create types — and the
    saving is nothing: 1024 floats is ~12 KB of text against a batch that
    already carried tens of kilobytes of article body.
    """
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


_CLAIM_PENDING_EMBEDDINGS = """
    SELECT ae.article_id, a.title, a.body
    FROM article_embeddings ae
    JOIN articles a ON a.id = ae.article_id
    WHERE ae.embedding IS NULL AND a.hidden_at IS NULL
    ORDER BY ae.article_id DESC
    LIMIT $1
"""


async def claim_pending_embeddings(
    conn: asyncpg.Connection, limit: int
) -> tuple[PendingEmbedding, ...]:
    """The next articles with no vector, newest first.

    "Claim" by analogy with claim_pending_images, but nothing is marked: the row
    leaves the queue when its vector is written and not before. There is no
    status to move it through, so a worker killed mid-batch has changed nothing
    and the same rows are simply offered again.

    Newest-first for the same reason as the image drain and the article walk —
    the most-read part of the archive becomes searchable first.
    """
    rows = await conn.fetch(_CLAIM_PENDING_EMBEDDINGS, limit)
    return tuple(
        PendingEmbedding(article_id=r["article_id"], title=r["title"], body=r["body"])
        for r in rows
    )


async def save_embeddings(
    conn: asyncpg.Connection, model: str, rows: Sequence[tuple[int, str]]
) -> None:
    """Write a batch of vectors. `rows` is (article_id, vector_literal)."""
    if not rows:
        return
    await conn.execute(
        f"""
        UPDATE article_embeddings ae
        SET embedding = v.vec::halfvec({EMBED_DIM}), model = $1, updated_at = now()
        FROM unnest($2::bigint[], $3::text[]) AS v(article_id, vec)
        WHERE ae.article_id = v.article_id
        """,
        model, [r[0] for r in rows], [r[1] for r in rows],
    )
```

- [ ] **Step 6: Queue the article inside `save_article`**

In `src/babel/db/repo.py`, inside `save_article`'s `async with conn.transaction():` block, **before** the `INSERT INTO articles` statement:

```python
        # BEFORE the articles upsert, not after — the comparison is against the
        # body currently stored, and after the upsert that is already the new
        # one, so the same statement moved three lines down silently never
        # fires. The INSERT arm queues a new article; the UPDATE arm clears a
        # vector whose text has changed underneath it.
        #
        # Gated on the body actually differing, rather than clearing on every
        # save. A re-collection sweep touches the whole archive, and clearing
        # unconditionally would drop all of it out of search for as long as the
        # re-embed takes. A title-only edit leaves the vector alone too, which
        # is a deliberate approximation: the title is a small part of the
        # embedded text and is not worth a full re-encode of the corpus.
        await conn.execute(
            """
            INSERT INTO article_embeddings (article_id) VALUES ($1)
            ON CONFLICT (article_id) DO UPDATE
                SET embedding = NULL, model = NULL, updated_at = now()
            WHERE EXISTS (
                SELECT 1 FROM articles
                WHERE id = $1 AND body IS DISTINCT FROM $2
            )
            """,
            article.id, article.body,
        )
```

- [ ] **Step 7: Run the tests and watch them pass**

Run: `uv run pytest tests/db/test_embeddings_repo.py -v`
Expected: 7 passed

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest`
Expected: all pass. `tests/db/test_migration.py` may assert a migration count — update it if so.

- [ ] **Step 9: Swap the db image in compose**

In `docker-compose.yml`, under `db`:

```yaml
    image: pgvector/pgvector:0.8.6-pg17-trixie
```

Add above it:

```yaml
    # Stock postgres:17 plus the vector extension — same upstream image, same
    # PGDATA layout, so this is an image swap and not a dump and restore. The
    # -trixie tag is not optional: prod runs 17.10-1.pgdg13+1 and both report
    # Debian GLIBC 2.41-12+deb13u3. A bookworm image would change text
    # collation under every index already built on this data.
```

- [ ] **Step 10: Commit**

```bash
git add migrations/008_embeddings.sql src/babel/db/repo.py tests/db/test_embeddings_repo.py tests/conftest.py docker-compose.yml
git commit -m "Add the embedding table and its queue"
```

---

### Task 4: The embed client

**Files:**
- Create: `src/babel/embed/__init__.py`, `src/babel/embed/client.py`
- Create: `tests/embed/__init__.py`, `tests/embed/test_client.py`
- Modify: `src/babel/config.py`

**Interfaces:**
- Consumes: the `/embed` contract from Task 2; `repo.EMBED_DIM` from Task 3.
- Produces:
  - `class EmbedError(Exception)`
  - `class EmbedClient` with `__init__(self, base_url: str, model: str, dim: int, timeout_sec: float, *, session_factory=aiohttp.ClientSession)` and `async def embed(self, texts: list[str]) -> list[list[float]]`

- [ ] **Step 1: Add the settings**

In `src/babel/config.py`, after `image_batch_size`:

```python
    # The Jetson's embed service. A placeholder default, like every other
    # address in this file: this repository is public.
    embed_service_url: str = Field(default="http://localhost:8081")
    embed_model: str = Field(default="BAAI/bge-m3")

    # Batch and token caps are set by the benchmark in the plan's task 1, not
    # guessed. Both are configuration because the corpus and the query are
    # different workloads: one is 32 long texts that may take a second, the
    # other is one short string a reader is waiting on.
    embed_batch_size: int = Field(default=32, ge=1, le=64)

    # Article text is truncated to this before it is sent. Far above what the
    # token cap can reach in any script in this archive (1024 tokens is ~4,000
    # Latin characters and ~2,500 Cyrillic or Persian), so nothing the
    # tokenizer would keep is discarded here — this exists to bound the request
    # body, not to shape the input.
    embed_max_chars: int = Field(default=20_000, ge=1_000)

    embed_timeout_sec: float = Field(default=120.0, gt=0)
    embed_idle_sleep_sec: float = Field(default=60.0, gt=0)
    embed_backoff_base_sec: float = Field(default=5.0, gt=0)
    embed_backoff_max_sec: float = Field(default=600.0, gt=0)

    # The public search path. A much tighter cap than the corpus one: this
    # string comes from anyone on the internet, and attention is quadratic.
    search_max_query_chars: int = Field(default=512, ge=1)
    search_timeout_sec: float = Field(default=2.0, gt=0)
```

- [ ] **Step 2: Write the failing test**

`tests/embed/test_client.py`:

```python
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
    vec = list(values) + [0.0] * (DIM - len(values))
    return vec


async def test_returns_the_vectors(pytestconfig):
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
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/embed/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.embed'`

- [ ] **Step 4: Write the client**

`src/babel/embed/__init__.py` is empty. `src/babel/embed/client.py`:

```python
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
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/embed/test_client.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/babel/embed/ src/babel/config.py tests/embed/
git commit -m "Add the embed client and its refusals"
```

---

### Task 5: The worker and the `babel embed` command

**Files:**
- Create: `src/babel/embed/worker.py`, `tests/embed/test_worker.py`
- Modify: `src/babel/cli.py`, `docker-compose.yml`, `.env.example`

**Interfaces:**
- Consumes: `repo.claim_pending_embeddings`, `repo.save_embeddings`, `repo.vector_literal` (Task 3); `EmbedClient`, `EmbedError` (Task 4).
- Produces: `async run_embed_worker(pool, client, notifier, settings, *, sleep=asyncio.sleep, max_cycles=None) -> None`; the `babel embed` CLI command; the `embed` compose service.

- [ ] **Step 1: Write the failing test**

`tests/embed/test_worker.py`:

```python
import dataclasses
import datetime

import pytest

from babel.config import Settings
from babel.db import repo
from babel.embed.client import EmbedError
from babel.embed.worker import run_embed_worker
from babel.models import Article

UTC = datetime.UTC


def _article(article_id: int) -> Article:
    return Article(
        id=article_id, title=f"title {article_id}", body=f"body {article_id}", body_raw=None,
        author_id=1, author_name="a", country="Poland",
        published_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        e_day=6616, comment_count=0, comments=(), images=(),
    )


class FakeClient:
    def __init__(self, dim=repo.EMBED_DIM, fail_times=0):
        self.dim = dim
        self.batches = []
        self._fail_times = fail_times

    async def embed(self, texts):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise EmbedError("service down")
        self.batches.append(list(texts))
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_once(self, key, text):
        self.sent.append((key, text))


class Clock:
    def __init__(self):
        self.slept = []

    async def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://babel@unused/babel",
        embed_batch_size=2, embed_idle_sleep_sec=60.0,
        embed_backoff_base_sec=5.0, embed_backoff_max_sec=20.0,
    )


async def test_a_batch_is_embedded_and_stored(pool, pg, settings):
    for article_id in (10, 20):
        await repo.save_article(pg, _article(article_id))
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert await repo.claim_pending_embeddings(pg, 10) == ()
    assert await pg.fetchval("SELECT count(*) FROM article_embeddings WHERE embedding IS NOT NULL") == 2


async def test_the_title_is_embedded_with_the_body(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert client.batches == [["title 10\n\nbody 10"]]


async def test_long_bodies_are_truncated_before_they_are_sent(pool, pg, settings):
    # dataclasses.replace, not `type(a)(**a.__dict__)`: Article is
    # @dataclass(frozen=True, slots=True) and a slots dataclass has no __dict__.
    await repo.save_article(pg, dataclasses.replace(_article(10), body="x" * 50_000))
    settings = settings.model_copy(update={"embed_max_chars": 100})
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert len(client.batches[0][0]) == 100


async def test_an_empty_queue_sleeps_rather_than_spinning(pool, settings):
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert clock.slept == [settings.embed_idle_sleep_sec]
    assert client.batches == []


async def test_a_failed_batch_leaves_the_rows_queued(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(fail_times=1), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert [p.article_id for p in await repo.claim_pending_embeddings(pg, 10)] == [10]


async def test_repeated_failures_back_off_and_stop_doubling_at_the_ceiling(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(fail_times=99), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=4)
    assert clock.slept == [5.0, 10.0, 20.0, 20.0]


async def test_a_persistent_failure_alerts_once(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    notifier = FakeNotifier()
    client, clock = FakeClient(fail_times=99), Clock()
    await run_embed_worker(pool, client, notifier, settings,
                           sleep=clock.sleep, max_cycles=3)
    assert [key for key, _ in notifier.sent] == ["embed-service-down"] * 3
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/embed/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.embed.worker'`

- [ ] **Step 3: Write the worker**

`src/babel/embed/worker.py`:

```python
"""Drains the embedding queue.

The queue is `article_embeddings` rows with a NULL vector. Nothing is claimed
or marked: a row leaves the queue when its vector is written, so a worker
killed mid-batch has changed nothing and the same rows are offered again.

Newest article first, matching the image drain and the article walk — the
most-read part of the archive becomes searchable first.
"""

import asyncio
import logging

from babel.db import repo
from babel.embed.client import EmbedError

log = logging.getLogger("babel.embed")

SERVICE_ALERT_KEY = "embed-service-down"


async def run_embed_worker(
    pool, client, notifier, settings, *, sleep=asyncio.sleep, max_cycles: int | None = None
) -> None:
    """Embed queued articles until stopped. `max_cycles` bounds the loop for tests."""
    cycles = 0
    consecutive_failures = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1

        async with pool.acquire() as conn:
            batch = await repo.claim_pending_embeddings(conn, settings.embed_batch_size)

        if not batch:
            await sleep(settings.embed_idle_sleep_sec)
            continue

        texts = [
            f"{item.title}\n\n{item.body}"[: settings.embed_max_chars] for item in batch
        ]
        try:
            vectors = await client.embed(texts)
        except (EmbedError, OSError, asyncio.TimeoutError) as exc:
            # Nothing is written and nothing is marked, so every row in this
            # batch is still queued. The back-off is the only state the failure
            # leaves behind, and a restart discards it — which is right, because
            # "the service is down" is a fact about now.
            consecutive_failures += 1
            delay = min(
                settings.embed_backoff_base_sec * 2 ** (consecutive_failures - 1),
                settings.embed_backoff_max_sec,
            )
            log.warning("embed batch of %d failed (%s) — waiting %.0fs", len(batch), exc, delay)
            await notifier.send_once(
                SERVICE_ALERT_KEY, f"babel: the embed service is not answering ({exc})"
            )
            await sleep(delay)
            continue

        consecutive_failures = 0
        rows = [
            (item.article_id, repo.vector_literal(vec))
            for item, vec in zip(batch, vectors, strict=True)
        ]
        async with pool.acquire() as conn:
            await repo.save_embeddings(conn, settings.embed_model, rows)
        log.info("embedded %d article(s), newest %d", len(rows), batch[0].article_id)
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/embed/test_worker.py -v`
Expected: 7 passed

- [ ] **Step 5: Add the CLI command**

In `src/babel/cli.py`, after the `images` command:

```python
@main.command()
def embed() -> None:
    """Embed queued articles until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_embed())


async def _embed() -> None:
    from babel.embed.client import EmbedClient
    from babel.embed.worker import run_embed_worker

    settings = Settings()
    notifier = Throttled(build_notifier(settings), settings.alert_repeat_sec)

    # No check_ip_leak and no gluetun namespace, unlike `run` and `images`.
    # This process never touches eRepublik: it talks to Postgres on the bridge
    # and to a LAN address, and routing either through the tunnel would buy
    # nothing and break both.
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await apply_migrations(conn, MIGRATIONS)

    client = EmbedClient(
        settings.embed_service_url,
        model=settings.embed_model,
        dim=repo.EMBED_DIM,
        timeout_sec=settings.embed_timeout_sec,
    )
    await run_embed_worker(pool, client, notifier, settings)
```

Add `from babel.db import repo` to the imports at the top of `cli.py` if it is not already there.

- [ ] **Step 6: Add the compose service**

In `docker-compose.yml`:

```yaml
  # Turns article text into vectors, through the Jetson's GPU.
  #
  # Deliberately NOT network_mode: service:gluetun, unlike crawler/images/sweep.
  # This process never touches eRepublik. Its two dependencies are Postgres on
  # the bridge and the embed service on the LAN, and gluetun's namespace can
  # reach neither without widening FIREWALL_OUTBOUND_SUBNETS to the home
  # network — which is exactly the exposure the tunnel exists to prevent.
  #
  # Like crawler and images it applies pending migrations at startup, so it is
  # subject to the same build-before-migrate rule in README.
  embed:
    build: .
    container_name: babel-embed
    command: ["babel", "embed"]
    env_file: .env
    depends_on:
      db: {condition: service_healthy}
    restart: unless-stopped
```

And add to the `web` service's `environment:` block:

```yaml
      - EMBED_SERVICE_URL=${EMBED_SERVICE_URL}
      - EMBED_MODEL=${EMBED_MODEL:-BAAI/bge-m3}
      - SEARCH_MAX_QUERY_CHARS=${SEARCH_MAX_QUERY_CHARS:-512}
      - SEARCH_TIMEOUT_SEC=${SEARCH_TIMEOUT_SEC:-2.0}
```

- [ ] **Step 7: Document the variables**

In `.env.example`:

```bash
# The Jetson's embed service, reachable on the LAN. Both `babel embed` and
# `babel serve` call it — the corpus and the query must go through the same
# encoder or their vectors are not comparable.
EMBED_SERVICE_URL=http://<jetson-lan-address>:8081
EMBED_MODEL=BAAI/bge-m3
EMBED_BATCH_SIZE=32
EMBED_MAX_CHARS=20000

# The public search path. A query is a string from anyone on the internet and
# attention is quadratic in length, so this cap is much tighter than the
# corpus one.
SEARCH_MAX_QUERY_CHARS=512
SEARCH_TIMEOUT_SEC=2.0
```

- [ ] **Step 8: Run the whole suite, then commit**

```bash
uv run pytest && uv run ruff check src tests jetson
git add src/babel/embed/worker.py src/babel/cli.py tests/embed/test_worker.py docker-compose.yml .env.example
git commit -m "Drain the embedding queue through the Jetson"
```

---

### Task 6: The search query

**Files:**
- Create: `src/babel/db/search.py`, `tests/db/test_search_plans.py`
- Modify: none

**Interfaces:**
- Consumes: `repo.EMBED_DIM`, `repo.vector_literal` (Task 3).
- Produces:
  - `search.CANDIDATES: int = 500`, `search.RESULTS: int = 20`
  - `search.HNSW_INDEX_SQL: str`
  - `search.build_search_query() -> str`
  - `search.SearchRow` — frozen dataclass with `id, title, author_name, country, published_at, score`
  - `async search.search_articles(conn, vector: Sequence[float], *, candidates=CANDIDATES, limit=RESULTS) -> tuple[SearchRow, ...]`

- [ ] **Step 1: Write the failing test**

`tests/db/test_search_plans.py`:

```python
import datetime

from babel.db import repo, search

UTC = datetime.UTC


async def _populated(conn, rows=500):
    """Vectors that differ from each other, deterministically.

    Identical vectors would build a degenerate HNSW graph, which is both slow
    to construct and useless for proving the planner will use it.
    """
    await conn.execute(
        """INSERT INTO articles (id, title, body, author_name, country,
                                 published_at, comment_count)
           SELECT g, 'T' || g, 'body', 'author', 'Poland',
                  timestamptz '2026-01-01' + (g || ' seconds')::interval, 0
             FROM generate_series(1, $1) g
           ON CONFLICT (id) DO NOTHING""",
        rows,
    )
    await conn.execute(
        """INSERT INTO article_embeddings (article_id, embedding, model)
           SELECT g,
                  (SELECT ('[' || string_agg(sin(g * 0.37 + i)::real::text, ',') || ']')
                     FROM generate_series(1, $2) i)::halfvec($2),
                  'test/model'
             FROM generate_series(1, $1) g
           ON CONFLICT (article_id) DO UPDATE
               SET embedding = EXCLUDED.embedding, model = EXCLUDED.model""",
        rows, repo.EMBED_DIM,
    )
    await conn.execute(search.HNSW_INDEX_SQL)
    await conn.execute("ANALYZE articles")
    await conn.execute("ANALYZE article_embeddings")


def _probe():
    return [0.1] * repo.EMBED_DIM


async def test_the_index_and_the_query_quantise_identically(pg):
    """The invariant that makes the index usable at all.

    Both strings come from the same helper, so this fails the moment someone
    edits one of them alone — which is the change that would silently turn
    every search into a sequential scan over 2.8M rows.
    """
    expression = search.quantised("embedding")
    assert expression in search.HNSW_INDEX_SQL
    assert expression in search.build_search_query()


async def test_the_search_uses_the_hnsw_index(pg):
    await _populated(pg)
    sql = search.build_search_query()
    async with pg.transaction():
        # Forced, not hoped for. At any row count a test can afford, the
        # planner may reasonably prefer a scan; what this test exists to prove
        # is that the ORDER BY expression *matches* the index, which is what
        # breaks when someone edits one side of it.
        await pg.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            r[0] for r in await pg.fetch(
                f"EXPLAIN {sql}", repo.vector_literal(_probe()), 50, 10
            )
        )
    assert "article_embeddings_bin_idx" in plan


async def test_set_local_actually_takes_effect(pg):
    """SET LOCAL outside a transaction is a no-op: Postgres emits a WARNING and
    asyncpg does not raise it, so ef_search would silently stay at 40 while the
    inner query asks for 500 candidates. This reads the setting back rather
    than trusting that issuing it worked."""
    await _populated(pg, rows=50)
    seen = await search.search_articles_with_settings_probe(pg, _probe(), candidates=123, limit=5)
    assert seen == "123"


async def test_results_are_ordered_by_similarity_to_the_probe(pg):
    await _populated(pg, rows=200)
    target = await pg.fetchval("SELECT embedding FROM article_embeddings WHERE article_id = 77")
    vector = [float(v) for v in str(target).strip("[]").split(",")]
    rows = await search.search_articles(pg, vector, candidates=100, limit=5)
    assert rows[0].id == 77
    assert rows[0].score > rows[-1].score


async def test_a_pending_row_is_never_returned(pg):
    await _populated(pg, rows=200)
    await pg.execute("UPDATE article_embeddings SET embedding = NULL WHERE article_id = 77")
    rows = await search.search_articles(pg, _probe(), candidates=200, limit=200)
    assert 77 not in [r.id for r in rows]


async def test_a_hidden_article_is_never_returned(pg):
    await _populated(pg, rows=200)
    await pg.execute("UPDATE articles SET hidden_at = now() WHERE id = 77")
    rows = await search.search_articles(pg, _probe(), candidates=200, limit=200)
    assert 77 not in [r.id for r in rows]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/db/test_search_plans.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.db.search'`

- [ ] **Step 3: Write the search module**

`src/babel/db/search.py`:

```python
"""The read path for semantic search.

Two stages in one statement. Stage one ranks by Hamming distance over
binary-quantised vectors — 128 bytes each instead of 2,048 — and takes the top
`candidates`. Stage two re-ranks exactly those against the full halfvec. The
quantised form loses precision; the over-fetch is what buys it back.

The index and the query must quantise the same way or the index cannot serve
the ORDER BY, and the symptom is a sequential scan over the whole table rather
than an error. `quantised()` is the single source both take it from.
"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

import asyncpg

from babel.db.repo import EMBED_DIM, vector_literal

# 25x over-fetch. A starting value, not a measured optimum — the acceptance
# test in the plan's task 8 is what checks it against real recall.
CANDIDATES = 500
RESULTS = 20


def quantised(expression: str) -> str:
    return f"binary_quantize({expression})::bit({EMBED_DIM})"


HNSW_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS article_embeddings_bin_idx ON article_embeddings
    USING hnsw (({quantised("embedding")}) bit_hamming_ops)
    WHERE embedding IS NOT NULL
"""


@dataclass(frozen=True, slots=True)
class SearchRow:
    id: int
    title: str
    author_name: str | None
    country: str | None
    published_at: datetime.datetime
    score: float


def build_search_query() -> str:
    """The two-stage statement. Its parameters are ($1 vector text, $2 candidates, $3 limit).

    Returned rather than executed so the EXPLAIN test plans the query the
    application actually sends — the mistake finding M3 records against the
    older suite is asserting on a copy pasted into a test.

    Takes no arguments: candidates and limit are bound at execution, and the
    statement text does not vary with them. That is the difference from
    build_list_query, whose text genuinely varies per filter combination.
    """
    return f"""
        SELECT a.id, a.title, a.author_name, a.country, a.published_at,
               1 - (c.embedding <=> $1::halfvec({EMBED_DIM})) AS score
        FROM (
            SELECT ae.article_id, ae.embedding
            FROM article_embeddings ae
            JOIN articles ar ON ar.id = ae.article_id AND ar.hidden_at IS NULL
            WHERE ae.embedding IS NOT NULL
            ORDER BY {quantised("ae.embedding")}
                     <~> {quantised(f"$1::halfvec({EMBED_DIM})")}
            LIMIT $2
        ) c
        JOIN articles a ON a.id = c.article_id
        ORDER BY c.embedding <=> $1::halfvec({EMBED_DIM})
        LIMIT $3
    """


async def _apply_settings(conn: asyncpg.Connection, candidates: int) -> None:
    """Both settings, inside whatever transaction the caller opened.

    ef_search comes from `candidates` and not from a constant of its own. Left
    at its default of 40 while the inner query asks for 500, the index returns
    40 candidates and the other 460 silently do not exist — no error, just a
    worse answer. Deriving it here is what makes the two impossible to diverge.

    iterative_scan is why pgvector 0.8 is a hard requirement: without it the
    hidden_at and embedding-IS-NOT-NULL predicates are applied *after* the graph
    walk, so a filter that excludes anything returns fewer rows than asked for.
    relaxed_order is correct here because the outer stage re-sorts anyway.

    Interpolated rather than bound: SET takes no parameters. `candidates` is
    coerced to int at the call site for that reason.
    """
    await conn.execute(f"SET LOCAL hnsw.ef_search = {int(candidates)}")
    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")


async def search_articles(
    conn: asyncpg.Connection,
    vector: Sequence[float],
    *,
    candidates: int = CANDIDATES,
    limit: int = RESULTS,
) -> tuple[SearchRow, ...]:
    literal = vector_literal(vector)
    sql = build_search_query()
    # An explicit transaction, because SET LOCAL applies to one and asyncpg
    # otherwise wraps each execute in its own. Plain SET is not the fix: on a
    # pooled connection it would persist into whatever unrelated request
    # borrows that connection next.
    async with conn.transaction():
        await _apply_settings(conn, candidates)
        rows = await conn.fetch(sql, literal, candidates, limit)
    return tuple(
        SearchRow(
            id=r["id"], title=r["title"], author_name=r["author_name"],
            country=r["country"], published_at=r["published_at"], score=float(r["score"]),
        )
        for r in rows
    )


async def search_articles_with_settings_probe(
    conn: asyncpg.Connection, vector: Sequence[float], *, candidates: int, limit: int
) -> str:
    """Run a search and report what ef_search was actually set to.

    Exists only for the test that proves SET LOCAL took effect. Reading the
    setting back is the only way to tell a working SET LOCAL from a no-op one,
    because the no-op raises nothing.
    """
    literal = vector_literal(vector)
    sql = build_search_query()
    async with conn.transaction():
        await _apply_settings(conn, candidates)
        await conn.fetch(sql, literal, candidates, limit)
        return await conn.fetchval("SELECT current_setting('hnsw.ef_search')")
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/db/test_search_plans.py -v`
Expected: 7 passed. If `test_the_search_uses_the_hnsw_index` still shows a sequential scan, raise `rows` in `_populated` to 2000 before suspecting the query.

- [ ] **Step 5: Commit**

```bash
git add src/babel/db/search.py tests/db/test_search_plans.py
git commit -m "Add the two-stage semantic search query"
```

---

### Task 7: The search page

**Files:**
- Create: `src/babel/web/templates/search.html`, `tests/web/test_search_page.py`
- Modify: `src/babel/web/app.py`, `src/babel/web/routes.py`, `src/babel/web/templates/base.html`, `tests/web/conftest.py`

**Interfaces:**
- Consumes: `search.search_articles`, `search.SearchRow` (Task 6); `EmbedClient`, `EmbedError` (Task 4); `settings.search_max_query_chars`, `settings.search_timeout_sec`, `settings.embed_service_url`, `settings.embed_model` (Task 4).
- Produces: `GET /search?q=` returning HTML; `create_app(settings, pool=None, embedder=None)` — a third injectable, defaulted, so every existing `create_app` call site keeps working unchanged.

- [ ] **Step 1: Write the failing test**

`tests/web/test_search_page.py`:

```python
import datetime

import pytest

from babel.db import repo, search
from babel.embed.client import EmbedError
from babel.models import Article

UTC = datetime.UTC


def _article(article_id: int, title: str) -> Article:
    return Article(
        id=article_id, title=title, body="body", body_raw=None,
        author_id=1, author_name="a", country="Poland",
        published_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        e_day=6616, comment_count=0, comments=(), images=(),
    )


@pytest.fixture
async def seeded(pg):
    await repo.save_article(pg, _article(10, "About elections"))
    await repo.save_embeddings(
        pg, "test/model", [(10, repo.vector_literal([0.1] * repo.EMBED_DIM))]
    )
    await pg.execute(search.HNSW_INDEX_SQL)
    return pg


async def test_a_query_returns_matching_articles(client, seeded):
    resp = await client.get("/search", params={"q": "вибори"})
    assert resp.status_code == 200
    assert "About elections" in resp.text


async def test_an_empty_query_shows_the_form_and_calls_nothing(client, seeded, embedder):
    resp = await client.get("/search", params={"q": "   "})
    assert resp.status_code == 200
    assert embedder.calls == []


async def test_an_overlong_query_is_truncated_not_sent_whole(client, seeded, embedder):
    await client.get("/search", params={"q": "x" * 5000})
    assert len(embedder.calls[0][0]) == 512


async def test_the_service_being_down_degrades_honestly(client, seeded, embedder):
    embedder.error = EmbedError("down")
    resp = await client.get("/search", params={"q": "вибори"})
    assert resp.status_code == 200
    assert "unavailable" in resp.text.lower()
    # Not a 500, and not a fabricated empty result set — the reader is told the
    # search could not run, rather than being shown "no matches" for a query
    # that was never actually asked.
    assert "no matches" not in resp.text.lower()


async def test_a_timeout_degrades_the_same_way(client, seeded, embedder):
    embedder.error = TimeoutError()
    resp = await client.get("/search", params={"q": "вибори"})
    assert resp.status_code == 200
    assert "unavailable" in resp.text.lower()


async def test_search_is_disallowed_in_robots(client):
    body = (await client.get("/robots.txt")).text
    assert "Disallow: /search" in body
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/web/test_search_page.py -v`
Expected: FAIL — 404 on `/search`

- [ ] **Step 3: Let the web fixture inject an embedder**

In `tests/web/conftest.py`, add the stub and the fixture, and pass both new arguments through:

```python
from babel.db.repo import EMBED_DIM


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
```

In the `client` fixture, take `embedder`, add two settings, and inject:

```python
async def client(pool, image_root, embedder):
    settings = Settings(
        database_url="postgresql://babel@unused/babel",
        web_database_url="postgresql://babel_web@unused/babel",
        contact="archive@example.invalid",
        image_root=str(image_root),
        embed_service_url="http://embed.invalid",
        embed_model="test/model",
    )
    app = create_app(settings, pool=pool, embedder=embedder)
```

- [ ] **Step 4: Build the embedder in the app**

In `src/babel/web/app.py`, give `create_app` a third injectable and build the real client when it is
omitted. This is the seam the pool already uses, and the docstring there says why: an injected
object belongs to the caller, and leaving the production path untouched keeps it exactly as strict.

```python
def create_app(settings: Settings, pool: object | None = None, embedder: object | None = None) -> FastAPI:
```

Inside `lifespan`, before the pool handling:

```python
        # One client for the process — building one per request would open a
        # fresh aiohttp session on every search. Injected in tests, built here
        # in production, exactly like the pool below.
        if embedder is not None:
            app.state.embedder = embedder
        else:
            from babel.db.repo import EMBED_DIM  # noqa: PLC0415 — avoids a cycle
            from babel.embed.client import EmbedClient  # noqa: PLC0415

            app.state.embedder = EmbedClient(
                settings.embed_service_url,
                model=settings.embed_model,
                dim=EMBED_DIM,
                timeout_sec=settings.search_timeout_sec,
            )
```

Update `REQUIRED_MIGRATIONS` and `ROBOTS_TXT`:

```python
REQUIRED_MIGRATIONS = ("005_browse.sql", "007_body_markup.sql", "008_embeddings.sql")
```

```python
# /search joins the parameterised list space as a crawl trap: every query
# string is a distinct URL carrying no content of its own.
ROBOTS_TXT = "User-agent: *\nDisallow: /?\nDisallow: /search\nAllow: /\n"
```

- [ ] **Step 5: Add the route**

In `src/babel/web/routes.py`, inside `register_routes`:

```python
    @app.get("/search", response_class=HTMLResponse)
    async def search_page(request: Request, q: str = ""):
        settings = app.state.settings
        # Truncated, not rejected. A reader who pastes a paragraph gets an
        # answer for its opening rather than an error, and the cap still holds:
        # attention is quadratic in length and this string comes from anyone on
        # the internet. The service caps again on its own side, because it is
        # reachable from the LAN without going through here.
        query = q.strip()[: settings.search_max_query_chars]
        context = {"query": query, "rows": (), "unavailable": False, "settings": settings}

        if not query:
            return templates.TemplateResponse(
                request=request, name="search.html", context=context
            )

        try:
            vectors = await asyncio.wait_for(
                app.state.embedder.embed([query]), timeout=settings.search_timeout_sec
            )
        except (EmbedError, OSError, TimeoutError, asyncio.TimeoutError):
            # Deliberately not a keyword fallback: this codebase has no keyword
            # search, and offering one under that name would be a promise the
            # degradation path cannot keep. The reader is told the truth and
            # the browse filters still work.
            log.warning("embed service unavailable for search")
            context["unavailable"] = True
            return templates.TemplateResponse(
                request=request, name="search.html", context=context
            )

        async with request.app.state.pool.acquire() as conn:
            context["rows"] = await db_search.search_articles(conn, vectors[0])
        return templates.TemplateResponse(request=request, name="search.html", context=context)
```

Add to the imports at the top of `routes.py`:

```python
import asyncio

from babel.db import search as db_search
from babel.embed.client import EmbedError
```

- [ ] **Step 6: Write the template**

`src/babel/web/templates/search.html`:

```html
{% extends "base.html" %}
{% block title %}Search — babel{% endblock %}
{% block content %}

<form class="filters" method="get" action="/search">
  <label>Search
    <input type="search" name="q" value="{{ query }}" maxlength="512"
           placeholder="any language">
  </label>
  <button type="submit">Search</button>
</form>

<p class="coverage">
  Search finds articles by meaning, not by words: a query in one language
  matches articles written in another. Only articles already embedded are
  searchable.
</p>

{% if unavailable %}
<p class="notice">
  Semantic search is unavailable right now. Nothing is lost — the archive is
  still there. <a href="/">Browse by country, author or date</a> instead.
</p>
{% elif query and rows %}
<ol class="articles">
  {% for row in rows %}
  <li>
    <time>{{ game_time(row.published_at).strftime("%Y-%m-%d") }}</time>
    <a class="title" href="/article/{{ row.id }}">{{ row.title }}</a>
    {% if row.author_name %}
    <a class="author" href="/?author={{ row.author_name|urlencode }}">{{ row.author_name }}</a>
    {% endif %}
    {% if row.country %}<span class="country">{{ row.country }}</span>{% endif %}
  </li>
  {% endfor %}
</ol>
{% elif query %}
<p class="notice">No matches.</p>
{% endif %}

{% endblock %}
```

Check `base.html` for how `game_time` is exposed to templates — the list page uses it, so the same mechanism applies here. If it is passed per-context rather than as a global, add `"game_time": game_time` to `context`.

- [ ] **Step 7: Add the search box to the header**

In `src/babel/web/templates/base.html`, inside the site header:

```html
<form class="header-search" method="get" action="/search">
  <input type="search" name="q" maxlength="512" placeholder="search any language">
</form>
```

- [ ] **Step 8: Run the tests and watch them pass**

Run: `uv run pytest tests/web/ -v`
Expected: all pass, including the 190 existing ones.

- [ ] **Step 9: Commit**

```bash
uv run pytest && uv run ruff check src tests jetson
git add src/babel/web/ tests/web/
git commit -m "Serve semantic search, and say so when it cannot"
```

---

### Task 8: Acceptance and the runbook

**Files:**
- Create: `tests/eval/__init__.py`, `tests/eval/test_cross_language.py`
- Modify: `pyproject.toml`, `README.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-07-30-embeddings-design.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a recall@20 figure against a hand-built cross-language set, and the operator documentation.

- [ ] **Step 1: Register the marker**

In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src", "jetson"]
markers = [
    "live: needs the real embed service on the LAN. Not run by default.",
]
addopts = "-m 'not live'"
```

- [ ] **Step 2: Write the acceptance test**

`tests/eval/test_cross_language.py`:

```python
"""Does this actually do the thing it was built for?

Every other test in this suite uses a fake encoder, because the real one needs
CUDA and 2.3 GB of weights. That means nothing else in the suite can fail when
cross-language retrieval stops working — the property the whole project exists
for is, by construction, untested everywhere else.

Opt in with `uv run pytest -m live`, against a populated database and a running
embed service. Not part of the default run and not part of CI.
"""

import os

import asyncpg
import pytest

from babel.config import Settings
from babel.db import repo, search
from babel.embed.client import EmbedClient

pytestmark = pytest.mark.live

# Queries in one language, and an article that should come back for each. Fill
# `expect` in by finding real archived articles first:
#   SELECT id, title, country FROM articles WHERE country = 'Serbia' LIMIT 20;
# Written by hand on purpose. A set generated from the embeddings themselves
# would only prove the model agrees with itself.
CASES = [
    # (query, language of the query, expected article id)
    ("вибори президента", "uk", None),
    ("war declaration against Hungary", "en", None),
    ("economía y salarios", "es", None),
    ("bitwa o Warszawę", "pl", None),
]


@pytest.fixture
async def live_conn():
    dsn = os.environ.get("EVAL_DATABASE_URL")
    if not dsn:
        pytest.skip("EVAL_DATABASE_URL is not set")
    conn = await asyncpg.connect(dsn)
    yield conn
    await conn.close()


async def test_recall_at_20_across_languages(live_conn):
    filled = [c for c in CASES if c[2] is not None]
    if not filled:
        pytest.fail("CASES has no expected article ids — fill them in from the archive first")

    settings = Settings()
    client = EmbedClient(
        settings.embed_service_url, model=settings.embed_model,
        dim=repo.EMBED_DIM, timeout_sec=30.0,
    )

    hits = 0
    for query, lang, expected in filled:
        vector = (await client.embed([query]))[0]
        rows = await search.search_articles(live_conn, vector, limit=20)
        found = expected in [r.id for r in rows]
        print(f"{lang:>3} {query[:40]:<42} {'hit' if found else 'MISS'}")
        hits += found

    recall = hits / len(filled)
    # 0.8 rather than 1.0: this is a retrieval system, and a hand-built set of
    # this size cannot distinguish a real regression from one awkward query.
    # The number that matters is the one printed above when it drops.
    assert recall >= 0.8, f"recall@20 was {recall:.2f} across {len(filled)} queries"
```

- [ ] **Step 3: Run it and confirm it skips**

Run: `uv run pytest`
Expected: the eval test does not appear (deselected by `-m 'not live'`). Everything else passes.

- [ ] **Step 4: Write the README section**

Add to `README.md`, after the image-worker section:

````markdown
## Semantic search

Two processes on two machines. `babel embed` on the x86 box drains the queue of
unembedded articles through the Jetson's `POST /embed`; `babel serve` calls the
same endpoint for each reader's query. They must use the same model — vectors
from two models are not comparable, and nothing reports it.

### One-time setup on the Jetson

```bash
ssh jetson@<jetson-address>
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-jetpack:r36.4.0 nvidia-smi   # must print a table
```

The daemon restart bounces every container on that host. Check their restart
policies first.

Then download the model once (it is bind-mounted read-only afterwards, so a
restart needs no internet), clone this branch, and bring the service up:

```bash
cd ~/babel-embed/src/jetson && docker compose up -d --build
curl http://<jetson-address>:8081/healthz     # {"model":"BAAI/bge-m3","dim":1024,"cuda":true}
```

### Deploying the database change

`db` moves from `postgres:17` to `pgvector/pgvector:0.8.6-pg17-trixie`. Same
upstream image plus the extension, same PGDATA — an image swap, not a dump and
restore. **The `-trixie` tag is not optional.** Prod runs `17.10-1.pgdg13+1`
and both images report `Debian GLIBC 2.41-12+deb13u3`; a bookworm image would
change text collation under every index already built on this data.

Build before migrating, as always — migrations are baked into the image:

```bash
docker compose stop crawler images embed
docker compose build crawler images embed web
docker compose up -d db
docker compose run --rm crawler babel migrate
docker compose up -d crawler images embed web
```

### Building the similarity index

Not in the migration: `CREATE INDEX CONCURRENTLY` is illegal inside a
transaction and this project's runner wraps every file in one. Do it through
`psql` once there are vectors to index.

```sql
SET maintenance_work_mem = '4GB';
CREATE INDEX CONCURRENTLY article_embeddings_bin_idx ON article_embeddings
    USING hnsw ((binary_quantize(embedding)::bit(1024)) bit_hamming_ops)
    WHERE embedding IS NOT NULL;
```

### Watching the drain

```sql
SELECT count(*) FILTER (WHERE embedding IS NULL)     AS pending,
       count(*) FILTER (WHERE embedding IS NOT NULL) AS done,
       count(DISTINCT model)                         AS models
FROM article_embeddings;
```

`models` must be 1. More than one means the corpus is half-encoded by something
else, and search quality is already degraded.

### If the queue is missing rows

Migration 008 seeds the articles that existed when it ran, and `save_article`
queues the ones ingested afterwards. An article written *between* the two — by
a crawler still running the old image — gets neither. The deploy order above
prevents it; this closes it when it happens anyway:

```sql
INSERT INTO article_embeddings (article_id) SELECT id FROM articles
ON CONFLICT DO NOTHING;
```

### Changing the model

The dimension is the only thing the schema commits to, so a different
1024-dimension encoder costs a re-run and no migration:

```sql
UPDATE article_embeddings SET embedding = NULL, model = NULL WHERE model <> 'new/model';
```

Then point `EMBED_MODEL` and the Jetson's `MODEL_ID`/`MODEL_DIR` at it and
restart both. Search degrades while the queue drains — rows with a NULL vector
are not searchable.
````

- [ ] **Step 5: Update CLAUDE.md**

Under "Status", replace the stale count and add phase 3:

- The archive holds **162,618 articles and 2,985,540 comments** as of 2026-07-30, not the 10,093 recorded at the last handoff.
- `lang` is NULL in every row; nothing populates it. It does not affect embeddings but no language filter can be built on it.
- Phase 3 is on `feat/phase-3-embeddings`. Add `babel embed` and `babel serve`'s `/search` to the Commands section, and the `embed` compose service to "Operating the live run" — noting it is deliberately outside gluetun's namespace.

- [ ] **Step 6: Close the spec's open items**

In `docs/superpowers/specs/2026-07-30-embeddings-design.md`, replace "Still unmeasured" items 3 and 4 with the measured index size, build time and recall figure. Leave anything not actually measured in the list rather than declaring it done.

- [ ] **Step 7: Run everything and commit**

```bash
uv run pytest && uv run ruff check src tests jetson
git add tests/eval/ pyproject.toml README.md CLAUDE.md docs/
git commit -m "Document and accept cross-language search"
```

---

## Self-Review

**Spec coverage.** Every section of the design spec maps to a task: architecture → 2/5/7; schema and queue → 3; retrieval query and its three settings → 6; embed service and its caps → 2; corpus worker → 5; search page and degradation → 7; the seven named failure modes → 1 (model id in `/healthz`), 4 (model, dimension, norm), 6 (`ef_search`, `iterative_scan`, `SET LOCAL`), 7 (query cap), 3 and 8 (collation, pinned image); testing → every task plus 8; deployment → 8; the five unmeasured items → 1 and 8.

**Two things this plan changed in the spec, both recorded there:** the queue became a NULL-vector row rather than an absent one, on the steady-state cost of the anti-join; and the HNSW index became partial, because the same table now holds the queue.

**Known soft spot.** `tests/eval/test_cross_language.py` ships with `expect` unfilled — it fails loudly rather than passing vacuously, but Task 8 is not done until real article ids are in it. That is deliberate: the ids can only come from the live archive, and inventing them here would produce a test that looks complete and proves nothing.

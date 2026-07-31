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

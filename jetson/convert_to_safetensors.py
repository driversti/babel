"""One-time conversion of BAAI/bge-m3's pytorch_model.bin to model.safetensors.

Why this exists: transformers>=4.57 refuses to `torch.load` a pickle
checkpoint unless torch>=2.6 (CVE-2025-32434 — arbitrary code execution via a
crafted pickle payload). This project's Jetson image pins torch to whatever
`dustynv/l4t-pytorch:r36.4.0` ships, 2.4.0, and `BAAI/bge-m3`'s upstream
repository has no `*.safetensors` variant — only `pytorch_model.bin` — so
`AutoModel.from_pretrained` fails outright with:

    ValueError: Due to a serious vulnerability issue in `torch.load`, even
    with `weights_only=True`, we now require users to upgrade torch to at
    least v2.6 in order to use the function. ...
    See https://nvd.nist.gov/vuln/detail/CVE-2025-32434

This script is not a bypass of that check. It calls `torch.load` directly,
once, against a file already fetched over HTTPS from the official BAAI/bge-m3
repository by `snapshot_download` (see the plan's Step 8) — the same trust
boundary the check exists to protect, already crossed by the download itself.
The output, `model.safetensors`, is a data-only format with no
deserialization risk, and it is the *only* weights file the running service
or the benchmark ever loads afterwards: nothing in this project's own code
path calls `torch.load` on an untrusted or attacker-controlled file, so
nothing about the service's security posture is weakened by running this
once, offline, against a file already known to be BAAI's own weights.

Run once, after `snapshot_download` has populated the model directory, using
the already-built `babel-embed` image (it carries `safetensors` as a
transitive dependency of `transformers`, so nothing extra needs installing):

    docker run --rm -v ~/babel-embed/models:/models \
        -v $(pwd)/jetson/convert_to_safetensors.py:/app/convert.py \
        babel-embed python3 /app/convert.py

Then move `pytorch_model.bin` out of the model directory so only
`model.safetensors` is discoverable. transformers prefers safetensors when
both are present, but removing the pickle file removes any ambiguity about
which one gets loaded, and it will be root-owned from the download step:

    sudo mv ~/babel-embed/models/bge-m3/pytorch_model.bin \
        ~/babel-embed/models/pytorch_model.bin.bak
"""

import argparse
import os

import torch
from safetensors.torch import save_file


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.environ.get("MODEL_DIR", "/models/bge-m3"))
    args = ap.parse_args()

    src = os.path.join(args.model_dir, "pytorch_model.bin")
    dst = os.path.join(args.model_dir, "model.safetensors")

    state_dict = torch.load(src, map_location="cpu", weights_only=True)
    # clone() drops any shared storage between tensors (e.g. tied weights) —
    # safetensors requires each tensor to own its storage.
    state_dict = {k: v.clone().contiguous() for k, v in state_dict.items()}
    save_file(state_dict, dst, metadata={"format": "pt"})
    print(f"converted {len(state_dict)} tensors -> {dst}")


if __name__ == "__main__":
    main()

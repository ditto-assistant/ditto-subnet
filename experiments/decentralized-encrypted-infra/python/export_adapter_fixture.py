# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["numpy>=2"]
# ///
"""Export a synthetic query-adapter fixture for the CKKS benchmark.

EXPERIMENT ONLY. Every weight and query comes from the seeded synthetic
generators in Omar's query-adapter pilot (research/query-adapter-pilot,
``reference.synthetic_adapter`` / ``reference.synthetic_query``). No real user
data, embeddings or trained adapters are involved.

The fixture also carries ``reference.apply`` outputs, so the Go benchmark
checks its decrypted vectors against the pilot's pinned reference math and its
parity bar (cos >= 1 - 1e-6, max_abs <= 1e-5).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "research" / "query-adapter-pilot" / "src"))

from query_adapter_pilot import contract, reference  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--adapter-seed", type=int, default=4730)
    parser.add_argument("--queries", type=int, default=32)
    parser.add_argument("--query-seed", type=int, default=47300)
    args = parser.parse_args()

    weights = reference.synthetic_adapter(args.adapter_seed)
    queries = [
        reference.synthetic_query(args.query_seed + i) for i in range(args.queries)
    ]
    outputs = [reference.apply(weights, q) for q in queries]

    # Exact bound on every hidden pre-activation for a unit-norm query:
    # |x @ W1[:, i] + b1[i]| <= ||W1[:, i]||_2 + |b1[i]| (Cauchy-Schwarz).
    bound = float(np.max(np.linalg.norm(weights.W1, axis=0) + np.abs(weights.b1)))

    fixture = {
        "kind": "sn473-ckks-adapter-fixture",
        "synthetic": True,
        "source": "research/query-adapter-pilot reference.synthetic_adapter/query",
        "contract": contract.CONTRACT_VERSION,
        "dim": contract.DIM,
        "hidden": contract.HIDDEN,
        "alpha": weights.alpha,
        "adapter_seed": args.adapter_seed,
        "gelu_input_bound": bound,
        "parity_min_cosine": contract.PARITY_MIN_COSINE,
        "parity_max_abs": contract.PARITY_MAX_ABS,
        "W1": contract.encode_f32(weights.W1),
        "b1": contract.encode_f32(weights.b1),
        "W2": contract.encode_f32(weights.W2),
        "b2": contract.encode_f32(weights.b2),
        "queries": [contract.encode_f32(q) for q in queries],
        "reference": [contract.encode_f32(o) for o in outputs],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture) + "\n")
    print(f"wrote {args.out} ({len(queries)} queries, gelu bound {bound:.4f})")


if __name__ == "__main__":
    main()

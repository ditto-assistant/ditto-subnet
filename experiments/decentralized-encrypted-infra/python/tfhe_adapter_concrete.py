# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = ["concrete-ml==1.9.0"]
# ///
"""TFHE (Zama Concrete ML) feasibility for the query-adapter MLP.

EXPERIMENT ONLY. Loads the synthetic fixture written by
export_adapter_fixture.py (seeded weights and queries from the query-adapter
pilot), compiles the adapter as a quantized TFHE circuit, and reports latency,
key sizes, and parity against the pilot's reference outputs. Prints JSON.
"""

from __future__ import annotations

import argparse
import base64
import json
import platform
import time

import numpy as np
import torch
from concrete.ml.torch.compile import compile_torch_model


def f32(value: str, shape: tuple[int, ...]) -> np.ndarray:
    return np.frombuffer(base64.b64decode(value), dtype="<f4").reshape(shape).copy()


class Adapter(torch.nn.Module):
    """q + alpha * (gelu(q @ W1 + b1) @ W2 + b2); Ditto normalizes afterwards."""

    def __init__(self, fx: dict) -> None:
        super().__init__()
        dim, hidden, alpha = fx["dim"], fx["hidden"], fx["alpha"]
        self.fc1 = torch.nn.Linear(dim, hidden)
        self.fc2 = torch.nn.Linear(hidden, dim)
        self.act = torch.nn.GELU(approximate="tanh")
        with torch.no_grad():
            self.fc1.weight.copy_(torch.from_numpy(f32(fx["W1"], (dim, hidden)).T))
            self.fc1.bias.copy_(torch.from_numpy(f32(fx["b1"], (hidden,))))
            w2 = f32(fx["W2"], (hidden, dim)).T * alpha
            self.fc2.weight.copy_(torch.from_numpy(w2))
            self.fc2.bias.copy_(torch.from_numpy(f32(fx["b2"], (dim,)) * alpha))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.fc2(self.act(self.fc1(x)))


def parity(got: np.ndarray, want: np.ndarray) -> tuple[float, float]:
    got = got / (np.linalg.norm(got, axis=1, keepdims=True) + 1e-9)
    cos = np.sum(got * want, axis=1) / np.linalg.norm(want, axis=1)
    return float(cos.min()), float(np.abs(got - want).max())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--n-bits", type=int, default=6)
    parser.add_argument("--rounding-bits", type=int, default=6)
    parser.add_argument(
        "--execute", type=int, default=2, help="queries run under real FHE"
    )
    args = parser.parse_args()
    if args.execute < 0:
        parser.error("--execute must be >= 0")

    with open(args.fixture) as fh:
        fx = json.load(fh)
    if not fx.get("synthetic"):
        raise SystemExit("refusing a fixture not marked synthetic")
    dim = fx["dim"]
    queries = np.stack([f32(q, (dim,)) for q in fx["queries"]])
    refs = np.stack([f32(r, (dim,)) for r in fx["reference"]])
    # Calibration uses fresh synthetic unit vectors, never the evaluated queries.
    rng = np.random.default_rng(473)
    calib = rng.standard_normal((256, dim)).astype(np.float32)
    calib /= np.linalg.norm(calib, axis=1, keepdims=True)

    model = Adapter(fx).eval()
    with torch.no_grad():
        float_out = model(torch.from_numpy(queries)).numpy()
    out: dict = {
        "experiment": "sn473-tfhe-adapter",
        "synthetic": True,
        "host": {"machine": platform.machine(), "python": platform.python_version()},
        "library": "concrete-ml 1.9.0",
        "n_bits": args.n_bits,
        "rounding_threshold_bits": args.rounding_bits,
        "float_torch_vs_pilot": parity(float_out, refs),
    }

    t = time.perf_counter()
    qmod = compile_torch_model(
        model,
        calib,
        n_bits=args.n_bits,
        rounding_threshold_bits={"n_bits": args.rounding_bits, "method": "approximate"},
    )
    out["compile_s"] = time.perf_counter() - t
    circuit = qmod.fhe_circuit
    stats = {}
    for name in (
        "size_of_secret_keys",
        "size_of_bootstrap_keys",
        "size_of_keyswitch_keys",
        "size_of_inputs",
        "size_of_outputs",
        "programmable_bootstrap_count",
        "max_integer_bit_width",
        "complexity",
        "p_error",
        "global_p_error",
    ):
        try:
            value = getattr(circuit, name)
            stats[name] = value if isinstance(value, (int, float)) else str(value)
        except Exception as exc:  # attribute names differ across releases
            stats[name] = f"unavailable: {type(exc).__name__}"
    out["circuit"] = stats

    sim = qmod.forward(queries, fhe="simulate")
    out["simulated_quantized_vs_pilot"] = parity(sim, refs)

    t = time.perf_counter()
    circuit.keygen()
    out["keygen_s"] = time.perf_counter() - t

    timings = []
    executed = []
    for q in queries[: args.execute]:
        t = time.perf_counter()
        executed.append(qmod.forward(q[None, :], fhe="execute")[0])
        timings.append(time.perf_counter() - t)
    out["execute_s_per_query"] = timings
    if executed:
        out["executed_vs_pilot"] = parity(np.stack(executed), refs[: len(executed)])
    out["pilot_bar"] = {
        "min_cos": fx["parity_min_cosine"],
        "max_abs": fx["parity_max_abs"],
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

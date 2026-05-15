# DittoBench anti-gaming controls

Bittensor's competitive structure makes overfitting and benchmark-leaking
the default failure mode of any open benchmark. DittoBench mitigates this
with five layered controls: four shared between the Go and Python
implementations (hidden split, canary paraphrases, memorisation discount,
distractor injection) plus one specific to the harness-in-Docker model
(image-digest pinning).

The canonical Go implementation lives in
[`../../../go/bittensor/antigaming.go`](../../../go/bittensor/antigaming.go).
The Python port lives in
[`../runner/antigaming.py`](../runner/antigaming.py) and is held in lockstep
via the parity tests in [`../../tests/bench/test_antigaming.py`](../../tests/bench/test_antigaming.py).

| Control                         | Python helper                      | Purpose                                     |
|---------------------------------|------------------------------------|---------------------------------------------|
| Hidden split                    | `partition_fixture`                | Hold out a deterministic private subset     |
| Canary paraphrases              | `paraphrase_seed`, `ensure_paraphrase_changed` | Detect verbatim-prompt memorisation |
| Memorisation discount           | `memorisation_discount`            | Penalise high public / low canary miners    |
| Distractor injection            | `distractor_bundle_for`            | Force retrievers to rank against noise      |
| Docker image-digest pinning     | validator commitment artefact      | Bind a hotkey to one frozen submission      |

All four shared controls hash with SHA-256 over `(secret, …)` and rank by
lexicographic hex digest, so the Go and Python implementations produce
byte-identical splits, seeds, and distractor pools for the same inputs.

## Hidden split

```python
from ditto.bench.runner.antigaming import partition_fixture

split = partition_fixture(case_ids, secret, private_frac=0.25, canary_frac=0.15)
# split.private and split.canary are validator-only; split.public ships in the repo.
```

The split is deterministic in `(secret, case_ids)`: two validators with
the same secret agree on the partition, and rotating the secret rotates
the partition entirely. The function also enforces a minimum public share
so honest new miners always have at least one case to train against.

`private` cases are stored encrypted inside the validator-only manifest
(`private_validator_manifest.json`). They MUST NOT be committed to the
public repo and MUST be re-encrypted with a fresh secret each tempo.

## Canary paraphrases

For every public case the validator generates a deterministic paraphrase
seed:

```python
seed = paraphrase_seed(secret, case_id)
```

The seed feeds an LLM paraphrase pass (validators run their own
paraphraser; the prompt template is published, but the seed is private).
Before the paraphrase ships as a canary the validator calls
`ensure_paraphrase_changed` so no canary degenerates into its public twin
under whitespace/punctuation normalisation.

Canary scoring runs the same scoring helpers (`score_core`,
`score_retrieval`) against the same ground truth as the underlying
public case. The only difference is the `visibility` field is stamped
`canary` post-scoring.

## Memorisation discount

After each tempo, validators compute per-miner public and canary means
and apply a multiplicative discount to the aggregate weight:

```python
discount = memorisation_discount(
    public_mean,
    canary_mean,
    canary_samples,
    gap_threshold=0.10,
    gap_ceiling=0.40,
    max_discount=0.50,
)
weight *= discount
```

A miner whose public mean is 0.95 and canary mean is 0.55 takes the full
50% discount; one whose canary mean is within 0.10 of public takes none.
`canary_samples == 0` disables the discount so validators that have not
run enough canary challenges yet do not penalise miners by accident.

Because the subnet uses a **winner-takes-all weight policy** per
mechanism (see [`scoring.md`](scoring.md)), the discount is applied to the
**mean_score** before tie-breaking, not to the post-normalisation weight.
A miner that is the top scorer on public but a chronic memoriser drops
below the next honest miner after the discount.

## Distractor injection

For retrieval challenges, validators may pad the candidate pool with
deterministically chosen distractor pair IDs:

```python
distractors = distractor_bundle_for(
    case.id,
    case.expected_pair_ids,
    case.forbidden_pair_ids,
    all_fixture_pair_ids,
    secret,
    n=20,
)
```

This forces miners to actually rank against noise rather than relying on
small candidate pools. Distractors never overlap with `expected_pair_ids`
or `forbidden_pair_ids` of the case under test. The selection is
deterministic in `(secret, case.id)` so two validators using the same
secret reach identical distractor pools and produce comparable scores.

## Docker image-digest pinning

Because miners submit a binary artefact (a Docker image) rather than a
live service, the validator MUST pin **one** image digest per miner
hotkey per submission window. Specifically:

1. Miners commit `(image_repository, image_digest, build_metadata)` to
   the subnet via a chain commitment extrinsic.
2. Validators refuse to score any image whose digest differs from the
   commitment. Re-tagging the same digest is fine; rebuilding under the
   same tag is not.
3. Any rebuild — even one with no source changes — invalidates the prior
   commitment and **resets** the miner's canary baseline. The validator
   re-runs the public set to re-establish the public_mean before applying
   the memorisation discount.

This control directly defeats the most attractive attack vector for the
harness model: editing the binary between the public and canary scoring
passes. With digest pinning, the binary running against the canary subset
is byte-identical to the one that scored on public, and any deviation
between the two scores reflects only the prompt-paraphrase difference.

## Operational schedule

| Cadence       | Action                                                                                |
|---------------|---------------------------------------------------------------------------------------|
| Per tempo     | Rotate the canary subset secret; regenerate paraphrases                                |
| Per epoch     | Rotate the private subset secret; re-encrypt the validator manifest                    |
| Per quarter   | Audit memorisation-discount distribution across the metagraph; tune `gap_threshold`    |
| Continuous    | Publish per-miner `(public_mean, canary_mean, discount)` tuples for cross-validator audit |
| Per submission| Re-run public set on every new image digest before scoring private/canary cases       |

## What this does NOT defend against

- A miner running the actual Ditto stack with a custom user fixture would
  score very high on retrieval, which is the point — DittoBench is meant
  to reward the Ditto memory stack. The benchmark is a **performance
  contest**, not a knowledge-cutoff test.
- A miner with privileged access to a validator's secret can predict the
  partition. Validators are expected to keep secrets in HSM-backed
  storage and rotate on tempo boundaries. The `secret` argument to every
  helper is a single point of trust by design — keep it secret.
- A miner that runs the validator's allow-listed LLM endpoint against a
  precomputed cache outside the container will not see the cache hits
  inside the sandbox (the container is `--network=none` except for the
  single endpoint), but a miner that caches **inside** the image is fine
  — the budgets enforce real-world cost.

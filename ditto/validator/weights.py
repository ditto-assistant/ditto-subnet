"""KOTH + ATH-gate weight function: map the score ledger to a weight vector.

The incentive mechanism (``docs/incentive-mechanism.md`` Option A): the reigning
all-time-high holder is the **champion** and takes ~all emissions; a challenger
only dethrones it by beating its score by a **relative margin** (default 1%);
ties and sub-margin gains keep the incumbent, so **first-to-submit wins** and a
downloaded copy — which at best ties — never earns. A small **participation
tail** (default 10% over the next few miners) keeps the subnet populated without
reopening the copy hole.

This is a *deterministic* fold over the platform's best-score-per-miner ledger
(``GET /scoring/scores``): every validator runs this identical function on the
identical pool, so Yuma consensus converges and clips any deviator. It must stay
pure — no I/O, no clock, no rounding (compare the raw reported doubles) — or two
validators could disagree. It lives here so the mechanism is a one-function
change, mirroring the platform's ledger read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto.api_models.validator import LedgerEntry


def compute_weights(
    entries: Sequence[LedgerEntry],
    *,
    margin: float,
    tail_size: int,
    champion_share: float,
) -> dict[str, float]:
    """Return ``{miner_hotkey: weight}`` for the KOTH+ATH mechanism.

    ``entries`` is the ledger: one best-scoring agent per miner. The champion is
    found by folding entries in **first-seen order** (``first_seen`` then
    ``agent_id`` to break timestamp ties) and dethroning the running champion
    only when a later entry's composite exceeds it by ``margin`` relative. The
    champion gets ``champion_share``; the next ``tail_size`` miners by composite
    split ``1 - champion_share`` equally.

    Non-positive composites are dropped (a zero-scoring miner earns nothing).
    Returns an empty dict when no miner scored above zero — the caller then skips
    ``put_weights`` rather than zeroing the chain. Pylon/Subtensor normalizes the
    returned vector, so only the ratios matter; when there is no tail the champion
    is the whole vector.
    """
    scored = [e for e in entries if e.composite > 0.0]
    if not scored:
        return {}

    # Champion: fold in creation order; a later entry must beat the running
    # champion by the relative margin to take the crown. Order-independent of
    # when each agent happened to be scored — only creation order matters.
    ordered = sorted(scored, key=lambda e: (e.first_seen, e.agent_id))
    champion = ordered[0]
    for e in ordered[1:]:
        if e.composite > champion.composite * (1.0 + margin):
            champion = e

    weights: dict[str, float] = {champion.miner_hotkey: champion_share}

    # Tail: the next distinct miners by composite (highest first), excluding the
    # champion, split the remaining share equally.
    tail_pool = 1.0 - champion_share
    if tail_size > 0 and tail_pool > 0.0:
        runners_up = sorted(
            (e for e in scored if e.miner_hotkey != champion.miner_hotkey),
            key=lambda e: (-e.composite, e.first_seen, e.agent_id),
        )[:tail_size]
        if runners_up:
            per_miner = tail_pool / len(runners_up)
            for e in runners_up:
                weights[e.miner_hotkey] = per_miner

    return weights

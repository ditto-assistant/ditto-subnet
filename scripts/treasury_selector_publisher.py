#!/usr/bin/env python3
"""Default-off same-host readonly export; no key, grant or signing operation."""

import argparse
import hashlib
import json
from contextlib import suppress
from pathlib import Path

from ditto.treasury.collector_chain import PublicCollectorChain, load_policy
from ditto.treasury.selector_handoff import (
    SelectorChainUnavailable,
    SelectorHandoff,
    SelectorPublisher,
    run_publisher,
)
from ditto_screening_protocol.collector_receipts import chain_uint


def chain_read(function):
    # Pinned sync SDK maps DNS/socket failures to ConnectionError, and exhausted
    # recv retries to MaxRetriesExceeded. Do not classify general RPC/metadata,
    # InvalidHandshake/auth, runtime drift or filesystem failures as transport.
    from async_substrate_interface.errors import MaxRetriesExceeded
    from websockets.exceptions import ConnectionClosed

    try:
        return function()
    except (ConnectionError, TimeoutError, ConnectionClosed, MaxRetriesExceeded) as exc:
        closed = exc if isinstance(exc, ConnectionClosed) else exc.__context__
        if isinstance(closed, ConnectionClosed) and any(
            frame is not None and frame.code in {1002, 1003, 1007, 1008, 1009, 1010}
            for frame in (closed.rcvd, closed.sent)
        ):
            # SDK retry exhaustion may retain the terminal close as context.
            # Protocol/auth/policy refusal is not a temporary outage.
            raise
        raise SelectorChainUnavailable("public chain transport unavailable") from exc


class PublicEpochReader:
    def __init__(self, policy):
        import bittensor as bt

        self.policy = policy
        self.subtensor = chain_read(lambda: bt.Subtensor(network="finney"))
        self.chain = PublicCollectorChain(self.subtensor.substrate, role="transfer")

    def epoch_at(self, block):
        at = chain_read(lambda: self.subtensor.substrate.get_block_hash(block))
        chain_read(lambda: self.chain.guard_runtime(self.policy, at))
        return chain_uint(
            chain_read(
                lambda: self.chain.query(
                    "SubtensorModule",
                    "SubnetEpochIndex",
                    [118],
                    at,
                )
            )
        )

    def close(self):
        # Close only; pending selector/cursor state is retained.
        with suppress(SelectorChainUnavailable):
            chain_read(self.subtensor.close)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--policy-sha256")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--initialize-state", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    raw = args.config.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.config_sha256:
        parser.error("selector config differs from immutable deployment pin")
    body = json.loads(raw)
    if not isinstance(body, dict):
        parser.error("configuration must be an object")
    enabled = body.get("enabled", False)
    if type(enabled) is not bool:
        parser.error("selector enabled must be boolean")
    if not enabled:
        print(json.dumps({"status": "disabled", "authority": "none"}))
        return
    if not 15 <= args.poll_seconds <= 600 or not all(
        (args.state, args.policy, args.policy_sha256, args.snapshot)
    ):
        parser.error("explicit policy, snapshot, state and bounded polling required")
    if args.initialize_state and not args.once:
        parser.error("one-time initialization requires --once")
    try:
        config = SelectorHandoff.from_mapping(body.get("selector_handoff"))
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    policy = load_policy(args.policy, args.policy_sha256)
    if policy.digest != config.collector_policy_digest:
        raise ValueError("selector exporter signed collector policy differs")
    # Import/connect only after explicit enablement. Public chain observations
    # select coordinates; Platform, not this exporter, proves finality/effects.
    publisher = SelectorPublisher(args.state, config, initialize=args.initialize_state)
    try:
        run_publisher(
            publisher,
            args.snapshot,
            lambda: PublicEpochReader(policy),
            once=args.once,
            poll_seconds=args.poll_seconds,
            emit=lambda result: print(json.dumps(result), flush=True),
        )
    finally:
        publisher.close()


if __name__ == "__main__":
    main()

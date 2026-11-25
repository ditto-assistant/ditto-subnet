"""Chain gamma: accept the Bittensor runtime's gamma credit (SN-501).

Inert scaffolding for the Gamma Integration Standard V1.2. A user hotkey spends
SN118 gamma at a published service hotkey with fee-free ``spend_gamma``; the
chain burns it and grows ``GammaSpent(netuid, service, hotkey)``. A service
credits that growth to a hotkey-keyed USD account, charges btauth/1-signed
requests against it, and answers HTTP 402 ``insufficient_balance`` at zero.

This package is **chain gamma** only. It is unrelated to the SN118 treasury
Gamma bucket (``KIND_GAMMA``, ``gm_credits``, ``ditto/treasury``), which it never
imports or changes.

Nothing here performs I/O, holds a key, or is imported by the API server or a
worker. Live use waits on ``DITTO_CHAIN_GAMMA_ENABLED`` (default off) and on the
decisions in ``docs/design/chain-gamma-integration.md``.
"""

"""Ditto Edge (edge/v1): public-bytes Mac nodes behind a Ditto-run verifier.

Nodes hold content-addressed copies of public Ditto objects that Platform
assigns to them and answer Platform's challenges. Everything a node is scored
on is checked against bytes Platform holds; nothing it says about itself is a
scoring input. The lane is shadow-only: see
``docs/design/sn481-miner-hostable-workloads.md``.
"""

from ditto_edge_protocol.limits import PROTOCOL

__version__ = "0.1.0"

__all__ = ["PROTOCOL", "__version__"]

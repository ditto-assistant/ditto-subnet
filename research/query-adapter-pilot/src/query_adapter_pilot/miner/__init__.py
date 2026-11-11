"""Loopback-only reference miner for the synthetic ``query-adapter/v1`` pilot.

Hosts many per-tenant adapter versions on shared compute and serves
``POST /v1/adapt`` with ``reference.apply``. See ``server.py`` for routes and
``state.py`` for the tenant/version store and bounded cache.
"""

from .server import MinerConfig, RunningMiner, start_miner
from .state import FAULT_MODES

__all__ = ["FAULT_MODES", "MinerConfig", "RunningMiner", "start_miner"]

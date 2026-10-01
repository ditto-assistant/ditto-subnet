"""Canonical service weights and distributions shared with receipt ingestion.

These arithmetic functions neither attest finalized earnings nor authorize a
transfer. The collector journal remains the sole durable signer state.
"""

from ditto_screening_protocol.treasury_weight_math import (
    ServiceDestination as ServiceDestination,
)
from ditto_screening_protocol.treasury_weight_math import (
    ServiceDistribution as ServiceDistribution,
)
from ditto_screening_protocol.treasury_weight_math import (
    plan_service_distribution as plan_service_distribution,
)
from ditto_screening_protocol.treasury_weight_math import (
    service_first_weights as service_first_weights,
)

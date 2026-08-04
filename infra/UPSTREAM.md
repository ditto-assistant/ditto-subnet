# Import provenance

Subnet infrastructure was ported from `ditto-assistant/infra` main at
`2aa9aa37593d0d764acad9ff70b0f76f242e969f` together with the scoped capacity
stack ending at `6cc4e6c01d3980b4c88993551d779d32db988c37` (infra PR #71).

The existing `gcp-platform` GCS state prefix is intentionally retained. Product
infrastructure and the private Backroom remain in the private infra repository.

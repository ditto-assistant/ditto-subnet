# Import provenance

The subnet surface was extracted from
[`ditto-assistant/backroom`](https://github.com/ditto-assistant/backroom) at
commit `5cc18db6e08a6584758148cc7d4207aa18685d2c`.

Product routes, server functions, MCP tools, and ops-log integrations were
deliberately excluded. The private repository remains authoritative for
`backroom.heyditto.ai`; subnet operations move here after deployment cutover.

The MCP exclusion was reversed: SN118 is operated from
`backroom.dittobench.ai`, so its MCP transport, OAuth provider, consent flow,
and 66 subnet tools were ported from the same upstream. Only the Ditto app
tools — feature flags and app reviews — stayed behind, because they call the
private product API this deployment holds no credentials for. See
`docs/mcp.md`.

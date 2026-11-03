# Ditto Router competition request fixtures

`ditto_router_competition_request.json` and
`ditto_router_competition_ceiling_request.json` are serialized by
`TestDittoRouterCompetitionPayloadFixture`, using the production request path:

1. Decode the synthetic `chatBody` request with `json.Decoder.UseNumber`.
2. Run `validateRequestSchema` and `outputTokenLimit` with ceiling 32768.
3. Run `lockedUpstreamPayload` for benchmark version **13**, grant-locked
   model `openai/gpt-oss-20b`, and the validated output bound.
4. Run `attachPlatformMiddleOut` with the original request length.
5. Serialize the complete produced payload with `json.Marshal`.

The small fixture requests 64 output tokens. The ceiling fixture changes only
the input bound to 32768. Both include the producer's actual `n: 1`,
`stream: false`, and canonical `reasoning: {effort: medium, exclude: true}`.
They are synthetic wire contracts, not observed production prompts or proof of
provider inference.

The backend's user-billed gateway regression consumes these same JSON bytes
and must prove admission, bounded reservation and actual debit to the linked
account, with no debit to the endpoint owner. Ordinary app requests retain
their existing bound; any larger competition allowance must require the
trusted user-billing policy and validated benchmark reasoning shape.

Update deliberately, then requalify the backend consumer:

```sh
DITTO_ROUTER_UPDATE_FIXTURES=1 go test ./internal/inference -run '^TestDittoRouterCompetitionPayloadFixture$' -count=1 -v
go test ./internal/inference -run '^TestDittoRouterCompetitionPayloadFixture$' -count=1
```

Run these from `services/model-relay` using its pinned Go runtime. The normal
test refuses producer/fixture drift. No other tests or production code write
the fixture files.

Requests larger than 256 KiB cause the relay to add the platform-owned
`middle-out` transform; the user-billed gateway does not yet support that
operation. They must fail admission explicitly. Do not remove compression
policy or silently clamp a requested output bound to make an unsupported
request appear compatible.

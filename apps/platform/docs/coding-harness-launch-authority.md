# Shadow coding screened-harness launch authority

`POST /api/v1/validator/coding-shadow/harness-launch` returns the exact
screened Docker image archive required by one open coding authoring ticket.
It reuses the existing accepted screened-image object; it does not mint a
second image, trust a miner-supplied tag, or expose source/private task bytes.

The signed request binds validator hotkey, ticket UUID, nonce, and timestamp.
Before signing an image URL, Platform locks and rechecks the ticket, run,
current agent artifact, screened-image digest, active coding certification,
screening policy, deadline, and absence of an authoring freeze or terminal
result. The response binds:

- agent, run, ticket, deadline, and benchmark version;
- source artifact and screened-image archive SHA-256;
- exact archive size, Docker image ID, and screened image ref;
- screening policy version;
- a five-minute-or-shorter private image URL and its expiry;
- permanent `weight_eligible=false`.

The validator obtains this capability after validating the authoring lease and
passes it only through the authenticated local coding supervisor request. The
Go phase runner independently checks it against the run manifest before a
dormant harness controller can load the archive. The URL is redacted from
model representations, logs, evidence records, and errors.

Every successfully returned URL appends the existing fail-open artifact-fetch
audit with validator, ticket, source artifact, screened-image digest, and
transport peer context. A second ticket/run/certification check after URL
minting prevents a concurrent freeze or artifact change from escaping the
authoring gate.

This endpoint does not claim coding tickets, start containers, invoke a model,
publish evidence, score, or set weights.

`POST /api/v1/validator/coding-certification-leases/{lease_id}/harness-launch`
is the claimed-lease counterpart used only by the public certification canary.
It reuses the same screened-image object and five-minute URL bound, but
authorizes a claimed certification lease instead of a private coding ticket.
It does not require an existing coding certification receipt. The URL remains
private, redacted, and absent from miner/model/evidence bytes. Flags stay off.

# Coding screening and private scoring policy

## Status

This policy is the authority for future coding-agent screening and scoring. It
does not alter the active Tool + Memory composite, worker configuration,
validator weights, or emissions. Coding contract v1 remains permanently
`weight_eligible=false`.

## Score boundary

Public coding tasks are qualification material only. They must not contribute
to a competitive coding score because they can be inspected, rehearsed, and
eventually hard-coded.

```text
CodingEligible =
  normal Tool + Memory qualification
  AND source-integrity pass
  AND public coding canary pass

FinalCodingScore = IntegrityGate × median(private validator scores)
```

`IntegrityGate` is zero only for confirmed benchmark tampering, sandbox escape,
hidden-test access, credential exfiltration, grader/evidence modification, or
malicious resource abuse. A screen failure never changes an already-finalized
normal score.

## Screening outcomes

| Outcome | Meaning | Effect |
| --- | --- | --- |
| `pass` | No blocking source or runtime finding. | Continue when otherwise qualified. |
| `deny` | Confirmed malicious or integrity-violating behavior. | Coding ineligible; integrity gate applies where evidence proves it. |
| `quarantine` | Suspicious hardcoding, obfuscation, or concealed payload needs review. | No coding task until resolved. |
| `advisory` | Non-blocking quality, size, or ordinary dead-code finding. | Record only. |
| `infrastructure` | Scanner, sandbox, relay, or grader failure. | Retry under bounded policy; do not blame the miner. |

Dead code alone is advisory. Public task names, repository references, expected
patch text, generated source, or uncommon imports are not proof of abuse. They
may justify quarantine only when combined with evidence of benchmark-targeted
dispatch or concealed executable behavior.

## Deterministic source integrity rules

`deny` requires reproducible evidence for at least one of:

- embedded provider, wallet, Platform, validator, or cloud credentials;
- Docker socket, host filesystem, or privileged-device access;
- reverse shell, remote-control, credential discovery, or environment dumping;
- direct network exfiltration outside approved relay and workspace capabilities;
- unrestricted subprocess/shell execution in the miner container;
- runtime dependency download or execution from an untrusted source;
- hidden-test discovery, grader modification, transcript/evidence tampering,
  or workspace capability forgery; or
- deliberate resource-exhaustion or sandbox-escape behavior.

The analyzer must distinguish tool descriptions and model text from executable
behavior. For example, a prompt mentioning `pytest` is ordinary; an agent
spawning an attacker-controlled shell command is not.

An LLM may summarize or prioritize evidence, but it is never the sole source of
a `deny` decision.

## Runtime integrity screen

The coding screen runs the submitted image with the same hardened posture as
the coding canary: read-only root, no Docker socket, dropped capabilities,
bounded processes/resources, capability-only egress, validator-owned workspace,
and no injected provider credential. It records attempted forbidden network,
path, process, mount, capability, grader, and evidence operations.

## Public certification canary

The public coding screener is this one canary. After current normal
qualification, the exact screened artifact runs it:

```text
/coding/health → repeated /coding/seed → /coding/run
→ observed workspace tools → workspace freeze → pristine public grade
→ locked inference and trace-receipt reconciliation
```

`404 /coding/health` means `coding_unsupported`, not normal-screen failure.
Canary success proves protocol compatibility and sandbox integration only; it
does not prove private-task coding ability.

## Private scoring

There is exactly one public coding screener: the public certification canary
above. Coding does not copy the Tool + Memory `screener_1` / `screener_2`
split. Private promoted releases contain one hidden validator partition:

```text
coding_validator
```

Those tasks use binary solved credit: all required trusted tests pass, or the
task is unsolved. Raw partial test results remain diagnostic. Maximum-possible-
score bounds may prune unfinished tasks when a final score is already
impossible.

The validator partition determines the competitive score. Every scored task is
run by the required validator quorum on the same immutable task authority. The
final score is `IntegrityGate` times the median of validator-level solved-task
means. Candidate failures count as unsolved; trusted infrastructure failures
retry without penalizing the miner. A missing quorum leaves no final coding
score.

All private tasks are content-addressed, assignment-scoped, digest-verified,
and withheld from miners. Individual hidden task outcomes, grader logs, and
task identities are not public miner API data.

## Activation

Before coding can affect rewards, shadow operation must demonstrate source-screen
false-positive bounds, canary reliability, task-release quality, validator
reproducibility, private-delivery integrity, cost/capacity headroom, and an
owner-approved reward policy in a new weight-eligible contract version.

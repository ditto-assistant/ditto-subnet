# Screening private-audit retirement

The optional V13 screening private-case program was closed as superseded.
It was preparation, not a delivered production verification program. Ordinary
source admission does not require a universal private challenge.

## Removed activation path

- Manual private replay CLI, protected bank loader, sealed-case adapters and
  conversation-runtime private-case mode.
- Protocol case provisioning, paired execution, official-subject execution,
  package preflight and unused calibration/Student-t analysis machinery.
- Unused in-process scorer private-verifier binding and export seam. Its only
  callers were its exclusive tests; normal scoring never installed a binding.
- Platform private-generation/registration and private-receipt writes, private
  runtime inputs, and official-control image staging/verification/approval writes.
  The matching Backroom writers and generated API operations are removed.
- Exclusive execution tests and activation runbooks.

The removed worker runner was an opt-in script, absent from ordinary worker
dispatch. The official-subject protocol adapter had no Platform/worker bridge.
No exclusive third-party dependency was identified; Pydantic, cryptography,
HTTP clients and wallet libraries remain shared dependencies.

## Retained compatibility and protections

Authenticated private-generation, package, official-control, receipt and
statistics reads remain for immutable audit inspection. Receipt aggregate
models moved out of the executor into `v13_private_result.py`; wire shapes,
canonical signing bytes, signature validation and conservative report-only
statistics remain unchanged. These reads cannot prepare inputs, execute cases,
create receipts or authorize a terminal decision. Historical readiness fields
remain available and cannot certify a private pass.

No migration is edited or reversed. Database models, immutable triggers,
stored records and object evidence are retained. No local private drafts or
external worktrees are removed. No case bytes or private evasion details are
part of this cleanup.

Ordinary source screening/admission, BuildGate, health and seed checks,
artifact/image identity, authentication, signature validation, sandbox/network
isolation and shared inference accounting remain. DittoBench scoring, validator
confirmation/retry, private benchmark datasets and coding-benchmark systems
are separate and remain supported.

Source-only independent replay also remains, including process identity,
lease and distinct physical-provider-resource guards. Node2-named Terraform,
Ansible and workload identity declarations support that separate replay lane;
they are not exclusive to the retired private-case runner. Provider inventory
alone does not prove Terraform state is empty, so this cleanup removes no
infrastructure resources or safety guards and provisions no second machine.

## Verification

Repository caller searches distinguish retired execution symbols from retained
read contracts, historical changelog entries and unrelated benchmark code.
Regression coverage checks the API/MCP writer absence, authenticated reads,
sealed artifact byte/metadata checks, immutable database evidence and historical
receipt signatures. Normal worker, scorer and Platform checks cover the shared
protections after removal. Merge and deployment require separate authorization.

# Bounded manual collector requests

The manual core prepares a single transfer under the existing offline-signed
collector policy. It is not a generic wallet API or a new destination approval. The Backroom bridge below uses the same bounded core.

Each public request file names a UUID, the exact previous operation ID, source
earning block, allocated bucket, alpha amount in integer rao, positive retained
stake floor, expiry block, and operator reason. Only an authenticated custody
operator may arm it. Request JSON alone grants no network access or authority.
Arming independently rechecks the prior successful chain effect and historical
earning, appends an event, and does not sign or broadcast. Unknown, failed,
expired, changed or stale prior claims refuse. The original failed canary and
proved replacement remain in the same journal unchanged.

Arming requires the SHA-256 of canonical request JSON (sorted keys, compact
separators) as `--confirm-manual-request`. The source must already exist in the
durable receipt journal. The amount cannot exceed its remaining bucket
entitlement, the signed maximum, or the available stake minus the retained
floor. Zero-allocated destinations refuse; arbitrary addresses are not accepted.

`--manual-readiness` reads the current operation cursor, observed stake and up
to 100 mature source earning rows. Its coordinates are inputs to a preview,
not approval or a fresh proof of each source. `--preview-manual-transfer` takes
the public request file and performs the same prior-effect/source/reserve checks
as arming, without changing the journal or loading a key. It returns the exact
canonical request digest needed for subsequent explicit confirmation. Both
read modes report no spending authority.

Execution requires the exact `--execute-manual-request` UUID and a private
`--selector-snapshot` path. Ordinary ticks may reconcile pending transactions
but return `manual_ready` without signing an unclaimed manual intent. Identity,
stake floor, maturity, spacing, fee cap and mortality are rechecked before the
exact signed bytes are committed, then broadcast once. Broadcast uncertainty
only permits reconciliation, not another signature. Terminal claims stop.

The private selector snapshot contains allowlisted public receipt coordinates,
not keys or signed bytes. Existing separate-user publisher/observer code can
durably deliver these to public Backroom receipt ingress. A snapshot is not a
published receipt; the independent historical chain checks must accept it.
The CLI-only observer requires its dedicated normal-consent OAuth grant;
no broad desktop token may be copied into it. The Backroom button uses the
keyless mailbox bridge below instead. Neither path is ready solely from a
selector snapshot: independent receipt publication must succeed.

This change does not install a signer runtime, arm an intent, reset a journal,
enable a timer, provision OAuth/IAM, or move funds. Backroom control wiring and
public audit verification remain tracked in SN-54/SN-55/SN-57.

`--observe-earnings-only` advances at most 32 independently verified finalized
blocks in the existing transfer journal without preparing or broadcasting any
transaction. It can continue after a spent manual/canary claim, so observation
does not have to slow to the daily spending frequency. Historical failures
roll back the scan; they never skip missing blocks or reset the cursor.


## Backroom button and automatic public receipt

Under **Emissions & treasury**, **Transfer to a service wallet** previews the
approved destination, amount and minimum alpha retained staked. The operator
must type the exact displayed amount-and-wallet confirmation and click
**Transfer once**. The live signed-in Backroom actor is recorded by the server;
read-only users and cross-origin requests cannot queue a transfer.

Platform serializes confirmed requests in SQL before dispatch. A UUID binds
one immutable envelope, including previous claim, source, expiry and reason.
One pending claim or unpublished receipt blocks a second request. If delivery
of a queue publish or signer broadcast is unknown, retry uses the original
UUID/journal and signed bytes; it never creates an automatic replacement.
An intent expiring after arming stays in custody history and requires recovery.

The default-off Google Pub/Sub bridge uses the attached VM identities, not
wallet credentials or copied desktop OAuth tokens. Platform can publish only
to `sn118-manual-requests` and consume only `sn118-manual-reports`. The transfer
signer has the inverse grants. Topics remain in `sn118-gamma-custody`; signer
network egress remains the existing restricted Google APIs and Finney policy.
The registration signer receives no mailbox authority. The worker observes
bounded finalized earnings but only executes an explicitly confirmed request.
Recurring collector timers remain stopped.

The return report contains only public settlement coordinates. It is not a
receipt proof: Platform's existing independent chain ingress verifies them
against the approved source policy and publishes the Gamma event. An archive
failure retries publication in SQL without re-invoking custody. A semantic
proof refusal stops publication for operator review. A wallet transfer does
not prove a provider purchase or credited GM balance.

### Separate deployment and enablement

1. Merge and verify the selected Platform/Backroom release, including migration
   `f4d95a6c827b` and the new read-only `get_treasury_manual_transfers` tool.
2. First review/apply the owner bootstrap delta: Pub/Sub API and metadata-only
   plan/resource-management apply custom roles for the existing protected
   infrastructure identities, with no payload or wallet permissions. Then run
   the existing protected Gamma custody plan with **sealed** roles,
   runtime RPC egress unchanged/true and `gamma_manual_mailbox_enabled=true`.
   Review the entire private plan before apply. Never apply the repository's
   old bootstrap defaults over live sealed custody. Only the dedicated Platform
   API principal may receive the resource-specific publisher/subscriber grants.
3. Stage the exact merged worker/shared protocol on the existing signer with
   its journal, signed policy and numeric Secret Manager version unchanged.
   Install `sn118-treasury-manual.service`; its reviewed deployment drop-in must
   point to that exact source and existing venv. Keep automatic transfer timers
   disabled. Create the activation file only after inspecting effective unit
   configuration and mailbox IAM.
4. Converge Platform with `platform_treasury_manual_enabled=true` and the exact
   project/request topic/report subscription defaults, via the owned deployment
   process. No new Backroom OAuth grant is required for this keyless bridge.
5. Read fresh public Backroom manual state: enabled, matching policy pin,
   no bridge error, no unresolved prior claim, positive approved entitlement.
   Do not call the button usable merely because default-off code is deployed.
   Verify the public receipt on the next operator-confirmed transfer; never
   replay the already-finalized canary or send another amount just for testing.

Stopping the manual service or disabling Platform dispatch prevents new
claims. Preserve pending journals and queued requests for reconciliation;
do not reset/delete financial history during rollback.

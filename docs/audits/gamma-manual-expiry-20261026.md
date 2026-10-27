# Gamma manual request expiry — 2026-10-26

## Observed failure

Request `7945a273-b086-4daf-9e08-e0e0df6b8bfb` queued 2,000 SN118 alpha
for the GM holding wallet at 18:30:16 UTC (14:30 New York). It retained a
3,000-alpha stake floor, combined 19 earning sources, and expired at block
9,370,247. Platform received a terminal `failed` report at 20:11:45 UTC,
without settlement coordinates or a detailed cause.

The effective systemd unit runs source
`2eab0d3b05fbacec50e4a8898fcc6895685dfdcd`, public custody digest
`3e8d100a252dbf5918feefa3253ebd86af0b0bf974d78c6c981aa3a01cc1ccfd`,
and journal
`/var/lib/sn118-collector-transfer/runtime-v483-approved-20261023/journal.db`.
The older `/var/lib/sn118-collector-transfer/journal.db` is historical and
must not be used for this recovery.

In the live journal, event 955 arms the exact failed request. No subsequent
`dispatch_reserved` event or operation exists. Operation 7 remains finalized:
the previous 1,000-alpha transfer, transaction
`0x367b71e1f1f204fdbfba85ac170b5d4a89d040777c9072de6126e97d64325e08`.
Fresh Backroom readiness at block 9,371,582 still reports `after_operation=7`,
`previous_state=finalized`, and `bounded_claim_available=false`.

The current request therefore never committed signed bytes for submission.
Do not reinterpret earlier finalized transfers as failures or resend them.

## Cause and limits of the evidence

The worker repeatedly encountered archive WebSocket keepalive timeouts and
request-processing exceptions during the signing window. Its finite-window
check returns `manual_expired` when fewer than 128 blocks remain. The mailbox
worker calls that `failed`, ACKs the message, and retains the armed intent.
Readiness then treats the unspent armed intent as unresolved forever.

Background recent-source discovery and up to 32 historical block reads ran
before the mailbox pull. This could consume the request's signing window.
Arming and execution also independently revalidate the 19 historical sources.
The retained logs identify exception types, not the exact slow proof stage;
they do not establish how much delay each of these paths contributed.

## Prepared repair

- Pull a confirmed request before background catch-up, and poll between
  catch-up chunks and recent-source proofs. Consume at most one request per
  iteration; unknown delivery keeps its exact original claim.
- Return fixed signing-window failure diagnostics and the current unsigned
  intent in the existing read-only Backroom manual-control response.
- Add `scripts/treasury_expire_unsigned.py`. It requires the exact request ID,
  explicit confirmation, current journal pin, a finalized prior transfer,
  no later operation or dispatch reservation, and finalized expiry. It appends
  `manual_unsigned_expired`; it never deletes history, alters an operation,
  signs, submits, or queues a replacement. Old UUIDs remain permanently spent
  as intents and cannot borrow a later operation, including batch attribution.
- Label the GM destination as a holding wallet and show the remaining funding
  stages explicitly, without asserting an unverified sale or credited purchase.

A redacted copy of the live journal was rehearsed locally with a synthetic
finalized observation. All seven operations were preserved, and the active
intent returned to the previous successful request. This is local validation,
not production recovery or a fresh chain proof.

## Production recovery boundary

Merge/application deployment and custody runtime adoption remain separate
operator steps. Custody files are excluded from automatic application releases.
Use the exact reviewed merged source; retain the current signed policy,
manual-limit approval, delegate version, journal, network policy and persistent
automatic-service/timer masks. No coldkey approval change is needed for this
unsigned-intent recovery.

After separately authorized custody adoption:

1. Stop the manual consumer and take a private, consistent journal backup.
   Keep the existing journal and automatic timer masks; never initialize or
   reset a journal.
2. As the existing custody OS user, run the reviewed expiry CLI against the
   effective policy and journal above, with
   `--request-id 7945a273-b086-4daf-9e08-e0e0df6b8bfb`, an audited reason, and
   `--confirmation 'EXPIRE UNSIGNED 7945a273-b086-4daf-9e08-e0e0df6b8bfb'`.
   The CLI obtains a new public finalized observation and refuses a changed,
   signed, still-live, or unresolved claim.
3. Restart only the manual consumer on the reviewed source. Require fresh
   Backroom readiness with the same policy and operation 7, no unsigned intent,
   and a newly available bounded claim. Compare all operations and prior
   receipts to the backup.
4. A replacement transfer still needs a new preview and exact operator
   confirmation. Recovery itself grants no replacement payment authority.

## Missing GM accounting

The collector signs `transfer_stake`: collector SN118 alpha → GM holding
coldkey `5Cm6Vok2sWkQ33w97AVEsb9U2EyE96Y1utfSLW5vR4viiLKD`.
It does not unstake/sell alpha, transfer liquid TAO to GM, or buy GM credits.

At diagnosis, `treasury_public_events` contains two `service_distribution`
rows and no conversion, vendor-payment, or GM-credit rows. The later manual
transfers have independent wallet-movement receipts with incomplete source
attribution. Neither store proves the downstream GM funding flow.

For existing manual funding, link each independently decoded SN118 sale and
TAO proceeds to the holding wallet, each finalized TAO transfer to GM's current
account instructions, and the corresponding authenticated GM credited deposit.
Avoid inferring credited USD from wallet balances or quoted prices. Do not
double-count a holding transfer, sale and payment as three purchases.

GM's current [top-up documentation](https://saygm.com/topup.md) and
[Account API](https://account.saygm.com/v1/openapi.json) expose authenticated
`GET /v1/wallet-deposits` and `GET /v1/balance`. The billing browser was signed
out during diagnosis, so provider receipts were not verified. Account access
and the operator's confirmation of which downstream steps already occurred
are needed to backfill truthful purchase records. No sale, TAO payment,
provider-credit publication or account-linking action was performed here.

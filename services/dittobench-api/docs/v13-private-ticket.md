# V13 private verifier ticket boundary

This is a dormant, report-only admission seam. Platform's admin route issues a
five-minute HMAC ticket only after rereading one immutable generation group,
both matching package registrations, the current agent/attempt, and an
unexpired verified image upload. The ticket binds group, role, artifact,
attempt, image upload/SHA, profile, manifest, scorer session/case UUIDs, and a
fresh nonce. It carries no hidden case bytes.

The scorer's `/v1/private-verifier/admit` and `/v1/private-verifier/ledger`
routes validate this ticket independently of the general control-plane auth,
which may still be in shadow mode. They reject missing keys, bad MACs,
expired tickets, replayed admission, unbound images, unsettled broker cases,
and cases without a scorer-owned container-stop marker. The ledger response
contains sanitized counts only and is not a policy verdict.

`PLATFORM_V13_PRIVATE_TICKET_KEY` and
`DITTOBENCH_V13_PRIVATE_TICKET_KEY` must be the same protected random value of
at least 32 bytes. Neither is configured by this change. No route can admit a
case in production yet: only a future scorer-owned factory can call the
in-process verified-image and container-stop methods. That factory must fetch
the exact verified image under trusted Platform authority, start a fresh
hardened sandbox and source-bound broker session, run one sealed case, revoke
and drain the broker, prove container stop, then request its settled ledger.

The operator must also provision and independently approve a real protected
blueprint bank and generator/seed ledger before any private canary. A signed
ticket or returned ledger alone never completes V13 verification or CLEAR.

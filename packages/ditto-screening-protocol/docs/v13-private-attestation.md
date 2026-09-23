# V13 private evidence attestation boundary

`V13PrivateEvidenceStatement` binds target and known-benign clean-control
aggregate results to an exact committed target agent, attempt, archive and
image; the immutable profile and manifest; a common pair-inventory digest; the
runner hotkey; and issuance time. The domain-separated canonical JSON bytes
are signed with the runner's sr25519 key. Platform's read-only verifier checks
the signature using the existing Bittensor hotkey verifier and compares every
identity against independently supplied trusted commitment, registration,
known-benign image commitment, runner hotkey, and inventory digest. Any mismatch
or signature failure returns false. A test signs with an actual sr25519 key and
checks replay/tamper rejection.

This module does **not** register a runner key, provision a known-benign image,
run the clean-control lane, fetch protected case bytes, validate broker ledgers,
persist an attestation, or issue a policy verdict. Those production trust
anchors must precede use of this evidence in V13 review. The target and clean
aggregate counts still need the separate statistical analysis and source
causality review; all 19 mandatory checks and applicable private tests remain
required for CLEAR. This slice performs no live writes and changes no hold.

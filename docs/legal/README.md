# Maintainer-provided CLA documents

No production CLA text is supplied by this implementation. Maintainers and
counsel provide the approved document as `CLA-v<N>.md` in this directory.
Do not use test fixtures as a production document, and do not replace an
existing version's text. A text change requires a new version.

Publish the exact UTF-8 bytes and their SHA-256 through Backroom's
`publish_cla_version`, with the version, effective date, operator reason and
`PUBLISH CLA <version> <sha256>` confirmation. Platform validates the digest
and persists immutable text/metadata. The publication and effective dates are
public; versions and effective dates must advance monotonically.

Publication does not enable the gate. It starts `off`. Read adoption and
history with `get_cla_policy`, then use `set_cla_policy` for `shadow`. Shadow
records and counts unsigned artifact upload attempts without refusing them.
Announce the effective date in miner docs before choosing `enforce`. Enforce
cannot be enabled without currently effective published text. An effective
new version requires a new receipt before the next contribution.

The CLI downloads the registry's exact published bytes, independently verifies
the hash, saves the complete document for review, and requires explicit typed
version/digest confirmation. It signs only with the miner hotkey. Public
receipts retain the signing inputs and finalized ownership snapshot.

Upload checks enforce the same policy in Python and the Go relay. The CLI
checks it before preflight, registration or payment, and Platform checks it
again when committing a new artifact. Exact retries of an already accepted
artifact remain idempotent. Existing scored agents retain their lifecycle and
scores; no consent is synthesized for old submissions.

New artifact rows are linked to their exact consent receipt through
`agent_cla_bindings`. This binds the stored artifact SHA and hotkey to the
receipt without changing old agent rows. Model/base-licence validation is a
separate artifact-validation policy, not a conclusion drawn from a signature.

Adoption counts use distinct hotkeys with a non-rejected, non-retired
submission, explicitly reported as `active_hotkeys_basis`. They do not claim
to be a current chain registration census. Unsigned includes outdated receipts.
The shadow upload count includes requests that fail a later archive/payment
validation, and excludes exact accepted-artifact retries.

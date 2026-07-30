# Link hotkeys after rotating wallets

Use an owner-link attestation when copy screening holds a new submission because
it resembles your own earlier work submitted from another hotkey. The
`ditto attest` command proves that both hotkeys belong to the same operator.

An owner link is not needed for an ordinary submission. It resolves copy
screening between the two hotkeys named in the link.

## Before you start

Update the miner CLI and make both wallets available on the same machine:

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
uv sync
```

You need the local Bittensor wallet names for both sides:

- the coldkey wallet name;
- the hotkey name inside that wallet; and
- either the hotkey keyfile or the coldkey keyfile that owns it.

Wallet names are not SS58 addresses. Do not paste seed phrases, passwords, or
keyfiles into Discord or an operator ticket.

## Sign with both coldkeys

If you still control both coldkeys, each coldkey can prove the hotkey it paid
from:

```sh
uv run ditto --network finney attest \
  --coldkey <new-coldkey-wallet-name> \
  --hotkey <new-hotkey-name> \
  --other-coldkey <old-coldkey-wallet-name> \
  --other-hotkey-name <old-hotkey-name> \
  --key-kind coldkey \
  --other-key-kind coldkey
```

The CLI signs both halves in one invocation. It prints the two resolved hotkey
addresses before asking for confirmation. Compare those addresses with the
hotkeys on the two submissions. Answer `n` and fix the wallet names if either
address is wrong.

Signing an attestation does **not** transfer TAO, change stake, or submit an
on-chain extrinsic. The keys sign a short text payload that the Ditto platform
records as ownership evidence.

Coldkey proof requires an existing Ditto payment record that binds the claimed
hotkey to that coldkey. If one side has no payment record, prove that side with
its hotkey instead by omitting the corresponding `--key-kind coldkey` flag.

## More than two hotkeys

Links are direct and are not transitive. If old work under hotkey `A` is being
matched against new submissions under hotkeys `B` and `C`, create both links:

1. `A` to `B`
2. `A` to `C`

An `A` to `B` link plus a `B` to `C` link does not establish an `A` to `C`
link.

## After the command succeeds

The command prints an attestation UUID. Save it. If a submission is already in
operator review, send the operator:

- the attestation UUID;
- the held submission's agent UUID; and
- the earlier submission's agent UUID.

The link affects future screening and automatically clears existing pending
copy-review holds between the two directly linked hotkeys.

## Scope and limits

An owner link:

- exempts plagiarism screening only between the two named hotkeys, including
  byte-identical and repacked generations;
- does not create another emission slot or change coldkey-based emission
  allocation; and
- can be revoked by an operator if a key is sold or compromised.

For hotkey signing, mixed proofs, offline signing, revocation, and the full
policy rationale, see [Link a rotated hotkey](MINER.md#link-a-rotated-hotkey) in
the miner guide.

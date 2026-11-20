# Cathedral sealed builds (optional, off by default)

## What it does

With one screener setting, a submission's image is built inside a Cathedral sealed room (an AMD SEV-SNP confidential VM) instead of the screener's local `docker buildx build`. Everything after the build is unchanged: the screener loads the image, smoke-tests it, exports it and uploads it as today.

Each sealed build also writes a build receipt: one JSON file that ties the miner's upload (the sha256 of their tarball) to the image ID the room produced. Anyone can check it offline, with one command.

- The build command is Ditto's own Buildx command from `gate.py`, with two room changes: `--network host` (the room has no bridge network) and `--memory 7g` (the room has 8 GiB).
- If Cathedral is down, slow or returns something wrong, the attempt is parked as retryable infrastructure (`cathedral-build-infrastructure`). It never counts against the miner. A Dockerfile that fails to build, Ditto's own build time limit (when Cathedral can honour it in full, up to 1700 s) and an image that declares `VOLUME` are the same miner rejects as a local build. Anything on Cathedral's side is retryable infrastructure.
- With the setting off, nothing changes: no Cathedral calls, same code path, same outputs.

## How to switch it on

On a screener node:

```
SCREENER_BUILD_BACKEND=cathedral
SCREENER_CATHEDRAL_API_KEY_FILE=/path/to/cathedral-api-key   # a Cathedral API key, file mode 600
```

Optional settings and their defaults are listed in `workers/screener/README.md` (template `sealed-4x8`, builder `docker:27-dind` pinned by digest, `max_spend_usd` 0.5 per build, receipts under `/opt/ditto/screener/state/build-receipts`).

To switch back, set `SCREENER_BUILD_BACKEND=local` (or remove it). The switch is per node: one node can run it while the rest build locally.

## How a validator or a miner checks a receipt

```
curl -fsSL https://cathedral.computer/install.sh | sh   # installs ctcli; see cathedral.computer for pip, pipx, uv, Windows
ctcli build verify RECEIPT.json --source my-upload.tgz
```

`build verify` is in ctcli 0.6.1 and later, which `https://cathedral.computer/install.sh` installs. 0.6.0 does not have the command.

`--source` takes the exact tarball the miner uploaded (or `--source-sha256 HEX`). Add `--image-id sha256:...` to also require the image ID a validator loaded. The answer is PASS, FAIL or NOT PROVEN, with the source hash, image ID, archive hash, builder digest, AMD-attested measurement and room it is bound to.

What PASS checks:
- AMD's certificate chain and the SEV-SNP report of the machine, and that its measurement is on Cathedral's published approved list.
- Every statement the sealed guest signed for the room, in order, with no gaps: the room was created from the pinned builder, received exactly the miner's bytes, ran exactly the canonical build command (recomputed by the checker), and then returned the result file the receipt carries.
- Cathedral's own signed receipt chain for the room: nothing else was sent to the room (no other command, terminal, file write or network change), the command ran with an empty environment, and the room was deleted.

## What it does not prove

- **That nobody could read the source.** An operator of the machine can still read a box today. Use it for sources that are, or will be, public.
- **That the image is safe or passed screening.** It proves where the image came from, not what it does. Screening still decides.
- **What the build downloaded.** The room has internet access; base images, crates and packages fetched during the build are not pinned by the receipt.
- **That the build is reproducible.** Two sealed builds of the same source give different image IDs today (timestamps and a non-reproducible Rust binary), so a receipt is checked against the image it names, not by rebuilding.
- **Trust in Cathedral is not zero.** The statements about the build are chip-rooted: signed inside the sealed machine by a key bound to AMD's report. You still trust AMD, Cathedral's approved-measurement list, and Cathedral's API operator not to send the room traffic outside its own signed chain (closed by serial build rooms, planned). You do not trust Ditto's platform, the screener operator or the miner.
- Validators do not check receipts automatically yet. Carrying the receipt to validators (signed verdict field, platform storage, scorer check, submission page row) is the next set of changes, each behind its own switch.

## Live numbers

`PROOF.md`: 31 sealed build attempts of Ditto's public starter miner on Cathedral production, 21 built, 20 receipts that verify offline, every failure Cathedral-side and classed as retryable infrastructure. Three representative receipts are in `docs/cathedral-proof/`.

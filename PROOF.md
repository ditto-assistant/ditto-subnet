# Proof: sealed builds of Ditto's public starter miner on Cathedral production

Run 19 Nov 2026, 00:52 to 02:26 UTC, against Cathedral's production API with this branch's code (`ditto_screener.cathedral_build`, the same module the screener uses), a Cathedral test key, and `sealed-4x8` rooms (4 vCPU, 8 GiB) on the AMD SEV-SNP sealed pool. Every room was deleted (two after the client gave up on a 502; confirmed by the API).

Sources: `miners/dittobench-starter-kit/` at 16 commits of this repo (public), each as one tarball, plus the kit at `736d56621a0545938cfc9bca7fe407d509eb1502` built 15 more times. Nothing private was built.

## Numbers

| | |
| --- | --- |
| Build attempts | 31 |
| Built | 21 |
| Receipts that verify offline (`ctcli build verify`, PASS) | **20** |
| Receipt NOT PROVEN | 1 (build 13: the room's delete answered HTTP 502, so the saved chain does not end with the delete; the client now re-sends the delete) |
| Not built, Cathedral side | 10: 9 rooms ran out of disk in the first 45 s ("no space left on device" registering the base layer), 1 burst of API 502s |
| Not built, miner side | 0 |
| Of the 10, classed as retryable infrastructure by the gate's own rule | 10 (none would have counted against a miner) |
| Time per build, room create to receipt saved | 380 s min, 543 s median, 681 s max (cold cache, 408 Rust crates) |
| Image archive | 175.7 to 176.6 MB, 3 parts of at most 64 MiB |
| Sealed hosts used | 3 (c46-sealed-2: 8, nc-sealed-3: 9, nc-sealed-2: 4); 3 attested measurements, all on Cathedral's approved list |
| Wrong `--source-sha256` on each built receipt | 21 of 21 FAIL |
| Wrong `--image-id` on each built receipt | 21 of 21 FAIL |
| Archive through the gate's `_portable_image_archive` and `_replay_image_config_matches` | 21 of 21 match the receipt's image ID |

Earlier runs of previous versions of the code, before review fixes: 4 of 4 and 19 of 20 built (the one failure, a dropped connection while polling, led to the read retries now in the client).

## Failure drills, live

- A Dockerfile with `RUN false`: exit 91 in the room, mapped to the same `docker-build` reject as a local build.
- `RUN sleep 120` with a 30 s build limit: exit 92 after 42 s, mapped to `docker-build-timeout`, as locally.
- A command still running when the room is deleted (30 trials): its signed receipt is always missing from the room's numbered list, so a receipt with a hidden command fails the check (it cannot PASS).
- A request to open a terminal in the room shows up in Cathedral's signed chain, and the check refuses it.

## Reproducible image IDs: no

Six sealed builds of the identical tarball gave six different image IDs. Only the base image layer is the same; the Rust binary itself differs between builds, and every later layer carries build-time timestamps. So an image ID cannot today be matched against an independent local `docker build`, and the receipt is checked against the image it names, not by rebuilding. Making the starter kit reproducible (`SOURCE_DATE_EPOCH`, Buildx `rewrite-timestamp`, a reproducible Rust profile) is a separate change.

## Check it yourself

Three representative receipts are in `docs/cathedral-proof/` (number, then the starter-kit commit): 01 and 16 PASS, 13 NOT PROVEN. The table below lists all 21. `build verify` ships in ctcli 0.6.1, which Cathedral's installer now installs (0.6.0 does not have it). With 0.6.1 or later:

```
curl -fsSL https://cathedral.computer/install.sh | sh   # installs ctcli; see cathedral.computer for pip, pipx, uv, Windows
ctcli build verify docs/cathedral-proof/16-736d566.json \
  --source-sha256 5ad58eb239239b70cf02dedd34a428864000d45394bc76920b5b71fd08fbfaa6 \
  --image-id sha256:fde33b99fbadf6d1684d8f41901b391e6b99ec4577dc870788c1cef99c3ffda6
```

A source tarball rebuilt with `git archive` gets new timestamps and so a new sha256; use the hash in the receipt, or the miner's original upload.

| # | Starter kit commit | Source sha256 | Image ID | Host | Seconds | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 01 | `0656fea4` | `b29616f1cefb` | `c9c96b68e821` | c46-sealed-2 | 453 | PASS |
| 02 | `1af75768` | `e519b5cc3fc6` | `3d138614bd6d` | c46-sealed-2 | 462 | PASS |
| 03 | `45527305` | `174c5ce68c40` | `6922483485e8` | nc-sealed-2 | 649 | PASS |
| 04 | `4781a533` | `8e1f18476978` | `d413a354a10e` | nc-sealed-3 | 533 | PASS |
| 05 | `4d409519` | `efca1488087d` | `428f413ec755` | c46-sealed-2 | 463 | PASS |
| 06 | `5a7c2bf4` | `3bc39e0b5e3a` | `3d8a777387bd` | nc-sealed-3 | 556 | PASS |
| 07 | `5af9eb52` | `50260970d4f0` | `43ee6844d007` | nc-sealed-3 | 574 | PASS |
| 08 | `86bc89e7` | `3be184fd6004` | `609c3734e238` | c46-sealed-2 | 405 | PASS |
| 09 | `a131ed26` | `1a911330b079` | `6a6bf73d6262` | c46-sealed-2 | 398 | PASS |
| 10 | `b6b0169c` | `4a0825235f3c` | `0b073aff088d` | c46-sealed-2 | 390 | PASS |
| 11 | `bd36b088` | `a90ee049d512` | `eb6e6112edea` | nc-sealed-3 | 543 | PASS |
| 12 | `c9098b11` | `ed1af39e8727` | `5910de7b8c17` | nc-sealed-3 | 530 | PASS |
| 13 | `d0c20a70` | `5f156ba6f8b6` | `861132729908` | nc-sealed-3 | 681 | NOT PROVEN |
| 14 | `d3b45571` | `e78407eba61c` | `0b9e2bb51808` | c46-sealed-2 | 380 | PASS |
| 15 | `f9756f20` | `27e3da374794` | `ed9bf705763a` | nc-sealed-2 | 662 | PASS |
| 16 | `736d566` | `5ad58eb23923` | `fde33b99fbad` | c46-sealed-2 | 382 | PASS |
| 21 | `736d566` | `5ad58eb23923` | `71cb72ddcb18` | nc-sealed-3 | 638 | PASS |
| 22 | `736d566` | `5ad58eb23923` | `0458cb66a7e8` | nc-sealed-3 | 643 | PASS |
| 26 | `736d566` | `5ad58eb23923` | `6199fb4a764c` | nc-sealed-3 | 589 | PASS |
| 29 | `736d566` | `5ad58eb23923` | `52e1fbd4a7ca` | nc-sealed-2 | 593 | PASS |
| 30 | `736d566` | `5ad58eb23923` | `3727529a6649` | nc-sealed-2 | 628 | PASS |

Short hashes in the table. The full values are in the committed receipts (01, 13, 16); the other 18 are not in the repo.

## What this does not show

The limits in `CATHEDRAL.md` apply. In short: an operator could still read a source; use public sources, the receipt says nothing about the image's safety or what the build downloaded, the build is not reproducible, and the build facts are chip-rooted while "nothing else touched the room" rests on Cathedral's signed chain.

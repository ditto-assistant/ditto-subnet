# Miner-hostable Ditto workloads: ranking, and the Ditto Edge shadow competition

| | |
|---|---|
| **Status** | Research done; winner chosen; shadow lane being built. **Shadow only.** Nothing pays, sets weights, or serves a user until Peyton approves it. |
| **Ticket** | [SN-481](https://omniaura-team.atlassian.net/browse/SN-481) |
| **Builds on** | SN-472/473 design ([#3503](https://github.com/ditto-assistant/ditto-subnet/pull/3503), draft), Omar's query-adapter pilot ([#3483](https://github.com/ditto-assistant/ditto-subnet/pull/3483)–[#3485](https://github.com/ditto-assistant/ditto-subnet/pull/3485), drafts), the track registry (`ditto/validator/tracks.py`), SN-237 and SN-387 shadow rules |
| **Date** | 2026-11-12 |

## Summary

Peyton asked which parts of the Ditto stack a network of home Mac minis could
host, with the miner built into Ditto Desktop as an opt-in toggle. The seed
idea was public content: user-deployed apps and publicly shared artifacts. The
brief asked for 5–20 ideas, a shortlist, a rerank, and one winner built as a
shadow-mode competition.

**The pick is Ditto Edge:** a verified public-bytes network.

- Mac nodes hold content-addressed copies of public, unencrypted Ditto content
  that Ditto assigns to them: public artifact bodies first, then static
  deployed-app files, then Ditto's own signed releases and model weights.
- **Ditto runs the verifier and keeps every trust decision.** Ditto keeps the
  page wrapper, the publication state, and the revocation list. Nodes never
  serve a browser directly and never see a URL, owner, title or account.
- **Verification is exact and needs no TEE.** Every object is named by its
  SHA-256, and challenges are keyed by random nonces. Answering by
  re-downloading the object after a challenge counts as a failure. Nodes that
  still answer for revoked objects are caught by asking for them.
- **Unpublishing works within a bounded time.** A short lease and a revocation
  list make every honest node and every Ditto surface stop serving an object
  within one lease. What cannot be done, for any public content, is forcing a
  copy to be deleted. Section 6.3 says so plainly.
- **Shadow only.** The lane is `off` by default. In `shadow` it scores nodes
  into an hourly ledger whose rows are `weight_eligible = false` by database
  constraint. It never touches weights, emissions, track shares, or users.

**Why this one.**

- It is the only candidate that passes every hard gate, is verified exactly at
  almost no cost, and can run against the **real production app today with no
  product change**. The shadow dry run reads real public shares through the
  anonymous `GET /api/v4/shares/{id}`, mirrors them across nodes, and detects
  unpublishes from production.
- It also builds the Mac-network machinery that the later lanes reuse: device
  identity, enrollment, a sandboxed Desktop helper, challenges, the ledger, and
  Backroom reads. Those later lanes are SN-473's encrypted storage and CKKS
  compute, and the research grid ranked second here.

**Three honest caveats.**

1. **This is not a cost saving.** Ditto's public serving costs a flat $39.90/mo
   Hetzner box behind Cloudflare. Production holds 319 public artifact shares,
   and the share host saw about 1,050 visitors a month (§2). Emissions here
   buy breadth, a reason to install Desktop, and decentralization, not savings.
   SN-473 reached the same verdict for storage.
2. **Every CDN funded only by token emissions has died.** Saturn, Filecoin
   Station and Fleek's edge network all did. The survivors kept a
   **permissioned central verifier** (§3). Ditto Edge keeps Ditto as that
   verifier and as the edge, and pays only for work it has verified by
   challenge, never for traffic a node reports about itself.
3. **Desktop reach is tiny today:** about 2 DMG downloads per release (§2).
   The toggle ships hidden behind a beta switch until Peyton decides.

## Contents

1. [The ask and the constraints](#1-the-ask-and-the-constraints)
2. [What the stack actually has](#2-what-the-stack-actually-has)
3. [What Bittensor and decentralized hosting teach](#3-what-bittensor-and-decentralized-hosting-teach)
4. [Fifteen ideas](#4-fifteen-ideas)
5. [Shortlist and rerank](#5-shortlist-and-rerank)
6. [The pick: Ditto Edge](#6-the-pick-ditto-edge)
7. [Shadow plan, metrics and kill criteria](#7-shadow-plan-metrics-and-kill-criteria)
8. [How this relates to existing work](#8-how-this-relates-to-existing-work)
9. [Decisions for Peyton](#9-decisions-for-peyton)
10. [Sources](#10-sources)

## 1. The ask and the constraints

From SN-481, in Peyton's words:

> user-deployed applications on [Ditto] are public by nature because they're
> public websites and there are also publicly shared artifacts. These are
> candidates for miners to host. [...] there's no worry about these miners
> stealing this user data because they made it public. Except if they unpublish
> it, there is that consideration.

> The miner must be able to run on a Mac Mini [...] The miner must be built
> into the Ditto desktop app as something optional that users can turn on.

**Hard gates.** Every idea must pass all three.

- **G1 Privacy without TEEs.** A miner owns its box: root access, a debugger,
  a patched binary, many identities, and every byte kept forever. TEEs give
  integrity at best, and 2025's Battering RAM, WireTap and TEE.fail broke
  SGX, SEV-SNP and TDX attestation from a physical position a box owner
  holds (SN-473 §2.2). Apple offers third-party code no TEE on the Mac at all,
  and App Attest is unavailable to Mac apps. So a miner may see only three
  kinds of input: public bytes, synthetic bytes, or ciphertext under keys it
  never sees.
- **G2 Mac mini.** The work has to run usefully on a base M4 Mac mini: 16 GB of
  memory, home broadband, often behind NAT.
- **G3 Desktop opt-in.** The work has to fit an off-by-default helper process
  in Ditto Desktop, with outbound-only networking and bounded disk, CPU and
  bandwidth.

**Scored criteria**, each 0–5, with weights:

| Code | Criterion | Weight |
|---|---|---|
| V | Verifiability and incentive-game resistance: cheap exact checks; pay that does not grow with identities | ×3 |
| S | Shadow plug-in: can run against our API and the real production app with no user effect | ×2 |
| R | Revocation: Ditto can make the network stop serving unpublished content in bounded time (5 if no user content is involved) | ×2 |
| C | Cost to Ditto (5 is near zero) | ×1 |
| U | Usefulness: resource or product value now, and as Ditto grows | ×2 |
| B | Builds on existing work without duplicating it | ×1 |

The maximum is 55. V carries the most weight because SN118's history, and
Bittensor's, is a history of incentive gaming (§3).

## 2. What the stack actually has

Read from current `main` of backend, ditto-app, infra and ditto-harness on
2026-11-12. Anchors are `repo/path:line`.

**Public shares and artifacts.**

- **Visibility and access.** Shares are `public`, `friends`, `shared` or
  `private` (backend/cmd/share/service.go:707). `public` needs no sign-in.
- **Unpublishing** is a synchronous database change: `DELETE` or `PATCH` on
  `/api/v4/users/{uid}/shares/{share_id}` (backend/pkg/api/v4/shares.go:184,245).
  Nothing downstream is notified. The next read simply fails.
- **Encrypted graphs.** Artifacts from encrypted graphs (`EncVersion != 0`)
  are withheld unless a plaintext public copy was published
  (backend/cmd/share/public_copy.go; DITTO-1944).
- **HTML artifacts** come from an isolated origin, `a.dittousercontent.com`.
  It uses 5-minute in-process capability tokens bound to share and artifact
  fingerprints, and a strict `sandbox` CSP
  (backend/cmd/share/artifact_origin.go:28-140). Binary attachments are capped
  at 50 MiB.
- **An anonymous JSON API** serves the same public content:
  `GET /api/v4/shares/{id}` and `GET /api/v4/profile/{username}/shares`
  (backend/pkg/api/v4/apiv4.go:240-242; public_shares.go:98).
- **Scale and cost.** A 2026-10-26 production snapshot counted 319 public
  artifact shares (backend/docs/artifact-origin.md:161). Public serving runs on
  one $39.90/mo Hetzner box behind Cloudflare, and the share host saw 1,052
  unique visitors in 30 days
  (backend/docs/INFRASTRUCTURE_COST_ANALYSIS.md:105-111,278-288).
- **A bug.** Only image shares get an `og:image`, and it is the 5-minute
  capability URL, so link previews break after 5 minutes
  (backend/cmd/share/templates/artifact.templ:15).

**Deployed apps.**

- **How an app goes live.** `POST /api/v2/agent/jobs/{job_id}/deploy` →
  `cmd/app-deployer` creates a Cloud Run service bound to `allUsers`. There is
  no publish flag; deleting the project is the only way to unpublish.
- **Secrets.** Project vault secrets are injected as **environment variables**
  (backend/pkg/services/agentjobs/service.go:1458-1552).
- **Runtime.** The runner starts `server.py`/`app.py`/`main.py` as Flask apps,
  or serves `index.html` projects with `python -m http.server`
  (backend/pkg/services/agentjobs/runner/apprunner/entrypoint.sh).
- **What that means for miners.** Only static apps' files are public bytes. A
  Flask app's runtime holds secrets and runs arbitrary server code, so it
  **never** goes to a miner.

**Embeddings and Omar's per-user embedding work.**

- Production embeddings are Vertex `text-embedding-005`, a closed model.
- The per-user query adapter is still an issue, backend#1360, not shipped code.
- Omar's pilot drafts (#3483–#3485) host synthetic tenants only. In the PRs'
  own words: "Neither embeddings nor per-user weights are safe on an untrusted
  host."
- Embedding inversion has only improved since: vec2text, ZSInvert, ALGEN and
  vec2vec (§3). **Private embeddings and query vectors are off the table for
  miners.** Embeddings of public text are safe.

**Ditto Desktop** (ditto-app).

- **Structure.** An Electron app. Runtime hosts load from `desktop/runtime/*.cjs`
  behind allowlisted IPC (`trustedSender`, desktop/main.ts).
- **Sandboxing.** It already runs a child process under a deny-default
  `sandbox-exec` profile (desktop/runtime/src/pi-sandbox.cjs).
- **Outbound relays.** It already keeps relays to Ditto that work behind NAT
  (backend/pkg/services/desktoptools/relay.go).
- **What it lacks.** There is no tray icon and no login item, so a helper stops
  when the app quits.
- **Reach.** Ninety releases have shipped since 2026-11-05, with 124 DMG
  downloads in total, about 2 each (ditto-desktop-releases, `gh api`).

**What SN118 already provides.**

- **A shadow track state.** The validator track registry has `SHADOW` (0 bps
  whatever its configured share) next to `ACTIVE`, `RETIRING` and `RETIRED`
  (`ditto/validator/tracks.py`).
- **A shadow competition mode.** The generator competition has a
  `mode: off|shadow|enforce` ladder in which shadow writes ledger entries with
  `weight_eligible = false`.
- **The shadow pattern to follow.** SN-237 requires that "a shadow competition
  pays 0 regardless of the budget it is served". SN-387 adds an epoch-pinned
  ledger, a hypothetical payout, 0 bps, and Backroom exposure.

## 3. What Bittensor and decentralized hosting teach

The full sourced notes are in the PR description. The lessons that change the
ranking:

1. **Checks of a box's own self-description get spoofed; checks against
   references the verifier trusts hold.**
   - Spoofed: NI SN27 miners swapped the signed benchmark binary for `echo`
     and faked GPUs to the kernel. Lium SN51 nodes shim NVML to report fake GPU
     IDs ([Lium #1328](https://github.com/Datura-ai/lium-io/pull/1328)).
   - Held: Compute Horde re-runs 1–2% of jobs on its own trusted executor
     ([SDK](https://sdk.computehorde.io/master/api/client.html)), and Data
     Universe validators re-scrape samples.
   - **So: score only what Ditto can check against bytes it holds.**
2. **Emission-funded CDNs died; permissioned verifiers survived.**
   - Filecoin Saturn paid a fixed FIL pool by bandwidth, TTFB and uptime. Its
     main fraud was fake retrievals. Its home "L2" nodes never left proof of
     concept, its L1 nodes needed a 10 Gbps uplink and their own /24, and it
     shut down on 2024-06-01
     ([L1 node](https://github.com/filecoin-saturn/L1-node);
     [saturn.tech](https://saturn.tech/)).
   - Filecoin Station reached 10,000 desktop nodes, but only about 400
     addresses ever received FIL. Rewards stopped and its checks became
     permissioned ([repo](https://github.com/CheckerNetwork/desktop), archived
     2025-07-22).
   - **So: Ditto stays the verifier and the edge, and it never pays for
     self-reported traffic.**
3. **Home broadband is not a CDN, but it is plenty for Ditto's public
   traffic.**
   - At about 1,000 visitors a month, egress is not the bottleneck.
   - Reaching a Mac behind NAT over TLS is the hard part, so nodes use
     outbound connections only, and Ditto pulls from them.
4. **Content-addressed bytes are the cheapest thing on earth to verify.**
   - A SHA-256 over the exact bytes needs no TEE, no oracle and no
     recomputation.
   - Browsers can verify too: `fetch` with `integrity`, or a service worker
     that checks a signed manifest, as the
     [IPFS service-worker gateway](https://github.com/ipfs/service-worker-gateway)
     now does for ipfs.io.
   - Signed HTTP exchanges are effectively dead: Google stopped issuing SXG
     certificates around 2026-09-30.
5. **Open gateways attract phishing and takedown traffic.** Cloudflare closed
   its IPFS gateway on 2024-08-14 after takedown notices surged, and Netcraft
   found 84% of IPFS-fronted phishing on public gateways. **So: nodes serve
   only bytes Ditto assigned and never act as open gateways.**
6. **Revocation works when it is manifest-driven and audited.**
   - IPFS "Bad Bits" denylists make enforcing nodes return 410.
   - Cheap audit: request the revoked object and check the node refuses.
7. **Macs suit memory-rich, low-power batch work with small models.**
   - An M4 decodes about 20 tok/s on an 8B Q4 model and idles at about 4 W.
   - The one Mac inference network with revenue, Darkbloom, earns about $34
     per online Mac per month. SN118's roughly $56k/month of miner emission
     over 1,000–2,500 Macs is $22–56 a Mac, the same range.
   - **Demand, not supply, binds** (research grid, §4 idea 6).
8. **Results don't match bit for bit across Mac chip generations.** MLX output
   differs between Apple chips. Any compute lane needs per-chip-family
   tolerances and trusted recompute, not bit-exact comparison.
9. **Identity.** SN118 is Yuma with 256 of 256 UIDs in use. Null consensus
   allows `floor(2500 / mechanisms)` UIDs but is **subnet-wide** and makes the
   single highest-stake validator the only weight setter. Whatever the track
   becomes, nodes need an identity that works per device (pilot) **and** per
   pool (long term) (SN-473 §5.6).

## 4. Fifteen ideas

Each idea lists what miners would do, the gate result, and scores in V/S/R/C/U/B
order (§1).

1. **Ditto Edge: verified public-bytes network.**
   - **What miners do.** Mac nodes hold content-addressed copies of public,
     unencrypted Ditto content: public artifact bodies, static-app files, and
     Ditto-signed releases and model weights.
   - **What Ditto keeps.** The page wrapper, publication state, short leases
     and revocations.
   - **How it is verified.** By challenge. Nodes are paid only for holding and
     retrieval that passes a challenge.
   - Gates pass. **4/5/4/5/2/4 = 43.**
2. **Static deployed-app hosting.**
   - **What miners do.** Host `index.html` projects only; Flask apps are
     excluded because of secrets.
   - **How it is verified.** Exactly like idea 1, but apps have no anonymous
     listing and live on `*.run.app`.
   - **Cost and benefit.** Scale-to-zero Cloud Run costs almost nothing, so the
     gain is moving apps onto their own origin (DITTO-1072).
   - Gates pass. **4/3/4/5/2/3 = 38.** A source for idea 1, not a separate
     competition.
3. **Signed release and model-weight mirror.**
   - **What miners do.** Mirror Desktop DMGs and ZIPs, local model weights
     (embeddinggemma, VAD) and benchmark fixtures among Desktop installs.
   - **How it is verified.** Exactly, by signatures and hashes.
   - **Demand** is near zero: about 2 downloads a release, and GitHub serves
     them for free.
   - Gates pass. **5/3/5/5/1/2 = 40.** Also a source for idea 1.
4. **Encrypted erasure-coded storage** (SN-473 Phase 1).
   - **What miners do.** Hold Ditto-keyed ciphertext under Reed–Solomon
     coding, checked by Merkle proofs.
   - **Value.** Storage absorbs well under 1% of emissions. Ditto is moving off
     Hippius (SN75) after 402 refusals and rate limits.
   - **Ownership.** SN-472.
   - Gates pass. **4/2/3/4/2/2 = 32.**
5. **CKKS-encrypted query-adapter compute** (SN-473 Phase 2, Omar's pilot).
   - **What miners do.** Run per-user adapters on encrypted queries.
   - **Cost and value.** About 10,000× plaintext cost and 378 MiB of keys per
     miner. It saves Ditto no compute while Ditto holds the key.
   - **Ownership.** SN-472.
   - Gates pass. **5/2/5/2/1/2 = 35.**
6. **Research grid.**
   - **What miners do.** Run batch jobs with small models on public or
     synthetic data: LongMemEval and DittoBench retrieval with local
     embeddinggemma or gemma3:4b, MLP training-set synthesis (backend#1412),
     and parameter sweeps.
   - **How it is verified.** A trusted re-run of a 1–5% sample, plus hidden
     known-answer jobs.
   - **Strengths and limits.** Macs are a good fit, but demand is sporadic, and
     results vary across chip families.
   - Gates pass. **3/2/5/4/3/3 = 36.**
7. **Public-corpus embedding and discovery index.**
   - **What miners do.** Embed public shares and profiles for a future
     discovery feature.
   - **How it is verified.** Redundant computation with tolerance.
   - **Limits.** There is no discovery product yet, and the corpus is 319
     shares.
   - Gates pass. **3/3/3/4/1/2 = 29.**
8. **Public-Ditto or public-space vector search for visitors.**
   - **Fails G1.** Visitors' queries are private, and query embeddings invert.
     Spaces cannot be published publicly in any case.
9. **Ditto Pulse: residential monitoring.**
   - **What miners do.** Watch the availability of Ditto's public endpoints
     from homes.
   - **How it is verified.** Ditto injects canary outages to catch nodes that
     misreport.
   - **Limits.** It is self-reported measurement, the pattern that lesson 1
     says gets spoofed. Its value is ops visibility that a cheap SaaS already
     sells.
   - Gates pass. **3/4/5/4/2/1 = 36.**
10. **Home Node.**
    - **What miners do.** The user's own Mac becomes their private Ditto
      server: local-first memory and user-held keys, which backend#2059 asks
      for.
    - **Strengths and limits.** It is a real product need. But a node serving
      only its owner gives the network nothing to verify, and paying emissions
      for it would pay for product use.
    - Gates pass. **1/2/5/4/3/2 = 29.** Better as a product track.
11. **Agentic public-content stewards.**
    - **What miners do.** Local agents write alt text, summaries and preview
      cards, and audit link rot on public shares and apps.
    - **How it is verified.** An LLM council judges the work. That is
      gameable, and the outputs are nondeterministic.
    - Gates pass. **2/3/3/3/2/2 = 27.**
12. **Residential fetch and unfurl workers.**
    - **Fails G1.** Which links a user saved is private, and unlisted links act
      as passwords.
    - Proxy egress also puts legal risk on the Mac owner.
13. **Miner Macs as SN118 screener or validator capacity.**
    - **Fails G1.** Benchmark seeds and datasets must stay secret from
      competitors until revealed. Evaluators must be trusted.
14. **Immutable archive or notary of public content.**
    - **Fails the unpublish requirement by design.** R is 0.
15. **Small-model inference market for public or synthetic prompts**,
    Darkbloom-style.
    - **Demand.** Ditto has none: its benchmarks call large hosted models.
    - **How it is verified.** Without a TEE it needs DiFR or TOPLOC-style
      checks, tuned per chip family.
    - Gates pass. **2/1/5/3/1/1 = 24.**

## 5. Shortlist and rerank

**Shortlist**: the top six by score that pass every gate.

| Rank | Idea | V | S | R | C | U | B | Total |
|---|---|---|---|---|---|---|---|---|
| 1 | Ditto Edge (1) | 4 | 5 | 4 | 5 | 2 | 4 | **43** |
| 2 | Signed release and model-weight mirror (3) | 5 | 3 | 5 | 5 | 1 | 2 | 40 |
| 3 | Static-app hosting (2) | 4 | 3 | 4 | 5 | 2 | 3 | 38 |
| 4 | Research grid (6) | 3 | 2 | 5 | 4 | 3 | 3 | 36 |
| 5 | Ditto Pulse (9) | 3 | 4 | 5 | 4 | 2 | 1 | 36 |
| 6 | CKKS adapter compute (5) | 5 | 2 | 5 | 2 | 1 | 2 | 35 |

**The rerank** applies four adjustments that a per-idea score cannot express.

1. **Merge ideas that share one mechanism.**
   - Ideas 2 and 3 are the same content-addressed holding and challenge
     machinery as idea 1, with a different object source.
   - Run them as **sources** inside one competition: public artifacts first,
     static apps second, signed releases and model weights third.
   - One lane, one ledger, one Desktop helper.
2. **Apply the precedents (§3).**
   - **Pulse falls below the research grid.** Self-reported measurement is the
     pattern lesson 1 says gets spoofed.
   - **Ditto Edge is reshaped by lessons 2–3.** Nodes become an origin tier
     behind Ditto's verifier and edge, not CDN servers, and organic traffic is
     never paid.
3. **Respect ownership.**
   - CKKS compute and encrypted storage belong to SN-472/473. They stay there.
   - Ditto Edge's identity, enrollment and ledger are built so those lanes can
     plug in later instead of duplicating it.
4. **Order by what unlocks the most later work.**
   - Ditto Edge stands up the whole Mac-network path: identity, sandboxed
     helper, challenges, ledger, Backroom.
   - It does so with the least risk: public bytes, exact checks, no product
     change.

**Final order**

1. **Ditto Edge.** Public artifacts, then static apps, then signed releases and
   model weights. Built in this ticket.
2. **Research grid.** The next lane, once Ditto has a steady public or
   synthetic batch workload. It reuses identity, enrollment and the ledger, and
   verifies by trusted re-runs of a sample with per-chip-family tolerance.
3. **Encrypted storage.** SN-473 Phase 1, owned by SN-472. A lane on the same
   machinery.
4. **CKKS compute.** SN-473 Phase 2, owned by SN-472. Worth doing only once
   user-held keys ship.
5. **Ditto Pulse.** Only if ops needs residential vantage points.

**Why Ditto Edge wins.** It ranks first on the brief's own criteria:

- **Mac mini:** a few watts, disk and a small uplink.
- **Desktop toggle:** a sandboxed helper with outbound-only networking.
- **Plugs into the API and production app in shadow:** anonymous reads of real
  public shares, with unpublishes detected from production.
- **Cost:** near zero.
- **Incentive-game resistance:** exact hashes, nonce-keyed challenges,
  re-download detection, revoked-object audits, and pay only for verified
  units.
- **Unpublish:** leases and revocation within bounded time.
- **No TEEs:** none anywhere.

**Its weakness is value, not safety.** That is why it starts in shadow, with a
dry run against production and a written kill criterion (§7).

## 6. The pick: Ditto Edge

### 6.1 What nodes hold, and what they never see

| Nodes hold | Nodes never see |
|---|---|
| The exact bytes of an assigned object, named by its SHA-256 | The share URL, share ID, title, owner, username or account |
| Its size and media type | Cookies, sessions, tokens, vault secrets, keys |
| A lease expiry | Anything from `friends`, `shared`, `private`, or an encrypted graph without its published public copy |
| Revocations for digests they held | Which viewer read what, or any viewer IP (nodes never serve browsers in shadow) |

**What counts as public.** An object qualifies only if the production origin
already serves exactly those bytes to an anonymous, cookieless GET. That is
`visibility=public` and `EncVersion=0`, or the plaintext public copy of an
encrypted artifact. A static app qualifies only if it has no server entry point.
Flask apps are never eligible, because their runtime holds secrets.

**What nodes are not.** They are not open gateways. A node answers only for
digests Ditto assigned to it, and only to Ditto (§3, lesson 5).

### 6.2 Shape: an origin tier behind Ditto's verifier

```
                    Ditto (centralized, trusted)
 production app ──► ingest + publication state ──► Platform edge lane
 (public shares)       (anonymous reads)            │  assignments, leases,
                                                    │  revocations, challenges,
                                                    │  hourly shadow ledger
                                                    ▼
                         outbound HTTPS only (signed requests)
                    ┌─────────────┬─────────────┬─────────────┐
                    │ Mac node A  │ Mac node B  │ Mac node C  │  (decentralized,
                    │ Desktop     │ headless CLI│ Desktop     │   untrusted)
                    └─────────────┴─────────────┴─────────────┘
```

- **Nodes open every connection.** They poll Platform (with a long poll), fill
  assigned objects from Platform, and answer challenges. No inbound ports, no
  UPnP, and no NAT traversal. This matches how Desktop's tool relays already
  reach Ditto.
- **In shadow nobody downstream reads from nodes.** Viewers keep getting every
  byte from today's origin.
- **Enforce mode (not built).** Ditto's edge would pull a public object from a
  node through the same outbound connection and check its SHA-256 before
  serving it. It would fall back to the origin on a mismatch or a timeout.
  - Browsers never connect to a home Mac, so viewer IPs never reach miners.
  - A home uplink only ever serves Ditto's edge.
  - This is the "permissioned verifier, open supply" hybrid the survivors
    converged on (§3, lesson 2).

### 6.3 Leases, revocation and unpublish

- **Leases.** An assignment carries a lease (900 s by default). A node renews
  every lease it holds by polling. An honest node deletes an object whose lease
  lapsed without renewal.
- **Revocation.** Revoking a digest is a Platform state change. Every poll
  from a node that held the digest carries it in `revocations`. The node
  deletes the bytes, acknowledges, and from then on must refuse challenges for
  it.
- **Unpublish detection, shadow.**
  - The dry-run ingester rechecks each source share anonymously. A 404 or 401
    means the share was unpublished, deleted, or made friends-only, and the
    ingester revokes the digest.
  - Detection latency is measured, as is revocation latency: unpublish
    detected → last honest node refuses.
- **Unpublish detection, enforce (needs a decision).**
  - The backend would publish a signed feed of public objects and tombstones.
  - The edge would also refuse any digest without a fresh lease.
  - This needs a signing key in Secret Manager, which is Peyton's or infra's
    call (§9).
- **Auditing compliance.** After revoking a digest, Platform keeps sending the
  nodes that held it **revoked challenges**: ordinary possession challenges for
  that digest.
  - The only correct answer is a refusal.
  - A valid proof means the node still holds revoked content. That is a
    violation: the node's window becomes ineligible, and it takes a strike.
- **What we can promise.** Within one lease plus one poll interval of
  detection, no Ditto surface and no honest node serves an unpublished object.
  A node that ignores revocations is caught by its next revoked challenge.
- **What we cannot promise.** That every copy is deleted. That holds for any
  public content: every visitor who loaded the page could have saved it. It is
  the same limit SN-473 states for storage ("deletion from miners can never be
  verified"). The user-facing copy must not claim otherwise.

### 6.4 Challenges and the shadow ledger

All challenges are issued by Platform, lazily, when a node polls.

| Kind | What Platform asks | Correct answer | Verified how |
|---|---|---|---|
| `possession` | a random 32-byte nonce and a byte range of an assigned object | `sha256(nonce ‖ bytes[offset:offset+length])` | Platform recomputes it from its own copy |
| `retrieval` | a byte range of up to 256 KiB | the exact bytes | byte equality, with time to answer recorded |
| `revoked` | a possession challenge for a digest the node has since had revoked | refusal | any proof is a violation |

**Re-download detection.** Platform logs every fill. A challenge answered by a
node that re-fetched the object after the challenge was issued is marked
`refilled` and fails. A node therefore cannot hold nothing and fetch on demand
from the one place it can get the bytes.

**The hourly ledger**: one row per node per window, insert-once.

- **Counts:** challenges issued, passed, failed, missed, refilled, and revoked
  violations.
- **Held GiB-hours:** acknowledged, unrevoked assignments, intersected with the
  window.
- **`availability`** = passed ÷ issued.
- **`eligible`** requires all of:
  - at least `min_challenges` issued;
  - no integrity failure;
  - no revoked violation;
  - availability ≥ `availability_floor`.
- **`verified_units`** = held GiB-hours × availability. It is 0 when the window
  is ineligible.
- **`hypothetical_share`** = a node's verified units ÷ the window's eligible
  total. This is what the node *would* earn of an Edge track share, as SN-387
  requires.
- **`weight_eligible` is `false`**, pinned by a database `CHECK` constraint.
  Moving any of this into weights needs a migration that drops it. That PR
  cannot be mistaken for anything else.

**Pricing for any future enforce mode** follows SN-473 §6.3:

- Price verified units at a market reference: Storj's operator rate, $1.35
  per TB-month, times a stated premium.
- Cap the track's share.
- **Burn the surplus.** Never pay for capacity Ditto does not use.

### 6.5 Gaming and mitigations

| Attack | Mitigation | What is left |
|---|---|---|
| Lazy node: holds nothing, fetches when challenged | Nonce-keyed range proofs plus re-download detection; nodes don't know source URLs | A node fetching from a colluding peer (below) |
| Outsourcing to a colluding node | Pay per verified unit, so collusion adds no value. Placement spreads replicas across nodes. Retrieval challenges record time. | Fewer real replicas than paid for. Acceptable for a cache tier with an origin fallback; durability never depends on nodes. |
| Tampered bytes or wrong range | Exact proof or byte check against Platform's copy; a strike and an ineligible window | None |
| Ignoring revocations to keep held bytes | Revoked challenges | None, beyond the inherent copy limit (§6.3) |
| Sybil farms | Pay grows with verified units, not identities. Placement prefers the least-replicated objects. Node count and per-node quota caps apply. | Determined sybils add capacity rather than stealing pay. |
| Self-dealing traffic (Saturn's main fraud) | Organic traffic is never paid; only challenges Platform issues count | None |
| Replaying or forging requests | Ed25519 request signatures over method, path, body hash, time and nonce; nonces consumed once in Postgres; a ±120 s clock window | None |
| Telling challenges apart from real traffic | No real traffic reaches nodes in shadow. In enforce mode, edge pulls and challenges share one path and format. | Timing analysis in enforce; measure it in Phase 2 |
| Binary or self-description spoofing (SN27, Lium) | Nothing is scored from what a node says about itself. Hardware fields are labels, not inputs. | None |
| Validator or operator capture | Platform is the oracle in shadow, with the same trust as today's ledger. Enforce would make the fold deterministic, as for every track. | Governance (SN-473 T11, T14) |

### 6.6 Identity: device keys now, hotkeys and pools later

- **Device key.** Every node holds an **Ed25519 device key**, generated
  locally and never uploaded.
  - `node_id = "edn_" + hex(sha256(public_key))[:32]`.
  - Node's built-in `crypto` signs Ed25519 with no added dependency, and
    Platform already verifies Ed25519 (`cryptography`).
- **Optional hotkey binding.** A node may present an SN118 hotkey binding: the
  hotkey signs `ditto-edge-bind:v1:{node_id}:{hotkey}:{issued_at}`, checked by
  the existing sr25519 verifier (`attestation.verify_signature`). In shadow
  this only labels the node, so the ledger can show what a hotkey would earn.
- **Both identity futures in SN-473 §5.6 fit:**
  - one hotkey per Mac under Null consensus, for the pilot;
  - many device keys bound to one pool hotkey, long term.
- **Desktop users need no TAO and no wallet** to run the shadow.

### 6.7 Mac mini and Desktop

- **Nodes.** There are two implementations of the same `edge/v1` protocol,
  pinned by shared conformance vectors:
  - a headless reference node in Python (`ditto-edge node run`), for Mac minis
    without Desktop;
  - a Desktop helper in Node.js.
- **Isolation.**
  - The Desktop helper runs as a separate child process under a deny-default
    `sandbox-exec` profile: writable only in its own cache directory, with
    network allowed.
  - It has no access to the account vault, passkeys, or the Ditto session. Its
    only credential is its device key.
  - This follows the existing `pi-sandbox.cjs` pattern and SN-473 §5.2.
- **Resources.**
  - A hard disk quota (2 GiB default in shadow) and a 15% free-space floor.
  - It pauses on battery, in Low Power Mode, and at a serious thermal state.
  - Work is small: mostly hashing ranges of small files. An M4 idles at about
    4 W.
- **UX.**
  - Off by default, and hidden behind a beta switch until Peyton decides.
  - One screen says what the Mac stores ("only public files Ditto users chose
    to publish"), what it never receives, the quota, the status, and
    "**Shadow test: no earnings yet**".
  - Turning it off deletes the cache and tells Platform to reassign the
    objects.

### 6.8 Shadow plumbing

**Shared protocol, `packages/ditto-edge-protocol`.**

- Wire models, request signing, proof functions, and pure placement and
  ledger functions.
- An in-memory coordinator used by tests and by the dry run.
- Conformance vectors that the Desktop helper must reproduce.

**Platform edge lane.**

- Revisioned settings with `mode: off|shadow`, written with the usual
  expected-revision and confirmation phrase. **There is no `enforce` value.**
- Tables for nodes, objects, assignments, request nonces, challenges, and the
  hourly ledger.
- Node endpoints under `/edge/v1`: enroll, poll, fill, acknowledge, answer,
  leave.
- Admin reads and guarded admin writes: settings, object ingest, revoke.
- With the lane `off`, every node endpoint refuses, and nothing is assigned or
  challenged.

**Backroom MCP reads.** These expose settings, nodes and the ledger, as SN-387
and `CLAUDE.md` require.

**Dry run.** `ditto-edge dry-run` runs the coordinator and N local nodes in one
process against prod-shaped traffic.

- **Content.** Real public share IDs fetched anonymously from
  `api.heyditto.ai`, or a synthetic generator that matches production's size
  and type mix.
- **What it exercises.** Assignment, fills, challenges, revocation (including
  recheck-driven unpublish detection), and adversarial nodes: lazy,
  revocation-ignoring and tampering.
- **What it needs.** No deployment, no secret, and no admin write.

**Not built in this ticket.**

- Any validator fold.
- Any track id in `KNOWN_TRACKS` or `PAYABLE_TRACKS`.
- Any Cloudflare Worker.
- Any backend feed.
- Any production setting change.

### 6.9 What a Mac owner's machine is exposed to

- **Content.** The node stores public content that other Ditto users
  published, and it cannot vet that content. Because nodes never serve it to
  the public, a node is a cache, not a host.
- **Abuse.** It follows Ditto's moderation: a takedown is a revocation.
- **Legal.** Whether a home node qualifies for DMCA §512 caching protection is
  untested and needs counsel before enforce mode (§9).
- **The Desktop copy must say plainly** that the Mac stores public files other
  people published.

## 7. Shadow plan, metrics and kill criteria

| Phase | What happens | Who decides | Success metrics | Kill criteria |
|---|---|---|---|---|
| 0 (this ticket) | Design. Lane code merged **off**. Dry run against prod-shaped traffic. Desktop helper behind a beta switch. | Merged under the normal review gate | Dry run: honest nodes pass ≥ 99% of challenges; every adversarial node is caught; revocation compliance 100% within one lease plus one poll | Any adversary class goes uncaught in the dry run |
| 1 | Lane set to `shadow` in production. 5–10 team Macs. Synthetic objects plus the team's own public shares. | **Peyton** (Backroom write) | Honest false-fail rate < 1%; median challenge answer < 2 s; Desktop helper under 1% CPU on average; no user-visible change | Honest false-fail rate ≥ 1%; any Desktop stability or battery regression |
| 2 | 20–50 opt-in volunteer Macs, labeled shadow, no earnings. All eligible public shares mirrored. Red-team week. | **Peyton** | Every red-team class caught; revocation latency within bound; volunteer retention over 2 weeks | Red-team success; volunteers churn because the helper is a nuisance |
| 3 | Enforce options: signed manifest feed, a Ditto edge Worker serving from nodes with fallback, a capped track share with surplus burned, hotkey binding or pools | **Peyton**, plus counsel | Written decision | No useful demand after Phase 2: retire the lane and keep the machinery for SN-473 or the research grid |

## 8. How this relates to existing work

- **SN-472/473** ([#3503](https://github.com/ditto-assistant/ditto-subnet/pull/3503)):
  - Same threat model, same rule ("nothing on a miner may need to stay secret
    from its owner"), same Desktop helper principles, and the same identity
    futures.
  - Ditto Edge is the public-content lane; SN-473 owns the encrypted storage
    and compute lanes.
  - They plug into the same node identity and ledger instead of building a
    second Mac network.
  - Nothing here redoes SN-473's measurements.
- **Omar's pilot** (#3483–#3485). The Edge reuses its patterns:
  - unpredictable verification, keyed by Platform randomness;
  - a fallback to the origin that never trusts an unverified miner byte;
  - a usage ledger that pays nothing.

  Its per-user adapters stay off miners, as the pilot itself says.
- **SN-237 / SN-387 / SN-261.**
  - The Edge is a shadow competition that pays 0 whatever its share, with an
    insert-once ledger, hypothetical shares, and Backroom reads.
  - It adds no track to the validator registry until enforce mode is decided.
- **DITTO-2023 (KubeTEE)** and **DITTO-2543 (ZK proofs).** Neither is needed:
  content addressing makes verification exact without TEEs or ZK.
- **SN-166 (Desktop bounties).** Unrelated mechanism. The Desktop helper is not
  a bounty.

## 9. Decisions for Peyton

1. **Flip the lane to `shadow` in production** for Phase 1. This is a Backroom
   write.
2. **Show the Desktop toggle** to volunteers, by un-hiding the beta switch.
3. **Approve an enforce path.** It needs four things:
   - a backend signed public-object feed, with a signing key in Secret Manager;
   - an edge Worker on `dittousercontent.com`;
   - a track share with surplus burned;
   - counsel on DMCA and on paying consumers.
4. **Identity:** per-Mac UIDs under Null (subnet-wide), or pools (SN-473 §5.6).

## 10. Sources

**Internal:**

- backend, ditto-app, infra, ditto-harness at `main` on 2026-11-12 (anchors
  inline);
- SN-473 design, [#3503](https://github.com/ditto-assistant/ditto-subnet/pull/3503);
- Omar's pilot, #3483–#3485;
- SN-237, SN-261, SN-363, SN-387, SN-166, DITTO-2023, DITTO-2543;
- backend issues #1360 and #2059, and ditto-app issue #1427.

**External, selected; the full sourced research is in the PR description:**

- Bittensor subnet and Etao docs, and tao.media on Null consensus (2026-10-15).
- Compute Horde SDK; Lium [#1328](https://github.com/Datura-ai/lium-io/pull/1328);
  the NI SN27 post-mortem.
- Filecoin Saturn [L1 node](https://github.com/filecoin-saturn/L1-node) and
  [FAQ](https://github.com/filecoin-saturn/L1-node/blob/main/docs/faq.md);
  FilecoinTLDR 2024; Checker/Station
  [repo](https://github.com/CheckerNetwork/desktop).
- The IPFS [Trustless Gateway spec](https://specs.ipfs.tech/http-gateways/trustless-gateway/)
  and [service-worker gateway](https://github.com/ipfs/service-worker-gateway);
  Netcraft on IPFS phishing (2026-09-29); Cloudflare's IPFS gateway closure
  (2024-08-14).
- [Bad Bits denylist](https://badbits.dwebops.pub/) and
  [IPIP-383](https://specs.ipfs.tech/compact-denylist-format/).
- [DiFR](https://arxiv.org/abs/2511.20621), [TOPLOC](https://arxiv.org/abs/2501.16007),
  [mlx-batch-invariant](https://github.com/waseemh/mlx-batch-invariant),
  [Thinking Machines on nondeterminism](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/).
- Darkbloom ([The Register](https://assets.theregister.com/2026/08/24/202624/?td=keepreading),
  2026-08-24) and a [critique](https://imiel.dev/blog/darkbloom-private-inference-apple-silicon).
- [vec2text](https://arxiv.org/abs/2310.06816) and [vec2vec](https://arxiv.org/abs/2505.12540).
- [Apple Mac mini power](https://support.apple.com/en-us/103253).

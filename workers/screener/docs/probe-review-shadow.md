# Probe-driven reviewer: report-only shadow prototype

Status: prototype, report-only. It never contacts Platform, signs nothing, and
cannot change a screening outcome, a quarantine, a ban, or a policy rotation.
Results here are shadow evidence measured offline against adjudicated
policy-v13 outcomes.

## Why

The v13 source-review pipeline runs L1 (240 steps), L2 analyst (256), an L3
critic, and the L3 adjudicator (128) as sequential reasoning-model calls. A
rejected artifact takes a median 25 minutes (p90 37) and a pass a median 10
(p90 28), measured from the 79 confirmed-violation rejections and 52 passes
in the corpus below. Reviews also park on provider 429s or come back
inconclusive. Oracle-only passes missed real violations (SN-245, SN-247,
SN-231) because the v8 behavioral oracle checks one nonce round-trip, not the
invariants.

The probe reviewer turns invariants into black-box properties of the running
submission: if the model writes `-5`, the harness must serve `-5`. A verdict
is a replayable stimulus plus deterministic predicates, so a fresh harness
re-execution verifies it instead of more model layers.

## Design

```text
archive (SHA-verified)
  -> build in the gate's build-context normalizer (linux/amd64)
  -> zero-token static triage   fanout_discovery.semantic_discovery hints
  -> zero-token battery          18 scripted-model probes, I1-I4, I6-I8
  -> one reviewer agent          probe tools; confirm/refute leads
  -> deterministic replay        fresh harness, same stimuli, same predicates
```

Code: `ditto_screener/probe_review/`.

- `scripted_gateway.py` is a standard-library sidecar on the same isolated
  Docker network and aliases as `fake_gateway.py` (chat completions,
  Responses, embeddings, the scorer-shaped tool route, and the openrouter.ai
  TLS shim). The reviewer dictates what "the model" and each tool return
  (`rules`/`turns`/`default`, raw or malformed tool-call arguments, 429/500).
  Every model, embedding, and tool request is appended to a transcript.
- `sandbox.py` reuses the gate's runtime contract verbatim: non-root,
  read-only root, bounded tmpfs, memory/CPU/PID limits, all capabilities
  dropped, `--internal` network, `_gateway_runtime_env`, and the
  openrouter.ai shim certificates. A session persists so each reviewer tool
  call is one CLI process. Submitted `RUN` steps have network access, so
  builds go to an isolated daemon (`PROBE_REVIEW_DOCKER_HOST`, a same-host
  socket that must report rootless, like the gate's builder; its egress
  guard cannot be verified from the client). Any other daemon is used only
  with an explicit `--allow-host-daemon`.
- `battery.py` is the lead-independent battery. Each item dictates model
  output and checks what was served or executed: sign, hyphen, direction, and
  minor-unit fidelity (I4); abstention and confirmation-word re-ask, draft
  replacement (I3, with a neutral-draft control); malformed arguments to `{}`
  (I4/I6); an undelivered call reported as executed (I6); an unexpected valid
  call, an explicitly requested identical repeat, five distinct calls, a sticky
  veto after a declined coordinate lookup with the coordinate seeded in the
  user's records, a pinned `tool_choice`, and an overridden no-call decision
  (I7); system-instruction and full-receipt retention (I2); and seeded versus
  unseeded shape for an unrelated user (I8).
- `checks.py` holds deterministic predicates over the served response, the
  authoritative tool-endpoint ledger, and the recorded model requests.
- `tools.py` is the reviewer surface: `run_case` (with `/seed`
  preconditions), `show` (exact prompts the harness sent), `batch` (code
  mode, `foreach` templating), `mutate` (paraphrase, tool rename/reorder,
  case-id format, `bench_version`, system prompt, sign flip), `diff`,
  `search`/`read` for localization, and `verdict` (validation, citation
  check, replay).
- `reviewer_prompt.md` is the reviewer contract, including the calibrated
  safe harbors (W4 advisory hints, W7 declared critics, W8 normalization, W11
  duplicate suppression). Policy-judgment calls go to `leads_unresolved`, not
  findings.
- `agent_loop.py` is a minimal single-agent loop for shadow on the Ditto
  Router: OpenAI-compatible tool calling over the same surface, with a hard
  request/token/cost ledger. It was not exercised against a live model here
  (see Method).

## Method

- Corpus (Backroom read tools only): every policy-v13
  `source-review-confirmed-violation` rejection submitted since 2026-10-01
  with cited lines (79), every v13 scored/live pass in the same window (52),
  and the three oracle-only misses with documented mechanisms: Sky v4 and v6
  (SN-245) and topbot-p31 (SN-247). All 134 archives were downloaded through
  `get_screening_artifact` and SHA-256 verified.
- Reviewers were blind. Each was a fresh agent with only the session
  directory and the reviewer contract. It had no MCP access and no production
  reason, and it was told not to look up history.
- The reviewer model in this run was Claude Sonnet via local subagents
  driving the CLI. No Router API key was read, so the shadow `agent_loop`
  path is unit-tested but unmeasured. Token counts are the subagent totals,
  not split into input and output. Cost is bracketed at Sonnet list price.
- Infrastructure: a shared Apple-silicon workstation running OrbStack with
  Rosetta for amd64. Docker `run` latency on that host was 1-5 minutes under
  load, so build and start times here overstate production. Battery and
  reviewer probe latency is the comparable number.
- Scoring: "caught" means a replay-verified finding on a known violation.
  "Exact" means the verified mechanism is the one production cited. "Different"
  means a verified, policy-plausible mechanism other than the cited one.
  A verified finding on a production pass is reported individually, with its
  precedent, and is not counted as a false positive or a miss until an
  operator rules.
- Mechanism labels and pass assessments are the operator's reading of the
  production reason against the replayed finding. They are recorded per
  session in `review_meta.json`. "Policy-sensitive" marks a finding whose
  charge depends on an unsettled judgment (cap size, retrieval budget,
  identity guard). "Strict" excludes policy-sensitive findings and findings
  assessed as a likely artifact.

## Results

Snapshot: 132 of 134 archives built and battery-run, and 132
reviewed by a blind agent. The two unbuilt archives are Python images whose
build exceeded the 90-minute timeout on the shared host (listed at the end).
The aggregator is `scripts/probe_review_report.py`, and the full per-agent
table is in the draft PR description.

### Catch rate on known violations

| | Count | Share |
|---|---:|---:|
| Known violations reviewed (v13 rejects + 3 oracle misses) | 80 | |
| Any replay-verified finding | 62 | 78% |
| Strict (no policy-sensitive or doubtful finding) | 50 | 62% |
| Production mechanism reproduced (exact or same class) | 28 | 35% |
| Different, verified mechanism | 34 | |
| No verified finding | 18 | |

All three oracle-only misses were caught with the documented mechanism.
Sky v6 (SN-245) and topbot-p31 (SN-247) were caught by the zero-token
battery alone: a seeded sticky-veto item and a status-word re-ask item with a
neutral control. Sky v4 (SN-245) was caught when the reviewer tuned the
seeded trigger from source. The named regressions were also caught with the
production mechanism: leona v1 (I4 sign trim), cointaker v6 and v13 (I4
malformed arguments to `{}`), kestrel v1/v2, Thief v29/v31, stratum,
overhorizon v1, ditto v12, and agiorin v15 (I5, with a paraphrase control).
lucas_v33 was missed. Its decision-memory forced-tool path was never
triggered.

By production invariant (an artifact can carry several):

| Invariant | Rejects | Any verified finding | Same invariant |
|---|---:|---:|---:|
| I1 | 4 | 4 | 0 |
| I2 | 20 | 15 | 5 |
| I3 | 21 | 14 | 3 |
| I4 | 24 | 22 | 9 |
| I5 | 12 | 11 | 2 |
| I6 | 6 | 6 | 2 |
| I7 | 33 | 27 | 15 |
| I8 | 1 | 1 | 0 |

I4 and I7 are the strong classes. Their violations are black-box properties
(served value differs from the model's value, or a valid call does not run),
and the battery hits them at zero tokens. I3 and I5 are weak. I3 needs the
exact uncertainty trigger the harness keys on (length finish, parsed
abstention, contact decline). I5 recipes are often switched by a learned
classifier that the stub embeddings do not route. The high "any finding"
rate on I5 and I1 rejects comes from different, simpler violations in the
same artifact, not from the production mechanism.

### Why violations were missed

Each of the 18 misses has the production mechanism in its note. They
group into four causes:

1. The trigger was never constructed. Oversized identifier-linked records
   (Thief v32), event-count thresholds (blink-python, τaowolf v3), scoped
   documents over 4000 tokens (Pentagon1028), exhaustion recovery
   (Artemis v25), contact-decline review (comeτ-4.0), decision-memory
   forced tools (lucas_v33), and a deciding receipt continuation
   (aceron v7). The reviewer is cheap per probe but does not
   construct large or stateful stimuli unless a battery item or triage hint
   points at them.
2. The mechanism was proven but parked as policy. Artemis v22's fixed
   3-call memory-search cap was proven and parked before the "cap of 4 or
   fewer" precedent was added to the contract. logan v6's first-repeat
   hold on an explicitly requested repeat was proven and parked as W11. Love v1 and dan0l v15/v17
   parked receipt and record caps.
3. Battery coverage was added after the run. Abstain synthesis (piggy v7),
   echo re-ask (τaowolf v4), and length-finish discard (dan0l v17). A
   post-hoc rerun is stored separately as `prepare.posthoc.json` and is not
   counted.
4. A safe harbor was applied too leniently. lets_642's draft review was
   accepted as a declared W7 critic.

### Production passes

52 passes were reviewed. 19 carry a replay-verified finding. None
is counted as a false positive or a miss until an operator rules.

- Candidate production misses: 5. Each matches an existing rejection
  precedent.
  - King v8 `4a431bd8` (I2): prompt-only echo-collapse dedupe drops a later,
    distinct, updated response. It is the same class as the ditto v12 and
    Thief v29 rejections.
  - Harry v19 `ad88c1a4` (I2): `dedupe_group` drops the newer updated record.
  - clear v43 `0ec00c6d` and v44 `99b087eb` (I5): an identifier-keyed "change
    statement wins" revision recipe, the SN-215 change-wins precedent. Two
    blind reviewers found it independently.
  - Thief v30 `25c8699f` (I6): a save_memory call whose endpoint delivery
    fails is still self-reported as executed. This matches the kestrel v2
    rejection, though the trigger is a transport fault.
- Contested: 14, all policy-sensitive.
  - Oversized-record budget skips: 10. A record over the packet or grounding
    budget is silently dropped with no pointer, while a smaller control
    reaches the model. The 10 are Pentagon25, aiforge v4/v5, overhorizon v2,
    doctor v8, logan v7, Pig v1, om-parce, and Gryffindor v18/v19.
  - Receipt head-cut at 6000 characters: dan0l v10 and v14. The other
    dan0l siblings parked the same cap, so this is reviewer variance.
    Ruled after the run (2026-10-22): a fixed receipt cap with a truthful
    truncation marker is not a violation on its own, and ten bans that
    rested only on it were reversed. Both findings are therefore false
    positives, and the reviewer prompt now carries the ruling.
  - Fixed turn cap of 5 ordinary + 2 receipt turns: lucas_v56.
  - A reserved identity field blocking a user-typed `session_id`: logan v5.

crown-v189 `dcf2f827`, a named pass, came back clean. Its fixed 6-tool-turn
cap was parked as unresolved, the same cap class that was charged on King v7.

The oversized-record cluster is the main policy question this run surfaced.
Production rejected Thief v32, Love v1 and Pentagon1028 for withholding
oversized record text from the deciding model, yet passed ten artifacts with
the same black-box behavior. Either W3 bounded retrieval covers a silent
whole-record skip, and the rejections are inconsistent, or it does not, and
L2 did not see these skips. One ruling settles about a dozen verdicts in this
corpus.

### Battery alone

The battery runs in a median of 17 s per artifact with no model
tokens. It raised at least one lead on 60 of 80 violations and on
27 of 52 passes, so it is not a classifier by itself. Most pass
leads were calibration artifacts the reviewer refuted: a planner call
consumed the scripted reply, or an authorizer was given the wrong output
format. Its value is zero-token reproduction of black-box classes (sign,
hyphen, malformed arguments, unreachable-call reports, requested-repeat
vetoes, status-word re-asks) and a starting point for the reviewer.

### Cost and time

| | Probe reviewer | Production L1-L3 |
|---|---:|---:|
| Median wall time, rejects | 3.0 min (p90 5.9) | 25.1 min |
| Median wall time, passes | (same reviewer) | 10.2 min |
| Median reviewer tokens | 132k | not exposed |
| Cost per review (Sonnet list) | $0.41-$0.57 | not exposed |

Reviewer time excludes build and battery. Builds here took a median of
49 s on an overloaded workstation. Production wall time is
submission-to-verdict, including queueing. Backroom exposes only the L2/L3
caps ($25, 5M input / 1M output tokens), not actual per-artifact spend, so
a dollar comparison is not possible from read tools.

## Limitations

- Reviewers were Claude Sonnet subagents driving the CLI, not the
  `agent_loop` Router path. Token counts include the reviewer contract and
  file reads.
- The mechanism labels and pass assessments come from a single operator, not
  an adjudicated ruling.
- Reviewer variance on policy-judgment items is real. Sibling artifacts with
  the same code got different charges (dan0l receipt cap, King v7 vs
  crown-v189 turn caps).
- The citation check proves the cited line exists. It does not prove that
  line is the mechanism.
- Probes share one gateway with the running harness. Background inference
  the harness starts on its own could consume a scripted reply inside a
  probe's window and cannot be told apart from it there; replay on a fresh
  harness and paired controls are what catch that nondeterminism.
- Several battery items were added mid-run (abstain synthesis, echo re-ask,
  length-finish, oversized-record tail, a seed-first calibration). Earlier
  sessions did not get them.
- The gateway's embeddings are hashed bag-of-words. Harnesses that route by
  a learned classifier over embeddings take arbitrary lanes, which hides
  some I5 recipes (ditto v13).

## Recommendation

Do not replace L1-L3 with the probe reviewer. It reproduces the production
mechanism on only 35% of rejects, and on passes it raises
contested findings that need a policy ruling first.

Do run it as a report-only lane next to L2 for passes:

1. Get an operator ruling on the oversized-record budget-skip class and the
   five candidate misses above, then encode the rulings in
   `reviewer_prompt.md` as precedent, as was done for the cap-of-4 rule.
2. Run the battery and one reviewer on every v13 pass through
   `agent_loop.py` on the Ditto Router. Attach replayable findings to the
   L2/L3 dossier as evidence, not as a verdict. At about $0.50 and 3 minutes
   per artifact, this is a cheap second opinion on the outcome that is
   hardest to audit.
3. Close the trigger gaps that dominate the misses with battery items:
   oversized identifier-linked records, event-count thresholds, contact
   decline, JSON-format authorizers, and a real embedding model in the
   gateway for classifier-routed harnesses.
4. Re-measure on the next 50 v13 outcomes before deciding whether verified
   battery findings in the black-box I4/I6/I7 classes can short-circuit L3.
   I4/I6/I7 findings already carry a deterministic replay.

Follow-ups outside this prototype:

- Backroom read tool for actual L2/L3 tokens and spend per attempt.
- The gate's `fake_gateway.py` serves tools on port 11436, a different
  origin from inference. lucas_v33 checks that the tool origin equals the
  inference origin, so it behaves differently under the gate than under this
  gateway. Confirm which origin the live scorer uses and align the gate.

Not built or not reviewed (host capacity): blackbox v1 (python, reject), blackbox-ditto-miner v1 (python, reject).

## Gate battery shadow (report-only)

`probe_battery_mode: shadow` on a review-settings revision makes the gate run the
zero-token battery against the image it has already built and screened. It runs
after the v13 shadow block, once the decision is settled. The battery:

- runs as a killable `python -m ditto_screener.probe_review gate-battery`
  subprocess. The environment is limited to PATH/HOME/LANG/TMPDIR/
  XDG_RUNTIME_DIR plus the gate's own docker binary and daemon, with no
  Platform, provider or node credentials;
- is bounded by `probe_battery_timeout_seconds` (30-600, default 120) and by the
  remaining lease minus 30 s. The gate removes the session's containers and
  network on every exit path;
- prints only lead names and counts (`item_id`, `invariant`, status, encoding,
  probe count, time), never prompts, transcripts or harness output.

The worker submits that summary to `POST /api/v1/screener/agent/{id}/probe-battery`
alongside the shadow review, while the attempt is still running. Platform stores
it in `screener_probe_batteries` only when the attempt, artifact and an enabling
revision all match. The record is never read by any decision, queue or policy
path. Operators read the latest 100 under `probe_battery_observations` on
`get_screener_review_settings` / the Backroom review-settings panel.

Checksum compatibility: an `off` battery is hashed without its two keys on both
sides, so every existing revision keeps verifying. Enabling `shadow` binds both
keys and therefore requires the upgraded worker release.

## Gate reviewer agent (report-only)

`probe_review_mode: shadow` runs the reviewer agent after the battery, in the
same killable child, on nodes that hold the reviewer key. It requires
`probe_battery_mode: shadow`, and Platform refuses a revision without it.

- **Key:** the dedicated Ditto Router key lives in
  `projects/ditto-app-dev/secrets/screener-probe-review-router-key`.
  - It was created with `ditto endpoints keys create probe-review-shadow
    --gcp-secret …` and is never printed.
  - The `probe-review-shadow` endpoint has memory recall and record off. Its
    daily spend cap (20B Ditto tokens, about $20) is the second billing guard,
    enforced by the Router.
  - Terraform grants the fleet's x509 identities read access only.
  - Ansible `screener_fleet_probe_review_enabled: true` materializes the key to
    `secrets/probe-review-router-key` (mode 0400, worker-owned) and sets
    `SCREENER_PROBE_REVIEW_KEY_FILE`. Without that file the reviewer never runs,
    whatever the revision says.
- **Limits:**
  - per artifact: `probe_review_max_cost_usd` (default $1, at most $5), enforced
    by the agent ledger with fail-closed untracked spend;
  - wall time: `probe_review_timeout_seconds` (default 420 s), also bounded by
    the lease left after the decision;
  - the agent's own 48-request, 40-tool and 30-probe caps.

  The child prints the battery line first, so a reviewer killed at the budget
  still leaves the battery.
- **What the child sees:** the archive path, for source and triage, and the key
  file path. It gets no Platform, node or other provider credentials. Only
  `SSL_CERT_FILE` and proxy variables are added to its environment, for the
  Router call.
- **What is stored:** a bounded `review` object next to the battery:
  - status (`complete`, `no_verdict`, `budget_exhausted`, `untracked_spend`,
    `skipped`, `timeout`, `error`);
  - whether the replay verified;
  - verified invariants, and up to 4 findings with up to 4 `path:line`
    citations each;
  - requests, tokens, cost and time.

  Claims, prompts and transcripts never leave the node.

Rollout:

1. Apply the Terraform IAM grant through the protected plan/apply.
2. Converge one node with `screener_fleet_probe_review_enabled: true`.
3. Publish a revision for that node's exact scope with
   `probe_battery_mode: shadow` and `probe_review_mode: shadow`.
4. Compare `probe_battery_observations[].review` against L1/L2/L3 outcomes for
   the next 50 v13 attempts before widening.

Capability-only converge (no vault needed) for an already-converged primary that
has the conversation key, mirroring `conversation-shadow.yml`:

```bash
ansible-playbook -i infra/ansible/inventory/screener-ci-partition.yml \
  infra/ansible/playbooks/probe-review-shadow.yml --check --diff \
  -e ansible_ssh_private_key_file=$HOME/.ssh/id_ed25519
```

Drop `--check --diff` to apply. It re-renders the materializer with the
reviewer key added, restarts the materializer, asserts the key is non-empty,
0400 and worker-owned, and installs the `probe-review.conf` worker drop-in.
Workers pick it up on their next release restart.

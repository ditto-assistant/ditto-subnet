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
  call is one CLI process.
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

<!-- RESULTS -->

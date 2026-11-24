# Direct Product Improvements for Ditto (`heyditto.ai`) from SN118 Miner Agents

**Date:** 2026-11-24  
**Scope:** Read-only source audit of publicly released Bittensor Subnet 118 (SN118) champion agent harnesses across DittoBench v13–v18 (locked model: `openai/gpt-oss-20b`, `reasoning_effort=medium`), mapped directly against Ditto's production backend (`ditto-assistant/backend` `main` @ `517ed132`) and orchestration workspace (`ditto-assistant/heyditto-stack`).

---

## 1. Executive Summary & Methodology

SN118 miners compete on a king-of-the-hill (KOTH) benchmark where every harness runs the **same locked model** (`openai/gpt-oss-20b`, `reasoning_effort=medium`). On DittoBench v18 (activated `2026-11-11T23:44:02Z`), the benchmark scales each seed's tool catalog to **1,400–2,000 real tools** (~100k–200k tokens, exceeding the model's 131,072-token window), enforces a **$1.00 / 350-case run budget** (with priced-cost-v2 scaling steeply above $0.50), adds **multi-call tool encounters**, and grades composites as `0.5 × datagen + 0.1 × each counted generator column` (v16–v18 dueling split; within datagen, tool and memory are equally weighted).

For comparison, the stock starter kit's classic path scores **0.000** on v18 (every call refused with HTTP 400 from menu overflow), and its v18 default Jev-hybrid reference baseline scores **0.263** (`tool_mean=0.527`, `memory_mean=0.276` on seed 42; [`miners/dittobench-starter-kit/BASELINES.md`](../miners/dittobench-starter-kit/BASELINES.md)). Publicly released SN118 v18 champion submissions achieve **0.657–0.729 composite** with **`tool_mean` up to 0.993** and **`memory_mean` up to 0.988** (and earlier v14–v17 kings reached **0.963–0.987**).

### How source code was obtained and verified
- **Public Platform API only (read-only):** All leaderboards, scores, metadata, and released champion source tarballs were fetched from `https://dittobench.ai/api/v1/public` (`/leaderboard`, `/ledger-epochs`, `/bench/config`, `/bench/rollout`, `/bench/timeline`, `/miners/{hotkey}`, `/agent/{agent_id}/summary`, `/agent/{agent_id}/scores`, and `/agent/{agent_id}/artifact`).
- **41 publicly released champion artifacts** were downloaded and **SHA-256 verified** against the Platform API digest before static source inspection (no untrusted miner code was executed). They span **v13–v18**, **10+ distinct hotkeys**, and **4 independent architectural lineages**:
  1. Multi-stage Python agent-memory harnesses (`dan0l`, `beast-d41`, `Hades`, `crown-*`, `Tessellate`, `DrPresident`, `horizon`, `kaelith`, `tensorworker`)
  2. Native C++20 implementation (`blinkrazor v2/v3`)
  3. Standalone Python authorization-and-routing harness (`lets_640 v1`)
  4. Rust `ditto-harness` fork with a 4-stage local neural MLP lane router (`ditto v7`)
- **Production verification:** Every candidate improvement was checked directly against `ditto-assistant/backend` `main` (commit `517ed132`, 2026-11-24) so nothing Ditto already ships is misreported as missing. Benchmark-specific artifacts (e.g., `ANSWER:` line formatters, hardcoded 140-app static prefix tables, or narrow invoice/trip regexes) were **explicitly excluded**.

---

## 2. Verified Source Agent Index

Every agent cited in this document was verified live via `https://dittobench.ai/api/v1/public/agent/{agent_id}/{summary,scores,artifact}` on 2026-11-24. Scores are the median across the 3-validator quorum (`score_count=3`) for the listed `bench_version`.

| Agent | Version | UID | Miner Hotkey (SS58) | Agent UUID | Bench | Composite | Tool Mean | Memory Mean | Artifact SHA-256 (prefix) | Finalized (UTC) |
| --- | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| `beast-d41` | v18 | 222 | `5CBD7DW3w7pyheNSTUWLLU6bFNPxEbMoPRbkbEGcKbKgkFyv` | `d6d5c157-f724-46db-a7c8-e24c498f919f` | v18 | `0.729095` | `0.970000` | `0.988000` | `a4c0aaabea6f8a1d` | 2026-11-13 07:13 |
| `beast-d41` | v19 | 222 | `5CBD7DW3w7pyheNSTUWLLU6bFNPxEbMoPRbkbEGcKbKgkFyv` | `871a93d4-f578-4aed-80ad-497e6dd99008` | v18 | `0.707457` | `0.956667` | `0.969333` | `5d75d2966c003158` | 2026-11-13 11:56 |
| `dan0l` | v59 | 149 | `5DveEYviiPkDqq8kdXxgsf6tyboiRs27yxXV4hAXoGZU7Mtv` | `a06c97e5-bb98-4a0c-911e-04d1deb6a70b` | v18 | `0.657048` | `0.993333` | `0.960000` | `7353a32f6e6ebcd3` | 2026-11-13 14:09 |
| `crown-v228` | v1 | 22 | `5GNmn5BeybKcMV45E67PudrsbdHYNUbeNqHS6GF8vzBviMh2` | `673386d0-cc54-4acc-986c-f0d2663615ec` | v18 | `0.624786` | `0.934000` | `0.964000` | `212288958f1c0504` | 2026-11-11 18:34 |
| `crown-v220` | v1 | 22 | `5GNmn5BeybKcMV45E67PudrsbdHYNUbeNqHS6GF8vzBviMh2` | `b44ad4f7-dc2e-4c48-99af-342296b2b26f` | v17 | `0.971714` | `0.960000` | `0.972000` | `8d84a62f8765d0b2` | 2026-11-10 00:38 |
| `dan0l` | v46 | 149 | `5DveEYviiPkDqq8kdXxgsf6tyboiRs27yxXV4hAXoGZU7Mtv` | `252b5761-fb91-4f42-abbd-85e50006721b` | v17 | `0.942214` | `0.982000` | `0.972000` | `623e12ea3a8461d3` | 2026-11-07 23:50 |
| `crown-v221` | v1 | 22 | `5GNmn5BeybKcMV45E67PudrsbdHYNUbeNqHS6GF8vzBviMh2` | `ca429d06-1e47-4ce0-aa24-2345695db4eb` | v17 | `0.939095` | `0.980000` | `0.948000` | `5073d48093b20a37` | 2026-11-10 08:00 |
| `beast-d41` | v1 | 222 | `5CBD7DW3w7pyheNSTUWLLU6bFNPxEbMoPRbkbEGcKbKgkFyv` | `3e59be1e-808c-487f-8e64-9f59ac547a5d` | v17 | `0.928908` | `0.973333` | `0.940000` | `3c49c680e5fbaee9` | 2026-11-07 19:23 |
| `Tessellate` | v2 | 233 | `5Dkc95Bj3t74azUKrrQqDegq852XfkJuHExUuuGhNYZykneJ` | `19c596b5-8cbb-434a-8d04-aa77ed6d9e23` | v17 | `0.829951` | `0.986667` | `0.964000` | `cb9df08a9866a230` | 2026-11-07 02:24 |
| `dan0l` | v41 | 149 | `5DveEYviiPkDqq8kdXxgsf6tyboiRs27yxXV4hAXoGZU7Mtv` | `557684da-683c-4a06-815d-645bcd18f4f1` | v16 | `0.987000` | `1.000000` | `0.948000` | `a2ffd8b31de47ea1` | 2026-11-04 22:50 |
| `dan0l` | v42 | 149 | `5DveEYviiPkDqq8kdXxgsf6tyboiRs27yxXV4hAXoGZU7Mtv` | `e6959038-555c-4a49-a3e2-5377d020e79c` | v16 | `0.983000` | `0.980000` | `0.972000` | `06c2aab9e00f7204` | 2026-11-05 12:31 |
| `Hades` | v25 | 153 | `5DPzFGYVnzeknNknRneFftYoJrwboJVnXp8K27AiAJRzRwnK` | `30fa08cf-4d1d-4101-b159-b4c1c90094c9` | v15 | `0.963265` | `0.986667` | `0.946000` | `5dc63f1d1d9b74f5` | 2026-11-03 08:11 |
| `Hades` | v24 | 153 | `5DPzFGYVnzeknNknRneFftYoJrwboJVnXp8K27AiAJRzRwnK` | `a0fc00a9-580d-422b-9860-ed99b1124e9d` | v14 | `0.972815` | `0.990000` | `0.958000` | `8a497b8e592396a5` | 2026-11-02 19:25 |
| `DrPresident` | v23 | 175 | `5E7VQE3HAxTKnM25aYMuFZyhQGggkedfumJsbX532eaL8BQE` | `8474358a-0592-40f6-8c70-4cec0212513c` | v14 | `0.964000` | `0.987200` | `0.948000` | `9f3f21bc438352f6` | 2026-10-31 15:14 |
| `blinkrazor` | v2 | 138 | `5GW478LHZCKfzpour2WCneUn6suuHLsQgxM78EVt56kPybsJ` | `dc2a8f4f-a877-4ea0-ae44-a1dd6118666a` | v13 | `0.790694` | `0.812667` | `0.820667` | `bdcf36ff2a1d2608` | 2026-10-19 16:25 |
| `lets_640` | v1 | —* | `5ECg59rv1wM4a3dY7Aka2bGRCKKCQoHmasAhAyaAJeqemR7Z` | `f185f7ec-ed72-4c3b-86b9-5fffa07d6096` | v13 | `0.678523` | `0.695133` | `0.754000` | `1b8312fb76cc01bc` | 2026-10-03 16:28 |
| `ditto` | v7 | 154 | `5ECYdR9iBW2sZxZN55CMP4kG4XNvsm4v9EeAnkp4JMfFBrE8` | `fdc99e79-a7c5-4a5d-bc9e-f8d0f4756e0a` | v13 | `0.543475` | `0.753936` | `0.593000` | `4326ea2ed2434335` | 2026-09-23 08:05 |

*\*`lets_640` hotkey was not present on the current active UID metagraph map returned by `/leaderboard?bench_version=13..18`.*

Every artifact tarball is publicly retrievable via:
`GET https://dittobench.ai/api/v1/public/agent/<agent_id>/artifact` (returns a short-lived presigned download URL and full SHA-256).

---

## 3. Ranked Product Improvements (Impact vs. Effort)

| Rank | Improvement | Category | Product Impact | Effort | Primary Source Agents | Target Ditto Component (`ditto-assistant/backend`) |
| ---: | --- | --- | --- | --- | --- | --- |
| **1** | **Mutation-Aware Tool Idempotency Ledger & Delivery-State Tracking** | Tool Execution / Reliability | Very High | Low–Med | `beast-d41 v18` (`d6d5c157`), `dan0l v59` (`a06c97e5`), `dan0l v41` (`557684da`) | `pkg/api/v2/chatv2-tools.go`, `pkg/services/toolengine/tool.go` |
| **2** | **Zero-LLM App-Aware Tool Catalog Pruning + Cached TF-IDF Safe Harbor** | Tool Selection / Latency & Cost | Very High | Low–Med | `dan0l v59` (`a06c97e5`), `beast-d41 v18` (`d6d5c157`), `Tessellate v2` (`19c596b5`) | `pkg/api/v2/chatv2-tools.go`, `pkg/codemode/catalog.go`, `pkg/services/toolengine/catalog.go` |
| **3** | **Near-Duplicate Copy Collapsing & Chronological Connected-Component Seed Ordering** | Prompt Assembly / Memory | High | Low | `dan0l v59` (`a06c97e5`), `beast-d41 v18` (`d6d5c157`), `dan0l v42` (`e6959038`) | `pkg/services/memorystore/supersede.go`, `pkg/api/v2/chatv2_memory_context.go`, `pkg/api/v2/chat_prompt_context.go` |
| **4** | **Hub-Damped Entity-Anchor Multi-Hop Retrieval + Corpus-Bounded Typo Repair** | Memory Retrieval | Very High | Med–High | `dan0l v59` (`a06c97e5`), `beast-d41 v18` (`d6d5c157`), `Hades v24` (`a0fc00a9`), `blinkrazor v2` (`dc2a8f4f`) | `pkg/services/retrieval/hybrid.go`, `pkg/services/retrieval/subject_graph.go`, `pkg/services/retrieval/composite.go` |
| **5** | **Multi-Step Tool-Encounter Completeness Guardrails (Empty-Read Fallback & Superseded-Link Follow-Through)** | Agent Loop / Tool Use | High | Medium | `beast-d41 v18` (`d6d5c157`), `beast-d41 v19` (`871a93d4`) | `pkg/api/v2/chatv2.go`, `pkg/api/v2/chatv2-tools.go` |
| **6** | **Evidence-Grounded Entity Alias Resolution & Transparent Alias Attribution** | Memory Write & Retrieval | High | Medium | `dan0l v59` (`a06c97e5`), `beast-d41 v18` (`d6d5c157`), `crown-v228 v1` (`673386d0`), `lets_640 v1` (`f185f7ec`) | `pkg/services/insight/facts.go`, `pkg/services/retrieval/subject_graph.go`, `pkg/api/v2/chatv2_memory_mentions.go` |
| **7** | **Per-Turn User Tool-Revocation Gate (Quote-Masked + Lazy Confirmation)** | Safety / User Control | Med–High | Low–Med | `Hades v25` (`30fa08cf`), `Hades v24` (`a0fc00a9`), `lets_640 v1` (`f185f7ec`) | `pkg/api/v2/chatv2-tools.go`, `pkg/services/toolengine/router.go` |
| **8** | **Inference & Embedding Transport Self-Healing (Timeout-Adaptive `max_tokens`, Arg Pre-Validation, Batch-Split Recovery)** | Failure Handling / Infra | Medium | Low | `dan0l v59` (`a06c97e5`), `beast-d41 v18` (`d6d5c157`), `DrPresident v23` (`8474358a`) | `pkg/services/llm/openrouter/openrouter.go`, `pkg/services/llm/http-client.go`, `pkg/services/memorystore/embed_retry.go` |

---

## 4. Detailed Findings & Traceability

### #1 — Mutation-Aware Tool Idempotency Ledger & Delivery-State Tracking
- **What it is:** A turn-scoped tool execution wrapper (`ToolExecutor`) that tracks a 5-state delivery ledger (`completed`, `delivery_unknown`, `delivered_failed`, `not_delivered`, `blocked_before_execution`) keyed by canonical `(tool_name, sorted_JSON_args)`, alongside a monotonic `changes` mutation counter:
  - **Write duplicate protection:** If an identical non-read call already `completed` OR left the host with `delivery_unknown` (timeout/5xx after request transmission), a repeat attempt is blocked on first retry with an explicit explanatory note (`REPEAT_NOTE`). The model can still intentionally override on a second confirmation if the user truly requested a duplicate action.
  - **Mutation-invalidated read dedup:** Identical read calls are deduplicated *only* while `read_after[key] == self.changes` (no mutating call ran in between) and the prior result did not indicate an active in-progress job. The moment any write increments `changes`, subsequent identical reads execute fresh.
  - **Transport-error write safety:** Separates pre-delivery network errors (`ConnectError`, `ConnectTimeout`, `PoolTimeout` → `not_delivered`, safe to retry) from post-send failures (`TransportError` / 5xx → `delivery_unknown`, never silently retried on writes).
  - **Hallucinated memory-ID pre-check (`unheld_ids`) & in-turn write-through (`follow`):** Rejects synthetic prompt markers (e.g. `M4`) passed to `fetch_memories` or `search_memories_in_subjects` with an actionable error before querying storage, and immediately reflects in-turn `save_memory`/`update_memory` mutations in the active turn's read view.
- **Why it helps Ditto users:** In `ditto-assistant/backend`, `executeToolCalls` (`pkg/api/v2/chatv2-tools.go:1826`) tracks `seenPairIDs` for `search_memories` deduplication and `codeModeProposalCollector` for workflow cards, but has no general per-turn tool-call idempotency ledger or `delivery_unknown` write guard across connected apps (Gmail, Slack, Linear, Composio, user MCP servers). Adding this stops accidental double-sends/double-creates on retries, eliminates redundant identical read loops, and recovers cleanly when a model passes an invalid `pair_id` to `fetch_memories`.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2-tools.go` (`executeToolCalls`, `dispatchOne`, `case "fetch_memories"` at L3224–L3269)
  - `ditto-assistant/backend`: `pkg/services/toolengine/tool.go` (already classifies read vs. write via `Tool.Kind()`)
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2.go` (turn state passed across rounds in `streamPromptV25`)
- **Traceability:**
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, `5CBD7DW3w7pyheNSTUWLLU6bFNPxEbMoPRbkbEGcKbKgkFyv`, bench v18, composite `0.729095`, tool `0.970`, mem `0.988`, SHA-256 `a4c0aaabea6f8a1d...`): `app/tools.py:34-44` (5 delivery states & `REPEAT_NOTE` / `UNHELD_IDS_NOTE`), `app/tools.py:61-62` (`call_identity`), `app/tools.py:89-196` (`ToolExecutor.execute`, `send`, `_deliver`), `app/tools.py:225-260` (`unheld_ids`, `follow`).
  - **`dan0l v59`** (`a06c97e5-bb98-4a0c-911e-04d1deb6a70b`, UID 149, `5DveEYviiPkDqq8kdXxgsf6tyboiRs27yxXV4hAXoGZU7Mtv`, bench v18, composite `0.657048`, tool `0.993`, mem `0.960`, SHA-256 `7353a32f6e6ebcd3...`): `app/tools.py`, `app/agent.py:144`.
  - **`dan0l v41`** (`557684da-683c-4a06-815d-645bcd18f4f1`, UID 149, bench v16, composite `0.987`, tool `1.000`, mem `0.948`, SHA-256 `a2ffd8b31de47ea1...`): `app/tools.py`.

---

### #2 — Zero-LLM App-Aware Tool Catalog Pruning + Cached TF-IDF Safe Harbor
- **What it is:** A sub-millisecond, zero-LLM catalog selector designed for workspaces with hundreds or thousands of connected tools:
  - Builds a SHA-256 LRU-cached inverted TF-IDF index (`CatalogIndex` + `IndexCache`) over `tool.name + tool.description` (`weight(t) = log(1 + N / (1 + df[t]))^2`, cosine-normalized).
  - Automatically infers each external tool's `AppKey` (display name + normalized regexes) from toolkit prefixes (`GITHUB_CREATE_ISSUE`) or description framing (`"Google Calendar: ..."`), without needing a manually maintained static list.
  - Always keeps Ditto's own core/memory tools; hides external app toolkits not mentioned in the user's message (`outside()`); groups large tool families by lead/object word (`FAMILY_MIN=4`) and keeps only the top `NAMED_FAMILY_CAP=12` tools per mentioned family; and appends a global **`SAFE_HARBOR_TOP_K=3`** TF-IDF fallback across the *entire* catalog so implicit requests ("check my flight itinerary") still expose the matching tool.
  - On pure memory-recall turns (`records_tools`), trims further to memory tools + explicitly named apps + 3 safe-harbor tools.
- **Why it helps Ditto users:** On `ditto-assistant/backend` `main`, `selectPromptV25Tools` (`pkg/api/v2/chatv2-tools.go:879-957`) concatenates every enabled built-in, Google, and MCP tool directly into the prompt whenever a user is not in Code Mode (`mcpToolCount <= codeModeToolThreshold` or explicit direct-tools preference). Meanwhile, inside Code Mode, `pkg/codemode/catalog.go` (`Catalog.Search`) uses flat substring/token overlap rather than IDF-weighted ranking. Porting `CatalogIndex` + safe-harbor pruning cuts prompt-token overhead and TTFT on direct-tool turns (where tool schemas are often the largest single prompt block) and sharpens `search_tools` precision inside Code Mode without adding an embedding or LLM call.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2-tools.go` (`selectPromptV25Tools` L879–L957, `codeModeForUser` L1103)
  - `ditto-assistant/backend`: `pkg/codemode/catalog.go` (`Catalog.Search`, `SearchRanked`)
  - `ditto-assistant/backend`: `pkg/services/toolengine/catalog.go` (`CatalogEntry`, `AppID`, `Group`)
- **Traceability:**
  - **`dan0l v59`** (`a06c97e5-bb98-4a0c-911e-04d1deb6a70b`, UID 149, `5DveEYviiPkDqq8kdXxgsf6tyboiRs27yxXV4hAXoGZU7Mtv`, bench v18, composite `0.657048`, tool `0.993`, mem `0.960`, SHA-256 `7353a32f6e6ebcd3...`): `app/finder.py:145-269` (`CatalogIndex`, `IndexCache`, `action_tools`, `records_tools`), `app/menu.py:10-85` (`MENU_CAP=48`, `NAMED_FAMILY_CAP=12`, `SAFE_HARBOR_TOP_K=3`, `ranked`).
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, `5CBD7DW3w7pyheNSTUWLLU6bFNPxEbMoPRbkbEGcKbKgkFyv`, bench v18, composite `0.729095`, tool `0.970`, mem `0.988`): `app/menu.py:58-81` (`trim_menu`, `records_menu`).
  - **`Tessellate v2`** (`19c596b5-8cbb-434a-8d04-aa77ed6d9e23`, UID 233, `5Dkc95Bj3t74azUKrrQqDegq852XfkJuHExUuuGhNYZykneJ`, bench v17, composite `0.829951`, tool `0.987`, mem `0.964`, SHA-256 `cb9df08a9866a230...`): `app/availability.py:149-169` (`available_tools` mention-gating predecessor).

---

### #3 — Near-Duplicate Copy Collapsing & Chronological Connected-Component Seed Ordering
- **What it is:** Two cheap, deterministic post-retrieval prompt-packing passes before memories enter the model's context (`app/packet.py`):
  1. **Normalized copy merging (`merge_copies`):** Strips short lead-in labels (`"Note: ..."`, `"Reminder: ..."`, ≤8 words before `:`) and outer quotes, case-folds whitespace, and collapses near-identical duplicate saved memories (`>= 24 chars`) into a single prompt entry while preserving all duplicate IDs in metadata.
  2. **Connected-component chronological arc ordering (`code_components` + `arc_order`):** Uses union-find over shared extracted identifiers/codes (and/or subject/topic links) to group related retrieved memories into connected components. Within each component, memories are sorted **chronologically oldest→newest**; across components, blocks stay ordered by their highest-ranked seed position.
- **Why it helps Ditto users:** Ditto's backend already has `markSupersededSeedRoots` (`pkg/services/memorystore/supersede.go:42-92`), which groups seed roots greedily by embedding cosine `>= 0.72` and re-seats them newest-first with a `SupersededBy` tag. However, `supersede.go` explicitly skips unembedded / sealed / lexical-only hits, does not collapse near-identical duplicate saves (e.g., repeated notes or cross-imported chat/Obsidian copies), and does not group identifier-linked multi-step histories (like an invoice, PR, ticket, or project arc) into a chronological narrative block. Combining copy collapsing + identifier-component chronological ordering makes state progressions immediately legible to every answering model while reclaiming context tokens.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/services/memorystore/supersede.go` (`markSupersededSeedRoots`)
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2_memory_context.go` (`preparePromptLongTermMemories`)
  - `ditto-assistant/backend`: `pkg/api/v2/chat_prompt_context.go` (`seedMemories` serialization)
- **Traceability:**
  - **`dan0l v59`** (`a06c97e5-bb98-4a0c-911e-04d1deb6a70b`, UID 149, bench v18, mem `0.960`, SHA-256 `7353a32f6e6ebcd3...`): `app/packet.py:59-87` (`saved_words`, `merge_copies`), `app/packet.py:93-140` (`codes_of`, `code_components`, `arc_order`), `app/packet.py:170-179` (`value_closure`).
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, bench v18, composite `0.729095`, mem `0.988`): `app/packet.py`.
  - **`dan0l v42`** (`e6959038-555c-4a49-a3e2-5377d020e79c`, UID 149, bench v16, composite `0.983`, mem `0.972`, SHA-256 `06c2aab9e00f7204...`): `app/packet.py`.

---

### #4 — Hub-Damped Entity-Anchor Multi-Hop Retrieval + Corpus-Bounded Typo Repair
- **What it is:** A structured, zero-LLM retrieval arm (`UserIndex` + `Retriever`) fused with dense embeddings via RRF (`RRF_K = 60`):
  - **Typed entity extraction (`extract_values`):** Extracts emails (`e:`, also indexing `first last` from the local part), structured alphanumeric codes (`c:`), quoted phrases (`q:`), multi-word proper names (`n:` + initial+surname `i:`), and capitalized entity tokens (`k:`) filtered dynamically by corpus case statistics (`lower_df <= cap_df / 2`) so ordinary sentence-initial words never pollute the graph.
  - **Corpus-bounded 1-edit typo repair (`within_one_edit`, `fix_typo`):** Repairs unknown query words (`df == 0`, len ≥ 4) within 1 Damerau-Levenshtein edit (substitution, adjacent transposition, insertion, deletion) toward the highest-DF term in the user's *own* memory vocabulary — recovering misspelled names, projects, and acronyms before lookup.
  - **Hub-damped, budget-capped multi-hop propagation (`rank`, `max_depth=3`):** Applies explicit degree damping (`hub_factor = min(1.0, HUB_VALUES / len(values))^0.5`), per-type fanout ceilings (`TYPE_FANOUT`), long-document edge restriction (documents >2,500 chars only traverse exact `e/c/q` edges), a per-target hop budget (`room = top_seed - hop_received[target]`) so multi-hop diffusion can never drown out direct matches, and a `HOP_CONJUNCTION_BONUS` when multiple distinct entities converge on the same target memory.
  - **IDF coverage floor (`coverage_seeds`):** Guarantees top length-normalized IDF-coverage memories enter the pool even when no proper entity anchor exists.
- **Why it helps Ditto users:**
  1. Ditto's own retrieval research ([`teaching-memory-to-find-itself.md` on heyditto.ai/blog](https://heyditto.ai/blog/teaching-memory-to-find-itself)) documented that Personalized PageRank over the subject↔memory graph *dropped* Recall@1 from `0.68` to `0.42–0.54` because diffusion flooded hub memories. SN118 miners independently solved hub flooding via degree damping + fanout caps + per-target hop ceilings, enabling reliable 2–3 hop recall (0.960–0.988 `memory_mean` on v18!).
  2. Ditto's current lexical arm (`pkg/services/retrieval/hybrid.go`, Postgres `websearch_to_tsquery('english', $1)`) has no typo repair for user-specific proper nouns or codes; a single mistyped surname misses the lexical arm completely.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/services/retrieval/subject_graph.go` (`subjectGraphCandidatesSQL`, currently `one-hop-v1`)
  - `ditto-assistant/backend`: `pkg/services/retrieval/hybrid.go` (`LexicalRetrieve`, `DefaultFusionRRFK`)
  - `ditto-assistant/backend`: `pkg/services/retrieval/composite.go` (V2 composite + candidate pool)
- **Traceability:**
  - **`dan0l v59`** (`a06c97e5-bb98-4a0c-911e-04d1deb6a70b`, UID 149, bench v18, mem `0.960`): `app/memory/retrieval.py:115-130` (`within_one_edit`), `195-251` (`hub_factor`, `name_like`, `extract_values`, `fix_typo`), `325-446` (`coverage_seeds`, hub-damped `rank`).
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, bench v18, composite `0.729095`, mem `0.988`): `app/memory/retrieval.py`.
  - **`Hades v24`** (`a0fc00a9-580d-422b-9860-ed99b1124e9d`, UID 153, `5DPzFGYVnzeknNknRneFftYoJrwboJVnXp8K27AiAJRzRwnK`, bench v14, composite `0.972815`, mem `0.958`): `app/memory/retrieval.py`.
  - **`blinkrazor v2`** (`dc2a8f4f-a877-4ea0-ae44-a1dd6118666a`, UID 138, `5GW478LHZCKfzpour2WCneUn6suuHLsQgxM78EVt56kPybsJ`, bench v13, composite `0.790694`, mem `0.821`, SHA-256 `bdcf36ff2a1d2608...`): native C++20 `include/retrieval.hpp` and `src/retrieval.cpp`.

---

### #5 — Multi-Step Tool-Encounter Completeness Guardrails
- **What it is:** Lightweight, advisory-only turn-completion checks (`app/complete.py`) over the current turn's tool receipts before accepting a final answer or mutating write (each fires at most once per turn, only adds an instruction note, and leaves the final decision to the model):
  - **Empty-read sibling fallback (`untried_read` / `UNTRIED_HOLD_NOTE`):** When a read tool for a named app returns an empty result (`0 results / nothing found`) while other read tools on the *same* app remain untried (e.g., searched Slack messages but didn't list threads/channels, or searched Gmail inbox without checking drafts), hold any premature write or final "not found" reply once and offer only that app's remaining read tools.
  - **Unread named app (`unread_app`):** If the user explicitly names a connected app, the model only ran reads on *other* sources, and zero calls targeted that app, offer only that app's read-only tools for one follow-up step.
  - **Superseded / errata URL follow-through (`unread_link`, `unread_pointer`, `asked_page`):** If a completed tool result explicitly states that its content is `"superseded / out of date / replaced"` or points to a `"correction / errata / amendment"` URL that hasn't been fetched yet, offer only `read_links` so the model reads the authoritative page before answering.
  - **Scoped caveat for Ditto's async jobs (`app/followup.py` + `app/jobs.py`):** `beast-d41` also tracks short-lived async job IDs (`jobs.py`) and intercepts premature *"I'll keep monitoring it"* deferrals (`followup.py`). In Ditto's backend, long-running Cloud Run `execute_agent_job` / workflow runs intentionally hand off to the Tasks UI (`chatv2-tools.go:564`: *"Do not poll repeatedly"*), so synchronous polling **must NOT** be applied to Ditto Code Cloud Run jobs — only the `JOB_HOLD_NOTE` pattern (holding a downstream write that depends on a short-lived MCP/tool operation's output) transfers cleanly.
- **Why it helps Ditto users:** Eliminates three of the most common multi-step connected-app failure modes: premature "I couldn't find anything" after trying only one search endpoint, creating duplicate items without checking sibling lists, and answering from stale wiki/status pages that explicitly link to a newer revision.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2.go` (agent loop end-of-turn & pre-write hook in `streamPromptV25`)
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2-tools.go` (`read_links` L2295, connected app tool metadata)
- **Traceability:**
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, bench v18, composite `0.729095`, tool `0.970`, mem `0.988`): `app/complete.py:14-245` (`unread_app`, `unread_link`, `unread_pointer`, `untried_read`, `asked_page`), `app/followup.py:1-35`, `app/jobs.py:13-162`.
  - **`beast-d41 v19`** (`871a93d4-f578-4aed-80ad-497e6dd99008`, UID 222, bench v18, composite `0.707457`, tool `0.957`, mem `0.969`, SHA-256 `5d75d2966c003158...`): `app/complete.py`, `app/jobs.py`.

---

### #6 — Evidence-Grounded Entity Alias Resolution & Transparent Attribution
- **What it is:** Resolving nicknames, messaging handles (`@handle`), and short names to their canonical person/entity *only when grounded in the user's own records*:
  - **Write-time / deterministic cue extraction (`crown-v228` `coverage.py`, `dan0l` `retrieval.py:alias_anchors`, `lets_640` `expand_named_aliases`):** Detects explicit co-mention cues (*"goes by"*, *"known as"*, *"nicknamed"*, *"a.k.a."*, quoted short names on short notes) where the alias token is a corpus-consistent proper name and not another person's name.
  - **Read-time alias expansion & transparent weaving (`dan0l`/`beast-d41` `aliases.py` + `packet.py:with_full_names`):** Pulls records that mention *only* the alias (`alias_records`), drops low-signal chatter (`chatter`), seats alias records adjacent to the entity's canonical records (`weave_ties`), and annotates the connection explicitly (`nameTies` / `"Full Name (nick)"`) rather than silently mutating stored text.
- **Why it helps Ditto users:** Real users constantly switch between full names, first names, nicknames, and Slack/Telegram/Discord handles across months of conversations. Moving the alias extraction into Ditto's asynchronous dreaming/subject-sync pipeline (`pkg/services/insight/`) gives zero-latency alias expansion at read time, solving recall misses on people/projects referred to by handle or nickname.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/services/insight/facts.go` & `build.go` (persist verified `(subject_id, alias, source_pair_id)` edges during dreaming)
  - `ditto-assistant/backend`: `pkg/services/retrieval/subject_graph.go` (match query tokens against `subject_aliases` alongside `subjects.embedding`)
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2_memory_mentions.go`
- **Traceability:**
  - **`dan0l v59`** (`a06c97e5-bb98-4a0c-911e-04d1deb6a70b`, UID 149, bench v18, mem `0.960`): `app/memory/aliases.py:1-124`, `app/memory/retrieval.py:309-323`, `app/packet.py:287-319`.
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, bench v18, mem `0.988`): `app/memory/aliases.py`, `app/memory/count_check.py:1-69`.
  - **`crown-v228 v1`** (`673386d0-cc54-4acc-986c-f0d2663615ec`, UID 22, `5GNmn5BeybKcMV45E67PudrsbdHYNUbeNqHS6GF8vzBviMh2`, bench v18, composite `0.624786`, mem `0.964`): `DECLARATION.md` rows 28–30 (`app/memory/coverage.py`, `app/packet.py` `nameTies` / `weave_ties`).
  - **`lets_640 v1`** (`f185f7ec-ed72-4c3b-86b9-5fffa07d6096`, `5ECg59rv1wM4a3dY7Aka2bGRCKKCQoHmasAhAyaAJeqemR7Z`, bench v13, composite `0.678523`, mem `0.754`): `agent2.py:1511-1585` (`expand_named_aliases`).

---

### #7 — Per-Turn User Tool-Revocation Gate (Quote-Masked + Lazy Confirmation)
- **What it is:** Enforcing per-message negative user constraints (*"don't search the web"*, *"answer from memory only"*, *"draft it, don't send it"*) in the tool dispatcher rather than relying solely on the answering model's instruction-following:
  - **Quote-masked deterministic pre-filter (`lets_640`):** Masks quoted/code spans (except exact tool names) so forwarded text or document titles cannot falsely trigger a revocation, preserves conditional scope (`"if ... don't ..."`), and flags unambiguous revocations immediately.
  - **Lazy two-stage confirmation (`Hades`):** Runs *only* if the model actually emits a tool call (zero overhead on turns with no tool calls, or when no negative cue appears in the user's message): Stage 1 classifies `NONE` / `ALL` / revoked tool names treating message + catalog as untrusted data; Stage 2 confirms YES/NO on the specific attempted tool; fail-open on error; returns an explicit `"Not sent: user requested not to use <tool>"` tool result.
- **Why it helps Ditto users:** Ditto routes across many models (including fast/cheap Auto-routed models and `gpt-oss` / Gemini / open-weight models) that still ignore negative tool prohibitions inside long threads. A lightweight negative-cue gate in `dispatchOne` enforces user intent deterministically at execution time without extra latency on ordinary turns.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/api/v2/chatv2-tools.go` (`dispatchOne` L1877 alongside `AgentToolPolicy` / `googleGate`)
  - `ditto-assistant/backend`: `pkg/services/toolengine/router.go`
- **Traceability:**
  - **`Hades v25`** (`30fa08cf-4d1d-4101-b159-b4c1c90094c9`, UID 153, `5DPzFGYVnzeknNknRneFftYoJrwboJVnXp8K27AiAJRzRwnK`, bench v15, composite `0.963265`, tool `0.987`, mem `0.946`, SHA-256 `5dc63f1d1d9b74f5...`): `app/revocation.py:1-105`.
  - **`Hades v24`** (`a0fc00a9-580d-422b-9860-ed99b1124e9d`, UID 153, bench v14, composite `0.972815`, tool `0.990`, mem `0.958`): `app/revocation.py:1-105`.
  - **`lets_640 v1`** (`f185f7ec-ed72-4c3b-86b9-5fffa07d6096`, `5ECg59rv1wM4a3dY7Aka2bGRCKKCQoHmasAhAyaAJeqemR7Z`, bench v13, composite `0.678523`, tool `0.695`): `agent2.py:824-1173` (`ToolAuthorization`, `_mask_authorization_quotes`, `resolve_tool_authorization`).

---

### #8 — Inference & Embedding Transport Self-Healing
- **What it is:** Battle-tested transport and payload resilience patterns developed under 350-case concurrent evaluation runs:
  - **Timeout-adaptive `max_tokens` shrink:** When a reasoning-model call times out, reduce `max_tokens` (`× 0.4`, floor `1200`) on retry instead of repeating the exact same runaway generation budget (`app/llm.py:287-289`).
  - **Separate quick-failure accounting & wedged HTTP pool reset:** Track sub-5s transient drops separately from full retry attempts, respect `Retry-After` on 429/503, and rebuild the underlying HTTP client after 3 consecutive transport failures (`app/llm.py:228-237, 277-326`).
  - **Pre-execution JSON/NaN tool-argument validation & provider schema-rejection recovery:** Reject non-JSON/NaN tool arguments before dispatch and return provider tool-validation errors (`"tool call validation failed"`) into the loop so the model self-corrects instead of aborting the turn (`app/llm.py:79-121`).
  - **Embedding batch-split fallback & finite-norm guard:** If an embedding batch returns a mismatched count, retry item-by-item before failing the batch, and reject zero/NaN norms (`app/memory/embedding.py:52-74`).
- **Why it helps Ditto users:** Reduces visible mid-turn chat failures on OpenRouter/BYOK reasoning routes and protects bulk memory/Passport imports from failing a whole batch on one bad record.
- **Ditto component mapping:**
  - `ditto-assistant/backend`: `pkg/services/llm/openrouter/openrouter.go`, `pkg/services/llm/http-client.go`, `pkg/api/v2/chatv2.go`
  - `ditto-assistant/backend`: `pkg/services/memorystore/embed_retry.go`, `pkg/services/llm/googai/embeddings-google.go`
- **Traceability:**
  - **`dan0l v59`** (`a06c97e5-bb98-4a0c-911e-04d1deb6a70b`, UID 149, bench v18, tool `0.993`): `app/llm.py:79-149, 228-337`, `app/memory/embedding.py:52-101`.
  - **`beast-d41 v18`** (`d6d5c157-f724-46db-a7c8-e24c498f919f`, UID 222, bench v18, composite `0.729095`): `app/llm.py`, `app/memory/embedding.py`.
  - **`DrPresident v23`** (`8474358a-0592-40f6-8c70-4cec0212513c`, UID 175, `5E7VQE3HAxTKnM25aYMuFZyhQGggkedfumJsbX532eaL8BQE`, bench v14, composite `0.964`, tool `0.987`, mem `0.948`): `app/llm.py`.

---

### Exploratory Watchlist (Not Yet Ranked for Immediate Shipping)
- **Local Multi-Stage Neural Lane Router (`ditto v7`, `fdc99e79-a7c5-4a5d-bc9e-f8d0f4756e0a`, UID 154, `5ECYdR9iBW2sZxZN55CMP4kG4XNvsm4v9EeAnkp4JMfFBrE8`, bench v13, composite `0.543475`, Rust):** Trains tiny CPU MLP ensembles (`stage1..stage4`, ~1–3 MB each in `fixtures/router/`) over the query embedding + corpus statistics to predict `ACT / RECALL / CONVERSE` (with conservative `TAU_ACT = 0.70`) and skip unnecessary retrieval or tool bloat on pure conversational turns (`src/router/mod.rs:1-120`). Worth revisiting once Ditto has labeled production turn-type telemetry; right now Ditto's existing 4-feature logistic seed gate (`pkg/api/v2/chatv2_seed_gate.go`, AUC `0.84`) already captures much of the recall-gating win at near-zero maintenance cost.

### What We Deliberately Excluded (Benchmark-Specific Tricks)
To keep Ditto's product clean and general, we rejected several high-scoring benchmark modules that do **not** transfer to real users:
- **`ANSWER:` single-line slot extractors and final-check formatters** (`app/answer.py`, `app/memory/final_check.py` in `dan0l`/`beast-d41`/`crown`) — tuned to DittoBench's deterministic grader slot.
- **Hardcoded 140-app static prefix dictionaries** (`app/availability.py` in `Tessellate v2`, `app/connectors.py` in `crown-v228`) — replaced in our #2 recommendation by `dan0l v59`'s automatic, catalog-derived `AppKey` + TF-IDF index.
- **Narrow domain-specific views** (`app/evidence.py` invoice/trip/payables extractors in `crown-v221`, `app/casefile.py` case-file hash replay in `Tessellate v2`) — tailored to recurring benchmark case templates rather than general personal memory.
- **Synchronous job polling on Ditto Code Cloud Run jobs** (`app/followup.py` / `app/jobs.py` in `beast-d41 v18`) — contradicts Ditto's explicit Tasks UI architecture (`chatv2-tools.go:564`).

---

## 5. Concrete Implementation Plans for the Top 3 (`ditto-assistant/backend`)

Per guidance ("open a draft PR in the product repo only if the change is small and clearly safe; otherwise just the plan"), the top 3 changes touch `chatv2` tool dispatch, tool-catalog selection, and prompt seed assembly — hot paths that require Boat/Depot Go + PostgreSQL integration tests before opening a PR. Below are the exact, file-and-symbol-anchored implementation sketches ready to execute in `ditto-assistant/backend`.

### Plan #1: Per-Turn Mutation-Aware Tool Idempotency Ledger (`pkg/api/v2/chatv2_tool_ledger.go`)
1. **Add `turnToolLedger`** in a new file `pkg/api/v2/chatv2_tool_ledger.go` (scoped to one `streamPromptV25` turn, protected by `sync.Mutex` because `executeToolCalls` runs parallel `errgroup` goroutines):
   ```go
   type toolDeliveryState string
   const (
       toolStateCompleted       toolDeliveryState = "completed"
       toolStateDeliveryUnknown toolDeliveryState = "delivery_unknown"
       toolStateDeliveredFailed toolDeliveryState = "delivered_failed"
       toolStateNotDelivered    toolDeliveryState = "not_delivered"
       toolStateBlocked         toolDeliveryState = "blocked"
   )

   type turnToolLedger struct {
       mu        sync.Mutex
       changes   int
       sent      map[string]toolDeliveryState // key = name + "\x00" + canonicalJSON(args)
       readAfter map[string]int               // changes snapshot when a read last succeeded
       noted     map[string]bool              // allows one intentional model override on explicit repeat
   }
   ```
2. **Classify read vs. write** using `toolengine.Tool.Kind()` (already populated on every registered/dynamic tool in `pkg/services/toolengine/catalog.go:20`), defaulting unknown external MCP tools conservatively to **write** (non-read) so unknown tools never suffer accidental double-execution.
3. **Wire into `executeToolCalls` / `dispatchOne`** (`pkg/api/v2/chatv2-tools.go:1826-1895`):
   - Pass `*turnToolLedger` inside `executeToolCallsParams` (initialized once per turn in `chatv2.go:3037`).
   - Before executing a tool in `dispatchOne`: call `ledger.CheckBefore(tc.Name, tc.Arguments, isRead)`. If blocked, return `types.NewToolResultSuccess(repeatNote, tc.ID)` (matching `beast-d41`/`dan0l`'s non-fatal guidance note) without dispatching to the upstream provider.
   - After execution: map `net.Error` dial/connect timeouts to `toolStateNotDelivered`, post-send timeouts/5xx to `toolStateDeliveryUnknown`, 4xx/app errors to `toolStateDeliveredFailed`, and success to `toolStateCompleted`. Increment `ledger.changes++` whenever `!isRead && (state == completed || state == delivery_unknown)`.
4. **Add `fetch_memories` invalid-ID pre-check** in `case "fetch_memories"` (`pkg/api/v2/chatv2-tools.go:3224`): if `len(result.Memories) == 0 && len(result.UnfetchedIDs) > 0`, append an explicit corrective hint naming the unfetched IDs and reminding the model to use `pairID` values from `seedMemories` or `search_memories` (not synthetic labels).
5. **Tests:** Unit tests in `pkg/api/v2/chatv2_tool_ledger_test.go` covering: (a) duplicate write blocked on 2nd call and allowed on intentional 3rd call, (b) identical read blocked while `changes` unchanged, (c) identical read re-enabled after an intervening write increments `changes`, (d) `delivery_unknown` write blocks silent retry while `not_delivered` allows retry, (e) parallel `errgroup` race safety (`go test -race`).

### Plan #2: Zero-LLM App-Aware Tool Pruning & IDF Ranking (`pkg/codemode/catalog.go` + `pkg/api/v2/chatv2-tools.go`)
1. **Add IDF-weighted `CatalogIndex`** in `pkg/codemode/idf_index.go` (shared by both Code Mode `search_tools` and direct-tool pruning):
   - Precompute document frequencies `df[token]` and L2 norms over `tokenize(entry.Name + " " + entry.Desc)` using simple suffix stemming (`-ing/-ed/-es/-s`) and `weight(t) = (log(1 + N / (1 + df[t])))^2`.
   - Upgrade `Catalog.Search` (`pkg/codemode/catalog.go`) to rank by IDF cosine similarity (falling back to deterministic name order), preserving exact-name matches at the top.
2. **Add `pruneDirectToolsForPrompt`** in `pkg/api/v2/chatv2_tool_prune.go` invoked right after `selectPromptV25Tools` (`pkg/api/v2/chatv2-tools.go:879`) **only when** `len(tools) > 48` and `!codeMode` (leaving small toolsets 100% untouched for prompt-cache stability):
   - Always keep Ditto built-ins (`promptV2Tools`, memory tools, `search_tools`, `present_connector_panel`) and any tool already called earlier in the same thread/turn.
   - For connected-app/MCP tools (those with `AppID != ""` or `Server != ""`), check whether the app's display name or slug is mentioned in the user's message (case-insensitive word boundary); cap each mentioned app family at top-12 by IDF score.
   - Always union in **`SAFE_HARBOR_TOP_K = 3`** highest-IDF tools from the entire excluded set so indirect requests without an explicit app name still retain the best-matching connected tools.
   - Gate behind a feature flag (`pkg/services/featureflags`) so prompt-cache hit rate vs. prompt-token savings can be measured cleanly on staging/prod before default-on rollout.
3. **Tests:** Unit tests in `pkg/codemode/idf_index_test.go` and `pkg/api/v2/chatv2_tool_prune_test.go` with a synthetic 500-tool catalog verifying: (a) ≤48 tools pass through untouched, (b) named app tools + core tools + top-3 safe-harbor tools survive, (c) unmentioned app toolkits are pruned, (d) indirect query ("check my flights") still surfaces the travel/calendar tool via safe harbor.

### Plan #3: Copy Collapsing & Chronological Arc Ordering for Prompt Seed Memories (`pkg/services/memorystore/supersede.go` + `pkg/api/v2/chatv2_memory_context.go`)
1. **Add `collapseDuplicateSeedMemories`** in `pkg/api/v2/chatv2_memory_context.go` inside `preparePromptLongTermMemories` (after `filterSeedMemoriesByCosine`):
   - Normalize each root's `Prompt` by stripping short lead-in prefixes (`^[^:"\n]{3,60}:\s+` with ≤8 words), outer quotes, and folding whitespace/case. For normalized strings ≥24 chars, keep the newest (or highest-ranked) root and record collapsed `PairID`s in diagnostics logging.
2. **Extend `markSupersededSeedRoots`** (`pkg/services/memorystore/supersede.go:42`) with a complementary **identifier-component pass (`orderIdentifierComponents`)**:
   - Extract high-precision structured identifiers (`CODE_RE`: hyphenated alphanumeric codes like `INV-1042`, `DITTO-2934`, `PR-3260`) from each root's `Prompt + Response`.
   - Group roots sharing any identifier via union-find (no chaining across generic words — structured codes only).
   - For any component with ≥2 roots, sort members **chronologically oldest→newest** while preserving the component's earliest rank slot in the seed list, so multi-step updates (including lexical-only or sealed-graph roots that lack `EmbeddingPrompt5`) are presented in timeline order alongside `SupersededBy` / `UpdatedInThread` annotations.
3. **Tests:** Table-driven tests in `pkg/services/memorystore/supersede_test.go` and `pkg/api/v2/chatv2_memory_context_test.go` verifying duplicate copy collapse, chronological ordering of multi-memory code arcs, preservation of unrelated roots' rank order, and zero regressions on existing `supersede` / `seedgate_parity.json` tests.

---

## 6. What Could Not Be Verified

To maintain strict factual integrity, the following items were **not** reachable or could **not** be verified in this run and are explicitly excluded from all claims:

1. **Current v18 Top-6 Active Leaderboard Source Code (Embargoed / Unreleased):**
   As of `2026-11-24T21:13Z`, the live v18 leaderboard's newest top entries are **not** publicly downloadable via `/api/v1/public/agent/{id}/artifact` because SN118's king-only release policy withholds non-king source (`HTTP 404`) and holds newly crowned kings until confirmed winner emissions + embargo (`HTTP 425`):
   - `wren-mem v43` (`281430d1-5d0a-47ad-9449-51ebeec24533`, composite `0.789743`) — `HTTP 404` ("only the king's source is released")
   - `wren-mem v42` (`8a9f11b6-277f-4906-9fe8-352331d391fa`, composite `0.766771`, current champion) — `HTTP 425` ("awaiting confirmed winner emissions from a completed tempo")
   - `Hades v51` (`c2867cba-793a-4c9e-8390-cb78d0b9ae9b`, composite `0.777619`) — `HTTP 404`
   - `bujhmml v7` (`288e7d47-e328-4b5b-87ae-7b6eb1690606`, composite `0.775429`) — `HTTP 425`
   - `crown-v278 v1` (`1b101912-3abf-494e-9be0-b7270587a55b`, composite `0.704629`) — `HTTP 404`
   - `logan v27` (`9d646e8c-bb26-4031-9b4f-70ff09b79c9d`, composite `0.690143`) — `HTTP 425`
   - `dinn v20` (`c84ffe11-ae15-4d51-bef7-b10970734d16`, composite `0.678738`) — `HTTP 425`
   - **Why private Backroom was not used:** The `sn118-backroom` MCP server is not connected in this session, and `gcloud compute ssh` for read-only prod Postgres requires interactive re-authentication (`gcloud auth login`). Accordingly, **every finding in this report traces exclusively to the 41 SHA-256-verified, publicly released champion artifacts** (including 4 v18 champions: `beast-d41 v18`, `beast-d41 v19`, `dan0l v59`, and `crown-v228 v1`).
2. **Per-Category Failure-Mode Breakdowns & Private Transcripts:** Public Platform endpoints do not expose raw per-case validator transcripts (`public_transcript_url_template: null` in `/bench/config`). Scores cited are verified validator-quorum medians (`composite`, `tool_mean`, `memory_mean`), not per-category ablations on Ditto production traffic.
3. **Production A/B Lift in `ditto-assistant/backend`:** None of the 8 improvements has been benchmarked on live `heyditto.ai` user traffic or LongMemEval-S inside `backend` yet; expected impact ratings reflect architectural gap analysis against `ditto-assistant/backend` `main` (`517ed132`) plus verified SN118 performance under `openai/gpt-oss-20b`.

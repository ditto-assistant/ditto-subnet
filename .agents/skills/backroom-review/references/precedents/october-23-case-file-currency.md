---
id: october-23-case-file-currency
resolution: guidance
tags: [policy-v13, i5, w3, w13, reversal, sn-215, case-file, chronology, generator-defect, t, love, overhorizon, harry, lets, orsentra, ditto]
holding: A case-file currency rule (the SN-215 shape) is not a rejection ground on its own. The rule says a self-marked change outranks filing dates, and may come with a date disclaimer, change cues, request-identifier scoping, an aside filter, a host current/supersedes note, or filing times omitted inside the scoped view. Bench v13 stamps shuffled case-file rows in shuffled order (#3088), so this is coping with a benchmark defect. A reject needs an independent ground that stands with the rule removed.
---

Operator ruling, 2026-10-23. Policy text: the "Clarification: case-file currency
rules (2026-10-23)" section under I5 in `workers/screener/docs/policy-v13.md`
(ditto-subnet#3089). Generator defect: ditto-subnet#3088, tagged `bench-v14`.

Why: `research/dittobench-datagen/universe/personal.go:561-580` and
`universe/v13_contract.go:1056-1072` shuffle case-file rows. `v13CalendarTimestamp`
(`universe/v13_calendar.go:26-33`) then stamps each row by its shuffled position.
A plain restatement can therefore be dated after the change that replaced it.
No real conversation does that. The general "trust the latest date" rule is
penalized, and miners respond by telling the model to trust the change
statement instead.

The elements are one behavior. Stacking several of them is not a second
factor. The court rulings themselves listed "host timestamp deletion" as one
SN-215 limb, and timestamp omission alone was already not SN-215 (Artemis v20,
Hexa v1). Independent grounds that still reject:

- record text or another material fact the request needs is withheld (W3);
- the host writes or rewrites the graded value (I4), or overrides a dissenting
  model answer (I3);
- tool capabilities are removed or forced (I7); or
- a separate benchmark recipe, such as the enterprise effective-step parser
  or a settings-request predicate (I5).

Batch `608aaa07-6333-4c15-afb9-3fb84ccb7511` (rulings sha256
`c201ac4831622e2f8ee4333b9559f6c5775444164924b8f0b8e6364e728db0f7`) withdrew
these rejects and returned each to `scored`. Each original reject rested only
on this shape, or only on the benign 6000-character result cap:

- T v1 `d09386e4-6cbb-4925-9cf4-89328d6df21a`, T v2
  `41aae56e-9c99-45c3-a9f7-b71d794419a7`, T v3
  `621e1c61-05a0-4d2f-8786-2aea6d1a77dd`
- Love v14 `7741e218-c527-48a0-82ba-dbe501876c7d`
- overhorizon v3 `ded41fcc-f75b-48a2-8474-2a2858549ee6`
- Harry v14 `30d854e5-60a4-4493-bafe-aa6b4d4ad702`
- lets_638 v1 `8d3208ad-85f5-4f55-970f-cb4da230d13f`, lets_639 v1
  `99c076bc-8e67-40f8-87e6-7d630a0f6398`, lets_640 v1
  `f185f7ec-ed72-4c3b-86b9-5fffa07d6096`
- Orsentra v1 `618b0306-40db-4274-9618-1e4c4234b074` and v5
  `c18e856a-2d0b-4f70-a6a5-02667f3661a9` (6000-character cap only; their
  currency rule and app-preference prompting are not independent factors)

Kept, because each has an independent ground:

- cointaker v5/v8/v9/v12: the enterprise effective-step parser, closure and
  regroup (SN-213)
- Harry v9/v12/v13: the settings-request predicates (SN-214)
- gale v2/v3 and clear v37: other I5/I7/I4 grounds

ditto v7 `fdc99e79-a7c5-4a5d-bc9e-f8d0f4756e0a` was restored separately in batch
`5399f1da-1504-4757-a927-128a0debf91b` (rulings sha256
`270f923899856f5143b6838a2207e82296438440c448b93514b6227671a23c8b`). Its
currency recipe is covered by this holding. The open W13 question about its
compiled stage-1..4 router weights (`fixtures/router/*.bin`,
`src/router/mod.rs:214-230`) was settled by operator ruling: compiled weights
of the miner's own classifier models are acceptable opaque components, not a
rejection ground on their own.

Apply this per exact UUID. A later artifact with the same rule still needs
its own full review for independent grounds.

---
id: november-03-connector-app-wording-filter-w4
resolution: guidance
tags: [policy-v13, i7, w4, i6, availability, connector, app-wording, tool-filter, catalog, screener-calibration]
holding: Hiding connector-gated or app-specific tools from the deciding model's catalog, on the published "Needs the <App> connector" marker or on the request's app-name wording, is W4 availability narrowing, not I7. Guessing from wording is allowed and over-filtering is performance. Fake execution of a hidden tool, or removing a needed tool and claiming it ran or that its effect is done, remains I6/I7; forcing the remaining trajectory remains I7.
---

The published November 3 connector and app-wording clarification is in
`workers/screener/docs/policy-v13.md`, W4 ("Clarification: connector and
app-wording tool filtering (2026-11-03)") and the paragraph after the I7 list,
together with `references/decision-path-review.md` ("I7 connector and
app-wording safe harbor"). Operator ruling by Peyton on 2026-11-03: "guessing
from wording is smart approach that should be allowed."

## Permitted (W4)

- Filtering on the published connector marker. Bench v15 connector tool
  descriptions end "Needs the <App> connector to be linked in Settings."; a
  harness may drop every tool carrying that marker, or every one whose app the
  request does not name.
- Filtering on the request's app-name wording: the request does not mention
  an app, so that app's tools are hidden from the catalog the model sees.
- The filter may be rules, regular expressions or a declared small model, and
  may run before the deciding model sees the catalog. It needs no proof that a
  hidden tool was logically impossible.

When the filter hides a tool the case needed, the honest consequence is a
missing call or a worse answer. The benchmark scores that; it is not a removed
required capability, a low-confidence router, an entity-name misscope or a
manufactured singleton under I7/W10. Resolve those I7 leads as refuted, not
unresolved. A call to a hidden tool may be refused when recorded honestly as
blocked (W11).

## Still charged

- Answering or reporting as if a hidden or removed tool ran, or fabricating
  its call, result, receipt or success (I6).
- Removing a tool the case needs and then claiming it ran or reporting its
  effect as done (I6, I7).
- Using the filter to force the remaining trajectory: a pinned `tool_choice`,
  a catalog replaced with a host plan, or a refused valid call the deciding
  model selected from the tools it was shown (I7).

Other I7 grounds (planning takeover, forced trajectories, execution after
revocation, name-only caps and duplicate handling) are unchanged. This record
names no submission and makes no ruling on any specific artifact.

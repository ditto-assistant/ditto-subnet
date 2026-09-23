# L4 decision-packet report-only replay, 2026-09-23

Four currently held policy-v13 artifacts were downloaded through Backroom and
matched against their exact SHA-256 before local replay. The replay constructed
the existing decision-only input and the proposed packet from the same retained
ledger and archive. It sent no model requests and changed no miner state. Raw
source and signed URLs remain in private temporary storage, outside this repo.

| Artifact SHA prefix | Historical failure | Notes | Old input bytes | New input bytes | Served source lines | Unreviewed concern |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `7d8c41db` | evidence incomplete | 13 | 62,928 | 40,446 | 202 | no |
| `13e30145` | completion timeout | 48 | 78,872 | 59,753 | 400 | yes |
| `bac8c60f` | response too large | 16 | 59,309 | 36,606 | 200 | no |
| `c845249d` | evidence incomplete | 48 | 72,640 | 57,333 | 342 | yes |

Input bytes include the system and user message JSON, but exclude tool schemas
and HTTP framing. The old input was reconstructed from the pre-change message
format, so it is a comparison estimate; the live diagnostic for the timeout
case recorded 79,264 prompt bytes. New bytes are measured directly from the
new packet builder. All four packets fit the 64,000-byte preflight ceiling;
none was truncated. The old decision-only path included the complete archive
inventory. The new one omits it while retaining the policy, finding, every
ledger note, exact source excerpts, and an artifact digest.

Two designs remain plausible: this bounded decision packet and a larger
source-reading reviewer with a multi-step tool budget. The packet reuses the
existing host certification and made the four observed inputs smaller without
another discovery pass. A larger reviewer could inspect missing source, but
its verdict and latency cannot be compared until an exact-artifact model replay
is available. This change therefore does not switch models or claim that the
packet has better verdict accuracy.

The two 48-note cases have concerns outside the existing preload selection.
The host already refuses a CLEAR in that state. This replay leaves that
selection unchanged because a separate evidence-ordering repair owns it.

No provider key or report-only model endpoint was supplied to this checkout.
Consequently this replay cannot measure verdict correctness, model citation
coverage, model latency, token count, or cost. The historical timeout case
records two 180-second streams of the same 79,264-byte prompt with 4,982 and
5,499 SSE events; the new retry rule would stop after the first active stream.
That is a control-flow bound, not a measured new-model outcome.

Before enforcing this prompt revision, run exact-artifact report-only model
replay and compare independent source-review rulings, citations, terminal
states, wall time, request counts, and token/cost use. A false CLEAR or REJECT,
lost citation, or unexplained rise in `adjudicator-packet-too-large` or
`adjudicator-evidence-incomplete` blocks enforcement. Keep the current
operator holds unchanged until that gate is satisfied.

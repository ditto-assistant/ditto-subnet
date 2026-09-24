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

At the time of this initial offline comparison, no provider key or report-only
model endpoint was supplied to this checkout; the later model replay below
measures verdict and latency separately. The historical timeout case
records two 180-second streams of the same 79,264-byte prompt with 4,982 and
5,499 SSE events; the new retry rule would stop after the first active stream.
That is a control-flow bound, not a measured new-model outcome.

Before enforcing this prompt revision, run exact-artifact report-only model
replay and compare independent source-review rulings, citations, terminal
states, wall time, request counts, and token/cost use. A false CLEAR or REJECT,
lost citation, or unexplained rise in `adjudicator-packet-too-large` or
`adjudicator-evidence-incomplete` blocks enforcement. Keep the current
operator holds unchanged until that gate is satisfied.

## Model replay, 2026-09-24

The four archives and active hold contexts were reread and SHA-verified before
one-request-per-artifact, report-only calls through a disposable OpenRouter key.
The key expires after one day and has a $2 total limit that includes BYOK usage.
The independent source reviewer labeled `13e30145` REJECT for I3 and the three
other exact artifacts CLEAR. No replay mutated Backroom or miner state.

| Artifact SHA prefix | Independent label | GLM 5.3 Flash packet result | GPT-5.6 Sol packet result |
| --- | --- | --- | --- |
| `13e30145` | REJECT, I3 | `stream-no-tool-progress` after 122 s and 2,658 SSE events | `adjudicator-evidence-incomplete` after 9 s |
| `c845249d` | CLEAR | `cited-unread-source` after 41 s | `adjudicator-evidence-incomplete` after 6 s |
| `7d8c41db` | CLEAR | `adjudicator-evidence-incomplete` after 119 s | `adjudicator-evidence-incomplete` after 6 s |
| `bac8c60f` | CLEAR | `stream-no-tool-progress` after 129 s and 3,178 SSE events | `adjudicator-evidence-incomplete` after 4 s |

Each Sol packet call returned a tool call, but its verdict was an escalation.
Neither model produced a certified REJECT or CLEAR on this four-case cohort.
Smaller prompts and bounded retries therefore improve request control but do
not satisfy the model-verdict gate. The packet change remains draft and must
not be deployed on these results.

A separate bounded single-Sol source-reading probe used the same archives,
four requests per artifact, medium reasoning, and a four-step limit. It
returned a validated high-risk finding on the independently rejected
`13e30145` artifact in 35 s, but this small probe did not retain the finding's
invariant/citations and cannot establish why the labels agree. On independently
clear `c845249d`, it returned `source-review-inconsistent-verdict` after 39 s.
This bounded probe does not validate the larger report-only single-Sol design
in draft PR #2022. The existing fanout design also has an exact-SHA false
CLEAR in a prior calibration and needs an independent certification gate.

## Bounded on-demand read prototype

The draft court now advertises only `read_file`, `submit_adjudication`, and
policy-v13 `request_operator_review` after its complete initial packet. It
allows at most three additional source windows over four model turns, removes
`read_file` on the final turn, and certifies citations against the exact lines
actually served. A terminal verdict cannot share a turn with a read. One
completed turn without a tool call gets one corrective prompt; a stalled
stream does not get replayed. Every error remains a held terminal result.
Local tests cover a late concern read, multi-read batch, final-turn restriction,
and citation certification.

The limited report-only checks still did not pass the activation gate:

| Exact SHA prefix | Independent label | On-demand observation |
| --- | --- | --- |
| `13e30145` | REJECT, I3 | Sol returned an invalid first-turn tool response in two trials; both held after one request. |
| `7d8c41db` | CLEAR | Sol inspected source over three requests, then produced a self-inconsistent REJECT with a CLEAR clause; host contract validation held it. |
| `bac8c60f` | CLEAR | GLM again streamed without a tool call; after accepting Sol's bounded read batch, Sol requested operator review after three requests. |

The fourth artifact was not repeated on this variant because these three
failures already block activation under the $2 experiment cap. Source-reading
ability and deterministic terminal HOLD behavior are verified locally, but no
live model in this sample produced a certified CLEAR or REJECT. Broader
rescreening remains blocked on independent exact-artifact accuracy and citation
evidence.

# Unreleased-copy signal

Status: **shadow**. Records a score per agent; never holds, rejects, or changes
status. Code: `ditto/api_server/distinctive_content.py` (extraction),
`ditto/api_server/unreleased_copy.py` (pure signal),
`ditto/db/queries/unreleased_copy.py` (storage),
`ditto/api_server/unreleased_copy_sentinel.py` (loop),
`ditto/api_server/endpoints/admin_unreleased_copy.py` (operator reads).
Related: #436.

## Why

The lexical, line, structural and prompt fingerprints measure code shape. A
port to another language keeps none of it: a Rust port of a Python agent shared
0-4 fingerprint hashes with its source, and a partial copy of an unreleased
agent stayed under every fingerprint bar. What a port keeps is the content the
score depends on: prompt and instruction text, ordered cue lists, header and
note constants, and the long names given to them.

## Items

Every regular text file is scanned with one tolerant, language-family-aware
lexer (Python, Rust, Go, TS/JS, C-family, shell/TOML/YAML, prose files).
Comments, vendored and fixture trees, tests, lockfiles, published starter-kit
files and files over 256 KiB are skipped. Each string literal (any quoting
form, adjacent literals joined) is normalized: escapes, format placeholders,
punctuation, quoting and case removed. It yields:

| kind | what | weight |
|---|---|---|
| `text` | each sentence/line of a literal with >= 4 words | 1.0 |
| `seq` | each window of 3 consecutive literals in a file | 1.0 |
| `ident` | a `snake`/`camel`/`SCREAMING` identifier of >= 3 words | 0.5 |
| `short` | a 2-3 word literal | 0.25 |
| `wordseq` | a `seq` whose elements are all single words | 0.1 |

`FILED_ORDER_HEADER`, `filed_order_header` and `filedOrderHeader` are the same
`ident`; a cue tuple in Python and the same `&[&str]` in Rust give the same
`short` and `seq` items. Keys are 64-bit hashes with the kind in the low three
bits. Each artifact's sorted key set, minus the packaged public corpus
(`ditto/anticopy/public_items_dc1.bin.gz`, built by
`scripts/build_public_items.py` from every starter kit, ditto-harness and the
datagen vocabulary and templates across their histories), is one row of
`agent_distinctive_items`. No paths or text are stored; evidence is
re-extracted from the two artifacts for pairs worth reading.

## Signal

For a candidate uploaded at `t`, against every earlier agent `S` of another
owner (hotkey, payment coldkey and attested links are one owner) that was not
publicly released before `t`, a shared item counts unless it is in any agent
released before `t`, in the candidate owner's own agents uploaded before `S`,
or held by at least four other owners uploaded before `t`. The pair score is
the weighted count; the candidate is flagged at **40**.

## Calibration (2026-11-01)

41 operator-labeled artifacts across Python, Rust and Go (ruled copies, the
sources they were ruled against, released bases and the forks cleared on them),
scored with the production code:

| set | rows | flagged | scores |
|---|---|---|---|
| ruled copies vs ruled source | 9 agents | 9 | 119 to 7,696 (source pair) |
| cleared released-base forks and other cleared rows | 16 | 0 | 0 to 3.25 |
| released bases | 7 | 0 | 0.35 to 11.7 |
| unlabeled pre-October rows | 9 | 8 | 1,418 to 6,380 |

The smallest copy (a cross-language port of an agent's unreleased delta)
scores 119 against its ruled source; the largest negative scores 11.7 (a
released base, before its release). 40 leaves ~3x and ~3.4x margins.
Markdown is not extracted: lineage declarations copy their base's prose
verbatim, and in the read-only backfill that prose alone pushed two
released-base forks over the bar. The unlabeled pre-October rows share large
unreleased code with each other and no publication record; most were later
banned. They are review leads, not calibration negatives. The operator-private table with names lives in the
review notes, not here.

## Backfill (read-only, 2026-11-01)

296 artifacts (every submission since 2026-10-15, the confirmed kings, and the
calibration panel) were scored locally with production release semantics. All
candidate-level flags are large structural overlaps (>= 54), several of them
cross-language. They are listed for operator review in the private review
notes; none was acted on.

## Operation

The sentinel (platform role, every 30 s) indexes up to 8 unindexed artifacts
oldest first from storage, then evaluates up to 4 indexed agents newest first,
but only agents whose whole upload history is indexed. New uploads are indexed
in the upload transaction. Reads: Backroom `get_unreleased_copy_signal`
(artifact scope) and `list_unreleased_copy_signals`.

## Enforce (not enabled)

Turning a flag into a copy hold needs, in order: a complete shadow backfill
with every flagged row reviewed; zero cleared rows among the flags (or a
recalibrated bar); a settings revision with an audited mode switch rather than
a code constant; the hold routed through the existing copy court so the
operator rules on it; and a miner-facing reason that cites only the candidate's
own `path:line` evidence, never the other owner's source.

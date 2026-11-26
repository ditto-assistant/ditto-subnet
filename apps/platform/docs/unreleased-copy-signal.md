# Unreleased-copy signal

Status: audited mode (`off` | `shadow` | `enforce`, threshold default 40) in
`unreleased_copy_settings_revisions`; no revision means shadow. It never
rejects. Code: `ditto/api_server/distinctive_content.py` (extraction),
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
(`ditto/anticopy/public_items_dc3.bin.gz`, built by
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

## Calibration (2026-11-01, items dc2 / signal uc2)

84 operator-labeled artifacts across Python, Rust and Go, scored with the
production code (exact released-base exclusion, then the fuzzy pass):

| set | rows | flagged | scores |
|---|---|---|---|
| ruled copies vs ruled source | 9 | 9 | 114 to 6,558 |
| cleared released-base forks and other cleared rows | 16 | 0 | 0 to 3.5 |
| released bases | 7 | 0 | 0.35 to 2.95 |
| file-reviewed ports of released Python (pair vs the reviewed source) | 4 | 0 | 20.5 to 39.8 |
| one owner under two unlinked keys (namespace hint) | 3 | 0 | suppressed (raw 1,169 to 1,432) |

What changed from dc1/uc1, each from a reviewed false positive:

- **Constants resolved.** ``{NAME}`` / ``${NAME}`` interpolations of module-level
  string and number constants are expanded before normalizing, as a port
  inlines them.
- **Newline-agnostic chunks.** Real and escaped newlines are whitespace;
  chunks break only at blank lines and sentence ends.
- **Fuzzy released exclusion** (``refine_released``, evidence stage): an item
  whose word 5-shingles are >= 90% contained in agents released before the
  upload, or a literal window or short literal whose every element is a single
  word, released, or public, is released content in another surface form.
- **Public runtime messages.** The public corpus adds CPython (stdlib, Rust
  ``core``/``alloc``/``std``, common Python and Rust libraries), strings of
  ``libpython`` and ``pydantic-core``, and messages CPython and pydantic-core
  format at runtime (argument-clinic errors, parser errors, the pydantic-core
  error table).
- **Manifests skipped**; identifier weight 0.25.
- **Owner hint.** A shared external namespace (Hugging Face, GitHub, container
  registry, Docker ``FROM``) that contains one of the candidate owner's
  agent-name tokens marks the pair ``probable_same_owner``: listed, never
  flagged. A namespace named after the source is what a copier carries over
  with the code, so it excuses nothing. The owner's names are every name its
  hotkeys have used, including later uploads (signal ``uc3``): an operator's
  first line can predate the agent its namespace is named after.

What changed in dc3 (2026-11-10), each from a held false positive:

- **One string, one spelling.** A non-raw literal's escaped backslash is
  decoded, so `"\\b(book|pencil in)\\b"` and `r"\b(book|pencil in)\b"`
  normalize alike. Before, the escaped spelling left a stray `b` in every
  regex word list, so a port that re-spelled a released agent's regexes missed
  both the exact and the fuzzy released match (XXX v3 `d882ff81`, held at 141
  against an unreleased renamed port of released dan0l v41; 67.5 with dc3,
  all of it renamed identifiers).
- **f-string code is code.** A Python f-string field other than a bare
  constant (`{json.dumps(receipt.args, ensure_ascii=False)}`) is blanked; a
  renamed variable inside one used to read as prose at full weight.
- **The corpus follows the monorepo.** `platform-refresh-anti-copy.yml` runs
  `build_public_items.py --merge` on every `miners/**` or datagen change. The
  dc2 bundle was frozen at 2026-11-01, so the later
  `scripts/local-rehearsal.py` made two agents shipping the current starter
  kit score 236 against each other (`04017d97`).

Follow-up (SN-465, 2026-11-10):

- **The refresh PR never opened.** The workflow opened a PR only when
  `gh pr view automation/starter-kit-reference` failed, and that command also
  finds the merged #405. From August on, every refresh went to the branch and
  never reached main: the dc2 corpus, the stock-kit file digests (last landed
  2026-08-13) and the shingle references (2026-10-03). It now looks for an
  open PR only. The fix landed only the unreleased-copy corpus refresh; the
  fingerprint references move corpus identity and follow the backfill order
  in `anti-copy-reference.md`, so they arrive in the next refresh PR.
- **Only published history.** The builder read `git log --all`, so an
  unmerged branch's kit or datagen files counted as public, and the bundle
  depended on which branches the building checkout had fetched. It now reads
  `HEAD`'s history. `--merge` only adds keys, so the about 6,800 branch-only
  keys already in the dc3 bundle stay until the next full rebuild (dc4).

The pair score is a weighted count of shared items (`text` and `seq` 1.0,
`ident` and `short` 0.25, `wordseq` 0.1), not a percentage, so it has no
ceiling: `04017d97` against uid236 shared 283 items (71 `text`, 150 `seq`,
28 `ident`, 34 `short`), which weighs 236.5 against a threshold of 100.

An `ITEMS_VERSION` bump re-indexes every artifact before the sentinel
evaluates anything new (24 per back-to-back tick); signals already recorded
under `uc3` are not re-scored, so no ruled agent is held again.

Threshold: **100** (the switch-on revision). Locally the smallest copy scores
114 and the largest non-copy pair 39.8. At full scale (every candidate since
2026-10-15 against all 3,922 indexed agents), the file-reviewed negatives
score: two ports of a banned agent's prompt, 84.25 and 75.25; a released-base
port, 37.5; others below 37. The ruled partial copy scores 113.75. A
threshold of 100 flags none of the listed negatives and keeps every ruled
copy. Holds are operator-ruled, so the operator can lower the threshold
through the audited setting if those negatives are reclassified.

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

## Enforce

Set with Backroom `apply_unreleased_copy_settings` (confirmation
`APPLY UNRELEASED COPY {MODE}`, optimistic `expectedRevision`, reason, operator
as actor); read with `get_unreleased_copy_settings`, and in
`get_copy_court_settings` under `unreleased_copy`.

- **Not retroactive.** `enforce_since` is the start of the current unbroken run
  of enforce revisions. Only agents uploaded at or after it can be held;
  earlier flags stay on the review list with outcome
  `not_held:uploaded_before_enforcement`.
- **Holds, never rejects.** A flag at or above the threshold on an eligible
  agent opens a copy-kind ATH hold (`ath_pending_review`, `duplicate_of` the
  matched agent, provenance `trigger: unreleased_copy`): at score
  finalization when the sentinel evaluated first (the usual order), or by the
  sentinel itself when the agent is already scored or live. The outcome is
  recorded on the signal (`held`, `deferred:held_at_score_finalization`,
  `not_held:<why>`).
- **Operator rules.** The hold reason carries no near-duplicate hint, so the
  copy court classes it `unknown` and always escalates; it is never applied
  automatically.
- **Miner-visible reason** cites only the candidate's own `path:line`
  locations, never the other owner's source.

# Report-only v14 generator-template lead

This is one isolated candidate from #1880. The corpus is a deterministic index
of the public DittoBench generator, not a secret or a proof of cheating. A lead
reports only a source path and line where a literal overlaps that corpus. It
does not emit the literal or matching hash and cannot CLEAR, REJECT, HOLD, or
change a screening decision. No runtime screener path imports the local
report script, and policy v13 stays unchanged.

Run it only on a safely extracted source tree:

```bash
cd workers/screener
uv run python scripts/report_generator_ngram_leads.py /path/to/extracted/source
```

`authority: none` and `incomplete: true` mean the report cannot establish
coverage. Even a complete report with no leads is not clearance. Any reported
path must be checked against the actual served path and an independent source
review before it can inform a policy decision. The corpus builder reads the
public `research/dittobench-datagen` tree; rerun
`uv run python scripts/build_generator_ngram_corpus.py` when its generator
sources change, then use `--check` to verify the committed corpus.

#2274 is a separate report-only Sol reviewer. This utility has no call into
that candidate and no shared verdict channel. A future comparison may use
the location-only leads as navigation hints, with independently labeled
controls, while preserving #2274's `authority: none` boundary.

The current public starter kit itself yields two location leads (one in a
rehearsal script, one in a playground file) and an incomplete scan under the
default file-size bound. This is a concrete false-positive/coverage control:
neither lead is a violation, and the candidate is not calibrated for authority.

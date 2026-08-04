# Frozen human-name data

These files are build inputs for DittoBench V8. Runtime generation is fully
offline: it never calls a name, nickname, or demographic API.

## `given_names.tsv`

- Source: United States Social Security Administration national baby-name data
  (mirrored by the `hadley/babynames` project because SSA's download endpoint
  rejects automated retrieval in some environments).
- Upstream snapshot: `hadley/babynames` `master`, data through 2017.
- Transformation: sum births from 1990 through 2017 by given name; sort by
  descending count then name; retain the first 10,000 names.
- SHA-256: `f0f08ad16ba65cf77ca3009c1754926f9655148f7f5d040adf6c8fe8b8287249`.
- Rights: SSA is a United States government agency; this file is a factual,
  mechanically transformed public record.

## `surnames.tsv`

- Source: U.S. Census Bureau, 2010 Census surnames, `Names_2010Census.csv`.
- URL: <https://www2.census.gov/topics/genealogy/2010surnames/names.zip>
- Transformation: remove the aggregate `ALL OTHER NAMES` row; title-case the
  published surname; retain ranks 1-10,000 and their counts.
- SHA-256: `c9288bad400045790dd196bf10b080f7cf67a4860b4872af01df8c519c4b2a82`.
- Rights: U.S. Census Bureau public data.

## `nicknames.tsv`

- Source: Washington State Department of Commerce `commerce-wa-ols/nicknames`.
- Upstream file: `src/names.json` on `master` as retrieved 2026-07-30.
- Transformation: canonical and nickname strings title-cased, mappings sorted,
  duplicate mappings removed, and serialized as TSV.
- SHA-256: `b6ca554c42d0201950509598049cec58aff96cff686e7a08b4dccb5cf5fb4aa5`.
- License: MIT; the upstream license is retained in
  `NICKNAMES_LICENSE.txt`.

The benchmark does not infer or label any person's ethnicity from a name. The
observed-frequency head and explicit long-tail sampling are only used to avoid
the current tiny, synthetic, answer-table-friendly name pool.

# Frozen public vocabulary corpora

These files are build inputs for DittoBench v13. Runtime generation is fully
offline: it never calls a place, occupation, font, colour, or organisation API.
Each table was retrieved on 2026-09-13 and mechanically transformed by
`../tools/freeze.py`; the transformed TSV is what is frozen, SHA-256 pinned
(`publicdata_test.go` `TestFrozenCorpusIdentity`), and embedded. Re-running the
freeze against a newer upstream snapshot changes bytes and therefore requires a
new `bench_version`.

## `cities.tsv` — `name\tcountry\tpopulation`

- Source: GeoNames `cities15000.zip` (all cities with a population ≥ 15,000).
- URL: <https://download.geonames.org/export/dump/cities15000.zip>
- Upstream snapshot SHA-256 (zip as downloaded):
  `15b9401f1e3216219bc58474a1d150c1c9e81dfbfb58e6188c96261f94a393db`.
- Transformation: keep the `asciiname`, ISO country code, and population
  columns; retain names made only of ASCII letters, spaces, apostrophes,
  hyphens, and periods (3–30 characters); collapse duplicate names to the most
  populous entry; sort by population descending, then name.
- Rows: 31,799.
- SHA-256: `f3c93ac6accb20821d0ed13e0ebdf1fdad18e35adafe13e83fea818c2c1c6004`.
- License: Creative Commons Attribution 4.0 (CC-BY 4.0), © GeoNames
  contributors. Attribution: "This product includes data created by GeoNames
  (<https://www.geonames.org>)".

## `occupations.tsv` — `title`

- Source: O*NET 29.1 Database, `Occupation Data.txt` (O*NET-SOC titles).
- URL: <https://www.onetcenter.org/dl_files/database/db_29_1_text/Occupation%20Data.txt>
- Upstream snapshot SHA-256:
  `63e6029d3d30ff5c7cf39b5304a733b77a409e01c65ae7095fe92f6d18d74a66`.
- Transformation: take the head of each title (the segment before the first
  comma, "and", "or", or slash); drop "All Other" aggregates; singularise the
  final word (`-ies`→`-y`, `-sses/-shes/-ches/-xes`→drop `es`, trailing `s`
  dropped); lower-case; keep role-shaped heads (last word ends in an agentive
  suffix or a reviewed role noun, never `-ment`, and single-word `-ic`/`-ive`
  adjectives are excluded except mechanic/medic/paramedic and
  executive/detective/representative); 4–40 characters; deduplicate; sort.
- Rows: 721.
- SHA-256: `cbf91627ace1b935da67eb8be3bc6a9d22334990f86da788d4b95e381b7b4649`.
- License: Creative Commons Attribution 4.0 (CC-BY 4.0). O*NET® is a
  trademark of the U.S. Department of Labor, Employment and Training
  Administration; this table is a mechanical transformation of the published
  occupation titles.

## `fonts.tsv` — `family\tcategory\tpopularity`

- Source: Google Fonts family metadata (`familyMetadataList`).
- URL: <https://fonts.google.com/metadata/fonts>
- Upstream snapshot SHA-256:
  `1f554b7d51ee9f6ccba0336c32829f82541e4ca9fd66a2af2e9a20815e948b55`.
- Transformation: keep `family`, `category`, and the `popularity` rank for
  every open-source family whose name is ASCII; sort by popularity rank.
- Rows: 1,946.
- SHA-256: `8029e779b6ebd5a69175caa3e46bbe2d38aa05b59047a84fcd9c89467f334cc3`.
- Rights: family names and categories are factual metadata. Every listed family
  is distributed by Google Fonts under the SIL Open Font License 1.1, Apache
  License 2.0, or Ubuntu Font License 1.0; the benchmark embeds only the names,
  never font software.

## `colors.tsv` — `name\tsource`

- Sources: the xkcd colour survey result list (949 named colours) and the CSS
  Color Module Level 4 named colours (148 keywords).
- URL: <https://xkcd.com/color/rgb.txt> (CSS keywords transcribed from the W3C
  specification).
- Upstream snapshot SHA-256 (`rgb.txt`):
  `450cca88fa6fa9a1e79c969969e05e6900b41a94f0a3a5f134e3d0b79077f890`.
- Transformation: keep xkcd names made of lower-case letters, spaces,
  apostrophes, and hyphens; drop names containing a slash or any of a small
  reviewed list of unpleasant tokens (bodily-fluid and insult words) so an
  accent-colour preference reads naturally; split CSS keywords into words
  (`lightgoldenrodyellow` → `light goldenrod yellow`); merge with the source
  column recording `xkcd`, `css`, or `xkcd+css`; sort by name.
- Rows: 952.
- SHA-256: `ce308f2165e03d1f51238923b4895e4a15967add6cded1579f9a8a72996edca0`.
- License: xkcd colour survey data is released under CC0 1.0. CSS colour
  keywords are part of a W3C specification (W3C Software and Document License);
  the keyword list is factual.

## `org_stems.tsv` — `stem`

- Source: Wikidata — English labels of items that are instances (or subclass
  instances) of *business* (Q4830453) with more than 25 sitelinks, ordered by
  sitelink count.
- URL: <https://query.wikidata.org/sparql> (query recorded in `tools/freeze.py`).
- Upstream snapshot SHA-256 (TSV as returned):
  `0e5b7292a7d17f096918bfa8538a9b9980e630f7404fd752ae956999602a093f`.
- Transformation: take the first token of each label; keep pure-ASCII
  alphabetic tokens of 4–14 letters; drop a reviewed stop-list of generic and
  geographic words; skip the 200 most-linked household names; Title-case;
  deduplicate; keep sitelink order.
- Rows: 1,528.
- SHA-256: `e0da61d10a87a7e91ee9ea4fdcdd1036eb1d7ea57b2cde48a63ed1d103cce747`.
- License: Wikidata content is CC0 1.0.

## `purposes.tsv` — `kind\tpurpose`

- Source: authored for this benchmark (no upstream dataset publishes project
  or trip purposes). 121 project purposes and 60 trip purposes.
- SHA-256: `78057f19f5e77d3423747df31635b62a857662b310880e3e3ca959ca6650d60d`.
- License: same as this repository.

## Closed enums

`companySuffixes` (32 entries, in `publicdata.go`) is the one deliberately
small list in this package: the legal/brand suffix that follows an organisation
stem is a closed set in the real world too. Colour *modes* (`dark`, `light`,
`system`) remain a product enum owned by `universe`.

The benchmark makes no claim about any real person, place, company, or
product. Names are combined into fictional employers, clients, projects, and
preferences; the frozen tables only widen the vocabulary a harness must read.

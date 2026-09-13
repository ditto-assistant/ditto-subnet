#!/usr/bin/env python3
"""Freeze the DittoBench v13 public vocabulary corpora into TSV tables.

Inputs (downloaded 2026-09-13; URLs and SHA-256 in ../data/SOURCES.md):
  cities15000.txt, onet_occupation_data.txt, gfonts_metadata.json,
  xkcd_rgb.txt, wikidata_orgs.tsv
Outputs: out/{cities,occupations,fonts,colors,org_stems}.tsv

The Wikidata input is the TSV returned for this SPARQL query against
https://query.wikidata.org/sparql (Accept: text/tab-separated-values):

  SELECT ?item ?itemLabel ?links WHERE {
    ?item wdt:P31/wdt:P279* wd:Q4830453 ; wikibase:sitelinks ?links .
    FILTER(?links > 25)
    ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel)="en")
  } ORDER BY DESC(?links) LIMIT 6000

Re-running against a newer upstream snapshot changes bytes and therefore
requires a new bench_version; the embedded tables are what is frozen.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ASCII_NAME = re.compile(r"^[A-Za-z][A-Za-z .'\-]*[A-Za-z.]$")
LOWER_PHRASE = re.compile(r"^[a-z][a-z' \-]+$")

# Colour tokens the CSS keywords are split on (longest match first).
CSS_TOKENS = [
    "goldenrod",
    "aquamarine",
    "cornsilk",
    "burlywood",
    "honeydew",
    "firebrick",
    "seashell",
    "alice",
    "antique",
    "aqua",
    "marine",
    "blue",
    "violet",
    "burly",
    "wood",
    "cadet",
    "blanched",
    "almond",
    "chartreuse",
    "cornflower",
    "corn",
    "silk",
    "dark",
    "golden",
    "rod",
    "gray",
    "grey",
    "green",
    "khaki",
    "magenta",
    "olive",
    "orange",
    "orchid",
    "red",
    "salmon",
    "sea",
    "slate",
    "turquoise",
    "deep",
    "pink",
    "sky",
    "dim",
    "dodger",
    "fire",
    "brick",
    "floral",
    "white",
    "forest",
    "ghost",
    "gold",
    "yellow",
    "honey",
    "dew",
    "hot",
    "indian",
    "lavender",
    "blush",
    "lawn",
    "lemon",
    "chiffon",
    "light",
    "coral",
    "cyan",
    "steel",
    "lime",
    "medium",
    "purple",
    "spring",
    "midnight",
    "mint",
    "cream",
    "misty",
    "rose",
    "navajo",
    "old",
    "lace",
    "drab",
    "pale",
    "papaya",
    "whip",
    "peach",
    "puff",
    "powder",
    "rebecca",
    "rosy",
    "brown",
    "royal",
    "saddle",
    "sandy",
    "shell",
    "smoke",
    "tomato",
    "wheat",
]

CSS_COLORS = [
    "aliceblue",
    "antiquewhite",
    "aqua",
    "aquamarine",
    "azure",
    "beige",
    "bisque",
    "black",
    "blanchedalmond",
    "blue",
    "blueviolet",
    "brown",
    "burlywood",
    "cadetblue",
    "chartreuse",
    "chocolate",
    "coral",
    "cornflowerblue",
    "cornsilk",
    "crimson",
    "cyan",
    "darkblue",
    "darkcyan",
    "darkgoldenrod",
    "darkgray",
    "darkgreen",
    "darkgrey",
    "darkkhaki",
    "darkmagenta",
    "darkolivegreen",
    "darkorange",
    "darkorchid",
    "darkred",
    "darksalmon",
    "darkseagreen",
    "darkslateblue",
    "darkslategray",
    "darkslategrey",
    "darkturquoise",
    "darkviolet",
    "deeppink",
    "deepskyblue",
    "dimgray",
    "dimgrey",
    "dodgerblue",
    "firebrick",
    "floralwhite",
    "forestgreen",
    "fuchsia",
    "gainsboro",
    "ghostwhite",
    "gold",
    "goldenrod",
    "gray",
    "green",
    "greenyellow",
    "grey",
    "honeydew",
    "hotpink",
    "indianred",
    "indigo",
    "ivory",
    "khaki",
    "lavender",
    "lavenderblush",
    "lawngreen",
    "lemonchiffon",
    "lightblue",
    "lightcoral",
    "lightcyan",
    "lightgoldenrodyellow",
    "lightgray",
    "lightgreen",
    "lightgrey",
    "lightpink",
    "lightsalmon",
    "lightseagreen",
    "lightskyblue",
    "lightslategray",
    "lightslategrey",
    "lightsteelblue",
    "lightyellow",
    "lime",
    "limegreen",
    "linen",
    "magenta",
    "maroon",
    "mediumaquamarine",
    "mediumblue",
    "mediumorchid",
    "mediumpurple",
    "mediumseagreen",
    "mediumslateblue",
    "mediumspringgreen",
    "mediumturquoise",
    "mediumvioletred",
    "midnightblue",
    "mintcream",
    "mistyrose",
    "moccasin",
    "navajowhite",
    "navy",
    "oldlace",
    "olive",
    "olivedrab",
    "orange",
    "orangered",
    "orchid",
    "palegoldenrod",
    "palegreen",
    "paleturquoise",
    "palevioletred",
    "papayawhip",
    "peachpuff",
    "peru",
    "pink",
    "plum",
    "powderblue",
    "purple",
    "rebeccapurple",
    "red",
    "rosybrown",
    "royalblue",
    "saddlebrown",
    "salmon",
    "sandybrown",
    "seagreen",
    "seashell",
    "sienna",
    "silver",
    "skyblue",
    "slateblue",
    "slategray",
    "slategrey",
    "snow",
    "springgreen",
    "steelblue",
    "tan",
    "teal",
    "thistle",
    "tomato",
    "turquoise",
    "violet",
    "wheat",
    "white",
    "whitesmoke",
    "yellow",
    "yellowgreen",
]

# Reviewed unpleasant tokens dropped from the xkcd survey names.
COLOR_DENYLIST = {
    "shit",
    "poo",
    "poop",
    "puke",
    "vomit",
    "barf",
    "piss",
    "pee",
    "booger",
    "snot",
    "diarrhea",
    "bile",
    "ugly",
    "baby",
}

ROLE_SUFFIX = re.compile(
    r"(er|or|ist|ant|ian|ive|ent|eur|ess|ary|ic|ee|nurse|judge|chef|pilot|clerk"
    r"|aide|guide|coach|cook|umpire|athlete|model|attorney|lawyer|barber|butcher"
    r"|baker|tailor|firefighter|dentist|physician|surgeon|therapist|midwife"
    r"|medic|navigator)$"
)
SINGLE_IC_ALLOWED = {"mechanic", "medic", "paramedic"}
SINGLE_IVE_ALLOWED = {"executive", "detective", "representative"}

# Generic and geographic first tokens that are not organisation stems.
ORG_STOPLIST = {
    "the",
    "bank",
    "national",
    "university",
    "first",
    "united",
    "general",
    "american",
    "british",
    "royal",
    "group",
    "air",
    "new",
    "north",
    "south",
    "east",
    "west",
    "grand",
    "great",
    "union",
    "state",
    "city",
    "global",
    "international",
    "world",
    "central",
    "standard",
    "federal",
    "public",
    "company",
    "society",
    "institute",
    "college",
    "ministry",
    "department",
    "office",
    "council",
    "agency",
    "association",
    "foundation",
    "school",
    "hospital",
    "museum",
    "library",
    "order",
    "house",
    "church",
    "party",
    "fund",
    "trust",
    "saint",
    "french",
    "german",
    "japan",
    "japanese",
    "china",
    "chinese",
    "india",
    "indian",
    "korea",
    "korean",
    "russian",
    "swiss",
    "dutch",
    "italian",
    "spanish",
    "canada",
    "canadian",
    "australia",
    "australian",
    "europe",
    "european",
    "africa",
    "african",
    "asia",
    "asian",
    "america",
    "london",
    "paris",
    "berlin",
    "tokyo",
    "york",
    "california",
    "texas",
    "states",
    "kingdom",
    "republic",
    "people",
    "government",
    "army",
    "navy",
    "force",
    "police",
    "post",
    "postal",
    "railway",
    "railways",
    "airlines",
    "airways",
    "telecom",
    "energy",
    "electric",
    "power",
    "water",
    "gas",
    "steel",
    "motors",
    "motor",
    "oil",
    "petroleum",
    "mining",
    "holding",
    "holdings",
    "partners",
    "capital",
    "media",
    "news",
    "press",
    "radio",
    "television",
    "studios",
    "pictures",
    "records",
    "music",
    "games",
    "software",
    "systems",
    "technologies",
    "technology",
    "industries",
    "industrial",
    "corporation",
    "limited",
    "incorporated",
    "enterprises",
    "financial",
    "insurance",
    "securities",
    "exchange",
    "stock",
    "market",
    "store",
    "stores",
    "food",
    "foods",
    "pharma",
    "pharmaceuticals",
    "chemical",
    "chemicals",
    "airport",
    "port",
    "line",
    "lines",
    "service",
    "services",
    "solutions",
    "network",
    "networks",
    "communications",
    "wireless",
    "mobile",
    "digital",
    "online",
    "internet",
    "web",
    "data",
    "cloud",
    "labs",
    "life",
    "health",
    "medical",
    "care",
    "auto",
    "automotive",
    "aircraft",
    "airline",
    "shipping",
    "logistics",
    "transport",
    "transit",
    "bus",
    "rail",
    "metro",
    "hotel",
    "hotels",
    "resorts",
    "casino",
    "film",
    "films",
    "theatre",
    "theater",
    "ballet",
    "opera",
    "orchestra",
    "symphony",
    "academy",
    "conservatory",
    "polytechnic",
    "technical",
    "sciences",
    "science",
    "arts",
    "art",
    "design",
    "fashion",
    "beauty",
    "sports",
    "sport",
    "football",
    "basketball",
    "baseball",
    "hockey",
    "racing",
    "club",
    "team",
    "league",
    "federation",
    "committee",
    "commission",
    "authority",
    "board",
    "bureau",
    "center",
    "centre",
    "organization",
    "organisation",
    "alliance",
    "coalition",
    "movement",
    "front",
    "brigade",
    "battalion",
    "regiment",
    "division",
    "corps",
    "guard",
}
ORG_HOUSEHOLD_SKIP = 200


def freeze_cities(src: Path, out: Path) -> int:
    best: dict[str, tuple[str, int]] = {}
    with src.open(encoding="utf-8") as handle:
        for line in handle:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 15:
                continue
            name, country, population = cols[2], cols[8], int(cols[14] or 0)
            if population < 15000 or not ASCII_NAME.match(name):
                continue
            if len(name) < 3 or len(name) > 30:
                continue
            if name not in best or population > best[name][1]:
                best[name] = (country, population)
    rows = sorted(best.items(), key=lambda kv: (-kv[1][1], kv[0]))
    with out.open("w", newline="") as handle:
        handle.write("name\tcountry\tpopulation\n")
        for name, (country, population) in rows:
            handle.write(f"{name}\t{country}\t{population}\n")
    return len(rows)


def singular(word: str) -> str:
    lower = word.lower()
    if lower.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if lower.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def role_shaped(title: str) -> bool:
    last = title.split()[-1]
    if last.endswith("ment") or not ROLE_SUFFIX.search(last):
        return False
    if len(title.split()) == 1:
        if last.endswith("ic") and last not in SINGLE_IC_ALLOWED:
            return False
        if last.endswith("ive") and last not in SINGLE_IVE_ALLOWED:
            return False
    return True


def freeze_occupations(src: Path, out: Path) -> int:
    titles: list[str] = []
    seen: set[str] = set()
    with src.open(encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            _code, title = line.rstrip("\n").split("\t")[:2]
            head = re.split(r",| and | or |/", title)[0].strip()
            if not head or "all other" in head.lower():
                continue
            words = head.split()
            words[-1] = singular(words[-1])
            cleaned = " ".join(words).lower().replace("except ", "").strip()
            if not LOWER_PHRASE.match(cleaned) or not 4 <= len(cleaned) <= 40:
                continue
            if not role_shaped(cleaned) or cleaned in seen:
                continue
            seen.add(cleaned)
            titles.append(cleaned)
    titles.sort()
    with out.open("w", newline="") as handle:
        handle.write("title\n")
        for title in titles:
            handle.write(title + "\n")
    return len(titles)


def freeze_fonts(src: Path, out: Path) -> int:
    with src.open(encoding="utf-8") as handle:
        meta = json.load(handle)
    fonts: list[tuple[int, str, str]] = []
    for family in meta["familyMetadataList"]:
        name, category = family["family"], family["category"]
        popularity = family.get("popularity")
        if not family.get("isOpenSource", True) or popularity is None:
            continue
        if not re.match(r"^[A-Za-z][A-Za-z0-9 .+'\-]*$", name):
            continue
        fonts.append((int(popularity), name, category))
    fonts.sort()
    with out.open("w", newline="") as handle:
        handle.write("family\tcategory\tpopularity\n")
        for popularity, name, category in fonts:
            handle.write(f"{name}\t{category}\t{popularity}\n")
    return len(fonts)


def space_css(name: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(name):
        for length in range(len(name) - i, 0, -1):
            token = name[i : i + length]
            if token in CSS_TOKENS:
                out.append(token)
                i += length
                break
        else:
            out.append(name[i:])
            break
    return " ".join(out)


def freeze_colors(src: Path, out: Path) -> int:
    colors: dict[str, str] = {}
    with src.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            name = line.split("\t")[0].strip()
            words = name.replace("/", " ").split()
            if "/" in name or any(word in COLOR_DENYLIST for word in words):
                continue
            if not LOWER_PHRASE.match(name):
                continue
            colors[name] = "xkcd"
    for keyword in CSS_COLORS:
        spaced = space_css(keyword)
        colors[spaced] = "xkcd+css" if spaced in colors else "css"
    with out.open("w", newline="") as handle:
        handle.write("name\tsource\n")
        for name in sorted(colors):
            handle.write(f"{name}\t{colors[name]}\n")
    return len(colors)


def freeze_org_stems(src: Path, out: Path) -> int:
    seen: set[str] = set()
    stems: list[str] = []
    ranked = 0
    with src.open(encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            match = re.match(r'^"(.*)"@en$', parts[1])
            if not match:
                continue
            tokens = match.group(1).split()
            first = tokens[0] if tokens else ""
            if not re.match(r"^[A-Za-z]+$", first) or not 4 <= len(first) <= 14:
                continue
            if first.lower() in ORG_STOPLIST:
                continue
            ranked += 1
            if ranked <= ORG_HOUSEHOLD_SKIP:
                continue
            stem = first[0].upper() + first[1:].lower()
            if stem.lower() in seen:
                continue
            seen.add(stem.lower())
            stems.append(stem)
    with out.open("w", newline="") as handle:
        handle.write("stem\n")
        for stem in stems:
            handle.write(stem + "\n")
    return len(stems)


def main() -> None:
    base = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out = base / "out"
    os.makedirs(out, exist_ok=True)
    counts = {
        "cities": freeze_cities(base / "cities15000.txt", out / "cities.tsv"),
        "occupations": freeze_occupations(
            base / "onet_occupation_data.txt", out / "occupations.tsv"
        ),
        "fonts": freeze_fonts(base / "gfonts_metadata.json", out / "fonts.tsv"),
        "colors": freeze_colors(base / "xkcd_rgb.txt", out / "colors.tsv"),
        "org_stems": freeze_org_stems(
            base / "wikidata_orgs.tsv", out / "org_stems.tsv"
        ),
    }
    for name, count in counts.items():
        print(name, count)


if __name__ == "__main__":
    main()

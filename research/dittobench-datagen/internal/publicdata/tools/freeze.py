#!/usr/bin/env python3
"""Freeze the DittoBench v13 public vocabulary corpora into TSV tables.

Inputs (downloaded 2026-09-13; URLs and SHA-256 in ../data/SOURCES.md):
  cities15000.txt, onet_occupation_data.txt, gfonts_metadata.json,
  xkcd_rgb.txt, wikidata_orgs.tsv
Outputs: out/{cities,occupations,fonts,colors,org_stems}.tsv
Usage: freeze.py <inputs dir>, or freeze.py --refilter <data dir> to re-apply
the org-stem second-stage exclusions to an already frozen table in place.

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
# Second-stage exclusions applied to the ranked, deduplicated stem list (see
# refine_org_stems). The first ORG_HEAD_DROP retained stems are the next most
# linked labels after the skipped head and read as household names.
ORG_HEAD_DROP = 200

# Countries, demonyms, languages, regions, states, and national adjectives in
# the source languages of the labels. A stem naming a real place or nation must
# not become a fictional employer or web host.
ORG_PLACE_STOPLIST = {
    "afghanistan",
    "anadolu",
    "bujanovac",
    "cabo",
    "ghent",
    "santa",
    "sokobanja",
    "zurich",
    "afghan",
    "africa",
    "african",
    "albania",
    "albanian",
    "algeria",
    "algerian",
    "america",
    "american",
    "americas",
    "andorra",
    "angola",
    "arab",
    "arabia",
    "arabian",
    "argentina",
    "argentine",
    "argentinian",
    "armenia",
    "armenian",
    "asia",
    "asian",
    "atlantic",
    "australia",
    "australian",
    "austria",
    "austrian",
    "azerbaijan",
    "azores",
    "bahrain",
    "balkan",
    "baltic",
    "bangladesh",
    "bangladeshi",
    "bavaria",
    "bavarian",
    "belarus",
    "belarusian",
    "belgium",
    "belgian",
    "bengal",
    "bengali",
    "bolivia",
    "bolivian",
    "bosnia",
    "bosnian",
    "brazil",
    "brazilian",
    "britain",
    "british",
    "brunei",
    "bulgaria",
    "bulgarian",
    "burma",
    "burmese",
    "cambodia",
    "cambodian",
    "cameroon",
    "canada",
    "canadian",
    "caribbean",
    "catalan",
    "catalonia",
    "chile",
    "chilean",
    "china",
    "chinese",
    "colombia",
    "colombian",
    "columbia",
    "commonwealth",
    "congo",
    "continental",
    "croatia",
    "croatian",
    "cuba",
    "cuban",
    "cubana",
    "cyprus",
    "cypriot",
    "czech",
    "czechia",
    "danish",
    "danmarks",
    "danske",
    "denmark",
    "deutsch",
    "deutsche",
    "deutscher",
    "dominican",
    "dravida",
    "dutch",
    "east",
    "eastern",
    "ecuador",
    "ecuadorian",
    "eesti",
    "egypt",
    "egyptian",
    "emirati",
    "emirates",
    "england",
    "english",
    "estonia",
    "estonian",
    "ethiopia",
    "ethiopian",
    "eurasia",
    "eurasian",
    "europe",
    "european",
    "fiji",
    "fijian",
    "finland",
    "finnish",
    "flanders",
    "flemish",
    "france",
    "frankfurter",
    "french",
    "gaelic",
    "georgia",
    "georgian",
    "german",
    "germania",
    "germany",
    "ghana",
    "ghanaian",
    "greece",
    "greek",
    "guatemala",
    "gulf",
    "hainan",
    "hawaii",
    "hawaiian",
    "hellenic",
    "helvetic",
    "hessischer",
    "hokkaido",
    "holland",
    "honduras",
    "hong",
    "hungarian",
    "hungary",
    "iberia",
    "iberian",
    "iceland",
    "icelandic",
    "india",
    "indian",
    "indonesia",
    "indonesian",
    "iran",
    "iranian",
    "iraq",
    "iraqi",
    "ireland",
    "irish",
    "islamic",
    "israel",
    "israeli",
    "italia",
    "italian",
    "italy",
    "jalisco",
    "jamaica",
    "jamaican",
    "japan",
    "japanese",
    "jordan",
    "jordanian",
    "kazakh",
    "kazakhstan",
    "kenya",
    "kenyan",
    "korea",
    "korean",
    "kosovo",
    "kurdish",
    "kuwait",
    "kuwaiti",
    "kyushu",
    "laos",
    "latin",
    "latvia",
    "latvian",
    "latvijas",
    "lebanese",
    "lebanon",
    "libya",
    "libyan",
    "lithuania",
    "lithuanian",
    "luxembourg",
    "macedonia",
    "macedonian",
    "malaysia",
    "malaysian",
    "malta",
    "maltese",
    "marmara",
    "mediterranean",
    "mexican",
    "mexico",
    "middle",
    "moldova",
    "moldovan",
    "mongolia",
    "mongolian",
    "montenegro",
    "morocco",
    "moroccan",
    "muscovy",
    "myanmar",
    "nauru",
    "nederlandse",
    "nepal",
    "nepali",
    "netherlands",
    "nigeria",
    "nigerian",
    "nippon",
    "nordic",
    "nordisk",
    "norges",
    "norsk",
    "north",
    "northern",
    "northwest",
    "norway",
    "norwegian",
    "oceania",
    "oman",
    "omani",
    "orient",
    "oriental",
    "pacific",
    "pakistan",
    "pakistani",
    "palestine",
    "palestinian",
    "panama",
    "paraguay",
    "persia",
    "persian",
    "peru",
    "peruvian",
    "philippine",
    "philippines",
    "poland",
    "polish",
    "polska",
    "portugal",
    "portuguese",
    "prussia",
    "prussian",
    "qatar",
    "qatari",
    "romania",
    "romanian",
    "rossiya",
    "russia",
    "russian",
    "rwanda",
    "rwandan",
    "saarland",
    "saudi",
    "scandinavia",
    "scandinavian",
    "scotland",
    "scottish",
    "senegal",
    "serbia",
    "serbian",
    "siam",
    "siamese",
    "siberia",
    "siberian",
    "sichuan",
    "singapore",
    "singaporean",
    "slovak",
    "slovakia",
    "slovenia",
    "slovenian",
    "somalia",
    "south",
    "southern",
    "southwest",
    "soviet",
    "spain",
    "spanish",
    "srilankan",
    "statens",
    "sudan",
    "sudanese",
    "sveriges",
    "sweden",
    "swedish",
    "swiss",
    "switzerland",
    "syria",
    "syrian",
    "taiwan",
    "taiwanese",
    "tajik",
    "tajikistan",
    "tanzania",
    "tatarstan",
    "thai",
    "thailand",
    "tibet",
    "tibetan",
    "tohoku",
    "tunisia",
    "tunisian",
    "turkey",
    "turkish",
    "turkmen",
    "turkmenistan",
    "tyrolean",
    "uganda",
    "ukraine",
    "ukrainian",
    "ural",
    "uruguay",
    "uruguayan",
    "uzbek",
    "uzbekistan",
    "venezuela",
    "venezuelan",
    "vietnam",
    "vietnamese",
    "wales",
    "welsh",
    "west",
    "westdeutscher",
    "western",
    "wiener",
    "yemen",
    "yemeni",
    "yugoslav",
    "yugoslavia",
    "zambia",
    "zimbabwe",
    # US states and territories.
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "wisconsin",
    "wyoming",
}

# Adult, religious, political, weapons, notorious-collapse, and self-referential
# labels: never a fictional employer, outlet, or web host.
ORG_SENSITIVE_STOPLIST = {
    "academi",
    "kehlsteinhaus",
    "shebaa",
    "stonewall",
    "anthropic",
    "badoo",
    "bellator",
    "bitchute",
    "brazzers",
    "buddha",
    "catholic",
    "christian",
    "democratic",
    "durex",
    "enron",
    "gideons",
    "grindr",
    "heckler",
    "hooters",
    "ignalina",
    "islamic",
    "jesuit",
    "kalashnikov",
    "lehman",
    "mauser",
    "megaupload",
    "melkite",
    "nuclear",
    "omegle",
    "onecoin",
    "pinkerton",
    "pontifical",
    "rapidshare",
    "rosatom",
    "sellafield",
    "sinaloa",
    "tinder",
    "trump",
    "xhamster",
    "xvideos",
    "yukos",
}

# Personal names outside the 10,000-entry given-name and surname tables that
# still read as a person rather than an organisation.
ORG_PERSON_STOPLIST = {
    "alexandru",
    "avedis",
    "christoph",
    "comenius",
    "gadjah",
    "linnaeus",
    "masaryk",
    "plekhanov",
    "shakespeare",
    "tribhuvan",
    "vernadsky",
    "vytautas",
}

# Reviewed household consumer and technology brands that rank below the skipped
# head but read as real companies to any user; the residual list is still
# drawn from real organisations (see SOURCES.md).
ORG_HOUSEHOLD_STOPLIST = {
    "activision",
    "adidas",
    "airbnb",
    "aldi",
    "alibaba",
    "amazon",
    "amway",
    "apple",
    "asics",
    "atlassian",
    "audi",
    "autodesk",
    "bacardi",
    "bandai",
    "barilla",
    "bentley",
    "binance",
    "blackberry",
    "blizzard",
    "blockbuster",
    "bose",
    "braun",
    "breitling",
    "bungie",
    "cadbury",
    "canon",
    "capcom",
    "cartier",
    "casio",
    "cessna",
    "chanel",
    "cisco",
    "citrix",
    "cloudflare",
    "coinbase",
    "commodore",
    "compaq",
    "costco",
    "daimler",
    "decathlon",
    "deezer",
    "dell",
    "diageo",
    "disney",
    "dolby",
    "duracell",
    "dyson",
    "electrolux",
    "facebook",
    "fendi",
    "ferrero",
    "fiat",
    "fitbit",
    "ford",
    "gamestop",
    "garnier",
    "gatorade",
    "gillette",
    "givenchy",
    "google",
    "gopro",
    "grab",
    "grammarly",
    "gucci",
    "haribo",
    "hasbro",
    "hennessy",
    "herbalife",
    "hilton",
    "hisense",
    "honeywell",
    "hugging",
    "hyatt",
    "hyundai",
    "ikea",
    "instagram",
    "jetbrains",
    "kawasaki",
    "kenzo",
    "kraft",
    "kroger",
    "legoland",
    "lindt",
    "longines",
    "lufthansa",
    "lyft",
    "makita",
    "marriott",
    "maybelline",
    "mclaren",
    "mercedes",
    "merck",
    "metlife",
    "microsoft",
    "miele",
    "montblanc",
    "mozilla",
    "napster",
    "nespresso",
    "netflix",
    "nickelodeon",
    "nike",
    "nintendo",
    "nokia",
    "nordstrom",
    "nordvpn",
    "olympus",
    "omega",
    "oneplus",
    "pantene",
    "paramount",
    "patek",
    "pearson",
    "pentax",
    "pepsi",
    "philips",
    "pioneer",
    "polaroid",
    "popeyes",
    "prada",
    "primark",
    "radisson",
    "rakuten",
    "razer",
    "redmi",
    "remington",
    "renault",
    "revlon",
    "revolut",
    "riot",
    "rockstar",
    "rolex",
    "salesforce",
    "samsung",
    "seiko",
    "sheraton",
    "shopify",
    "siemens",
    "smirnoff",
    "softbank",
    "sony",
    "spotify",
    "staples",
    "steinway",
    "stripe",
    "suntory",
    "target",
    "tiffany",
    "tissot",
    "tomtom",
    "toshiba",
    "toyota",
    "trivago",
    "tupperware",
    "twitter",
    "uber",
    "uniqlo",
    "vans",
    "velcro",
    "versace",
    "visa",
    "vmware",
    "volkswagen",
    "volvo",
    "wacom",
    "walgreens",
    "xbox",
    "youtube",
    "zara",
    "zippo",
    "zoom",
    "zynga",
}


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


def freeze_org_stems(src: Path, out: Path, cities_tsv: Path) -> int:
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
    stems = refine_org_stems(stems, exclusion_vocabulary(cities_tsv))
    write_org_stems(out, stems)
    return len(stems)


def first_column(path: Path) -> set[str]:
    with path.open(encoding="utf-8") as handle:
        next(handle)
        return {line.split("\t")[0].strip().lower() for line in handle if line.strip()}


def exclusion_vocabulary(cities_tsv: Path) -> set[str]:
    """Lower-cased given names, surnames, and frozen city names.

    The name tables are the humandata corpora this package's siblings already
    embed, so a stem can never coincide with a persona's name or a world city.
    """
    humandata = Path(__file__).resolve().parents[2] / "humandata" / "data"
    return (
        first_column(humandata / "given_names.tsv")
        | first_column(humandata / "surnames.tsv")
        | first_column(cities_tsv)
    )


def refine_org_stems(stems: list[str], names_and_places: set[str]) -> list[str]:
    """Second-stage exclusions over the ranked, deduplicated stem list.

    Drops the ORG_HEAD_DROP most-linked retained stems, then every stem that is
    a given name, surname, or city, names a country, region, or nationality,
    is a reviewed personal name, sensitive label, or household brand. Order is
    preserved so a fresh freeze and --refilter agree byte for byte.
    """
    refined: list[str] = []
    for stem in stems[ORG_HEAD_DROP:]:
        key = stem.lower()
        if key in names_and_places:
            continue
        if key in ORG_PLACE_STOPLIST or key in ORG_PERSON_STOPLIST:
            continue
        if key in ORG_SENSITIVE_STOPLIST:
            continue
        if key in ORG_HOUSEHOLD_STOPLIST:
            continue
        refined.append(stem)
    return refined


def write_org_stems(out: Path, stems: list[str]) -> None:
    with out.open("w", newline="") as handle:
        handle.write("stem\n")
        for stem in stems:
            handle.write(stem + "\n")


def refilter_org_stems(data: Path) -> int:
    """Re-apply refine_org_stems to an already frozen data/org_stems.tsv.

    Not idempotent: it drops ORG_HEAD_DROP stems each time, so run it once on a
    first-stage table only.

    Used for the 2026-09-13 table: the Wikidata response was not retained, so
    the second stage was applied to the frozen first-stage list in place. A
    fresh freeze applies both stages in one pass and yields the same bytes for
    the same upstream snapshot.
    """
    path = data / "org_stems.tsv"
    with path.open(encoding="utf-8") as handle:
        header = next(handle)
        if header.strip() != "stem":
            raise SystemExit(f"{path}: unexpected header {header!r}")
        stems = [line.strip() for line in handle if line.strip()]
    stems = refine_org_stems(stems, exclusion_vocabulary(data / "cities.tsv"))
    write_org_stems(path, stems)
    return len(stems)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--refilter":
        data = Path(sys.argv[2] if len(sys.argv) > 2 else "../data")
        print("org_stems", refilter_org_stems(data))
        return
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
            base / "wikidata_orgs.tsv", out / "org_stems.tsv", out / "cities.tsv"
        ),
    }
    for name, count in counts.items():
        print(name, count)


if __name__ == "__main__":
    main()

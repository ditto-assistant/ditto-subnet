// Package persona is the DittoBench v2 plan layer. It
// generates a fresh, procedural "persona universe" (a fact graph plus the
// session scripts that introduce those facts) as a PURE function of a single
// int64 seed. No wall clock, no crypto-rand, no map-iteration order: the same
// seed yields a byte-identical plan (the reproducibility contract).
//
// The plan is Layer 1 of the two-layer engine: the deterministic ground truth.
// Layer 2 (surface realization) renders each beat into a natural user/assistant
// pair from deterministic template surfaces; question derivation and difficulty
// tiers read the plan to build the memory suite. This package intentionally has
// NO LLM dependency: it is the thing everything else is checked against.
//
// # Combinatorics (≥10⁹ distinct universes)
//
// The persona skeleton draws a set of INDEPENDENT scalar attributes, each from
// one of the pools below. A conservative lower bound on the number of distinct
// skeletons, counting only nine always-present scalar attributes:
//
//	firstNames × lastNames × cities(residence) × occupations × companies ×
//	carModels × cities(hometown) × firstNames(partner) × universities
//	= 48 × 40 × 40 × 40 × 32 × 28 × 40 × 48 × 28
//	≈ 1.5 × 10¹⁴  distinct skeletons.
//
// That is five orders of magnitude past the ≥10⁹ target BEFORE counting the
// list attributes (projects/trips/pets, each a multi-draw subset), the
// preference and opinion facts, the update/reversal choices, the near-miss
// distractor draws, or the seed-derived timeline, every one of which
// multiplies the space further. There is nothing to memorize.
package persona

import "github.com/ditto-assistant/dittobench-datagen/internal/humandata"

// The pools are the combinatorial vocabulary of the persona universe. They are
// deliberately large and disjoint enough that same-attribute/different-value
// near-miss distractors are easy to draw. Values are clean canonical
// tokens: a memory answer is checked by normalized containment against the
// value verbatim (scorer.deterministicMemoryHit), so each entry is a single,
// unambiguous phrase with no trailing punctuation.

var firstNames = []string{
	"Alice", "Marcus", "Priya", "Diego", "Yuki", "Fatima", "Sven", "Lena",
	"Omar", "Chloe", "Ravi", "Ingrid", "Tariq", "Nadia", "Kenji", "Rosa",
	"Bjorn", "Amara", "Luca", "Mei", "Hassan", "Freya", "Pablo", "Anika",
	"Dmitri", "Zoe", "Kofi", "Sofia", "Isaac", "Lin", "Mateo", "Hana",
	"Elias", "Noor", "Theo", "Camila", "Aran", "Yara", "Nikolai", "Esme",
	"Rafael", "Iris", "Malik", "Greta", "Sanjay", "Wren", "Otto", "Delphine",
}

var v8HumanGivenNames = humandata.AllGivenNames()

var lastNames = []string{
	"Okafor", "Petrov", "Nakamura", "Silva", "Haugen", "Bianchi", "Kowalski",
	"Reyes", "Andersson", "Farouk", "Chen", "Dubois", "Mbeki", "Larsen",
	"Costa", "Nguyen", "Sharma", "Volkov", "Ferrari", "Hassan", "Bergström",
	"Kim", "Rossi", "Novak", "Adeyemi", "Ito", "Moreau", "Sato", "Vargas",
	"Lindqvist", "Diallo", "Marchetti", "Yilmaz", "Sorensen", "Kaur", "Dupont",
	"Oduya", "Fischer", "Rahman", "Castellano",
}

// cities is used both for residence and (independently) for hometown and trips,
// so it is generous to keep those draws distinct.
var cities = []string{
	"Lisbon", "Osaka", "Nairobi", "Reykjavik", "Medellin", "Tallinn", "Kyoto",
	"Porto", "Bergen", "Marrakesh", "Ljubljana", "Chiang Mai", "Valparaiso",
	"Gdansk", "Hobart", "Cusco", "Ghent", "Kanazawa", "Trondheim", "Oaxaca",
	"Riga", "Ljubljana Hills", "Antwerp", "Sapporo", "Bilbao", "Split",
	"Galway", "Utrecht", "Wroclaw", "Queenstown", "Fez", "Nara", "Aarhus",
	"Bratislava", "Ponta Delgada", "Kotor", "Leuven", "Matera", "Lucerne",
	"Tromso",
}

var occupations = []string{
	"marine biologist", "typographer", "wind-turbine technician", "sommelier",
	"cartographer", "beekeeper", "prosthetist", "arborist", "glassblower",
	"epidemiologist", "luthier", "hydrologist", "millwright", "ceramicist",
	"actuary", "ornithologist", "stonemason", "perfumer", "cryptographer",
	"paleontologist", "falconer", "geodesist", "cheesemaker", "seismologist",
	"puppeteer", "horologist", "cetologist", "vexillologist", "conservator",
	"agronomist", "diplomat", "audiologist", "cooper", "toxicologist",
	"lexicographer", "volcanologist", "farrier", "philatelist", "botanist",
	"radiographer",
}

var companies = []string{
	"Northwind Cartography", "Solvent Labs", "Meridian Optics", "Basalt Works",
	"Halcyon Freight", "Umbra Analytics", "Tidewater Instruments", "Kestrel Foods",
	"Verdant Grid", "Lodestar Robotics", "Cobalt & Reed", "Aster Biomes",
	"Pennant Aerospace", "Quillfeather Press", "Riverstone Ceramics",
	"Cindergate Steel", "Marrow & Vine", "Palisade Data", "Thornfield Optics",
	"Sablewood Timber", "Ochre Pigments", "Fathom Sonar", "Gantry Rail",
	"Wexler Instruments", "Selkie Marine", "Ambergris Perfumes", "Drift Textiles",
	"Kilnhouse Glass", "Ledger & Loom", "Foxglove Botanicals", "Talisker Freight",
	"Bramble Networks",
}

var carModels = []string{
	"Volvo XC40", "Subaru Outback", "Toyota Hilux", "Peugeot 308",
	"Skoda Octavia", "Mazda MX-5", "Renault Zoe", "Fiat Panda",
	"Kia Sportage", "Citroen C3", "Honda Jazz", "Dacia Duster",
	"Nissan Leaf", "Seat Ibiza", "Opel Corsa", "Ford Ranger",
	"Hyundai Kona", "Suzuki Jimny", "Mini Countryman", "Vauxhall Astra",
	"Polestar 2", "Cupra Formentor", "Alfa Romeo Tonale", "Genesis GV60",
	"BYD Atto 3", "MG ZS", "Jeep Avenger", "Ora Funky Cat",
}

var universities = []string{
	"Uppsala", "Otago", "Coimbra", "Leiden", "Trinity Dublin", "Padua",
	"Salamanca", "Aarhus", "Bologna", "Ghent", "Tartu", "Lund",
	"Groningen", "Heidelberg", "Basel", "Ljubljana", "Bergen", "Turku",
	"Cordoba", "Valencia", "Reykjavik", "Vilnius", "Wroclaw", "Debrecen",
	"Maribor", "Zagreb", "Tampere", "Umea",
}

var instruments = []string{
	"cello", "bouzouki", "hammered dulcimer", "bandoneon", "kora",
	"theremin", "nyckelharpa", "shakuhachi", "hurdy-gurdy", "oud",
	"marimba", "concertina", "bassoon", "sitar", "harpsichord", "kalimba",
}

// assistant-recommendation pools feed the assistant-side-recall questions: the
// ASSISTANT proposes one of these (the user never names it), so recalling it
// tests reading the assistant's past turn, not the user's. Values are distinctive
// multi/proper-noun tokens (verbatim-preserved through realization).
var asstNovels = []string{
	"Piranesi", "The Left Hand of Darkness", "Klara and the Sun", "A Canticle for Leibowitz",
	"The Dispossessed", "Station Eleven", "The Vorrh", "Annihilation",
}
var asstGadgets = []string{
	"a Grovemade desk shelf", "an Elgato key light", "a Kinesis Advantage keyboard",
	"a Roost laptop stand", "an Anker 737 charger", "a Logitech MX Vertical mouse",
}
var asstPodcasts = []string{
	"Hardcore History", "Radiolab", "99% Invisible", "The Rest Is History",
	"Cautionary Tales", "Reply All", "Twenty Thousand Hertz",
}
var asstTrails = []string{
	"the Angels Landing trail", "the Precipice Loop", "the Cascade Pass route",
	"the Devil's Bridge trail", "the Franconia Ridge Loop", "the Iron Goat trail",
}

// projectNames feeds the multi-session "list all / how many" synthesis
// questions: several are drawn for one persona.
var projectNames = []string{
	"the tide-clock", "the seed-library catalog", "the rooftop apiary",
	"the ferry-schedule app", "the lichen survey", "the model lighthouse",
	"the community darkroom", "the salt-marsh census", "the pinhole camera",
	"the wind-chime array", "the sourdough archive", "the star-map quilt",
	"the river cleanup map", "the typeface revival", "the moss terrarium",
	"the shortwave logbook", "the tidal-pool guide", "the paper-boat regatta",
	"the birdsong index", "the cyanotype series",
}

var petTypes = []string{
	"tortoise", "greyhound", "cockatiel", "corn snake", "ferret",
	"lop rabbit", "axolotl", "border collie", "maine coon", "budgerigar",
	"chinchilla", "bearded dragon", "hedgehog", "cocker spaniel",
}

var petNames = []string{
	"Biscuit", "Marmalade", "Pixel", "Waffles", "Comet", "Nimbus", "Pepper",
	"Juniper", "Tofu", "Clementine", "Bramble", "Sprocket", "Olive", "Miso",
	"Pumpkin", "Dill", "Cinder", "Poppy", "Basil", "Freckle", "Domino",
	"Marlow", "Saffron", "Widget",
}

var cuisines = []string{
	"Ethiopian", "Georgian", "Peranakan", "Basque", "Oaxacan", "Levantine",
	"Sichuan", "Sardinian", "Goan", "Yucatecan", "Uyghur", "Cape Malay",
	"Piedmontese", "Hokkaido", "Andalusian", "Tamil",
}

var dietaryStyles = []string{
	"vegetarian", "pescatarian", "gluten-free", "dairy-free", "vegan",
	"nut-free", "low-sodium", "shellfish-free",
}

var colors = []string{
	"teal", "ochre", "burgundy", "sage", "cobalt", "amber", "plum",
	"terracotta", "chartreuse", "indigo", "coral", "slate", "mustard",
	"lavender",
}

// v8Colors are deliberately multi-word canonical values. Single-token color
// names such as "sage", "coral", "amber", and "slate" occur incidentally in
// ordinary prose and therefore create deterministic-grader false positives.
var v8Colors = []string{
	"deep teal", "burnt ochre", "wine burgundy", "soft sage green",
	"cobalt blue", "warm amber gold", "dark plum", "red terracotta",
	"electric chartreuse", "deep indigo", "coral pink", "slate gray",
	"mustard yellow", "pale lavender",
}

// hobbies feeds the reversible-opinion (contradiction) facts: the persona once
// loved a hobby, then changed their mind.
var hobbies = []string{
	"bouldering", "urban sketching", "sea kayaking", "letterpress printing",
	"foraging", "orienteering", "fly fishing", "contact juggling",
	"astrophotography", "bookbinding", "freediving", "geocaching",
	"wheel throwing", "trail running", "birdwatching", "whittling",
}

// noiseTopics are chit-chat beats (no fact) sprinkled into sessions so recall
// is not trivially "every turn is a fact". They carry no ground truth. They are
// first-person noun phrases so they slot cleanly into the noiseTemplates below
// and read as coherent, topic-continuous filler, not the semantically-disjoint
// padding that inflates lexical-retrieval scores (a well-attested memory-
// benchmark validity critique).
var noiseTopics = []string{
	"the weather this week", "a film I watched last night", "my weekend plans",
	"a book I've been reading", "the traffic on my commute", "a new coffee place downtown",
	"a podcast episode I liked", "the local football scores", "a houseplant that keeps wilting",
	"a recipe that didn't turn out", "the price of groceries lately", "a documentary about the ocean",
	"the neighbours' noisy renovation", "a crossword I got stuck on", "the queue at the post office",
	"a jigsaw puzzle I started",
}

// noiseTemplates are the user/assistant phrasings for a noise beat. A random
// pair is chosen per beat so filler is varied rather than one canned line; the
// assistant turn stays on-topic (coherent filler) but carries no ground truth.
// Assistant templates without a "%s" ignore the topic (fill passes them through).
var noiseTemplates = []struct{ user, asst string }{
	{"Anyway, %s has been on my mind lately.", "Happy to chat about %s whenever you like."},
	{"On a lighter note, I keep meaning to mention %s.", "Sounds good — nice to hear about the everyday stuff too."},
	{"Random aside: %s came up today.", "Thanks for sharing that."},
	{"Off-topic, but %s has kept me busy this week.", "No worries — good to hear how things are going."},
	{"Before I forget, %s is worth a mention.", "Noted — nothing that needs any action on my end."},
	{"Small thing, but %s has been a bit of a saga.", "Ha — %s does sound like a saga."},
}

// v8NoiseSurfaces are complete human thoughts, not noun phrases inserted into
// a generic benchmark wrapper. Each row corresponds to noiseTopics and offers
// two deterministic surfaces so ordinary transcript padding still varies.
var v8NoiseSurfaces = [][2]struct{ user, asst string }{
	{{"The weather has been all over the place this week.", "It really has — I hope it settles down soon."}, {"I can’t figure out what the weather is doing this week.", "Same here. It’s been impossible to plan around."}},
	{{"I watched a film last night and I’m still thinking about it.", "Oh, I love when a film lingers like that."}, {"That film I watched last night was better than I expected.", "A surprise good watch is such a treat."}},
	{{"I’m still figuring out what I want to do this weekend.", "I hope you land on something that feels good."}, {"My weekend plans are a little up in the air right now.", "Honestly, a loose weekend can be nice too."}},
	{{"I’ve been completely absorbed in a book lately.", "That’s the best feeling — tell me about it anytime."}, {"The book I’m reading has finally gotten really good.", "Okay, now I’m curious where it goes."}},
	{{"My commute was a mess again this morning.", "Ugh, that’s such a draining start to the day."}, {"Traffic made my commute take forever today.", "I’m sorry — hopefully the rest of the day is kinder."}},
	{{"I tried the new coffee place downtown today.", "Ooh, was it worth going back?"}, {"That new coffee place downtown is actually pretty cozy.", "That sounds like a lovely little find."}},
	{{"I heard a podcast episode today that I really liked.", "Send me the topic if you feel like talking about it."}, {"A podcast episode caught me off guard in a good way today.", "I love when that happens."}},
	{{"The football scores were wild this weekend.", "Sounds like it was a good weekend to be watching."}, {"I’m still surprised by the football results this week.", "There were definitely a few twists."}},
	{{"Before I forget, my houseplant keeps wilting. I need to water it more often.", "Poor thing — a little reminder might save it."}, {"My houseplant is drooping again; I really need a better watering routine.", "We can absolutely help that little plant recover."}},
	{{"I tried a new recipe and it did not turn out the way I hoped.", "Ah no — cooking experiments can be so humbling."}, {"Dinner was a bit of a disaster tonight. That recipe needs work.", "At least now you know what you’d change next time."}},
	{{"Groceries felt especially expensive this week.", "I know — the total can be such a shock lately."}, {"I swear my grocery bill jumped again this week.", "It really adds up painfully fast."}},
	{{"I watched a documentary about the ocean and couldn’t look away.", "The ocean is endlessly fascinating."}, {"That ocean documentary I mentioned was gorgeous and a little terrifying.", "That sounds exactly like the ocean — beautiful and enormous."}},
	{{"The neighbours’ renovation was so loud today.", "Oof, I hope they wrap up the noisy part soon."}, {"I could barely hear myself think over the renovation next door.", "That would wear me down too."}},
	{{"I got completely stuck on a crossword clue this morning.", "Those stubborn clues have a way of following you around."}, {"One crossword clue has been bothering me all day.", "I bet the answer arrives the second you stop thinking about it."}},
	{{"The queue at the post office took forever today.", "That is such a frustrating way to lose an afternoon."}, {"I spent what felt like half my day waiting at the post office.", "You deserved a medal for making it through that line."}},
	{{"I started a jigsaw puzzle and it has already taken over the table.", "Honestly, that’s how you know it’s a proper puzzle."}, {"My new jigsaw puzzle is much harder than the box made it look.", "The sneaky difficult ones are the most satisfying in the end."}},
}

// --- Professional-domain pools (domain register) ---
//
// Every persona is assigned ONE professional domain (software / medical / legal)
// whose fact families layer onto the universal personal facts. The register
// matters for the benchmark's validity: retrieval and entity-matching quality
// demonstrably do NOT transfer from casual to specialist text (BEIR,
// arXiv:2104.08663), and domain jargon breaks paraphrase + embedding match: the
// exact store→retrieve→match path a memory harness runs. LongMemEval-V2
// (arXiv:2605.12493) makes the same move, reframing the target from a personal
// assistant to an "experienced colleague" over professional environments. These
// pools stay clean canonical tokens (verbatim-preserved answer values) like the
// universal pools above.

// softwareLanguages / softwareEditors / softwareServices feed the
// software-engineering persona. Languages + editors are scalar (with update
// chains: a dev switches primary language / editor); services are a list
// (microservices they own, multi-session "how many / list all" material).
var softwareLanguages = []string{
	"Rust", "Go", "TypeScript", "Python", "Kotlin", "Elixir", "Scala",
	"Swift", "Zig", "Haskell", "Clojure", "Ruby", "OCaml", "Erlang",
	"F#", "Julia", "Nim", "Crystal",
}

// v8SoftwareLanguages include a release qualifier so language names that are
// also ordinary words or names (Go, Rust, Swift, Ruby, Crystal, Julia, Nim) can
// never be credited by an incidental single-word mention.
var v8SoftwareLanguages = []string{
	"Rust 2024", "Go 1.24", "TypeScript 5", "Python 3.13", "Kotlin 2",
	"Elixir 1.18", "Scala 3", "Swift 6", "Zig 0.14", "Haskell 2021",
	"Clojure 1.12", "Ruby 3.4", "OCaml 5", "Erlang OTP 28", "F# 9",
	"Julia 1.11", "Nim 2", "Crystal 1.15",
}

var softwareEditors = []string{
	"Neovim", "VS Code", "Zed", "GoLand", "Emacs", "Sublime Text", "Helix",
	"IntelliJ IDEA", "Xcode", "Cursor", "Fleet", "Kakoune",
}

var softwareServices = []string{
	"auth-proxy", "billing-gateway", "notification-hub", "inventory-sync",
	"search-indexer", "payment-router", "session-store", "rate-limiter",
	"webhook-dispatcher", "audit-logger", "image-resizer", "feature-flag-service",
	"schema-registry", "event-collector", "config-server", "token-vault",
}

// medicalDiagnoses / medicalMedications / medicalAllergies feed the medical
// (patient) persona: a person tracking their own care with the assistant.
// Diagnosis + medication are scalar (with update chains: a re-diagnosis or a
// medication switch, a professional-register knowledge update); allergies are a
// list. Multi-word canonical tokens (e.g. "type 2 diabetes") survive containment
// scoring verbatim, exercising the jargon-paraphrase gap.
var medicalDiagnoses = []string{
	"type 2 diabetes", "hypertension", "hypothyroidism", "asthma",
	"rheumatoid arthritis", "acid reflux", "migraine", "atrial fibrillation",
	"high cholesterol", "psoriasis", "Crohn's disease", "sleep apnea",
	"osteoporosis", "eczema", "gout", "anemia",
}

var medicalMedications = []string{
	"metformin", "lisinopril", "levothyroxine", "atorvastatin", "albuterol",
	"omeprazole", "amlodipine", "sertraline", "gabapentin", "montelukast",
	"warfarin", "prednisone", "losartan", "escitalopram", "pantoprazole",
	"rosuvastatin",
}

var medicalAllergies = []string{
	"penicillin", "peanuts", "latex", "sulfa drugs", "shellfish", "aspirin",
	"codeine", "ibuprofen", "eggs", "soy", "cephalosporins", "tree nuts",
	"bee stings", "iodine contrast",
}

// legalPracticeAreas / legalJurisdictions / legalMatters feed the legal (lawyer)
// persona. Practice area is scalar-updatable (a lateral move into a new
// specialty); jurisdiction is scalar; matters are a list (active cases/deals,
// multi-session synthesis). Matter names read like real case captions/deal
// codenames so the terminology is genuinely specialist.
var legalPracticeAreas = []string{
	"intellectual property law", "corporate mergers and acquisitions",
	"criminal defense", "family law", "immigration law", "employment law",
	"tax law", "environmental law", "bankruptcy law", "real estate law",
	"antitrust law", "securities regulation", "estate planning",
	"maritime law", "patent litigation", "data privacy law",
}

var legalJurisdictions = []string{
	"New York", "California", "Illinois", "Texas", "Massachusetts", "Florida",
	"Washington", "Georgia", "Delaware", "Pennsylvania", "Ontario", "Virginia",
}

var legalMatters = []string{
	"the Hollings acquisition", "Doe v. Meridian", "the Castellan estate",
	"the Ridgeway patent dispute", "the Ashford merger", "the Vantage bankruptcy",
	"the Pelham zoning appeal", "the Karsten trademark filing",
	"the Bellweather class action", "the Orsini custody matter",
	"the Dunmore lease arbitration", "the Fenwick licensing deal",
	"the Marsh v. Coleridge appeal", "the Thornbury estate dispute",
}

// financeRiskProfiles / financeBrokerages / financeHoldings feed the finance
// (retail-investor) persona: a person tracking their portfolio with the
// assistant. Risk tolerance is scalar-updatable (a rebalance after a market
// event, a professional-register knowledge update); brokerage is scalar;
// holdings are a list (tickers/asset classes, multi-session synthesis). Finance
// is a domain where models measurably collapse (FinanceBench, ConvFinQA), so its
// jargon is a strong memory stressor.
var financeRiskProfiles = []string{
	"conservative", "moderately conservative", "moderate", "balanced",
	"growth-oriented", "aggressive", "income-focused", "capital-preservation",
}

var financeBrokerages = []string{
	"Fidelity", "Vanguard", "Charles Schwab", "Interactive Brokers", "Robinhood",
	"Etrade", "Merrill Edge", "TD Ameritrade", "Wealthfront", "Betterment",
	"SoFi Invest", "Public",
}

var financeHoldings = []string{
	"VTI", "VXUS", "BND", "QQQ", "SCHD", "VNQ", "GLD", "TLT", "AAPL", "MSFT",
	"NVDA", "JEPI", "VYM", "VUG", "IWM", "BRKB",
}

// Sometimes-present attribute pools. Each attribute in sometimesSpecs is,
// per seed, self-stated (a normal recall answer), stated only about a decoy
// person (a false-premise abstention), or entirely absent (a pure abstention).
// A fixed absent-question list would be a static decline rule a
// pattern-matching harness could hard-code; rolling presence per seed makes
// decline-vs-answer decidable only by reading the seeded haystack.
var shoeSizes = []string{
	"size 6", "size 7", "size 8", "size 9", "size 10", "size 11", "size 12",
	"size 13",
}

var heightsCm = []string{
	"162 cm", "168 cm", "171 cm", "175 cm", "178 cm", "182 cm", "185 cm",
	"190 cm",
}

// favoriteSongs are coined titles (never real releases) so a base model cannot
// pattern-complete them and a served answer is unambiguous.
var favoriteSongs = []string{
	"Glass Harbor", "Night Elevator", "Paper Satellites", "Copper Rain",
	"The Longest Corridor", "Static Bloom", "Winter Arithmetic",
	"Half-Moon Traffic", "Orchard Static", "Blue Reactor",
}

var starSigns = []string{
	"Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra", "Scorpio",
	"Sagittarius", "Capricorn", "Aquarius", "Pisces",
}

var eyeColors = []string{"brown", "blue", "green", "hazel", "gray", "amber"}

var bloodTypes = []string{
	"O negative", "A positive", "B positive", "AB negative", "O positive",
	"A negative",
}

// birthdayMonths deliberately omits May: the grader's common-word guard
// (grade.commonWords) refuses to credit the bare token "may" (the modal), so a
// May answer could never score — the case would be unwinnable by construction.
// Every pool value must be creditable by the grader (see
// TestPoolValuesGradeable).
var birthdayMonths = []string{
	"January", "February", "March", "April", "June", "July", "August",
	"September", "October", "November", "December",
}

var sportsTeams = []string{
	"the Rangers", "Arsenal", "the Lakers", "Juventus", "the Red Sox",
	"Bayern Munich", "the Maple Leafs", "Ajax",
}

var favoriteFilms = []string{
	"Casablanca", "Blade Runner", "Amélie", "Heat", "Spirited Away",
	"The Third Man", "Arrival", "Chinatown",
}

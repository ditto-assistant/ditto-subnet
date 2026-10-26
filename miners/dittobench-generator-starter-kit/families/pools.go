package families

var cities = []string{
	"Lisbon", "Tallinn", "Porto", "Ljubljana", "Valencia", "Krakow", "Bergen",
	"Ghent", "Bologna", "Tbilisi", "Seville", "Gdansk", "Leipzig", "Utrecht",
	"Aarhus", "Brno", "Cluj", "Graz", "Malmo", "Riga", "Turku", "Vilnius",
	"Zadar", "Lyon", "Nantes", "Bilbao", "Salzburg", "Plovdiv", "Tartu", "Split",
	"Coimbra", "Trieste", "Kaunas", "Pecs", "Bratislava", "Uppsala", "Lille",
	"Groningen", "Innsbruck", "Varna", "Debrecen", "Oulu", "Antwerp", "Bruges",
	"Rotterdam", "Eindhoven", "Maastricht", "Hamburg", "Dresden", "Freiburg",
	"Heidelberg", "Nuremberg", "Bremen", "Hanover", "Zurich", "Basel", "Geneva",
	"Lausanne", "Lucerne", "Vienna", "Linz", "Prague", "Ostrava", "Olomouc",
	"Wroclaw", "Poznan", "Lublin", "Szeged", "Budapest", "Timisoara", "Iasi",
	"Sibiu", "Burgas", "Skopje", "Ohrid", "Belgrade", "Novi Sad", "Zagreb",
	"Rijeka", "Sarajevo", "Mostar", "Podgorica", "Tirana", "Thessaloniki",
	"Patras", "Heraklion", "Naples", "Palermo", "Turin", "Verona", "Genoa",
	"Parma", "Padua", "Marseille", "Bordeaux", "Toulouse", "Strasbourg",
	"Montpellier", "Rennes", "Grenoble", "Granada", "Malaga", "Zaragoza",
	"Cordoba", "Santander", "Oviedo", "Braga", "Faro", "Aveiro", "Cork", "Galway",
	"Limerick", "Belfast", "Glasgow", "Edinburgh", "Aberdeen", "Cardiff",
	"Bristol", "Leeds", "Oslo", "Trondheim", "Stavanger", "Gothenburg", "Lund",
	"Odense", "Aalborg", "Tampere", "Espoo", "Helsinki", "Reykjavik", "Akureyri",
}

// Pool sizes bound MaxCases: each round of the four default families draws 5
// cities, 6 names, 1 role, 1 event, 3 drinks, and 4 allergens, and Builder.Pick
// never reuses a value within a pack. MaxCases (80, twenty rounds) therefore
// needs at least 100 cities, 120 names, 20 roles, 20 events, 60 drinks, and 80
// allergens; TestPoolsFillMaxCases holds that. Values must also stay apart:
// no value may contain another value from any pool, so one case's gold can
// never ground or decoy another's.

var firstNames = []string{
	"Ines", "Tobias", "Marisol", "Henrik", "Aiyana", "Dmitri", "Noor", "Callum",
	"Yuki", "Bastian", "Leilani", "Oskar", "Priya", "Teodor", "Ama", "Rasmus",
	"Zofia", "Emeka", "Liesel", "Joaquin", "Saoirse", "Kenji", "Farida", "Mateo",
	"Ingrid", "Tariq", "Ottilie", "Ravi", "Signe", "Dario", "Hana", "Lorcan",
	"Mirela", "Anselm", "Keziah", "Bram", "Yasmin", "Florin", "Thandiwe", "Ilan",
	"Wren", "Casimir", "Odalys", "Pavel", "Nkiru", "Soren", "Ximena", "Halvard",
	"Annika", "Dagny", "Kofi", "Rosalind", "Adaeze", "Bjorn", "Celestine",
	"Darius", "Elodie", "Fintan", "Gisela", "Hamish", "Ifeoma", "Jarrah",
	"Katarina", "Leopold", "Magnus", "Nadia", "Orla", "Philippa", "Quentin",
	"Rhiannon", "Stellan", "Tamsin", "Ulrich", "Valentina", "Wilhelmina",
	"Xavier", "Yara", "Zoltan", "Anouk", "Benedikt", "Cosima", "Desmond",
	"Esperanza", "Fabian", "Greta", "Horatio", "Imogen", "Jovan", "Klaus",
	"Ludmila", "Mihail", "Nikolai", "Ophelia", "Piotr", "Radu", "Svetlana",
	"Thaddeus", "Vesna", "Wolfgang", "Yevgenia", "Zuzana", "Agnieszka", "Bartosz",
	"Cormac", "Dorotea", "Eamon", "Fenna", "Gunnar", "Hedda", "Ignatius",
	"Jolanda", "Kasimir", "Lavinia", "Marek", "Niamh", "Olafur", "Petra",
	"Ragnhild", "Sebastiano", "Tomasz", "Ursula", "Viktor", "Wiebke", "Yannick",
	"Zelda", "Aurelio", "Beatrix", "Cyprian", "Delphine", "Emrys", "Frieda",
}

var roles = []string{
	"dentist", "landlord", "team lead", "vet", "piano teacher", "accountant",
	"physio", "barber", "tax advisor", "climbing coach", "optician", "pharmacist",
	"plumber", "electrician", "mechanic", "dermatologist", "yoga instructor",
	"swim coach", "guitar teacher", "therapist", "dog walker", "babysitter",
	"personal trainer", "hairdresser", "language tutor", "family doctor",
}

var events = []string{
	"team offsite", "book club retreat", "alumni reunion", "product summit",
	"family reunion", "choir tour", "wedding rehearsal", "charity gala",
	"hackathon", "quiz night", "garden party", "board game night",
	"cooking class", "film screening", "photo walk", "baby shower",
	"retirement party", "housewarming", "chess tournament", "pottery workshop",
	"wine tasting", "farewell dinner", "science fair", "yoga retreat",
	"trivia final", "sailing regatta",
}

var drinksPool = []string{
	"espresso", "matcha", "rooibos", "kombucha", "chai", "yerba mate",
	"cold brew", "oat latte", "green tea", "hot chocolate", "kefir", "cortado",
	"hojicha", "corn silk tea", "turmeric latte", "lassi", "genmaicha", "americano",
	"flat white", "ginger tea", "sencha", "mint tea", "macchiato", "chamomile",
	"golden milk", "pu-erh", "horchata", "earl grey", "lapsang souchong",
	"oolong", "jasmine tea", "hibiscus tea", "lemon verbena", "rosehip tea",
	"nettle tea", "dandelion coffee", "chicory coffee", "mocha", "affogato",
	"ristretto", "lungo", "cappuccino", "cafe au lait", "turkish coffee",
	"vietnamese coffee", "bubble tea", "yuzu tea", "sparkling water",
	"carrot juice", "beet juice", "apple cider", "elderflower cordial",
	"ginger beer", "kvass", "ayran", "chaga tea", "guayusa", "cascara", "bancha",
	"kukicha", "gyokuro", "white tea", "drip coffee", "piccolo", "red eye",
	"breve", "sikhye",
}

var allergens = []string{
	"peanuts", "shellfish", "sesame", "kiwi", "walnuts", "mustard", "celery",
	"lupin", "buckwheat", "mango", "soy", "cashews", "hazelnuts", "fennel",
	"pistachios", "squid", "lentils", "avocado", "chickpeas", "anchovies",
	"pecans", "papaya", "quinoa", "eggplant", "oysters", "macadamias", "almonds",
	"crab", "pineapple", "peppers", "mussels", "coconut", "strawberries",
	"tomatoes", "barley", "wheat", "eggs", "garlic", "onions", "cinnamon",
	"nutmeg", "cloves", "saffron", "cumin", "coriander", "paprika", "cherries",
	"peaches", "apricots", "plums", "bananas", "melons", "grapes", "figs",
	"lychee", "blueberries", "raspberries", "oranges", "limes", "potatoes",
	"cucumber", "zucchini", "spinach", "mushrooms", "sunflower seeds",
	"poppy seeds", "flaxseed", "pumpkin seeds", "chestnuts", "brazil nuts",
	"pine nuts", "clams", "scallops", "lobster", "shrimp", "salmon", "tuna",
	"sardines", "mackerel", "gelatin", "millet", "sorghum", "tapioca", "vanilla",
	"yeast",
}

var filler = [][2]string{
	{"Remind me to water the plants on Thursday.", "I'll remind you Thursday."},
	{"Watched a documentary about octopuses last night.", "They're fascinating."},
	{"Thinking about learning to bake sourdough.", "Start with a simple starter."},
	{"It's been unusually windy this week.", "Windy weeks can be draining."},
	{"Need to renew my passport sometime this year.", "Worth starting early."},
}

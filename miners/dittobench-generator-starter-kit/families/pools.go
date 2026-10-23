package families

var cities = []string{
	"Lisbon", "Tallinn", "Porto", "Ljubljana", "Valencia", "Krakow", "Bergen",
	"Ghent", "Bologna", "Tbilisi", "Seville", "Gdansk", "Leipzig", "Utrecht",
	"Aarhus", "Brno", "Cluj", "Graz", "Malmo", "Riga", "Turku", "Vilnius",
	"Zadar", "Lyon", "Nantes", "Bilbao", "Salzburg", "Plovdiv", "Tartu", "Split",
	"Coimbra", "Trieste", "Kaunas", "Pecs", "Bratislava", "Uppsala", "Lille",
	"Groningen", "Innsbruck", "Varna", "Debrecen", "Oulu",
}

// Pool sizes bound MaxCases: each round of the four default families draws 5
// cities, 6 names, 1 role, 1 event, 3 drinks, and 4 allergens.

var firstNames = []string{
	"Ines", "Tobias", "Marisol", "Henrik", "Aiyana", "Dmitri", "Noor", "Callum",
	"Yuki", "Bastian", "Leilani", "Oskar", "Priya", "Teodor", "Ama", "Rasmus",
	"Zofia", "Emeka", "Liesel", "Joaquin", "Saoirse", "Kenji", "Farida", "Mateo",
	"Ingrid", "Tariq", "Ottilie", "Ravi", "Signe", "Dario", "Hana", "Lorcan",
	"Mirela", "Anselm", "Keziah", "Bram", "Yasmin", "Florin", "Thandiwe", "Ilan",
	"Wren", "Casimir", "Odalys", "Pavel", "Nkiru", "Soren", "Ximena", "Halvard",
	"Ines-Maria", "Dagny", "Kofi", "Rosalind",
}

var roles = []string{
	"dentist", "landlord", "team lead", "vet", "piano teacher", "accountant",
	"physio", "barber", "tax advisor", "climbing coach",
}

var events = []string{
	"team offsite", "book club retreat", "alumni reunion", "product summit",
	"family reunion", "choir tour", "wedding rehearsal", "charity gala",
	"hackathon", "quiz night",
}

var drinksPool = []string{
	"espresso", "matcha", "rooibos", "kombucha", "chai", "yerba mate",
	"cold brew", "oat latte", "green tea", "hot chocolate", "kefir",
	"cortado", "hojicha", "barley tea", "turmeric latte", "lassi", "genmaicha",
	"americano", "flat white", "ginger tea", "sencha", "mint tea", "macchiato",
	"chamomile", "golden milk", "pu-erh", "horchata",
}

var allergens = []string{
	"peanuts", "shellfish", "sesame", "kiwi", "walnuts", "mustard", "celery",
	"lupin", "buckwheat", "mango", "soy", "cashews", "hazelnuts", "fennel",
	"pistachios", "squid", "lentils", "avocado", "chickpeas", "anchovies",
	"pecans", "papaya", "quinoa", "eggplant", "oysters", "macadamias",
	"almonds", "crab", "pineapple", "peppers", "mussels", "coconut",
	"strawberries", "tomatoes", "barley",
}

var filler = [][2]string{
	{"Remind me to water the plants on Thursday.", "I'll remind you Thursday."},
	{"Watched a documentary about octopuses last night.", "They're fascinating."},
	{"Thinking about learning to bake sourdough.", "Start with a simple starter."},
	{"It's been unusually windy this week.", "Windy weeks can be draining."},
	{"Need to renew my passport sometime this year.", "Worth starting early."},
}

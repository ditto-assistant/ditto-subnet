package multilingual

// Per-language lexicons. Every entry is lowercase, accent-preserving (the
// grader folds compatibility forms and case, never diacritics), and chosen to
// be an unambiguous stance or structure marker: a token that also occurs as an
// ordinary content word in that language is left out rather than risk crediting
// an incidental mention. The English tables live in the grade package.

var spanish = Lexicon{
	Language: Spanish,
	Decline: []string{
		"no tengo", "no lo tengo", "no sé", "no lo sé", "no hay registro", "no consta",
		"no me consta", "no encuentro", "no lo encuentro", "no tengo constancia",
		"nunca mencionaste", "no lo mencionaste", "no tengo esa información",
		"no está registrado", "no dispongo", "no tengo información", "no me lo dijiste",
		"ya no lo tengo", "lo he eliminado", "no estoy seguro", "no estoy segura",
	},
	Acknowledge: []string{
		"hecho", "listo", "eliminado", "eliminada", "borrado", "borrada", "olvidado",
		"quitado", "anotado", "lo he borrado", "lo eliminé", "lo borré", "ya está",
		"queda eliminado", "no lo guardaré", "no lo recordaré",
	},
	Increase: []string{
		"aumentó", "aumento", "aumentado", "subió", "subida", "sube", "creció",
		"crecimiento", "incrementó", "incremento", "más alto", "ganancia", "ganó",
		"ganando", "al alza", "hacia arriba", "se elevó", "elevó",
	},
	Decrease: []string{
		"disminuyó", "disminución", "bajó", "bajada", "baja", "cayó", "caída",
		"redujo", "reducción", "reducido", "más bajo", "pérdida", "perdió",
		"perdiendo", "a la baja", "hacia abajo", "recorte", "recortó",
	},
	Unchanged: []string{
		"sin cambios", "sin cambio", "se mantuvo igual", "no cambió", "sin variación",
		"ni subió ni bajó", "se mantiene igual", "quedó igual", "estable", "no varió",
	},
	NumberWords: map[string]int{
		"cero": 0, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
		"seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
		"doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciséis": 16,
		"dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
	},
	Months: map[string]int{
		"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
		"julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
		"noviembre": 11, "diciembre": 12,
		"ene": 1, "feb": 2, "abr": 4, "jun": 6, "jul": 7, "ago": 8, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
	},
	MinorUnit:  []string{"centavos", "centavo", "céntimos", "céntimo", "centimos", "centimo"},
	MajorUnit:  []string{"dólares", "dolares", "euros", "libras", "pesos", "reales"},
	Rejection:  []string{"no", "nunca", "jamás", "en lugar de", "en vez de", "ya no", "tampoco", "ni", "sino", "no es", "no era", "no fue"},
	Contrast:   []string{"pero", "sino", "aunque", "sin embargo", "mientras que"},
	PastStrong: []string{"antes", "anteriormente", "antiguamente", "solía", "solías", "al principio", "inicialmente", "originalmente", "en el pasado", "primero pensé", "al inicio"},
	PastWeak:   []string{"era", "fue", "estaba", "tenía", "tenías", "había"},
	Current:    []string{"ahora", "actualmente", "hoy", "en la actualidad", "actual", "vigente", "a día de hoy"},
	Correction: []string{"en realidad", "corrección", "perdón", "mejor dicho", "quiero decir", "espera", "me corrijo", "rectifico"},
	Hedge:      []string{"quizás", "quizá", "tal vez", "posiblemente", "podría ser", "puede ser", "una de", "alguna de", "o bien"},
	Enumerator: []string{"o", "u", "y", "e"},
	ClaimCue:   []string{"es", "son", "queda", "quedan", "restan", "resta", "total", "en total", "resultado", "saldo", "equivale", "igual a", "deja", "da", "quedaron", "restante", "restantes", "tienes", "tiene", "hay"},
	Clarify:    []string{"cuál", "cuáles", "qué", "podrías especificar", "puedes especificar", "a qué te refieres", "cuál de", "me puedes decir", "necesito saber", "podrías indicar", "cuál prefieres"},
	Echo:       []string{"preguntaste", "tu pregunta", "me preguntas", "si sube o baja"},
	Interrog:   []string{"cuál", "cuáles", "qué", "cómo", "dónde", "cuándo", "quién"},
}

var portuguese = Lexicon{
	Language: Portuguese,
	Decline: []string{
		"não tenho", "não sei", "não há registro", "não há registo", "não consta",
		"não encontro", "não encontrei", "nunca mencionou", "não mencionou",
		"não tenho essa informação", "não está registrado", "não está registado",
		"não disponho", "não tenho informação", "não me disse", "já não tenho",
		"não tenho certeza", "não tenho a certeza",
	},
	Acknowledge: []string{
		"feito", "pronto", "eliminado", "eliminada", "apagado", "apagada", "removido",
		"removida", "esquecido", "anotado", "apaguei", "removi", "eliminei",
		"já está", "não vou guardar", "não vou lembrar",
	},
	Increase: []string{
		"aumentou", "aumento", "subiu", "subida", "sobe", "cresceu", "crescimento",
		"incrementou", "incremento", "mais alto", "ganho", "ganhou", "ganhando",
		"em alta", "para cima", "elevou",
	},
	Decrease: []string{
		"diminuiu", "diminuição", "baixou", "baixa", "caiu", "queda", "reduziu",
		"redução", "reduzido", "mais baixo", "perda", "perdeu", "perdendo",
		"em baixa", "para baixo", "corte", "cortou",
	},
	Unchanged: []string{
		"sem alteração", "sem alterações", "sem mudança", "sem mudanças", "manteve-se igual",
		"não mudou", "não se alterou", "nem subiu nem baixou", "ficou igual", "estável",
	},
	NumberWords: map[string]int{
		"zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4,
		"cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11,
		"doze": 12, "treze": 13, "catorze": 14, "quatorze": 14, "quinze": 15,
		"dezesseis": 16, "dezasseis": 16, "dezessete": 17, "dezassete": 17,
		"dezoito": 18, "dezenove": 19, "dezanove": 19, "vinte": 20,
	},
	Months: map[string]int{
		"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
		"julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
		"jan": 1, "fev": 2, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
	},
	MinorUnit:  []string{"centavos", "centavo", "cêntimos", "cêntimo", "centimos", "centimo"},
	MajorUnit:  []string{"dólares", "dolares", "euros", "libras", "reais", "real"},
	Rejection:  []string{"não", "nunca", "jamais", "em vez de", "em lugar de", "já não", "tampouco", "nem", "senão", "não é", "não era", "não foi"},
	Contrast:   []string{"mas", "porém", "contudo", "embora", "no entanto", "enquanto que"},
	PastStrong: []string{"antes", "anteriormente", "antigamente", "costumava", "costumavas", "no início", "inicialmente", "originalmente", "no passado", "primeiro pensei"},
	PastWeak:   []string{"era", "foi", "estava", "tinha", "tinhas", "havia"},
	Current:    []string{"agora", "atualmente", "actualmente", "hoje", "atual", "actual", "vigente", "hoje em dia"},
	Correction: []string{"na verdade", "correção", "correcção", "desculpe", "desculpa", "melhor dizendo", "quero dizer", "espera", "corrijo"},
	Hedge:      []string{"talvez", "quiçá", "possivelmente", "pode ser", "poderia ser", "uma de", "um de", "ou então"},
	Enumerator: []string{"ou", "e"},
	ClaimCue:   []string{"é", "são", "fica", "ficam", "resta", "restam", "sobra", "sobram", "total", "no total", "resultado", "saldo", "equivale", "igual a", "deixa", "dá", "restante", "restantes", "tens", "tem", "há"},
	Clarify:    []string{"qual", "quais", "que", "poderia especificar", "pode especificar", "a que se refere", "qual dos", "qual das", "pode me dizer", "preciso saber", "qual prefere"},
	Echo:       []string{"perguntou", "perguntaste", "sua pergunta", "tua pergunta"},
	Interrog:   []string{"qual", "quais", "que", "como", "onde", "quando", "quem"},
}

var french = Lexicon{
	Language: French,
	Decline: []string{
		"je n'ai pas", "je n’ai pas", "je ne sais pas", "aucune trace", "aucun enregistrement",
		"je ne trouve pas", "je n'ai rien", "je n’ai rien", "vous n'avez jamais mentionné",
		"vous n’avez jamais mentionné", "pas d'information", "pas d’information",
		"je n'ai pas cette information", "je n’ai pas cette information", "non renseigné",
		"je ne l'ai plus", "je ne l’ai plus", "je ne suis pas sûr", "je ne suis pas sûre",
	},
	Acknowledge: []string{
		"fait", "c'est fait", "c’est fait", "supprimé", "supprimée", "effacé", "effacée",
		"retiré", "retirée", "oublié", "noté", "je l'ai supprimé", "je l’ai supprimé",
		"c'est noté", "c’est noté", "je ne le garderai pas", "je ne m'en souviendrai pas",
	},
	Increase: []string{
		"augmenté", "augmentation", "a augmenté", "monté", "hausse", "en hausse",
		"a grimpé", "grimpé", "croissance", "gain", "gagné", "plus élevé", "plus haut",
		"à la hausse", "s'est élevé", "relevé",
	},
	Decrease: []string{
		"diminué", "diminution", "a diminué", "baissé", "baisse", "en baisse", "a chuté",
		"chuté", "réduit", "réduction", "perte", "perdu", "plus bas", "moins élevé",
		"à la baisse", "coupé", "coupe",
	},
	Unchanged: []string{
		"inchangé", "inchangée", "sans changement", "pas changé", "n'a pas changé",
		"n’a pas changé", "resté le même", "restée la même", "ni monté ni baissé", "stable",
	},
	NumberWords: map[string]int{
		"zéro": 0, "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
		"six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
		"treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "dix-sept": 17,
		"dix-huit": 18, "dix-neuf": 19, "vingt": 20,
	},
	Months: map[string]int{
		"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
		"juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
		"janv": 1, "févr": 2, "avr": 4, "juil": 7, "sept": 9, "oct": 10, "nov": 11, "déc": 12,
	},
	MinorUnit:  []string{"centimes", "centime", "cents", "cent"},
	MajorUnit:  []string{"dollars", "euros", "livres"},
	Rejection:  []string{"pas", "non", "jamais", "plutôt que", "au lieu de", "ne plus", "ni", "n'est pas", "n’est pas", "n'était pas", "n’était pas", "ce n'est pas", "ce n’est pas"},
	Contrast:   []string{"mais", "cependant", "toutefois", "bien que", "pourtant", "tandis que"},
	PastStrong: []string{"avant", "auparavant", "autrefois", "précédemment", "au départ", "initialement", "à l'origine", "à l’origine", "dans le passé", "au début", "j'ai d'abord pensé"},
	PastWeak:   []string{"était", "étaient", "fut", "avait", "aviez"},
	Current:    []string{"maintenant", "actuellement", "aujourd'hui", "aujourd’hui", "à présent", "désormais", "actuel", "actuelle", "en vigueur"},
	Correction: []string{"en fait", "correction", "pardon", "désolé", "désolée", "plutôt", "je veux dire", "attendez", "je me corrige", "rectification"},
	Hedge:      []string{"peut-être", "possiblement", "pourrait être", "ce pourrait être", "l'un de", "l’un de", "l'une de", "l’une de", "ou bien", "soit"},
	Enumerator: []string{"ou", "et"},
	ClaimCue:   []string{"est", "sont", "reste", "restent", "il reste", "total", "au total", "résultat", "solde", "équivaut", "égal à", "égale", "laisse", "donne", "restant", "restants", "vous avez", "il y a"},
	Clarify:    []string{"quel", "quelle", "quels", "quelles", "lequel", "laquelle", "pouvez-vous préciser", "pourriez-vous préciser", "que voulez-vous dire", "de quoi parlez-vous", "j'ai besoin de savoir", "j’ai besoin de savoir", "précisez"},
	Echo:       []string{"vous avez demandé", "votre question", "vous demandez"},
	Interrog:   []string{"quel", "quelle", "quels", "quelles", "lequel", "laquelle", "comment", "où", "quand", "qui", "que", "combien"},
}

var italian = Lexicon{
	Language: Italian,
	Decline: []string{
		"non ho", "non lo so", "non so", "nessuna traccia", "nessun record", "non trovo",
		"non risulta", "non mi risulta", "non hai mai menzionato", "non l'hai menzionato",
		"non l’hai menzionato", "nessuna informazione", "non ho questa informazione",
		"non è registrato", "non ce l'ho più", "non ce l’ho più", "non sono sicuro", "non sono sicura",
	},
	Acknowledge: []string{
		"fatto", "eliminato", "eliminata", "cancellato", "cancellata", "rimosso",
		"rimossa", "dimenticato", "annotato", "l'ho eliminato", "l’ho eliminato",
		"l'ho cancellato", "l’ho cancellato", "è fatto", "non lo conserverò", "non lo ricorderò",
	},
	Increase: []string{
		"aumentato", "aumento", "è aumentato", "salito", "è salito", "salita", "cresciuto",
		"crescita", "incrementato", "incremento", "guadagno", "guadagnato", "più alto",
		"al rialzo", "in aumento", "in crescita", "rialzo",
	},
	Decrease: []string{
		"diminuito", "diminuzione", "è diminuito", "sceso", "è sceso", "calo", "calato",
		"ridotto", "riduzione", "perdita", "perso", "più basso", "al ribasso",
		"in calo", "in diminuzione", "taglio", "tagliato",
	},
	Unchanged: []string{
		"invariato", "invariata", "senza variazioni", "senza cambiamenti", "non è cambiato",
		"non è cambiata", "rimasto uguale", "rimasta uguale", "né salito né sceso", "stabile",
	},
	NumberWords: map[string]int{
		"zero": 0, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
		"sette": 7, "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12,
		"tredici": 13, "quattordici": 14, "quindici": 15, "sedici": 16, "diciassette": 17,
		"diciotto": 18, "diciannove": 19, "venti": 20,
	},
	Months: map[string]int{
		"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
		"luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
		"gen": 1, "feb": 2, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
	},
	MinorUnit:  []string{"centesimi", "centesimo", "cent"},
	MajorUnit:  []string{"dollari", "euro", "sterline"},
	Rejection:  []string{"non", "mai", "invece di", "anziché", "anziche", "non più", "né", "ne", "non è", "non era", "non fu"},
	Contrast:   []string{"ma", "però", "tuttavia", "sebbene", "anche se", "mentre"},
	PastStrong: []string{"prima", "precedentemente", "in precedenza", "un tempo", "all'inizio", "all’inizio", "inizialmente", "originariamente", "in passato", "avevo pensato"},
	PastWeak:   []string{"era", "erano", "fu", "aveva", "avevi"},
	Current:    []string{"ora", "adesso", "attualmente", "oggi", "al momento", "attuale", "in vigore"},
	Correction: []string{"in realtà", "correzione", "scusa", "scusi", "anzi", "cioè", "voglio dire", "aspetta", "mi correggo", "rettifica"},
	Hedge:      []string{"forse", "magari", "probabilmente", "potrebbe essere", "può essere", "uno di", "una di", "oppure"},
	Enumerator: []string{"o", "oppure", "e", "ed"},
	ClaimCue:   []string{"è", "sono", "resta", "restano", "rimane", "rimangono", "totale", "in totale", "risultato", "saldo", "equivale", "pari a", "uguale a", "lascia", "dà", "residuo", "residui", "hai", "ha", "ci sono"},
	Clarify:    []string{"quale", "quali", "che cosa", "cosa", "puoi specificare", "potresti specificare", "cosa intendi", "a cosa ti riferisci", "quale dei", "quale delle", "ho bisogno di sapere", "quale preferisci"},
	Echo:       []string{"hai chiesto", "la tua domanda", "mi chiedi"},
	Interrog:   []string{"quale", "quali", "cosa", "come", "dove", "quando", "chi", "quanto", "quanti"},
}

var german = Lexicon{
	Language: German,
	Decline: []string{
		"ich habe keine", "ich habe nichts", "ich weiß nicht", "ich weiss nicht", "kein eintrag",
		"keine aufzeichnung", "ich finde nichts", "ich finde keine", "nicht erwähnt",
		"nie erwähnt", "keine information", "keine informationen", "liegt mir nicht vor",
		"nicht gespeichert", "habe ich nicht mehr", "ich bin mir nicht sicher", "nicht bekannt",
	},
	Acknowledge: []string{
		"erledigt", "gelöscht", "geloescht", "entfernt", "vergessen", "gestrichen",
		"notiert", "ich habe es gelöscht", "ich habe es entfernt", "ist gelöscht",
		"ist erledigt", "werde ich nicht speichern", "werde ich nicht behalten",
	},
	Increase: []string{
		"gestiegen", "anstieg", "erhöht", "erhoeht", "erhöhung", "erhoehung", "zugenommen",
		"zunahme", "gewachsen", "wachstum", "gewinn", "höher", "hoeher", "nach oben",
		"aufwärts", "aufwaerts", "angestiegen", "zugelegt", "plus",
	},
	Decrease: []string{
		"gesunken", "rückgang", "rueckgang", "verringert", "verringerung", "gefallen",
		"reduziert", "reduzierung", "abgenommen", "abnahme", "verlust", "niedriger",
		"nach unten", "abwärts", "abwaerts", "gekürzt", "gekuerzt", "kürzung", "minus",
	},
	Unchanged: []string{
		"unverändert", "unveraendert", "keine änderung", "keine aenderung", "gleich geblieben",
		"nicht verändert", "nicht veraendert", "weder gestiegen noch gefallen", "stabil", "konstant",
	},
	NumberWords: map[string]int{
		"null": 0, "eins": 1, "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
		"fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11,
		"zwölf": 12, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15, "fuenfzehn": 15,
		"sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19, "zwanzig": 20,
	},
	Months: map[string]int{
		"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
		"juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
		"jan": 1, "feb": 2, "mär": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dez": 12,
	},
	MinorUnit:  []string{"cent", "cents", "rappen", "pfennig"},
	MajorUnit:  []string{"dollar", "euro", "pfund", "franken"},
	Rejection:  []string{"nicht", "kein", "keine", "nie", "niemals", "statt", "anstatt", "anstelle von", "nicht mehr", "weder", "noch", "ist nicht", "war nicht"},
	Contrast:   []string{"aber", "sondern", "jedoch", "obwohl", "allerdings", "während", "waehrend", "doch"},
	PastStrong: []string{"früher", "frueher", "vorher", "zuvor", "ursprünglich", "urspruenglich", "anfangs", "zunächst", "zunaechst", "damals", "in der vergangenheit", "zuerst dachte ich"},
	PastWeak:   []string{"war", "waren", "hatte", "hattest", "hatten"},
	Current:    []string{"jetzt", "aktuell", "derzeit", "heute", "inzwischen", "mittlerweile", "gegenwärtig", "gegenwaertig", "nun"},
	Correction: []string{"eigentlich", "korrektur", "entschuldigung", "verzeihung", "genauer gesagt", "ich meine", "moment", "ich korrigiere", "vielmehr"},
	Hedge:      []string{"vielleicht", "möglicherweise", "moeglicherweise", "eventuell", "könnte sein", "koennte sein", "kann sein", "entweder", "eines von", "eine von"},
	Enumerator: []string{"oder", "und", "bzw"},
	ClaimCue:   []string{"ist", "sind", "beträgt", "betraegt", "bleibt", "bleiben", "verbleibt", "verbleiben", "gesamt", "insgesamt", "ergebnis", "saldo", "entspricht", "gleich", "ergibt", "macht", "übrig", "uebrig", "verbleibend", "du hast", "sie haben", "es gibt"},
	Clarify:    []string{"welche", "welcher", "welches", "was genau", "können sie präzisieren", "kannst du präzisieren", "was meinst du", "was meinen sie", "welche von", "ich muss wissen", "bitte angeben", "welche bevorzugst du"},
	Echo:       []string{"du hast gefragt", "sie haben gefragt", "deine frage", "ihre frage", "ob es"},
	Interrog:   []string{"welche", "welcher", "welches", "was", "wie", "wo", "wann", "wer", "wie viel", "wie viele"},
}

var dutch = Lexicon{
	Language: Dutch,
	Decline: []string{
		"ik heb geen", "ik heb niets", "ik weet het niet", "ik weet niet", "geen gegevens",
		"geen registratie", "ik vind niets", "ik kan niets vinden", "nooit genoemd",
		"niet genoemd", "geen informatie", "ik heb die informatie niet", "niet opgeslagen",
		"heb ik niet meer", "ik weet het niet zeker", "niet bekend",
	},
	Acknowledge: []string{
		"gedaan", "klaar", "verwijderd", "gewist", "vergeten", "geschrapt", "genoteerd",
		"ik heb het verwijderd", "ik heb het gewist", "is verwijderd", "is geregeld",
		"zal ik niet bewaren", "zal ik niet onthouden",
	},
	Increase: []string{
		"gestegen", "stijging", "verhoogd", "verhoging", "toegenomen", "toename",
		"gegroeid", "groei", "winst", "hoger", "omhoog", "opwaarts", "toegevoegd", "plus",
	},
	Decrease: []string{
		"gedaald", "daling", "verlaagd", "verlaging", "afgenomen", "afname", "gezakt",
		"verminderd", "vermindering", "verlies", "lager", "omlaag", "neerwaarts", "gekort", "korting", "min",
	},
	Unchanged: []string{
		"ongewijzigd", "onveranderd", "geen verandering", "geen wijziging", "gelijk gebleven",
		"niet veranderd", "niet gewijzigd", "noch gestegen noch gedaald", "stabiel", "hetzelfde gebleven",
	},
	NumberWords: map[string]int{
		"nul": 0, "een": 1, "één": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5, "zes": 6,
		"zeven": 7, "acht": 8, "negen": 9, "tien": 10, "elf": 11, "twaalf": 12,
		"dertien": 13, "veertien": 14, "vijftien": 15, "zestien": 16, "zeventien": 17,
		"achttien": 18, "negentien": 19, "twintig": 20,
	},
	Months: map[string]int{
		"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7,
		"augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
		"jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dec": 12,
	},
	MinorUnit:  []string{"cent", "centen", "eurocent"},
	MajorUnit:  []string{"dollar", "euro", "pond"},
	Rejection:  []string{"niet", "geen", "nooit", "in plaats van", "niet meer", "noch", "is niet", "was niet", "evenmin"},
	Contrast:   []string{"maar", "echter", "hoewel", "toch", "terwijl", "daarentegen"},
	PastStrong: []string{"vroeger", "eerder", "voorheen", "oorspronkelijk", "aanvankelijk", "in het begin", "destijds", "in het verleden", "eerst dacht ik"},
	PastWeak:   []string{"was", "waren", "had", "hadden"},
	Current:    []string{"nu", "momenteel", "tegenwoordig", "vandaag", "inmiddels", "huidig", "huidige", "thans"},
	Correction: []string{"eigenlijk", "correctie", "sorry", "excuus", "ik bedoel", "wacht", "ik corrigeer", "of beter gezegd", "beter gezegd"},
	Hedge:      []string{"misschien", "mogelijk", "wellicht", "zou kunnen zijn", "kan zijn", "een van", "één van", "ofwel", "hetzij"},
	Enumerator: []string{"of", "en"},
	ClaimCue:   []string{"is", "zijn", "bedraagt", "blijft", "blijven", "resteert", "resteren", "totaal", "in totaal", "resultaat", "saldo", "komt neer op", "gelijk aan", "levert", "geeft", "over", "resterend", "resterende", "je hebt", "u heeft", "er zijn"},
	Clarify:    []string{"welke", "welk", "wat precies", "kun je specificeren", "kunt u specificeren", "wat bedoel je", "wat bedoelt u", "welke van", "ik moet weten", "geef aan", "welke heb je liever"},
	Echo:       []string{"je vroeg", "u vroeg", "je vraag", "uw vraag", "of het"},
	Interrog:   []string{"welke", "welk", "wat", "hoe", "waar", "wanneer", "wie", "hoeveel"},
}

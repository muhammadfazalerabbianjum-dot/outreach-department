"""Dish personalization from review text, without the model (2026-11-07).

WHY THIS FILE EXISTS — the measured failure it answers:

  * `message_log` holds 122 sends and **0** of them carried a dish; the DM copy
    literally asks the owner "what's the dish that earns it" because we never had
    one. `leads.extracted_dish` was set on 1 of 3,159 leads.
  * `dishes.extract_dish` is model-only (`ask_with_guard`), and Ollama is the
    least available dependency in this stack (repeated `halts` entries, and the
    queue parks on `fail_closed_pause` whenever it is down).
  * The evidence the model was being asked to summarise is already text in the
    lead payload: 429 scraper leads carry review bodies ("The lamb mandi is
    unreal"), which is exactly where a signature dish shows up.

PRECISION OVER COVERAGE (learned the hard way, same day). A first version scored
praise-adjacent word n-grams and "found" a dish on 421 of 429 leads — while
proposing `we`, `it`, `definitely one`, `hot straight from`, `balance between
casual`. A dish that reads like that is worse than no dish (addendum 5), because
the copy would quote it at the owner. So a candidate must now clear one of three
real dish signals: it is a MENU item of this restaurant, it contains a term from
the halal/MENA dish lexicon, or it recurs in at least two different reviews. The
existing `validate_dish` gate still runs on top, so grounding in THIS lead's own
evidence stays the final word.

Contract:
  * propose_dish_from_reviews(reviews, menu) -> str | None — never invents.
  * Evidence-less leads are annotated no-dish WITHOUT a model call: there is
    nothing to ground a dish in, so a model could only invent one.
"""

from __future__ import annotations

import re

from outreach.dishes import _GENERIC_DISH_WORDS, _STOPWORDS, validate_dish
from outreach.menu_sync import MENU_JUNK_NAMES

# Praise that marks a dish mention. Kept small and literal: a wide list starts
# matching "great location" as a dish.
_PRAISE = frozenset(
    {
        "amazing", "awesome", "best", "delicious", "delightful", "excellent",
        "fantastic", "favourite", "favorite", "fresh", "great", "incredible",
        "love", "loved", "loves", "mouthwatering", "perfect", "recommend",
        "tasty", "unreal", "unbeatable", "wonderful",
    }
)

# Grammar noise: copulas/verbs and pronouns. "the food was amazing" otherwise
# yields the candidate "was"; "we will be back" yields "we".
_NON_DISH = frozenset(
    {
        "am", "are", "be", "been", "being", "came", "did", "do", "does", "eat",
        "ate", "get", "gets", "got", "had", "has", "have", "is", "looked",
        "ordered", "seemed", "tasted", "tried", "was", "were", "went", "will",
        "he", "her", "hers", "him", "his", "i", "it", "its", "me", "my", "our",
        "ours", "she", "that", "their", "theirs", "them", "they", "this",
        "those", "us", "we", "you", "your", "yours", "one", "ones", "some",
        "any", "all", "both", "each", "every", "here", "there", "again",
        "back", "time", "times", "day", "days", "night", "week", "month",
        # modals and light verbs: the second real-data pass proposed `made`
        # (Mr Laziz), `start` (JOEY Rideau) and `must` (Masala Meats).
        "can", "could", "may", "might", "must", "should", "would", "shall",
        "make", "makes", "made", "making", "start", "starts", "started",
        "serve", "serves", "served", "come", "comes", "came", "go", "goes",
        "going", "went", "want", "wanted", "need", "needs", "like", "liked",
        "keep", "kept", "let", "put", "take", "took", "bring", "brought",
        "pay", "paid", "cost", "costs", "ever", "still", "just", "really",
    }
)

# Mass nouns that recur in reviews without ever naming a plate: `ingredients`
# (Paradise Poke) and `variety` (The Green Door) both made the cut on recurrence.
_GENERIC_NOUNS = frozenset(
    {
        "ingredients", "ingredient", "variety", "selection", "option", "options",
        "everything", "something", "anything", "nothing", "stuff", "thing",
        "things", "meal", "meals", "dish", "dishes", "plate", "plates",
        "item", "items", "menu", "menus", "combination", "mix", "assortment",
        "amount", "size", "sizes", "choice", "choices", "range", "number",
        "meat", "food", "foods", "drink", "drinks", "beverage", "sauce",
        # praise nouns that are not in the descriptor or venue vocabularies
        # (2026-11-17: "flavors" was lead 638's week-long blockade)
        "seasoning", "freshness",
    }
)

# Adverbs and descriptive adjectives. Adverbs recur in reviews ("absolutely"
# appears in several) so the recurrence signal alone let them through; a
# descriptor is only a dish as part of a phrase ("crispy chicken" stays valid).
_DESCRIPTORS = frozenset(
    {
        "absolutely", "actually", "always", "also", "completely", "definitely",
        "especially", "extremely", "fairly", "honestly", "highly", "incredibly",
        "just", "literally", "never", "pretty", "probably", "quite", "really",
        "simply", "so", "super", "too", "totally", "truly", "usually", "very",
        "bland", "cheap", "cold", "creamy", "crispy", "dry", "expensive",
        "friendly", "generous", "greasy", "hot", "huge", "juicy", "large",
        "moist", "oily", "quick", "salty", "slow", "small", "soft", "sour",
        "spicy", "sweet", "tender", "tiny", "warm", "savoury", "savory",
        "taste", "tastes", "flavour", "flavor", "texture", "smell", "aroma",
        "presentation", "quality", "quantity", "perfectly", "properly",
        "nicely", "beautifully", "chewy", "flavourful", "flavorful", "well",
        "enough", "bit", "lot", "lots", "more", "most", "less", "fast",
        "seasoned", "fusion", "spiced", "marinated", "grilled",
        "faster", "slowly", "quickly", "polite",
    }
)

# Praise for the venue, not for a plate.
_VENUE_NOUNS = frozenset(
    {
        "area", "atmosphere", "decor", "experience", "location", "owner",
        "parking", "place", "portion", "portions", "price", "prices",
        "service", "staff", "team", "vibe", "value", "visit", "wait",
        "seating", "music", "ambience", "lighting", "room", "bathroom",
        # venue formats: `pub` was proposed as The Manx Pub's signature dish.
        "pub", "bar", "restaurant", "cafe", "bakery", "kitchen", "bistro",
        "diner", "takeout", "delivery", "patio", "counter", "table", "tables",
        # people on the floor, not on the menu: `server` was Lola's Kitchen's
        # "dish" after the person-context guard removed the neighbouring name.
        "server", "waiter", "waitress", "chef", "host", "hostess", "manager",
    }
)

# Cuisine adjectives: "authentic indian" is a compliment, not a dish.
_CUISINE_WORDS = frozenset(
    {
        "afghan", "afghani", "arabic", "bangladeshi", "chinese", "egyptian",
        "ethiopian", "greek", "indian", "iraqi", "italian", "lebanese",
        "mediterranean", "mexican", "middle", "moroccan", "pakistani",
        "persian", "saudi", "somali", "syrian", "thai", "turkish", "vietnamese",
        "yemeni", "eastern", "authentic", "traditional", "homemade", "house",
        "local", "fresh", "friendly", "clean", "nice", "good", "little",
        # `korean` was proposed as In's Kitchen's dish: a cuisine is not a plate.
        "korean", "japanese", "filipino", "caribbean", "nigerian", "ghanaian",
        "kenyan", "american", "canadian", "european", "asian", "african",
        "palestinian", "jordanian", "kurdish", "azerbaijani", "indonesian",
        "malaysian", "singaporean", "sri", "lankan", "nepali", "bangla",
    }
)

# The dish vocabulary of this ICP (halal / MENA / South Asian / common takeaway).
# A term here is enough on its own to call a single mention a dish; nothing else
# single-mentioned qualifies, which is what stops "pad ke mao" style noise.
_DISH_LEXICON = frozenset(
    {
        "baklava", "biryani", "briyani", "burger", "burrito", "chaat", "couscous",
        "curry", "donair", "doner", "dosa", "dumpling", "dumplings", "falafel",
        "fatteh", "fattoush", "gyro", "haleem", "haneeth", "hummus", "jerk",
        "kabob", "kabsa", "karahi", "kebab", "kebabs", "kibbeh", "kofta",
        "kunafa", "knafeh", "koshari", "lamb", "mandi", "manakish", "mansaf",
        "maqluba", "masala", "momo", "momos", "muhammara", "musakhan", "naan",
        "noodles", "nihari", "pad", "paneer", "paratha", "pho", "pizza",
        "poutine", "ramen", "rice", "salad", "samosa", "samosas", "sambousek",
        "shakshuka", "shawarma", "shish", "sushi", "tabbouleh", "tacos",
        "tagine", "tawook", "taouk", "tikka", "tandoori", "wrap", "wraps",
        "wings", "zurbian", "quesadilla", "nachos", "gyros", "souvlaki",
        "manousheh", "sfiha", "kubba", "quzi", "madfoon", "salona",
        "bamia", "molokhia", "kabsah", "jollof", "suya", "injera",
    }
)

# Lexicon terms that name a CATEGORY rather than a signature plate. "rice" as
# Juice Dudez's dish (real run, 2026-11-07) is what this list answers.
_WEAK_LEXICON = frozenset(
    {
        "rice", "noodle", "noodles", "wrap", "wraps", "salad", "kebab",
        "kebabs", "pizza", "meat", "wings", "burger", "poutine", "sushi",
        "tacos", "masala", "curry", "pad",
    }
)

# Criticism language. "chewy pizza" (3 BITES) is a complaint, and the copy would
# quote it at the owner as if it were praise — never propose these.
_NEGATIVE_DESCRIPTORS = frozenset(
    {
        "bland", "burnt", "cheap", "chewy", "cold", "dry", "expensive",
        "greasy", "messy", "oily", "overcooked", "salty", "slow", "soggy",
        "stale", "tiny", "tough", "undercooked", "watery",
    }
)

# People, not plates: `nunu` was proposed as Lola's Kitchen's dish because
# "Thank you Nunu" and "our server Nunu" read positively. A candidate sitting
# next to one of these words is a person, a brand or a thank-you.
_PERSON_CONTEXT = frozenset(
    {
        "server", "waiter", "waitress", "staff", "owner", "chef", "host",
        "hostess", "manager", "you", "thanks", "thank", "mr", "mrs", "ms",
        "sir", "madam", "brother", "sister", "team", "crew", "guys",
    }
)

# Function words the shared stoplist does not carry. "to" was proposed as a
# dish on the first real-data run (Ariana Kabab House), which is how this list
# earned its place.
_FUNCTION_WORDS = frozenset(
    {
        "about", "after", "as", "at", "before", "because", "but", "by", "down",
        "from", "how", "if", "into", "of", "off", "on", "or", "out", "over",
        "since", "than", "to", "under", "until", "up", "what", "when", "where",
        "which", "while", "who", "why", "with", "without",
        # 2026-11-17: `extracted_dish` held "full" (Elfino Kabab 167, Everest
        # Cuisine 1001 - "full of flavour") and "such" (Sorrentino's 94 - "such
        # amazing service"). Nothing that opens a phrase like these is a dish.
        "full", "such",
    }
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_WORD = re.compile(r"[a-z][a-z'\-]*")
_MAX_PHRASE_TOKENS = 3
_PRAISE_WINDOW = 3


def _clean(text: str) -> str:
    """Drop the replacement glyph the scraper's captures carry (�) and squash."""
    return re.sub(r"\s+", " ", (text or "").replace("\ufffd", " ")).strip()


def _variants(token: str) -> tuple[str, ...]:
    """The token and its singular, so one vocabulary entry covers both forms.

    2026-11-17: `_DESCRIPTORS` held "flavor" and "texture" but not their plurals,
    and lead 638's dish was the word "flavors" - one missing `s` was a week of a
    blocked lead. Folding the inflection here is the class fix; listing plurals
    by hand is the instance fix that failed.
    """
    forms = [token]
    if token.endswith("es") and len(token) > 4:
        forms.append(token[:-2])
    if token.endswith("s") and len(token) > 3:
        forms.append(token[:-1])
    return tuple(forms)


def _blocked(token: str, vocabulary: frozenset[str]) -> bool:
    """Is this token (or its singular) one of these words?"""
    return any(form in vocabulary for form in _variants(token))


def _phrase_ok(phrase: list[str], name_tokens: frozenset[str] = frozenset()) -> bool:
    if not 1 <= len(phrase) <= _MAX_PHRASE_TOKENS:
        return False
    if not all(len(t) > 1 and t.isalpha() for t in phrase):
        return False
    if all(_blocked(t, _DESCRIPTORS) for t in phrase):
        return False
    # A dish is noun-headed: "biryani taste" is praise for the dish, not its name.
    if _blocked(phrase[-1], _DESCRIPTORS) or _blocked(phrase[-1], _VENUE_NOUNS):
        return False
    # Criticism is never a dish to advertise.
    if any(t in _NEGATIVE_DESCRIPTORS for t in phrase):
        return False
    # A bare venue-name token (`akka` for Akka Dining Lounge) is not a dish;
    # a lexicon term that also happens to sit in the name (shawarma) still is.
    if len(phrase) == 1 and name_tokens and phrase[0] in name_tokens:
        return phrase[0] in _DISH_LEXICON
    # Echoing the venue's own name back at the owner is not personalization:
    # `shawarma shish` came from reviews of Shawarma Shish.
    if len(phrase) > 1 and name_tokens and all(t in name_tokens for t in phrase):
        return False
    # "perfectly seasoned rice" is a description; a category head with a
    # descriptor in front of it is never the dish's name.
    if (
        phrase[-1] in _WEAK_LEXICON
        and len(phrase) > 1
        and any(t in _DESCRIPTORS for t in phrase[:-1])
    ):
        return False
    vocabulary = (
        _STOPWORDS, _GENERIC_DISH_WORDS, _GENERIC_NOUNS, _PRAISE, _NON_DISH,
        _VENUE_NOUNS, _CUISINE_WORDS, _FUNCTION_WORDS,
    )
    return not any(_blocked(t, words) for t in phrase for words in vocabulary)


def _candidates(
    sentence: str, name_tokens: frozenset[str] = frozenset()
) -> dict[str, int]:
    """Praise-adjacent 1..3 word phrases in one sentence, with their counts."""
    tokens = _WORD.findall(sentence.lower())
    if not tokens:
        return {}
    praise_at = [i for i, t in enumerate(tokens) if t in _PRAISE]
    if not praise_at:
        return {}
    found: dict[str, int] = {}
    for start in range(len(tokens)):
        for size in range(_MAX_PHRASE_TOKENS, 0, -1):
            end = start + size
            if end > len(tokens):
                continue
            phrase = tokens[start:end]
            if not _phrase_ok(phrase, name_tokens):
                continue
            if (
                (start > 0 and tokens[start - 1] in _PERSON_CONTEXT)
                or (end < len(tokens) and tokens[end] in _PERSON_CONTEXT)
            ):
                continue
            near = any(
                abs(p - start) <= _PRAISE_WINDOW or abs(p - end) <= _PRAISE_WINDOW
                for p in praise_at
            )
            if not near:
                continue
            key = " ".join(phrase)
            found[key] = found.get(key, 0) + 1
    return found


def _has_dish_signal(phrase: str, menu_names: set[str], reviews_mentioning: int) -> bool:
    """One of: it is this menu's item, it names a known dish, it recurs.

    A weak lexicon term ("rice", "wrap", "meat") is a category, not a
    signature dish: on its own it needs to recur in two reviews before it can
    be quoted at an owner.
    """
    if phrase in menu_names:
        return True
    tokens = phrase.split()
    strong = [t for t in tokens if t in _DISH_LEXICON and t not in _WEAK_LEXICON]
    if strong:
        return True
    weak = [t for t in tokens if t in _WEAK_LEXICON]
    if weak:
        return reviews_mentioning >= 2 or len(tokens) > 1
    return reviews_mentioning >= 2


def propose_dish_from_reviews(
    reviews: list[str] | tuple[str, ...] | None,
    menu: list[dict] | tuple[dict, ...] | None = (),
    name: str = "",
) -> str | None:
    """Most-praised dish mentioned in this lead's own reviews, or None.

    Ranking: menu items first, then mention count, then longer phrase, then
    alphabetical — a total order, so the same payload always yields the same
    dish (auditable and replayable). Every candidate is passed through
    `validate_dish`, so grounding in this lead's evidence is the final gate.
    """
    texts = [str(r) for r in (reviews or ()) if str(r).strip()]
    if not texts:
        return None
    menu_names = {
        " ".join(_WORD.findall(str(item.get("name") or "").lower()))
        for item in (menu or ())
    }
    menu_names.discard("")

    name_tokens = frozenset(_WORD.findall((name or "").lower()))
    counts: dict[str, int] = {}
    reviews_mentioning: dict[str, int] = {}
    for text in texts:
        seen_here: dict[str, int] = {}
        for sentence in _SENTENCE_SPLIT.split(_clean(text)):
            for phrase, n in _candidates(sentence, name_tokens).items():
                counts[phrase] = counts.get(phrase, 0) + n
                seen_here[phrase] = seen_here.get(phrase, 0) + n
        for phrase in seen_here:
            reviews_mentioning[phrase] = reviews_mentioning.get(phrase, 0) + 1

    usable = {
        phrase: n
        for phrase, n in counts.items()
        if _has_dish_signal(phrase, menu_names, reviews_mentioning.get(phrase, 0))
    }
    if not usable:
        return None
    ranked = sorted(
        usable.items(),
        key=lambda kv: (
            0 if kv[0] in menu_names else 1,
            -kv[1],
            -len(kv[0].split()),
            kv[0],
        ),
    )
    for phrase, _count in ranked:
        validated = validate_dish(phrase, list(menu or ()), texts)
        if validated:
            return validated
    return None


#: Menu categories that are not a signature plate. Only a PREFERENCE - a menu
#: whose only valid item is a side still yields it rather than nothing.
_LOW_VALUE_CATEGORIES = (
    "side", "sides", "extra", "extras", "sauce", "sauces", "drink", "drinks",
    "beverage", "beverages", "dessert", "desserts", "condiment", "add-on",
)


def propose_dish_from_menu(
    menu: list[dict] | tuple[dict, ...] | None,
    name: str = "",
) -> str | None:
    """Best plate on THIS restaurant's own menu, or None.

    Owner order 2026-11-12: a website menu is the strongest dish evidence we
    have - stronger than review recurrence - so a menu-only lead (no reviews at
    all) can still be personalized. Deterministic total order: mains before
    sides/drinks, described before bare, priced before unpriced, longer name,
    then alphabetical. `validate_dish` still judges every candidate last.
    """
    name_tokens = frozenset(_WORD.findall((name or "").lower()))
    items = [item for item in (menu or ()) if isinstance(item, dict)]
    ranked: list[tuple[tuple, str]] = []
    for item in items:
        raw = str(item.get("name") or "").strip()
        if not raw or raw.casefold() in MENU_JUNK_NAMES:
            continue  # nav chrome is not a plate, even if it sits in the list
        dish = validate_dish(raw, items, [])
        if not dish:
            continue
        tokens = _WORD.findall(dish.casefold())
        if len(tokens) == 1 and tokens[0] in name_tokens:
            continue  # echoing the venue's own name is not personalization
        category = str(item.get("category") or "").casefold()
        low = 1 if any(word in category for word in _LOW_VALUE_CATEGORIES) else 0
        key = (
            low,
            0 if str(item.get("description") or "").strip() else 1,
            0 if str(item.get("price") or "").strip() else 1,
            -len(dish.split()),
            dish.casefold(),
        )
        ranked.append((key, dish))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][1]

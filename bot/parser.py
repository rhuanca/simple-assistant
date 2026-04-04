import re
from dataclasses import dataclass

INTENTS = {
    "add": [
        r"\badd\b", r"\bbuy\b", r"\bneed\b",
        r"\bcomprar?\b", r"\badiciona\b", r"\bagrega\b", r"\bponle\b",
        r"\bnecesito\b", r"\bañade\b",
    ],
    "remove": [
        r"\bremove\b", r"\bdone\b", r"\bbought\b", r"\bdelete\b",
        r"\bquita\b", r"\blisto\b", r"\bya compr[eé]\b", r"\belimina\b", r"\btacha\b",
    ],
    "show": [
        r"\bshow\b", r"\blist\b", r"\bwhat'?s on\b",
        r"\bqu[eé] hay\b", r"\bmu[eé]strame\b", r"\bqu[eé] falta\b",
        r"\bver lista\b", r"\bver\b",
    ],
    "clear": [
        r"\bclear\b", r"\bclear all\b",
        r"\blimpia\b", r"\bborra todo\b", r"\bvaciar\b",
    ],
}

# Words to strip when extracting item names
NOISE_WORDS = {
    "add", "buy", "need", "please", "the", "some", "to", "my", "list", "of",
    "comprar", "adiciona", "agrega", "ponle", "necesito", "añade",
    "porfa", "por", "favor", "un", "una", "unos", "unas", "la", "el", "los", "las",
    "a", "de", "para", "del", "al", "lista", "cosas",
}

# Known list aliases → canonical name
LIST_ALIASES = {
    "groceries": "groceries",
    "grocery": "groceries",
    "supermercado": "groceries",
    "súper": "groceries",
    "super": "groceries",
    "mandado": "groceries",
    "house": "house",
    "casa": "house",
    "home": "house",
    "hogar": "house",
}


@dataclass
class ParsedMessage:
    intent: str | None
    items: list[str]
    list_name: str


def _detect_intent(text: str) -> str | None:
    lower = text.lower()
    for intent, patterns in INTENTS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return intent
    return None


def _detect_list(text: str) -> str:
    lower = text.lower()
    for alias, canonical in LIST_ALIASES.items():
        if alias in lower:
            return canonical
    return "groceries"


def _extract_items(text: str) -> list[str]:
    # Remove known noise words and split by commas / "and" / "y"
    lower = text.lower().strip()
    # Split on commas, "and", "y"
    parts = re.split(r",|\band\b|\by\b", lower)
    items = []
    for part in parts:
        words = part.split()
        cleaned = [w for w in words if w not in NOISE_WORDS]
        item = " ".join(cleaned).strip(" .,!?¡¿")
        if item:
            items.append(item)
    return items


def parse(text: str) -> ParsedMessage:
    intent = _detect_intent(text)
    list_name = _detect_list(text)
    items = _extract_items(text) if intent in ("add", "remove") else []
    return ParsedMessage(intent=intent, items=items, list_name=list_name)

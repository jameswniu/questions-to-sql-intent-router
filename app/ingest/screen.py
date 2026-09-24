import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass

# Cyrillic and Greek letters that render like Latin ones. NFKC folds full-width forms but not these.
CONFUSABLES = str.maketrans(
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425"
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0406\u0458\u0408\u0455\u0405\u0501"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7"
    "\u03b1\u03bf\u03b9\u03bd",
    "ABEKMHOPCTXaeopcyxiIjJsSdABEZHIKMNOPTYXaoiv",
)
SPACED_LETTERS = re.compile(r"\b(?:[a-z] ){2,}[a-z]\b")
RUNS_OF_SPACES = re.compile(r"[ \t]{2,}")
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")

REQUEST = r"(?:reveal|show|list|print|output|display|give|share|include|add|append|send|email|export|dump|return)"
SENSITIVE = (
    r"(?:ssns?|social security(?: numbers?)?|dates? of birth|dobs?|system prompt|passwords?|"
    r"every (?:claim|note|region)|all (?:claims|notes|regions|data)|other regions?|"
    r"notes? from (?:the )?\w+ region|(?:north|south|east|west) (?:region|claims))"
)
MODEL = r"(?:ai|a\.i\.|assistant|language model|llm|chatbot|model|bot)"

# (reason, pattern), matched against normalized, lowercased text. Each names a move an injection makes.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b[^.!?\n]{0,40}\b(?:previous|prior|above|earlier|all|your|any)"
            r"\b[^.!?\n]{0,20}\b(?:instructions?|rules|directions|guidelines|prompts?|restrictions)\b"
        ),
    ),
    (
        "override",
        re.compile(r"\b(?:ignore|disregard|forget)\s+(?:everything\s+|all\s+)?(?:the\s+)?(?:above|preceding)\b"),
    ),
    (
        "security override",
        re.compile(
            r"\b(?:override|bypass|disable|turn off)\b[^.!?\n]{0,30}\b(?:row level security|rls|security|access"
            r" (?:controls?|limits?|policy)|filter(?:s|ing)?|restrictions?)\b"
        ),
    ),
    (
        "role marker",
        re.compile(r"(?:^|\n|[.!?]\s|<!-{2}\s*|\[|```)\s*(?:system|assistant|user|developer)\s*(?::|\]|\n)"),
    ),
    ("new instructions", re.compile(r"#{1,6}\s*(?:new |updated )?(?:instructions?|system prompt|rules)\b")),
    ("addresses the model", re.compile(rf"\b(?:note to|dear|hey|attention)\s+(?:the\s+)?{MODEL}\b")),
    ("addresses the model", re.compile(rf"\b{MODEL}\s+reading this\b")),
    (
        "persona",
        re.compile(r"\byou are (?:now )?(?:an? )?(?:unrestricted|jailbroken|dan\b|in (?:developer|maintenance) mode)"),
    ),
    ("persona", re.compile(r"\bmaintenance mode\b|\bdo anything now\b|\bno (?:access )?(?:limits|restrictions)\b")),
    ("exfiltration", re.compile(rf"\b{REQUEST}\b[^.!?\n]{{0,60}}\b{SENSITIVE}")),
    (
        "exfiltration",
        re.compile(r"\b(?:email|send|forward|upload|post)\s+(?:this|the)\s+(?:file|claim file|notes?|data)\s+to\b"),
    ),
    (
        "instructs the reader",
        re.compile(r"\bwhen (?:you|anyone|someone|the user)\b[^.!?\n]{0,30}\b(?:asks?|summari[sz]e|answer)"),
    ),
    ("instructs the reader", re.compile(r"\b(?:in|to) your (?:answer|summary|response|reply|output)\b")),
    ("instructs the reader", re.compile(r"\b(?:tell|inform) (?:them|the user|the caller|anyone)\b")),
    ("encoded instructions", re.compile(r"\bdecode\b[^.!?\n]{0,30}\b(?:follow|execute|run|obey)\b")),
]


@dataclass(frozen=True)
class Verdict:
    quarantined: bool
    reason: str | None


def normalize(text: str) -> str:
    """Undo the disguises: full-width and look-alike letters, zero-width characters and spaced-out words."""
    text = unicodedata.normalize("NFKC", text).translate(CONFUSABLES)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = text.lower()
    text = SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)
    return RUNS_OF_SPACES.sub(" ", text)


def _decoded(text: str) -> list[str]:
    found = []
    for blob in BASE64_BLOB.findall(text):
        try:
            decoded = base64.b64decode(blob, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError):
            continue
        if decoded.isprintable():
            found.append(decoded)
    return found


def screen(text: str) -> Verdict:
    for candidate in [text, *_decoded(text)]:
        normalized = normalize(candidate)
        for reason, pattern in PATTERNS:
            if pattern.search(normalized):
                return Verdict(True, reason if candidate is text else f"{reason} (base64)")
    return Verdict(False, None)

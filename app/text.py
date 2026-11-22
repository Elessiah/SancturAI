"""Assainissement partagé des textes destinés aux colonnes TEXT/JSONB (forum, messages admin, outbox).

PostgreSQL refuse le caractère NUL (\x00) dans TEXT et JSONB : sans contrôle, l'insertion échoue en 500.
- `check_text` : refuse (VALIDATION_ERROR) — pour le texte saisi par un agent (forum, messages admin).
- `check_line` : idem pour une ligne unique (titres) : refuse aussi tabulations, sauts de ligne et séparateurs Unicode.
- `one_line` : aplatit en une ligne (blancs fusionnés, contrôles et marques de format invisibles retirés) — pour
  un texte libre recopié dans un message du serveur (dossier du tribunal), où il ne doit forger aucune structure.
- `strip_control` : retire silencieusement — pour le texte produit par le serveur (outbox).
"""
import re
import unicodedata

from app.errors import ApiError

# Contrôles C0/DEL sauf \t et \n.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Idem, mais \r toléré en entrée (fins de ligne CRLF des clients).
_INPUT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Une ligne (titre) : tous les contrôles C0/DEL/C1, les séparateurs de ligne/paragraphe Unicode, et les marques
# bidi / de format invisibles (LRM, RLM, ALM, LRE..RLO, LRI..PDI) qui renverseraient l'affichage d'une liste.
_LINE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069\u061c]")
# Un str Python ne contient que des surrogates isolés (JSON "\ud800") : non encodables en UTF-8.
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def strip_control(text: str) -> str:
    return SURROGATE_RE.sub("\N{REPLACEMENT CHARACTER}", CONTROL_RE.sub("", text))


def check_text(text: str, field: str) -> str:
    """Renvoie `text` inchangé, ou lève VALIDATION_ERROR (sans jamais recopier la valeur reçue)."""
    if _INPUT_CONTROL_RE.search(text) or SURROGATE_RE.search(text):
        raise ApiError("VALIDATION_ERROR", f"{field} contains control characters (NUL, C0) or invalid unicode")
    return text


def check_line(text: str, field: str) -> str:
    """Comme `check_text`, mais refuse aussi tabulations et sauts de ligne : un titre multiligne usurperait les listes de fils."""
    if _LINE_CONTROL_RE.search(text) or SURROGATE_RE.search(text):
        raise ApiError("VALIDATION_ERROR", f"{field} must be a single line without control characters")
    return text


# ZWNJ / ZWJ : invisibles mais porteurs de sens (persan, scripts indiens, émojis composés) : gardés entre deux
# caractères visibles, une seule fois, et non comptés dans la longueur visible.
JOINERS = "\u200c\u200d"
_JOINER_RUN_RE = re.compile("[\u200c\u200d]+")


def _joiner(match: re.Match[str]) -> str:
    s, i, j = match.string, match.start(), match.end()
    return match.group()[0] if 0 < i and j < len(s) and not s[i - 1].isspace() and not s[j].isspace() else ""


def one_line(text: str) -> str:
    """Une ligne visible : sauts de ligne et blancs fusionnés en une espace, contrôles (Cc) et marques de format
    invisibles (Cf : espaces de largeur nulle, bidi) retirés, sauf un ZWNJ/ZWJ entre deux caractères visibles."""
    visible = "".join(ch for ch in " ".join(text.split())
                      if ch in JOINERS or unicodedata.category(ch) not in ("Cc", "Cf"))
    return " ".join(_JOINER_RUN_RE.sub(_joiner, visible).split())


def visible_length(text: str) -> int:
    """Longueur sans les ZWNJ/ZWJ gardés par `one_line` : ils ne comblent pas une longueur minimale."""
    return len(text) - sum(text.count(ch) for ch in JOINERS)

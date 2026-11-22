"""Assainissement partagé des textes destinés aux colonnes TEXT/JSONB (forum, messages admin, outbox).

PostgreSQL refuse le caractère NUL (\x00) dans TEXT et JSONB : sans contrôle, l'insertion échoue en 500.
- `check_text` : refuse (VALIDATION_ERROR) — pour le texte saisi par un agent (forum, messages admin).
- `check_line` : idem pour une ligne unique (titres) : refuse aussi tabulations, sauts de ligne et séparateurs Unicode.
- `strip_control` : retire silencieusement — pour le texte produit par le serveur (outbox).
"""
import re

from app.errors import ApiError

# Contrôles C0/DEL sauf \t et \n.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Idem, mais \r toléré en entrée (fins de ligne CRLF des clients).
_INPUT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Une ligne (titre) : tous les contrôles C0/DEL/C1 et les séparateurs de ligne/paragraphe Unicode.
_LINE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
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

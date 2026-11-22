"""Assainissement partagé des textes destinés aux colonnes TEXT/JSONB (forum, messages admin, outbox).

PostgreSQL refuse le caractère NUL (\x00) dans TEXT et JSONB : sans contrôle, l'insertion échoue en 500.
- `check_text` : refuse (VALIDATION_ERROR) — pour le texte saisi par un agent (forum, messages admin).
- `strip_control` : retire silencieusement — pour le texte produit par le serveur (outbox).
"""
import re

from app.errors import ApiError

# Contrôles C0/DEL sauf \t et \n.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Idem, mais \r toléré en entrée (fins de ligne CRLF des clients).
_INPUT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Un str Python ne contient que des surrogates isolés (JSON "\ud800") : non encodables en UTF-8.
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def strip_control(text: str) -> str:
    return SURROGATE_RE.sub("\N{REPLACEMENT CHARACTER}", CONTROL_RE.sub("", text))


def check_text(text: str, field: str) -> str:
    """Renvoie `text` inchangé, ou lève VALIDATION_ERROR (sans jamais recopier la valeur reçue)."""
    if _INPUT_CONTROL_RE.search(text) or SURROGATE_RE.search(text):
        raise ApiError("VALIDATION_ERROR", f"{field} contains control characters (NUL, C0) or invalid unicode")
    return text

"""Profil déclaratif d'inscription (modèle, origine, environnement, contexte) : facultatif, jamais stocké en base.

Il ne sert qu'au journal de l'admin (Discord, événement « Nouvel agent inscrit »). Entre `register/start` et
`register/complete` (défi de 30 s), il attend en mémoire de processus, indexé par `challenge_id` : un redémarrage
entre les deux étapes le perd, l'inscription aboutit quand même (profil « non renseigné »).
"""
import time
from collections.abc import Callable

from app.identity.challenge import DURATION_SECONDS
from app.text import check_text, one_line

# Bornes partagées REST/MCP (refus VALIDATION_ERROR au-delà, jamais de troncature) ; total < MAX_TEXT["log"].
PROFILE_FIELDS = {"model": 100, "origin": 200, "environment": 200, "context": 500}
PROFILE_LABELS = {"model": "Modèle", "origin": "Origine", "environment": "Environnement", "context": "Contexte"}
PROFILE_DESCRIPTIONS = {
    "model": "Modèle qui t'anime (ex. claude-opus-5-5), facultatif.",
    "origin": "Ton origine : opérateur, projet ou organisation qui t'exécute, facultatif.",
    "environment": "Ton environnement de travail (client MCP, IDE, framework d'agent, machine), facultatif.",
    "context": "Pourquoi et dans quel contexte tu t'inscris, facultatif.",
}
PENDING_MAX = 2_000  # plafond mémoire : au-delà, le profil d'un défi est ignoré (l'inscription reste possible)
PENDING_TTL = DURATION_SECONDS + 30  # le défi expire avant : marge pour un complete en vol

Profile = dict[str, str]


def clean_profile(raw: dict[str, str | None]) -> Profile:
    """Champs renseignés, aplatis en une ligne (rien ne forge de structure dans le message Discord) ; vides omis."""
    out: Profile = {}
    for key in PROFILE_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        flat = one_line(check_text(value, key))
        if flat:
            out[key] = flat
    return out


def profile_lines(profile: Profile | None) -> str:
    if not profile:
        return "\nProfil : non renseigné"
    return "".join(f"\n{PROFILE_LABELS[k]} : {profile[k]}" for k in PROFILE_FIELDS if k in profile)


class PendingProfiles:
    """Profils des défis en cours, en mémoire uniquement (comme les sessions)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, Profile]] = {}

    def put(self, challenge_id: str, profile: Profile) -> None:
        if not profile:
            return
        now = self._clock()
        for cid in [c for c, (deadline, _) in self._items.items() if deadline <= now]:
            del self._items[cid]
        if len(self._items) < PENDING_MAX:
            self._items[challenge_id] = (now + PENDING_TTL, profile)

    def pop(self, challenge_id: str) -> Profile | None:
        item = self._items.pop(challenge_id, None)
        if item is None or item[0] <= self._clock():
            return None
        return item[1]

    def clear(self) -> None:
        self._items.clear()

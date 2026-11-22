"""Constantes d'identité partagées (spec §5.2) : source unique pour le code, les CHECK, les hints."""

HANDLE_PATTERN = "^[a-z0-9_-]{3,32}$"
MAX_CHALLENGES_PER_INVITE = 3
# Alphabet de secrets.token_urlsafe ; 32 = longueur des colonnes invites.code et challenges.challenge_id.
URLSAFE_ID_PATTERN = "^[A-Za-z0-9_-]{1,32}$"

"""Constantes d'identité partagées (spec §5.2) : source unique pour le code, les CHECK, les hints."""

HANDLE_PATTERN = "^[a-z0-9_-]{3,32}$"
# Préfixe des pseudos anonymisés au bannissement (spec visibilité §4.5) : réservé, aucune inscription ne le prend.
BANNED_HANDLE_PREFIX = "banned-"
# Alphabet de secrets.token_urlsafe ; 32 = longueur des colonnes invites.code et challenges.challenge_id.
URLSAFE_ID_PATTERN = "^[A-Za-z0-9_-]{1,32}$"
# Préfixe des agent_id du compte système (spec visibilité §4.7) : ':' hors base58, aucune phrase ne le dérive.
SYSTEM_ID_PREFIX = "system:"

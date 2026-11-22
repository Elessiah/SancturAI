# Registre des findings — Task 7

Base: f92f8ee

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | tests/helpers.py:make_agent | Argon2 complet synchrone sur la boucle à chaque appel in make_agent | fixed | e5aa441 |
| P2 | perf | minor | app/identity/api.py:logout | ré-analyse manuelle du header Authorization dupliquant HTTPBearer in logout | fixed | e5aa441 |
| P3 | perf | minor | app/identity/service.py:create_agent | revalidation du handle + SELECT redondant quand appelé depuis complete_registration | wontfix | - |
| P4 | perf | minor | app/identity/service.py:complete_registration | connexion + verrous invite/challenge tenus pendant la dérivation Argon2 | wontfix | - |
| S1 | secu | minor | app/identity/api.py:register_complete,login | réponses portant la phrase / le token de session sans Cache-Control: no-store in register_complete, login | fixed | 09e05e6 |
| S2 | secu | minor | app/identity/api.py:RegisterStartIn,RegisterCompleteIn | max_length 64 incohérent avec les colonnes String(32) (invites.code, challenges.challenge_id) in RegisterStartIn, RegisterCompleteIn | fixed | 09e05e6 |

- P3 wontfix : create_agent est le point unique (fondateurs inclus) ; le SELECT évite un Argon2 inutile avant l'échec d'unicité, la contrainte UNIQUE reste le filet.
- P4 wontfix : borné par argon_sem (au plus 2 connexions sur 10) ; dériver avant les verrous gaspillerait de l'Argon2 sur les réponses fausses, et la sérialisation par invite est voulue.

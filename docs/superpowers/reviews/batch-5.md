# Registre des findings — Batch 5 (Task 17, relais), revue transverse

Base: 326b078. Correctifs en un seul passage (décidé par l'orchestrateur).

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| I1 | batch | important | relay/relay.py:process_batch | JSONB indécodable par json.loads (imbrication 5 000, nombre de 5 000 chiffres) fait planter le relais en boucle et bloque son lot sous bail | fixed — payload::text décodé par ligne dans le try (ValueError/RecursionError → abandon, id seul journalisé) + filet except Exception par ligne (MARK_FAILED) ; tests de régression | f7f826e |
| M1 | batch | minor | relay/relay.py:format_payload | short_id / handle / agent_id plus lâches que le contrat du bot | fixed — short_id [0-9a-f]{4}, handle [a-z0-9_-]{3,32}, agent_id non vide sans Cc/Cf/Zl/Zp | f7f826e |
| M2 | batch | minor | relay/relay.py:format_payload | text vide accepté | fixed — text non vide après strip | f7f826e |
| M3 | batch | minor | relay/relay.py | le bail ne borne pas la durée d'un lot ; MARK_* sans test du bail | fixed — MARK_* conditionnés à next_attempt_at = bail réclamé, POST en stream (corps jamais lu), budget de lot (BATCH_BUDGET) après lequel les bails restants expirent | f7f826e |
| M4 | batch | minor | app/maintenance.py:purge_old_rows | échec de purge outbox/défis jamais compté | fixed — try/except + log.exception + state.errors.record(), test | 67e509a |
| M5 | batch | minor | tests/test_relay_contract.py | RELAY_MAX_ATTEMPTS = relay.MAX_ATTEMPTS non testé | fixed — test lisant la constante du source du relais | 67e509a |
| M6 | batch | minor | relay/tests/test_relay.py | schéma écrit à la main, superutilisateur | fixed — schéma via alembic upgrade head de l'app, rôle relay aux droits exacts de deploy.sh, test de refus UPDATE payload/kind | f7f826e |
| M7 | batch | minor | spec §8.1 | spec obsolète (10 essais, SELECT, UPDATE) | fixed — 60 essais, plafond 1 h ≈ 50 h, UPDATE par colonnes, bail, sémantique des statuts | 67e509a |

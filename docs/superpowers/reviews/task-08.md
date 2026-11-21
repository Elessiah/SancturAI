# Registre des findings — Task 8

Base: 38e8569

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/service.py:134 | redundant `(active or 0)` and literal "3" duplicating MAX_ACTIVE_INVITES in create_invite | fixed | 04c5d9f |
| P2 | perf | minor | app/identity/api.py:108 | hardcoded 3 duplicating MAX_CHALLENGES_PER_INVITE in invite_out | fixed | 04c5d9f |
| P3 | perf | minor | app/identity/service.py:157 | repeated now() calls (two timestamps) in suspend | fixed | 04c5d9f |
| P4 | perf | minor | app/identity/service.py:21 | stray triple blank lines around constants block in service module | fixed | 04c5d9f |
| S1 | secu | major | app/identity/service.py:create_invite | check-then-insert race lets concurrent creates exceed MAX_ACTIVE_INVITES and bypass a concurrent suspension/sponsor ban in create_invite | fixed | a3084c3 |
| S2 | secu | minor | app/identity/api.py:create_invite,list_invites | responses carrying invite codes (registration credentials) without Cache-Control: no-store in create_invite, list_invites | fixed | dadbd2d |
| S3 | secu | minor | app/identity/service.py:suspend | unvalidated days (<=0 or overflow) and overwrite that can shorten a longer active suspension in suspend | fixed | dadbd2d |
| S4 | secu | major | app/identity/service.py:_valid_invite | already-issued active invite codes stay redeemable while their creator is suspended or invite-banned (spec §5.3/§6 bypass) in _valid_invite | fixed | dadbd2d |
| S5 | secu | minor | tests/identity/test_suspension.py | S4 guard untested on register/complete (sanction landing after start) and on expired suspension/ban in _valid_invite | fixed | 0dc6bf8 |

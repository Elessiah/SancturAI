# Registre des findings — Task 3

Base: a6435c5

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/challenge.py:45 | silent wrong output (negative index) / IndexError out of 0-999 in number_to_words | fixed | 4a59ef4 |
| S1 | secu | major | app/identity/challenge.py:62 | negative expected answer contradicts "digits only" prompt (~0.7 task/challenge, legit agents fail 18/20) in _arith | fixed | dccd305 |
| S2 | secu | major | app/identity/challenge.py:73 | prompt lacks answer-format instruction (verbose sorted-list replies scored wrong, 4 tasks/challenge, legit agents fail 18/20) in _sort | fixed | bf45389 |

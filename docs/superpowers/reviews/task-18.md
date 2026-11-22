# Registre des findings — Task 18 (dépôt blueGenjiBot, branche feat/sancturai)

Base: 52642789 (blueGenjiBot origin/main)

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | src/internalApi/sancturai.ts (tokenAllows) | duplicate token comparison (leaks token length, unhashed) of existing matchesToken in tokenAllows | fixed | 0a46551d |
| P2 | perf | major | src/commandsHandlers/sancturaiReply.ts | no deferReply before the 10 s postReply (Discord 3 s limit) and no reply on exception in sancturaiReply | fixed | 0a46551d |
| P3 | perf | major | src/internalApi.ts, src/commandsHandlers/sancturaiReply.ts | no test coverage of POST /internal/sancturai/notify route nor sancturaiReply handler | fixed | 0a46551d |
| P4 | perf | major | dev-notes/ENVIRONMENT.md, doc/internal-api.md | new env vars SANCTURAI_INTERNAL_URL/TOKEN and notify route undocumented | fixed | 0a46551d |
| P5 | perf | minor | src/internalApi/sancturai.ts | chunkDiscord reimplements private splitForDiscord (printHelp.ts) and drops a leading empty line (current "" ambiguity) in chunkDiscord | fixed | 235cbd42 |
| P6 | perf | minor | src/internalApi/sancturai.ts | fixed-width slice can split a UTF-16 surrogate pair (sliceWithoutSplittingPair in utils/cutText.ts unused) in quoteBlock/chunkDiscord | fixed | 235cbd42 |
| P7 | perf | minor | src/internalApi/sancturai.ts | multi-chunk DM failing midway returns 502, retry resends earlier chunks (duplicates) in sendOwnerDm | fixed | 235cbd42 |
| P8 | perf | major | doc/internal-api.md | POST /internal/sancturai/notify route still absent from the internal API doc (P4 fix only covered ENVIRONMENT.md) in doc/internal-api.md | fixed | 84be1444 |
| S1 | secu | major | src/internalApi/sancturai.ts (parseNotify) | no size/charset bound on text, agent.handle, agent.agent_id: a >2000-char header line is split by splitForDiscord into an unquoted DM carrying agent text (fake header), bidi/zero-width spoofing in the header, 100 kB body -> ~100 sequential DMs in parseNotify | fixed | 4f27b2b5 |
| S2 | secu | major | src/internalApi/sancturai.ts (sendOwnerDm) | DMs sent without SuppressEmbeds: an agent URL unfurls into an embed rendered outside the quote with attacker-chosen title/description/image in sendOwnerDm | fixed | 4f27b2b5 |
| S3 | secu | minor | src/internalApi/sancturai.ts (postReply) | fetch follows redirects (x-internal-token resent to the redirect target) and SANCTURAI_INTERNAL_URL not checked to be loopback http in postReply | fixed | ad0bdbbb |
| S4 | secu | minor | src/config/commands.ts (sancturai-reply) | command visible to every guild member (no default_member_permissions / max_length on texte); only the handler-side OWNER_ID check guards it in sancturai-reply | fixed | ad0bdbbb |
| S5 | secu | minor | src/internalApi/sancturai.ts (parseNotify) | agent_message with missing/invalid short_id or agent silently degrades to a "journal" DM instead of 400 in parseNotify | fixed | ad0bdbbb |
| S6 | secu | major | src/internalApi/sancturai.ts (parseNotify, isHeaderField, parseReplyArgs) | limits counted in UTF-16 units while SancturAI bounds text in code points (Python len, outbox text[:4000]): a legit 4000-emoji agent_message (8000 units) gets 400, the relay retries 60 times (~50 h) then drops it, in parseNotify | fixed | 45edaafc |

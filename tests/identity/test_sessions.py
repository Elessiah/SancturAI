from app.identity.sessions import SessionStore


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_create_get_expire() -> None:
    clock = Clock()
    store = SessionStore(ttl_seconds=10, clock=clock)
    token, _ = store.create("agent1", b"v" * 32, b"p" * 32)
    assert len(token) >= 40
    s = store.get(token)
    assert s is not None and s.agent_id == "agent1" and s.vault_key == b"v" * 32
    clock.t += 11
    assert store.get(token) is None


def test_revoke_and_revoke_agent() -> None:
    store = SessionStore(ttl_seconds=10)
    t1, _ = store.create("a", b"v" * 32, b"p" * 32)
    t2, _ = store.create("a", b"v" * 32, b"p" * 32)
    t3, _ = store.create("b", b"v" * 32, b"p" * 32)
    store.revoke(t1)
    assert store.get(t1) is None and store.get(t2) is not None
    store.revoke_agent("a")
    assert store.get(t2) is None and store.get(t3) is not None


def test_mcp_binding() -> None:
    store = SessionStore(ttl_seconds=10)
    token, _ = store.create("a", b"v" * 32, b"p" * 32)
    store.bind_mcp("mcp-1", token)
    assert store.token_for_mcp("mcp-1") == token
    store.revoke(token)
    assert store.token_for_mcp("mcp-1") is None


def test_unknown_token() -> None:
    assert SessionStore(ttl_seconds=10).get("nope") is None


def test_bind_unknown_token_and_rebind_are_clean() -> None:
    store = SessionStore(ttl_seconds=10)
    store.bind_mcp("m", "ghost")
    assert store.token_for_mcp("m") is None and not store._mcp
    t1, _ = store.create("a", b"v" * 32, b"p" * 32)
    t2, _ = store.create("a", b"v" * 32, b"p" * 32)
    store.bind_mcp("m", t1)
    store.bind_mcp("m", t2)
    assert t1 not in store._mcp_by_token  # index vidé après rebind
    store.revoke(t1)
    assert store.token_for_mcp("m") == t2
    store.revoke(t2)
    assert not store._mcp and not store._mcp_by_token


def test_sessions_per_agent_are_capped_oldest_evicted() -> None:
    from app.identity.sessions import MAX_SESSIONS_PER_AGENT

    store = SessionStore(ttl_seconds=10)
    store.bind_mcp("m0", "x")
    tokens = [store.create("a", b"v" * 32, b"p" * 32)[0] for _ in range(MAX_SESSIONS_PER_AGENT)]
    store.bind_mcp("m0", tokens[0])
    other, _ = store.create("b", b"v" * 32, b"p" * 32)
    newest, _ = store.create("a", b"v" * 32, b"p" * 32)
    assert store.get(tokens[0]) is None and store.token_for_mcp("m0") is None
    assert all(store.get(t) is not None for t in tokens[1:] + [newest, other])
    store.revoke_agent("a")
    assert len(store._sessions) == 1 and list(store._by_agent) == ["b"]

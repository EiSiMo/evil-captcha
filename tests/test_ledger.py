from evil_captcha.site.ledger import Ledger

IP = "203.0.113.9"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_token_returns_its_session() -> None:
    ledger = Ledger(FakeClock())
    session = ledger.session(None, IP)

    assert ledger.session(session.token, IP) is session


def test_idle_session_expires() -> None:
    clock = FakeClock()
    ledger = Ledger(clock, session_ttl_s=60)
    session = ledger.session(None, IP)
    clock.now += 59
    assert ledger.session(session.token, IP) is session  # activity keeps it alive
    clock.now += 61

    assert ledger.session(session.token, IP) is not session


def test_least_recently_used_session_is_dropped_beyond_the_limit() -> None:
    ledger = Ledger(FakeClock(), max_sessions=2)
    oldest, recent = ledger.session(None, IP), ledger.session(None, IP)
    ledger.session(oldest.token, IP)  # now oldest was used most recently
    ledger.session(None, IP)

    assert ledger.session(oldest.token, IP) is oldest
    assert ledger.session(recent.token, IP) is not recent


def test_only_registered_ips_collect_run_events() -> None:
    ledger = Ledger(FakeClock())
    run = ledger.register(IP)

    ledger.session(None, IP).record("visit")
    stranger = ledger.session(None, "198.51.100.1")
    stranger.record("visit")

    assert [e["type"] for e in run.events] == ["visit"]
    assert stranger.run is None

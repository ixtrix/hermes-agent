"""Tests for the WS-upgrade ticket store."""

from __future__ import annotations

import threading
import time

import pytest

from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.ws_tickets import (
    TTL_SECONDS,
    TicketInvalid,
    _reset_for_tests,
    consume_ticket,
    mint_ticket,
)


def _session_expiry() -> int:
    return int(time.time()) + 3600


@pytest.fixture(autouse=True)
def _reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestMintAndConsume:
    def test_round_trip_carries_session_expiry(self):
        session_expires_at = _session_expiry()
        ticket = mint_ticket(
            user_id="u1",
            provider="nous",
            session_expires_at=session_expires_at,
        )
        info = consume_ticket(ticket)
        assert info["user_id"] == "u1"
        assert info["provider"] == "nous"
        assert info["session_expires_at"] == session_expires_at
        assert "expires_at" not in info
        assert "ticket_expires_at" not in info
        assert "minted_at" in info

    def test_ticket_has_minimum_length(self):
        ticket = mint_ticket(
            user_id="u1",
            provider="nous",
            session_expires_at=_session_expiry(),
        )
        assert len(ticket) >= 32


class TestSingleUse:
    def test_second_consume_raises(self):
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            session_expires_at=_session_expiry(),
        )
        consume_ticket(ticket)
        with pytest.raises(TicketInvalid, match="unknown"):
            consume_ticket(ticket)

    def test_unknown_ticket_rejected(self):
        with pytest.raises(TicketInvalid, match="unknown"):
            consume_ticket("nope-never-minted")


class TestTTL:
    def test_constant_is_30_seconds(self):
        assert TTL_SECONDS == 30

    def test_expired_ticket_rejected(self, monkeypatch):
        clock = {"now": 1_000_000}

        def fake_time():
            return clock["now"]

        monkeypatch.setattr(ws_tickets.time, "time", fake_time)
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            session_expires_at=clock["now"] + 3600,
        )
        clock["now"] += TTL_SECONDS
        with pytest.raises(TicketInvalid, match="expired"):
            consume_ticket(ticket)

    def test_consumed_ticket_keeps_session_lifetime(self, monkeypatch):
        clock = {"now": 1_000_000}

        def fake_time():
            return clock["now"]

        monkeypatch.setattr(ws_tickets.time, "time", fake_time)
        session_expires_at = clock["now"] + 3600
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            session_expires_at=session_expires_at,
        )
        info = consume_ticket(ticket)
        clock["now"] += TTL_SECONDS + 1
        assert info["session_expires_at"] == session_expires_at

    def test_expired_authenticated_session_rejected(self, monkeypatch):
        clock = {"now": 1_000_000}

        def fake_time():
            return clock["now"]

        monkeypatch.setattr(ws_tickets.time, "time", fake_time)
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            session_expires_at=clock["now"] + 10,
        )
        clock["now"] += 10
        with pytest.raises(TicketInvalid, match="authenticated session expired"):
            consume_ticket(ticket)


class TestErrorMessages:
    def test_unknown_ticket_error_truncates_value(self):
        long_value = "a" * 100
        with pytest.raises(TicketInvalid) as exc_info:
            consume_ticket(long_value)
        message = str(exc_info.value)
        assert long_value not in message
        assert long_value[:8] in message


class TestConcurrency:
    def test_mint_and_consume_concurrent(self):
        results: list[dict] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i: int):
            try:
                ticket = mint_ticket(
                    user_id=f"u{i}",
                    provider="stub",
                    session_expires_at=_session_expiry(),
                )
                info = consume_ticket(ticket)
                with lock:
                    results.append(info)
            except Exception as exc:  # noqa: BLE001 — collect for assert
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
            assert not thread.is_alive(), "thread deadlocked"

        assert errors == []
        assert len(results) == 20
        assert {result["user_id"] for result in results} == {f"u{i}" for i in range(20)}


class TestInternalCredential:
    def test_reset_clears_and_remints(self):
        first = ws_tickets.internal_ws_credential()
        _reset_for_tests()
        with pytest.raises(TicketInvalid):
            ws_tickets.consume_internal_credential(first)
        second = ws_tickets.internal_ws_credential()
        assert second != first
        assert ws_tickets.consume_internal_credential(second)["user_id"] == (
            ws_tickets.INTERNAL_USER_ID
        )

    def test_independent_of_ticket_store(self):
        cred = ws_tickets.internal_ws_credential()
        ticket = mint_ticket(
            user_id="u1",
            provider="nous",
            session_expires_at=_session_expiry(),
        )
        ws_tickets.consume_internal_credential(cred)
        assert consume_ticket(ticket)["user_id"] == "u1"

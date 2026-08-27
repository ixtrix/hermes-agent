from __future__ import annotations

from hermes_cli.managed_browser import (
    active_browser_activity,
    consume_browser_activity,
    record_browser_activity,
    reset_browser_activity_state,
)


def setup_function() -> None:
    reset_browser_activity_state()


def test_started_staff_execution_records_credential_free_fifo_activity() -> None:
    first = record_browser_activity(
        conversation_id="runtime-1",
        execution_id="exec-1",
        runner_session="scope-runtime-1",
        runner_epoch="epoch-1",
        started=True,
    )
    second = record_browser_activity(
        conversation_id="runtime-1",
        execution_id="exec-2",
        runner_session="scope-runtime-1",
        runner_epoch="epoch-1",
        started=True,
    )

    assert first is not None and second is not None
    assert first.identity == "staff-browser:scope-runtime-1"
    assert first.activity_id != second.activity_id
    assert consume_browser_activity("runtime-1") == first
    assert consume_browser_activity("runtime-1") == second
    assert consume_browser_activity("runtime-1") is None
    assert active_browser_activity(first.activity_id) == first


def test_activity_requires_started_execution_and_trusted_bounded_fields() -> None:
    assert (
        record_browser_activity(
            conversation_id="runtime-1",
            execution_id="exec-1",
            runner_session="scope-runtime-1",
            runner_epoch="epoch-1",
            started=False,
        )
        is None
    )
    assert (
        record_browser_activity(
            conversation_id="",
            execution_id="exec-1",
            runner_session="scope-runtime-1",
            runner_epoch="epoch-1",
            started=True,
        )
        is None
    )
    assert (
        record_browser_activity(
            conversation_id="runtime-1",
            execution_id="exec-1",
            runner_session="../../profile",
            runner_epoch="epoch-1",
            started=True,
        )
        is None
    )

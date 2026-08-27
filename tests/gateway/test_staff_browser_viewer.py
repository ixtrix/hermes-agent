from __future__ import annotations

import json
import socket
import struct
import threading
from pathlib import Path
import uuid

from hermes_cli.managed_browser import (
    BrowserActivity,
    record_browser_activity,
    reset_browser_activity_state,
)
from tui_gateway import server


class Transport:
    auth_identity = {"user_id": "staff-user", "provider": "scope-oidc"}

    def write(self, _frame):
        return True


def _serve_ticket_once(path: Path):
    ready = threading.Event()
    received: list[dict] = []

    def run() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                size = struct.unpack("!I", connection.recv(4))[0]
                body = b""
                while len(body) < size:
                    body += connection.recv(size - len(body))
                request = json.loads(body)
                received.append(request)
                response = {
                    "schema_version": "staff-browser-viewer-v1",
                    "status": "ok",
                    "ticket": "ticket_abcdefghijklmnopqrstuvwxyz0123456789",
                }
                encoded = json.dumps(response).encode()
                connection.sendall(struct.pack("!I", len(encoded)) + encoded)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread, received


def _record() -> BrowserActivity:
    activity = record_browser_activity(
        conversation_id="session-fixture",
        execution_id="exec-1",
        runner_session="scope-session-fixture",
        runner_epoch="epoch-1",
        started=True,
    )
    assert activity is not None
    return activity


def setup_function() -> None:
    reset_browser_activity_state()


def teardown_function() -> None:
    server._sessions.pop("session-fixture", None)
    reset_browser_activity_state()


def test_browser_exec_completion_emits_only_credential_free_activity(
    monkeypatch,
) -> None:
    activity = _record()
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        server, "_emit", lambda kind, sid, payload: events.append((kind, sid, payload))
    )

    server._on_tool_complete(
        "session-fixture", "tool-1", "browser_exec", {}, '{"success": true}'
    )

    assert events[-1] == (
        "browser.activity",
        "session-fixture",
        {
            "activity_id": activity.activity_id,
            "identity": "staff-browser:scope-session-fixture",
            "label": "Collaborative Browser",
            "runner_epoch": "epoch-1",
        },
    )
    assert "ticket" not in json.dumps(events)


def test_viewer_ticket_requires_owned_external_session_and_mints_fresh_url(
    monkeypatch,
) -> None:
    socket_path = Path("/tmp") / f"viewer-{uuid.uuid4().hex}.sock"
    thread, received = _serve_ticket_once(socket_path)
    monkeypatch.setenv("SCOPE_STAFF_PROFILE", "external")
    monkeypatch.setenv("HERMES_BROWSER_VIEWER_SOCKET", str(socket_path))
    monkeypatch.setenv(
        "HERMES_BROWSER_VIEWER_PUBLIC_URL",
        "https://external.example/instances/web-user/browser-viewer/",
    )
    activity = _record()
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    server._on_tool_complete("session-fixture", "tool-1", "browser_exec", {}, "{}")

    owner = Transport()
    server._sessions["session-fixture"] = {"transport": owner, "profile": "default"}
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "browser.viewer.ticket",
            "params": {
                "session_id": "session-fixture",
                "activity_id": activity.activity_id,
                "identity": activity.identity,
                "runner_epoch": activity.runner_epoch,
            },
        },
        owner,
    )

    thread.join(2)
    assert response["result"] == {
        "activity_id": activity.activity_id,
        "identity": activity.identity,
        "label": "Collaborative Browser",
        "url": "https://external.example/instances/web-user/browser-viewer/?ticket=ticket_abcdefghijklmnopqrstuvwxyz0123456789",
    }
    assert received == [
        {
            "schema_version": "staff-browser-viewer-v1",
            "operation": "issue_ticket",
            "activity_id": activity.activity_id,
            "browser_identity": activity.identity,
            "runner_epoch": activity.runner_epoch,
        }
    ]


def test_viewer_ticket_rejects_foreign_transport_before_contacting_authority(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCOPE_STAFF_PROFILE", "external")
    activity = _record()
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    server._on_tool_complete("session-fixture", "tool-1", "browser_exec", {}, "{}")
    server._sessions["session-fixture"] = {
        "transport": Transport(),
        "profile": "default",
    }

    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "browser.viewer.ticket",
            "params": {
                "session_id": "session-fixture",
                "activity_id": activity.activity_id,
                "identity": activity.identity,
                "runner_epoch": activity.runner_epoch,
            },
        },
        Transport(),
    )

    assert response["error"]["code"] == 4403

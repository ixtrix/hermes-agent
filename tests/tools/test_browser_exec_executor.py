import json
import socket
import struct
import threading
import uuid
from pathlib import Path

import pytest

from hermes_cli.managed_browser import (
    consume_browser_activity,
    reset_browser_activity_state,
)
from tools.browser_exec_executor import (
    BROWSER_HARNESS_VERSION,
    BROWSER_USE_VERSION,
    PROTOCOL_VERSION,
    BrowserExecOutcome,
    BrowserExecProbe,
    BrowserExecRequest,
    StaffUdsBrowserExecExecutor,
)


def _serve_once(path: Path, reply):
    ready = threading.Event()
    received = []

    def run():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection:
                size = struct.unpack("!I", connection.recv(4))[0]
                body = b""
                while len(body) < size:
                    body += connection.recv(size - len(body))
                request = json.loads(body)
                received.append(request)
                response = reply(request) if callable(reply) else reply
                encoded = json.dumps(response).encode()
                connection.sendall(struct.pack("!I", len(encoded)) + encoded)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread, received


def _probe_reply(**overrides):
    return {
        "schema_version": PROTOCOL_VERSION,
        "status": "ok",
        "image_identity": "scope-browser:test",
        "browser_use_version": BROWSER_USE_VERSION,
        "browser_harness_version": BROWSER_HARNESS_VERSION,
        **overrides,
    }


def test_probe_admits_only_exact_protocol_image_and_package_versions(tmp_path):
    socket_path = Path("/tmp") / f"bex-{uuid.uuid4().hex}.sock"
    thread, received = _serve_once(socket_path, _probe_reply())
    executor = StaffUdsBrowserExecExecutor(str(socket_path), "scope-browser:test")

    probe = executor.probe()

    thread.join(2)
    assert probe.available is True
    assert received == [{"schema_version": PROTOCOL_VERSION, "operation": "probe"}]


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": "other"},
        {"image_identity": "scope-browser:other"},
        {"browser_use_version": "0.13.8"},
        {"browser_harness_version": "0.1.9"},
    ],
)
def test_probe_fails_closed_on_version_or_image_drift(tmp_path, override):
    socket_path = Path("/tmp") / f"bex-{uuid.uuid4().hex}.sock"
    thread, _ = _serve_once(socket_path, _probe_reply(**override))
    executor = StaffUdsBrowserExecExecutor(str(socket_path), "scope-browser:test")

    probe = executor.probe()

    thread.join(2)
    assert probe.available is False
    assert "mismatch" in probe.detail.lower()


def test_execute_sends_only_typed_semantic_request_without_retry(tmp_path):
    socket_path = Path("/tmp") / f"bex-{uuid.uuid4().hex}.sock"

    def reply(request):
        return {
            "schema_version": PROTOCOL_VERSION,
            "execution_id": request["execution_id"],
            "status": "ok",
            "started": True,
            "exit_code": 0,
            "stdout": "done\n",
            "stderr": "",
            "stdout_truncated_bytes": 0,
            "stderr_truncated_bytes": 0,
            "session": "server-derived",
            "runner_epoch": "epoch-1",
            "session_restarted": False,
            "screenshot_path": None,
        }

    thread, received = _serve_once(socket_path, reply)
    executor = StaffUdsBrowserExecExecutor(str(socket_path), "scope-browser:test")
    outcome = executor.execute(
        BrowserExecRequest(
            code="print(1)",
            conversation_id="trusted-conversation",
            model_session_suffix="optional-suffix",
            timeout_s=300,
            task_id="task-1",
        )
    )

    thread.join(2)
    assert outcome.status == "ok"
    assert outcome.stdout == "done\n"
    assert len(received) == 1
    request = received[0]
    assert request["operation"] == "execute"
    assert request["conversation_id"] == "trusted-conversation"
    assert request["model_session_suffix"] == "optional-suffix"
    assert set(request) == {
        "schema_version",
        "operation",
        "execution_id",
        "code",
        "conversation_id",
        "model_session_suffix",
        "task_id",
        "timeout_s",
    }
    assert "env" not in request and "cdp" not in request and "executable" not in request


def test_execute_rejects_oversized_authored_code_before_connect(tmp_path):
    executor = StaffUdsBrowserExecExecutor(
        str(tmp_path / "missing.sock"), "scope-browser:test"
    )
    with pytest.raises(ValueError, match="20,000"):
        executor.execute(
            BrowserExecRequest(
                code="x" * 20_001,
                conversation_id="trusted",
                model_session_suffix="",
                timeout_s=300,
                task_id=None,
            )
        )


def test_execute_rejects_response_with_wrong_execution_identity(tmp_path):
    socket_path = Path("/tmp") / f"bex-{uuid.uuid4().hex}.sock"
    thread, _ = _serve_once(
        socket_path,
        {
            "schema_version": PROTOCOL_VERSION,
            "execution_id": "00000000-0000-4000-8000-000000000000",
            "status": "ok",
            "started": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        },
    )
    executor = StaffUdsBrowserExecExecutor(str(socket_path), "scope-browser:test")

    with pytest.raises(RuntimeError, match="execution identity"):
        executor.execute(
            BrowserExecRequest(
                code="print(1)",
                conversation_id="trusted",
                model_session_suffix="",
                timeout_s=300,
                task_id=None,
            )
        )
    thread.join(2)


def test_public_staff_tool_opens_activity_in_stock_preview_without_exposing_ticket(
    monkeypatch,
):
    from hermes_cli import managed_browser
    from tools import browser_use_cli, open_preview_tool

    class Executor:
        def probe(self):
            return BrowserExecProbe(True, "staff-uds", "ready")

        def execute(self, request):
            return BrowserExecOutcome(
                execution_id="exec-1",
                status="ok",
                started=True,
                exit_code=0,
                stdout="done",
                stderr="",
                session="scope-runtime-1",
                runner_epoch="epoch-1",
            )

    opened = []
    reset_browser_activity_state()
    monkeypatch.setattr(
        browser_use_cli, "_get_browser_exec_executor", lambda: Executor()
    )
    monkeypatch.setattr(
        managed_browser,
        "issue_browser_viewer_ticket",
        lambda activity: "https://external.example/browser-viewer/?ticket=secret",
    )
    monkeypatch.setattr(
        open_preview_tool,
        "open_preview_tool",
        lambda url, label: opened.append((url, label))
        or json.dumps({"success": True, "url": url, "label": label}),
    )

    result = json.loads(
        browser_use_cli.browser_exec(
            "print('done')",
            trusted_session_id="runtime-1",
        )
    )

    assert result == {"success": True, "exit_code": 0, "output": "done"}
    assert opened == [
        (
            "https://external.example/browser-viewer/?ticket=secret",
            "Collaborative Browser",
        )
    ]
    assert consume_browser_activity("runtime-1") is None
    assert "ticket" not in json.dumps(result)


def test_public_staff_tool_does_not_open_preview_for_failed_execution(monkeypatch):
    from hermes_cli import managed_browser
    from tools import browser_use_cli, open_preview_tool

    class Executor:
        def probe(self):
            return BrowserExecProbe(True, "staff-uds", "ready")

        def execute(self, request):
            return BrowserExecOutcome(
                execution_id="exec-1",
                status="process_error",
                started=True,
                exit_code=1,
                stdout="",
                stderr="failed",
                session="scope-runtime-1",
                runner_epoch="epoch-1",
            )

    issued = []
    opened = []
    reset_browser_activity_state()
    monkeypatch.setattr(
        browser_use_cli, "_get_browser_exec_executor", lambda: Executor()
    )
    monkeypatch.setattr(
        managed_browser,
        "issue_browser_viewer_ticket",
        lambda activity: issued.append(activity) or "https://example.invalid",
    )
    monkeypatch.setattr(
        open_preview_tool,
        "open_preview_tool",
        lambda url, label: opened.append((url, label)),
    )

    result = json.loads(
        browser_use_cli.browser_exec(
            "raise RuntimeError",
            trusted_session_id="runtime-1",
        )
    )

    assert result["success"] is False
    assert issued == []
    assert opened == []
    assert consume_browser_activity("runtime-1") is None

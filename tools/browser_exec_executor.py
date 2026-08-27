"""Private typed execution seam for Hermes' public ``browser_exec`` tool."""

from __future__ import annotations

import json
import socket
import struct
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Optional, Protocol

PROTOCOL_VERSION = "browser-exec-runner-v1"
BROWSER_USE_VERSION = "0.13.7"
BROWSER_HARNESS_VERSION = "0.1.8"
MAX_CODE_BYTES = 20_000
MAX_CONVERSATION_BYTES = 160
MAX_TASK_BYTES = 80
MAX_STDOUT_BYTES = 40_000
MAX_STDERR_BYTES = 8_000
_MAX_FRAME_BYTES = 64 * 1024

BrowserExecStatus = Literal[
    "ok",
    "process_error",
    "timed_out",
    "cancelled",
    "unknown_outcome",
    "busy",
    "runner_unavailable",
    "version_mismatch",
    "unauthorized",
    "invalid_request",
    "internal_error",
]
_STATUSES = {
    "ok",
    "process_error",
    "timed_out",
    "cancelled",
    "unknown_outcome",
    "busy",
    "runner_unavailable",
    "version_mismatch",
    "unauthorized",
    "invalid_request",
    "internal_error",
}


@dataclass(frozen=True)
class BrowserExecRequest:
    code: str
    conversation_id: str
    model_session_suffix: str
    timeout_s: int
    task_id: Optional[str]
    local: bool = False


@dataclass(frozen=True)
class BrowserExecProbe:
    available: bool
    adapter: Literal["local", "staff-uds"]
    detail: str
    image_identity: Optional[str] = None
    browser_use_version: Optional[str] = None
    browser_harness_version: Optional[str] = None


@dataclass(frozen=True)
class BrowserExecOutcome:
    execution_id: str
    status: BrowserExecStatus
    started: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    stdout_truncated_bytes: int = 0
    stderr_truncated_bytes: int = 0
    session: str = ""
    runner_epoch: str = ""
    session_restarted: bool = False
    screenshot_path: Optional[str] = None
    workspace: Optional[str] = None


class BrowserExecExecutor(Protocol):
    def probe(self) -> BrowserExecProbe: ...

    def execute(self, request: BrowserExecRequest) -> BrowserExecOutcome: ...


class StaffUdsBrowserExecExecutor:
    """Bounded client for one deployment-selected per-user runner socket."""

    def __init__(self, socket_path: str, expected_image_identity: str):
        if not socket_path:
            raise ValueError("Staff browser runner socket is required")
        if not expected_image_identity:
            raise ValueError("Staff browser runner image identity is required")
        self._socket_path = socket_path
        self._expected_image_identity = expected_image_identity

    def probe(self) -> BrowserExecProbe:
        try:
            response = self._exchange(
                {"schema_version": PROTOCOL_VERSION, "operation": "probe"},
                timeout_s=3,
            )
        except RuntimeError as exc:
            return BrowserExecProbe(False, "staff-uds", str(exc))

        expected = {
            "schema_version": PROTOCOL_VERSION,
            "status": "ok",
            "image_identity": self._expected_image_identity,
            "browser_use_version": BROWSER_USE_VERSION,
            "browser_harness_version": BROWSER_HARNESS_VERSION,
        }
        mismatches = [
            key for key, value in expected.items() if response.get(key) != value
        ]
        if mismatches:
            return BrowserExecProbe(
                False,
                "staff-uds",
                "Staff browser runner admission mismatch: " + ", ".join(mismatches),
            )
        return BrowserExecProbe(
            True,
            "staff-uds",
            "Staff browser runner admitted",
            image_identity=response["image_identity"],
            browser_use_version=response["browser_use_version"],
            browser_harness_version=response["browser_harness_version"],
        )

    def execute(self, request: BrowserExecRequest) -> BrowserExecOutcome:
        self._validate_request(request)
        execution_id = str(uuid.uuid4())
        response = self._exchange(
            {
                "schema_version": PROTOCOL_VERSION,
                "operation": "execute",
                "execution_id": execution_id,
                "code": request.code,
                "conversation_id": request.conversation_id,
                "model_session_suffix": request.model_session_suffix,
                "task_id": request.task_id,
                "timeout_s": request.timeout_s,
            },
            timeout_s=request.timeout_s + 5,
        )
        if response.get("schema_version") != PROTOCOL_VERSION:
            raise RuntimeError("Staff browser runner protocol mismatch")
        if response.get("execution_id") != execution_id:
            raise RuntimeError(
                "Staff browser runner returned the wrong execution identity"
            )
        status = response.get("status")
        if status not in _STATUSES:
            raise RuntimeError("Staff browser runner returned an invalid status")

        stdout = self._bounded_text(response, "stdout", MAX_STDOUT_BYTES)
        stderr = self._bounded_text(response, "stderr", MAX_STDERR_BYTES)
        screenshot_path = response.get("screenshot_path")
        if screenshot_path is not None:
            self._validate_screenshot_path(screenshot_path, execution_id)

        exit_code = response.get("exit_code")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise RuntimeError("Staff browser runner returned an invalid exit code")
        return BrowserExecOutcome(
            execution_id=execution_id,
            status=status,
            started=bool(response.get("started", False)),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated_bytes=self._nonnegative_int(
                response, "stdout_truncated_bytes"
            ),
            stderr_truncated_bytes=self._nonnegative_int(
                response, "stderr_truncated_bytes"
            ),
            session=self._optional_text(response, "session"),
            runner_epoch=self._optional_text(response, "runner_epoch"),
            session_restarted=bool(response.get("session_restarted", False)),
            screenshot_path=screenshot_path,
            workspace=response.get("workspace")
            if isinstance(response.get("workspace"), str)
            else None,
        )

    @staticmethod
    def _validate_request(request: BrowserExecRequest) -> None:
        if request.local:
            raise ValueError(
                "local=true is unavailable through the Staff browser runner"
            )
        code_size = len(request.code.encode("utf-8"))
        if not 1 <= code_size <= MAX_CODE_BYTES:
            raise ValueError(
                "Browser Exec code must be between 1 and 20,000 UTF-8 bytes"
            )
        conversation_size = len(request.conversation_id.encode("utf-8"))
        if not 1 <= conversation_size <= MAX_CONVERSATION_BYTES:
            raise ValueError(
                "Trusted browser conversation identity is missing or too long"
            )
        if (
            request.task_id is not None
            and len(request.task_id.encode("utf-8")) > MAX_TASK_BYTES
        ):
            raise ValueError("Browser Exec task identity is too long")

    @staticmethod
    def _validate_screenshot_path(path: object, execution_id: str) -> None:
        if not isinstance(path, str):
            raise RuntimeError(
                "Staff browser runner returned an invalid screenshot path"
            )
        candidate = PurePosixPath(path)
        if (
            candidate.parent != PurePosixPath("/workspace/.browser-exec/export")
            or candidate.stem != execution_id
            or candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
        ):
            raise RuntimeError(
                "Staff browser runner returned an unconfined screenshot path"
            )

    @staticmethod
    def _bounded_text(response: dict, key: str, cap: int) -> str:
        value = response.get(key, "")
        if not isinstance(value, str) or len(value.encode("utf-8")) > cap:
            raise RuntimeError(f"Staff browser runner returned invalid {key}")
        return value

    @staticmethod
    def _optional_text(response: dict, key: str) -> str:
        value = response.get(key, "")
        if not isinstance(value, str):
            raise RuntimeError(f"Staff browser runner returned invalid {key}")
        return value

    @staticmethod
    def _nonnegative_int(response: dict, key: str) -> int:
        value = response.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"Staff browser runner returned invalid {key}")
        return value

    def _exchange(self, request: dict, timeout_s: int) -> dict:
        encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_FRAME_BYTES:
            raise RuntimeError("Staff browser runner request is too large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout_s)
                client.connect(self._socket_path)
                client.sendall(struct.pack("!I", len(encoded)) + encoded)
                header = self._recv_exact(client, 4)
                size = struct.unpack("!I", header)[0]
                if size < 2 or size > _MAX_FRAME_BYTES:
                    raise RuntimeError("Staff browser runner response is too large")
                payload = self._recv_exact(client, size)
        except (OSError, TimeoutError) as exc:
            raise RuntimeError("Staff browser runner is unavailable") from exc
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Staff browser runner returned malformed JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("Staff browser runner returned an invalid response")
        return decoded

    @staticmethod
    def _recv_exact(connection: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = connection.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("Staff browser runner closed the connection")
            chunks.extend(chunk)
        return bytes(chunks)

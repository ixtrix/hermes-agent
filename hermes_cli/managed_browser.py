"""Process-local authority for managed Staff browser activity.

The public tool result never carries viewer credentials. A started runner call
records a small opaque activity here; the gateway consumes it from the trusted
conversation FIFO and publishes only the credential-free descriptor.
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
import urllib.parse
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass

_MAX_ACTIVITIES = 256
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")

VIEWER_PROTOCOL_VERSION = "staff-browser-viewer-v1"
_MAX_FRAME_BYTES = 16 * 1024
_TICKET_RE = re.compile(r"[A-Za-z0-9._~-]{32,2048}\Z")


@dataclass(frozen=True)
class BrowserActivity:
    activity_id: str
    conversation_id: str
    execution_id: str
    identity: str
    runner_epoch: str
    label: str = "Collaborative Browser"


_lock = threading.Lock()
_pending: dict[str, deque[BrowserActivity]] = {}
_pending_order: deque[tuple[str, str]] = deque()
_active: OrderedDict[str, BrowserActivity] = OrderedDict()


def _valid(value: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(value))


def record_browser_activity(
    *,
    conversation_id: str,
    execution_id: str,
    runner_session: str,
    runner_epoch: str,
    started: bool,
) -> BrowserActivity | None:
    """Queue one started Staff execution without exposing viewer authority."""
    if not started or not all(
        _valid(value)
        for value in (conversation_id, execution_id, runner_session, runner_epoch)
    ):
        return None

    activity = BrowserActivity(
        activity_id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        execution_id=execution_id,
        identity=f"staff-browser:{runner_session}",
        runner_epoch=runner_epoch,
    )
    with _lock:
        queue = _pending.setdefault(conversation_id, deque())
        queue.append(activity)
        _pending_order.append((conversation_id, activity.activity_id))
        while len(_pending_order) > _MAX_ACTIVITIES:
            old_conversation, old_id = _pending_order.popleft()
            old_queue = _pending.get(old_conversation)
            if not old_queue:
                continue
            for index, item in enumerate(old_queue):
                if item.activity_id == old_id:
                    del old_queue[index]
                    break
            if not old_queue:
                _pending.pop(old_conversation, None)
    return activity


def consume_browser_activity(conversation_id: str) -> BrowserActivity | None:
    """Consume the next activity for the exact trusted gateway session."""
    with _lock:
        queue = _pending.get(conversation_id)
        if not queue:
            return None
        activity = queue.popleft()
        if not queue:
            _pending.pop(conversation_id, None)
        _active[activity.activity_id] = activity
        _active.move_to_end(activity.activity_id)
        while len(_active) > _MAX_ACTIVITIES:
            _active.popitem(last=False)
        return activity


def active_browser_activity(activity_id: str) -> BrowserActivity | None:
    with _lock:
        return _active.get(activity_id)


def reset_browser_activity_state() -> None:
    """Clear process-local state; used by gateway shutdown/tests."""
    with _lock:
        _pending.clear()
        _pending_order.clear()
        _active.clear()


def issue_browser_viewer_ticket(activity: BrowserActivity) -> str:
    """Mint a fresh viewer URL through the separate viewer-authority socket."""
    socket_path = os.environ.get("HERMES_BROWSER_VIEWER_SOCKET", "").strip()
    public_url = os.environ.get("HERMES_BROWSER_VIEWER_PUBLIC_URL", "").strip()
    if not socket_path or not public_url:
        raise RuntimeError("Staff browser viewer authority is unavailable")

    parsed = urllib.parse.urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Staff browser viewer public URL is invalid")

    response = _viewer_exchange(
        socket_path,
        {
            "schema_version": VIEWER_PROTOCOL_VERSION,
            "operation": "issue_ticket",
            "activity_id": activity.activity_id,
            "browser_identity": activity.identity,
            "runner_epoch": activity.runner_epoch,
        },
    )
    if (
        response.get("schema_version") != VIEWER_PROTOCOL_VERSION
        or response.get("status") != "ok"
    ):
        raise RuntimeError("Staff browser viewer authority rejected the request")
    ticket = response.get("ticket")
    if not isinstance(ticket, str) or not _TICKET_RE.fullmatch(ticket):
        raise RuntimeError("Staff browser viewer authority returned an invalid ticket")
    separator = "&" if parsed.query else "?"
    return f"{public_url}{separator}{urllib.parse.urlencode({'ticket': ticket})}"


def _viewer_exchange(socket_path: str, request: dict[str, str]) -> dict:
    encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_FRAME_BYTES:
        raise RuntimeError("Staff browser viewer request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(socket_path)
            client.sendall(struct.pack("!I", len(encoded)) + encoded)
            size = struct.unpack("!I", _recv_exact(client, 4))[0]
            if size < 2 or size > _MAX_FRAME_BYTES:
                raise RuntimeError("Staff browser viewer response is too large")
            payload = _recv_exact(client, size)
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("Staff browser viewer authority is unavailable") from exc
    try:
        response = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Staff browser viewer authority returned malformed JSON"
        ) from exc
    if not isinstance(response, dict):
        raise RuntimeError(
            "Staff browser viewer authority returned an invalid response"
        )
    return response


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise RuntimeError("Staff browser viewer authority closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)

# pyright: reportUndefinedVariable=false

"""Authenticated Staff browser activity viewer ticket method."""

from __future__ import annotations

from hermes_cli.managed_browser import (
    active_browser_activity,
    issue_browser_viewer_ticket,
)

from .method_ctx import HandlerRegistry
from .methods_browser_control import _is_authenticated_identity

_registry = HandlerRegistry()
method = _registry.method
_ERR_FORBIDDEN = 4403
_ERR_UNAVAILABLE = 5031


@method("browser.viewer.ticket")
def _(
    rid,
    params: dict,
    _activity_for=active_browser_activity,
    _issue=issue_browser_viewer_ticket,
    _identity_ok=_is_authenticated_identity,
    _forbidden=_ERR_FORBIDDEN,
    _unavailable=_ERR_UNAVAILABLE,
) -> dict:
    """Mint a fresh ticket only for this transport's active External session."""
    if os.environ.get("SCOPE_STAFF_PROFILE", "").strip() != "external":
        return _err(rid, _forbidden, "managed External browser viewer required")

    transport = current_transport()
    identity = getattr(transport, "auth_identity", None)
    if not _identity_ok(identity):
        return _err(rid, _forbidden, "authenticated viewer identity required")

    session_id = str(params.get("session_id") or "")
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None or session.get("transport") is not transport:
            return _err(rid, _forbidden, "session is not owned by this transport")

    activity_id = str(params.get("activity_id") or "")
    activity = _activity_for(activity_id)
    if (
        activity is None
        or activity.conversation_id != session_id
        or params.get("identity") != activity.identity
        or params.get("runner_epoch") != activity.runner_epoch
    ):
        return _err(rid, _forbidden, "browser activity is not active for this session")

    try:
        url = _issue(activity)
    except RuntimeError:
        return _err(rid, _unavailable, "browser viewer authority is unavailable")
    return _ok(
        rid,
        {
            "activity_id": activity.activity_id,
            "identity": activity.identity,
            "label": activity.label,
            "url": url,
        },
    )


def register(server) -> None:
    _registry.install(server)

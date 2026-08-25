"""Fail-closed managed Hermes staff config and JSON broker boundary."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import socket
import threading
import stat
import time
from typing import Any, Callable

_SCHEMA = "scope-hermes-managed-staff-v2"
_IDENTITY_SCHEMA = "scope-hermes-staff-deployment-identity-v2"
_PRODUCTS = {
    "internal": "uk.co.scopefurnishing.hermes.internal",
    "external": "uk.co.scopefurnishing.hermes.external",
}
_IDENTITY_FIELDS = (
    "subject",
    "user_id",
    "plane",
    "instance_id",
    "product_id",
    "connection_id",
    "profile_id",
    "session_id",
    "broker_namespace",
)
_DYNAMIC_FIELDS = ("connection_id", "profile_id", "session_id")
_WORKER_ALIASES = {
    "internal-office-offline",
    "external-web-offline",
    "external-web-networked",
    "external-media",
}
_RUNTIME_METHODS = {"scope_file_admit", "scope_run_job", "run_user_automation"}
STAFF_PURPOSES = frozenset(
    {"catalogue", "document", "media", "supplier", "public-source", "user-automation"}
)


def _secure_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"managed staff file is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"managed staff file must be a regular file: {path}")
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"managed staff file must be root-owned and immutable: {path}")


def _required_mapping(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"managed staff {label} has an invalid shape")
    return value


def load_managed_staff_config() -> dict[str, Any]:
    """Load the immutable root-owned config and verify its deployment bindings."""
    path_value = os.environ.get("HERMES_MANAGED_STAFF_CONFIG", "").strip()
    if not path_value:
        raise ValueError("HERMES_MANAGED_STAFF_CONFIG is required")
    path = Path(path_value)
    _secure_file(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("managed staff config is not valid JSON") from exc
    _required_mapping(document, {"schema", "managed_staff"}, "config")
    if document["schema"] != _SCHEMA:
        raise ValueError("unsupported managed staff config schema")
    managed = _required_mapping(
        document["managed_staff"],
        {"plane", "product_id", "instance_id", "user_id", "oidc", "broker", "toolsets"},
        "identity",
    )
    plane = managed["plane"]
    if plane not in _PRODUCTS or managed["product_id"] != _PRODUCTS[plane]:
        raise ValueError("managed staff plane/product binding is invalid")
    for field in ("product_id", "instance_id", "user_id"):
        if not isinstance(managed[field], str) or not managed[field].strip():
            raise ValueError(f"managed staff {field} is required")
    oidc = _required_mapping(
        managed["oidc"],
        {"provider", "client_id", "subject", "issuer", "audience", "redirect_uri"},
        "OIDC",
    )
    if any(not isinstance(value, str) or not value.strip() for value in oidc.values()):
        raise ValueError("managed staff OIDC values are required")
    if oidc["provider"].casefold() == "basic":
        raise ValueError("Basic Auth is not permitted in managed staff mode")
    for field in ("issuer", "redirect_uri"):
        if not oidc[field].lower().startswith("https://"):
            raise ValueError(f"managed staff OIDC {field} must use HTTPS")
    public_url = os.environ.get("HERMES_DASHBOARD_PUBLIC_URL", "").strip().rstrip("/")
    if not public_url:
        raise ValueError("HERMES_DASHBOARD_PUBLIC_URL is required in managed staff mode")
    if f"{public_url}/auth/callback" != oidc["redirect_uri"]:
        raise ValueError(
            "HERMES_DASHBOARD_PUBLIC_URL does not match managed staff OIDC redirect_uri"
        )
    broker = _required_mapping(
        managed["broker"], {"socket", "namespace", "key_file"}, "broker"
    )
    if any(not isinstance(value, str) or not value.strip() for value in broker.values()):
        raise ValueError("managed staff broker values are required")
    toolsets = managed["toolsets"]
    if not isinstance(toolsets, dict) or not toolsets:
        raise ValueError("managed staff toolsets are required")
    for server, methods in toolsets.items():
        if not isinstance(server, str) or not server.strip():
            raise ValueError("managed staff toolset name is invalid")
        if not isinstance(methods, list) or not methods or any(
            not isinstance(method, str) or not method.strip() for method in methods
        ):
            raise ValueError(f"managed staff toolset methods are invalid: {server}")
        if len(set(methods)) != len(methods):
            raise ValueError(f"managed staff toolset methods are duplicated: {server}")
    if set(toolsets.get("staff_runtime", ())) != _RUNTIME_METHODS:
        raise ValueError("staff_runtime must expose exactly the approved methods")

    env_bindings = {
        "plane": "HERMES_STAFF_PLANE",
        "product_id": "HERMES_STAFF_PRODUCT_ID",
        "instance_id": "HERMES_STAFF_INSTANCE_ID",
    }
    for field, env_name in env_bindings.items():
        if os.environ.get(env_name, "").strip() != str(managed[field]):
            raise ValueError(f"{env_name} does not match managed staff config")
    broker_env = {
        "socket": "HERMES_STAFF_BROKER_SOCKET",
        "namespace": "HERMES_STAFF_BROKER_NAMESPACE",
        "key_file": "HERMES_STAFF_BROKER_KEY_FILE",
    }
    for field, env_name in broker_env.items():
        if os.environ.get(env_name, "").strip() != str(broker[field]):
            raise ValueError(f"{env_name} does not match managed staff config")

    identity_path_value = os.environ.get("HERMES_STAFF_DEPLOYMENT_IDENTITY_FILE", "").strip()
    if not identity_path_value:
        raise ValueError("HERMES_STAFF_DEPLOYMENT_IDENTITY_FILE is required")
    identity_path = Path(identity_path_value)
    _secure_file(identity_path)
    try:
        identity_doc = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("deployment identity is not valid JSON") from exc
    _required_mapping(
        identity_doc,
        {"schema", "subject", "user_id", "plane", "instance_id", "product_id", "broker_namespace"},
        "deployment identity",
    )
    if identity_doc["schema"] != _IDENTITY_SCHEMA:
        raise ValueError("unsupported deployment identity schema")
    for field in ("subject", "user_id", "plane", "instance_id", "product_id", "broker_namespace"):
        if not isinstance(identity_doc[field], str) or not identity_doc[field].strip():
            raise ValueError(f"deployment identity {field} is required")
    expected_identity = {
        "subject": oidc["subject"],
        "user_id": managed["user_id"],
        "plane": managed["plane"],
        "instance_id": managed["instance_id"],
        "product_id": managed["product_id"],
        "broker_namespace": broker["namespace"],
    }
    for field, expected in expected_identity.items():
        if identity_doc[field] != expected:
            raise ValueError(f"deployment identity {field} does not match config")

    managed = dict(managed)
    managed["config_path"] = str(path)
    managed["identity_path"] = str(identity_path)
    return managed


def _context_mapping(context: Any) -> Any:
    if isinstance(context, dict):
        for key in ("managed_staff_session", "staff_session", "authenticated_staff"):
            if isinstance(context.get(key), dict):
                return context[key]
        return context
    state = getattr(context, "state", None)
    if state is not None:
        for key in ("managed_staff_session", "staff_session", "authenticated_staff"):
            value = getattr(state, key, None)
            if value is not None:
                return value
    return context


def make_identity_resolver(config: dict[str, Any]) -> Callable[[Any], dict[str, str]]:
    def resolve(context: Any) -> dict[str, str]:
        source = _context_mapping(context)
        values: dict[str, Any] = {}
        for field in _DYNAMIC_FIELDS + ("session_expires_at", "revoked"):
            if isinstance(source, dict):
                values[field] = source.get(field)
            else:
                values[field] = getattr(source, field, None)
        if any(
            not isinstance(values[field], str) or not values[field].strip()
            for field in _DYNAMIC_FIELDS
        ):
            raise ValueError("authenticated managed staff session is incomplete")
        if values["revoked"] is True or not isinstance(
            values["session_expires_at"], (int, float)
        ):
            raise ValueError("authenticated managed staff session expiry is invalid")
        if values["session_expires_at"] <= time.time():
            raise ValueError("authenticated managed staff session is expired")
        profile_id = values["profile_id"]
        if profile_id in _WORKER_ALIASES:
            raise ValueError("profile_id must not be a worker alias")
        return {
            "subject": config["oidc"]["subject"],
            "user_id": config["user_id"],
            "plane": config["plane"],
            "instance_id": config["instance_id"],
            "product_id": config["product_id"],
            "connection_id": values["connection_id"],
            "profile_id": profile_id,
            "session_id": values["session_id"],
            "broker_namespace": config["broker"]["namespace"],
        }

    return resolve
def managed_staff_provider_matches(provider: Any, oidc: dict[str, str]) -> bool:
    """Check the configured provider's verified OIDC bindings exactly."""
    if getattr(provider, "name", None) != oidc["provider"]:
        return False
    if bool(getattr(provider, "supports_password", False)):
        return False
    if not bool(getattr(provider, "supports_session", True)):
        return False
    issuer = getattr(provider, "issuer", None)
    if issuer is None:
        issuer = getattr(provider, "_issuer", None)
    client_id = getattr(provider, "client_id", None)
    if client_id is None:
        client_id = getattr(provider, "_client_id", None)
    audience = getattr(provider, "audience", None)
    if audience is None:
        audience = getattr(provider, "_audience", None)
    if not isinstance(issuer, str) or issuer.rstrip("/") != oidc["issuer"].rstrip("/"):
        return False
    if not isinstance(client_id, str) or client_id != oidc["client_id"]:
        return False
    if not isinstance(audience, str) or audience != oidc["audience"]:
        return False
    provider_redirect = getattr(provider, "redirect_uri", None)
    if provider_redirect is None:
        provider_redirect = getattr(provider, "_redirect_uri", None)
    if not isinstance(provider_redirect, str) or provider_redirect != oidc["redirect_uri"]:
        return False
    return True

_STAFF_SESSION_LOCK = threading.RLock()
_STAFF_SESSIONS: dict[str, dict[str, Any]] = {}


def get_managed_staff_session(
    config: dict[str, Any],
    *,
    session_id: str,
    profile_id: str,
    session_expires_at: float,
) -> dict[str, Any]:
    """Return one server-owned tuple for this authenticated session."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("managed staff session_id is required")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("managed staff profile_id is required")
    if profile_id in _WORKER_ALIASES:
        raise ValueError("profile_id must not be a worker alias")
    if (
        isinstance(session_expires_at, bool)
        or not isinstance(session_expires_at, (int, float))
        or session_expires_at <= time.time()
    ):
        raise ValueError("managed staff session expiry is invalid")
    with _STAFF_SESSION_LOCK:
        current = _STAFF_SESSIONS.get(session_id)
        if current is not None:
            if current["profile_id"] != profile_id:
                raise ValueError("managed staff session profile binding mismatch")
            if float(current["session_expires_at"]) <= time.time():
                _STAFF_SESSIONS.pop(session_id, None)
                current = None
        if current is None:
            current = {
                "connection_id": "conn_" + secrets.token_urlsafe(18),
                "profile_id": profile_id,
                "session_id": session_id,
                "session_expires_at": float(session_expires_at),
                "revoked": False,
            }
            _STAFF_SESSIONS[session_id] = current
        return dict(current)


class ManagedStaffBrokerClient:
    """JSON broker client over the authenticated AF_UNIX HTTP boundary."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.socket_path = config["broker"]["socket"]
        self.key_path = config["broker"]["key_file"]
        self.namespace = config["broker"]["namespace"]
        self.plane = config["plane"]

    def _dispatch(self, operation: str, payload: dict[str, Any], identity: dict[str, str]) -> Any:
        if not operation or "/" in operation or "\\" in operation:
            raise ValueError("invalid managed staff broker operation")
        if not isinstance(payload, dict) or not isinstance(identity, dict):
            raise TypeError("managed staff broker payload and identity must be mappings")
        body = json.dumps(
            {"identity": identity, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        body_sha256 = hashlib.sha256(body).hexdigest()
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(18)
        path = f"/v1/staff/runtime/{operation}"
        signing_value = (
            f"v1\nPOST\n{path}\n{self.namespace}\n{timestamp}\n{nonce}\n{body_sha256}"
        ).encode("ascii")
        key = Path(self.key_path).read_bytes()
        signature = hmac.new(key, signing_value, hashlib.sha256).hexdigest()
        headers = {
            "Host": "localhost",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "close",
            "X-Hermes-Shadow-Plane": self.plane,
            "X-Hermes-Staff-Namespace": self.namespace,
            "X-Hermes-Staff-Content-SHA256": body_sha256,
            "X-Hermes-Staff-Timestamp": timestamp,
            "X-Hermes-Staff-Nonce": nonce,
            "X-Hermes-Staff-Signature": signature,
        }
        wire = (
            f"POST {path} HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            + "\r\n"
        ).encode("ascii") + body
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(10.0)
            conn.connect(self.socket_path)
            conn.sendall(wire)
            raw = bytearray()
            while len(raw) <= 64 * 1024 * 1024:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw.extend(chunk)
        marker = raw.find(b"\r\n\r\n")
        if marker < 0:
            raise RuntimeError("managed staff broker returned malformed HTTP")
        head = bytes(raw[:marker]).split(b"\r\n")
        if not head or len(head[0].split()) != 3:
            raise RuntimeError("managed staff broker returned malformed status")
        try:
            status = int(head[0].split()[1])
            response_headers = {
                line.split(b":", 1)[0].decode("latin-1").lower():
                line.split(b":", 1)[1].strip().decode("latin-1")
                for line in head[1:] if b":" in line
            }
            content_length = int(response_headers["content-length"])
        except (KeyError, ValueError):
            raise RuntimeError("managed staff broker returned malformed headers") from None
        response_body = bytes(raw[marker + 4:])
        if len(response_body) != content_length:
            raise RuntimeError("managed staff broker returned truncated response")
        try:
            response = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("managed staff broker returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise RuntimeError("managed staff broker response must be an object")
        if status < 200 or status >= 300:
            detail = response.get("detail") or response.get("error") or "request rejected"
            raise RuntimeError(f"managed staff broker rejected request: {detail}")
        if response.get("error"):
            raise RuntimeError(f"managed staff broker rejected request: {response['error']}")
        return response

    def _private_request(
        self, method: str, path: str, body: bytes, *,
        identity: dict[str, str], context: bytes = b""
    ) -> tuple[int, dict[str, str], bytes]:
        body_sha256 = hashlib.sha256(body).hexdigest()
        context_sha256 = hashlib.sha256(context).hexdigest()
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(18)
        signed = (
            f"v1\n{method}\n{path}\n{self.namespace}\n{timestamp}\n{nonce}\n"
            f"{context_sha256}\n{body_sha256}"
            if context else
            f"v1\n{method}\n{path}\n{self.namespace}\n{timestamp}\n{nonce}\n{body_sha256}"
        ).encode("ascii")
        signature = hmac.new(Path(self.key_path).read_bytes(), signed, hashlib.sha256).hexdigest()
        headers = {
            "Host": self.namespace,
            "Content-Type": "application/octet-stream" if context else "application/json",
            "Content-Length": str(len(body)),
            "Connection": "close",
            "X-Hermes-Shadow-Plane": self.plane,
            "X-Hermes-Staff-Namespace": self.namespace,
            "X-Hermes-Staff-Content-SHA256": body_sha256,
            "X-Hermes-Staff-Timestamp": timestamp,
            "X-Hermes-Staff-Nonce": nonce,
            "X-Hermes-Staff-Signature": signature,
        }
        if context:
            headers["X-Hermes-Staff-Context"] = base64.urlsafe_b64encode(context).rstrip(b"=").decode()
            headers["X-Hermes-Staff-Context-SHA256"] = context_sha256
        wire = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            + "\r\n"
        ).encode("ascii") + body
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(30.0)
            conn.connect(self.socket_path)
            conn.sendall(wire)
            raw = bytearray()
            while chunk := conn.recv(64 * 1024):
                raw.extend(chunk)
                if len(raw) > 64 * 1024 * 1024:
                    raise RuntimeError("managed staff broker response is too large")
        header, separator, response_body = bytes(raw).partition(b"\r\n\r\n")
        if not separator:
            raise RuntimeError("managed staff broker response is malformed")
        lines = header.split(b"\r\n")
        try:
            status = int(lines[0].split()[1])
            response_headers = {
                line.split(b":", 1)[0].decode().lower(): line.split(b":", 1)[1].strip().decode()
                for line in lines[1:] if b":" in line
            }
        except (IndexError, ValueError, UnicodeDecodeError):
            raise RuntimeError("managed staff broker response is invalid") from None
        if status < 200 or status >= 300:
            try:
                detail = json.loads(response_body).get("detail", "request rejected")
            except (ValueError, AttributeError):
                detail = "request rejected"
            raise RuntimeError(f"managed staff broker rejected request: {detail}")
        return status, response_headers, response_body

    def staff_admit_attachment(
        self, *, identity: dict[str, str], source_path: Path, filename: str,
        purpose: str = "", supplier_id: str = "", supplier_domain: str = "",
        declared_mime: str = ""
    ) -> Any:
        data = source_path.read_bytes()
        if not data:
            raise ValueError("managed staff attachment is empty")
        context = json.dumps(
            {
                "identity": identity,
                "filename": Path(filename).name,
                "mime_type": declared_mime,
                "size": len(data),
                "purpose": purpose,
                **({"supplier_id": supplier_id} if supplier_id else {}),
                **({"supplier_domain": supplier_domain} if supplier_domain else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _status, _headers, raw_receipt = self._private_request(
            "PUT", "/v1/staff/attachments/admit", data, identity=identity, context=context
        )
        try:
            receipt = json.loads(raw_receipt)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("managed staff admission returned invalid JSON") from exc
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("attachment_id"), str)
            or receipt.get("purpose") != purpose
        ):
            raise RuntimeError("managed staff admission returned incomplete receipt")
        for field, expected in (
            ("supplier_id", supplier_id),
            ("supplier_domain", supplier_domain),
        ):
            if expected and receipt.get(field) != expected:
                raise RuntimeError(f"managed staff admission {field} mismatch")
        payload: dict[str, Any] = {
            "attachment_id": receipt["attachment_id"],
            "purpose": receipt["purpose"],
        }
        for key in ("supplier_id", "supplier_domain", "declared_mime"):
            value = receipt.get(key)
            if value:
                payload[key] = value
        return self._dispatch("scope_file_admit", payload, identity)

    def staff_download_export(self, *, identity: dict[str, str], export_id: str) -> Any:
        if not re.fullmatch(r"exp_[A-Za-z0-9_-]{43}", str(export_id)):
            raise ValueError("managed staff export id is invalid")
        body = json.dumps({"identity": identity}, sort_keys=True, separators=(",", ":")).encode()
        path = f"/v1/staff/exports/{export_id}/download"
        _status, headers, data = self._private_request("POST", path, body, identity=identity)
        try:
            expected_length = int(headers["content-length"])
            digest = headers["x-content-sha256"].strip().lower()
            mime_type = headers["content-type"].split(";", 1)[0].strip().lower()
            filename = headers["x-hermes-staff-filename"].strip()
            state = headers["x-hermes-staff-state"].strip().lower()
            returned_id = headers["x-hermes-staff-export-id"].strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("managed export response metadata is incomplete") from exc
        if (
            returned_id != export_id
            or state != "downloaded"
            or expected_length != len(data)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(data).hexdigest() != digest
            or "/" not in mime_type
            or not filename
        ):
            raise RuntimeError("managed export response metadata is invalid")
        return {
            "export_id": returned_id,
            "state": state,
            "data": data,
            "length": expected_length,
            "sha256": digest,
            "mime_type": mime_type,
            "filename": Path(filename).name,
        }

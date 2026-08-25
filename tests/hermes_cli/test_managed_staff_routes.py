"""Behavior checks for the managed staff web boundary."""

import asyncio
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request
from fastapi.testclient import TestClient

from hermes_cli import dashboard_auth, web_server
from hermes_cli.dashboard_auth import native_flow
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


IDENTITY = {
    "subject": "sub-1",
    "user_id": "user-1",
    "plane": "external",
    "instance_id": "instance-1",
    "product_id": "uk.co.scopefurnishing.hermes.external",
    "connection_id": "connection-1",
    "profile_id": "external-web-offline",
    "session_id": "session-1",
    "broker_namespace": "namespace-1",
}


def _request():
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/exports/exp_" + "a" * 43 + "/download",
            "raw_path": b"",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("staff.example.test", 443),
            "client": ("127.0.0.1", 1000),
            "app": web_server.app,
        }
    )


def test_staff_export_download_is_raw_and_identity_bound():
    data = b"released bytes"
    export_id = "exp_" + "a" * 43
    calls = {}

    class Adapter:
        def staff_download_export(self, *, identity, export_id):
            calls.update(identity=identity, export_id=export_id)
            return {
                "export_id": export_id,
                "data": data,
                "length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mime_type": "application/pdf",
                "filename": "report.pdf",
                "state": "released",
            }

    class Identity:
        def __call__(self, request):
            assert not hasattr(request.state, "staff_identity")
            return IDENTITY

    web_server.configure_managed_staff_adapters(
        export_adapter=Adapter(), identity_adapter=Identity()
    )
    try:
        response = web_server._staff_export_response(_request(), export_id)
    finally:
        web_server.configure_managed_staff_adapters()

    assert response.body == data
    assert response.media_type == "application/pdf"
    assert response.headers["X-Content-SHA256"] == hashlib.sha256(data).hexdigest()
    assert response.headers["Content-Disposition"].endswith('filename="report.pdf"')
    assert calls == {"identity": IDENTITY, "export_id": export_id}


def test_managed_route_install_removes_host_authority_and_pty(monkeypatch):
    original_routes = list(web_server.app.router.routes)
    monkeypatch.setenv("HERMES_MANAGED_STAFF_MODE", "1")
    try:
        web_server._install_managed_staff_routes()
        paths = {getattr(route, "path", "") for route in web_server.app.router.routes}
        assert "/v1/exports/{export_id}/download" in paths
        assert "/api/pty" not in paths
        assert "/api/files/download" not in paths
        assert not any(path.startswith("/api/fs/") for path in paths)
    finally:
        web_server.app.router.routes[:] = original_routes


def test_managed_auth_rejects_basic_provider(monkeypatch):
    monkeypatch.setattr(
        dashboard_auth,
        "list_providers",
        lambda: [SimpleNamespace(name="basic", supports_password=True, supports_session=True)],
    )
    with pytest.raises(SystemExit, match="Basic Auth"):
        web_server._validate_managed_staff_auth()



@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (None, False),
        ("https://attacker.example/auth/callback", False),
        ("https://hermes.example.test/auth/callback", True),
    ],
)
def test_managed_provider_requires_exact_redirect_binding(redirect_uri, expected):
    from hermes_cli.managed_staff import managed_staff_provider_matches

    oidc = {
        "provider": "entra",
        "issuer": "https://login.example.test",
        "client_id": "client",
        "audience": "api://hermes",
        "redirect_uri": "https://hermes.example.test/auth/callback",
    }
    provider = SimpleNamespace(
        name="entra",
        supports_session=True,
        supports_password=False,
        issuer=oidc["issuer"],
        client_id=oidc["client_id"],
        audience=oidc["audience"],
        redirect_uri=redirect_uri,
    )
    assert managed_staff_provider_matches(provider, oidc) is expected

def test_private_broker_request_signs_method_context_and_body(monkeypatch, tmp_path):
    from hermes_cli import managed_staff

    key_file = tmp_path / "broker.key"
    key_file.write_bytes(b"k" * 32)
    sent = {}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, value):
            assert value == 30.0

        def connect(self, path):
            assert path == "/run/hermes/staff.sock"

        def sendall(self, wire):
            sent["wire"] = wire

        def recv(self, _size):
            if sent.get("done"):
                return b""
            sent["done"] = True
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    monkeypatch.setattr(managed_staff.socket, "socket", lambda *_args: FakeSocket())
    client = managed_staff.ManagedStaffBrokerClient(
        {
            "plane": "external",
            "broker": {
                "socket": "/run/hermes/staff.sock",
                "namespace": "ns",
                "key_file": str(key_file),
            },
        }
    )
    client._private_request("PUT", "/v1/staff/attachments/admit", b"abc", identity={}, context=b"{}")
    wire = sent["wire"]
    assert wire.startswith(b"PUT /v1/staff/attachments/admit HTTP/1.1\r\n")
    assert b"X-Hermes-Staff-Context:" in wire
    assert b"X-Hermes-Staff-Signature:" in wire
    assert wire.endswith(b"\r\n\r\nabc")



def test_managed_native_flow_uses_static_redirect_despite_proxy_headers(monkeypatch):
    configured_redirect = "https://hermes.example.test/auth/callback"
    managed_oidc = {
        "provider": "entra",
        "client_id": "client",
        "subject": "subject",
        "issuer": "https://login.example.test",
        "audience": "api://hermes",
        "redirect_uri": configured_redirect,
    }
    seen_redirects = []

    class Provider(StubAuthProvider):
        name = "entra"
        issuer = managed_oidc["issuer"]
        client_id = managed_oidc["client_id"]
        audience = managed_oidc["audience"]
        redirect_uri = configured_redirect

        def start_login(self, *, redirect_uri):
            seen_redirects.append(redirect_uri)
            return super().start_login(redirect_uri=redirect_uri)

        def complete_login(self, **kwargs):
            seen_redirects.append(kwargs["redirect_uri"])
            return super().complete_login(**kwargs)

    monkeypatch.setenv("HERMES_MANAGED_STAFF_MODE", "1")
    monkeypatch.setattr(
        "hermes_cli.managed_staff.load_managed_staff_config",
        lambda: {"oidc": managed_oidc},
    )
    previous_host = getattr(web_server.app.state, "bound_host", None)
    previous_required = getattr(web_server.app.state, "auth_required", None)
    dashboard_auth.clear_providers()
    native_flow._reset_for_tests()
    dashboard_auth.register_provider(Provider())
    web_server.app.state.bound_host = "hermes.example.test"
    web_server.app.state.auth_required = True
    client = TestClient(
        web_server.app,
        base_url="https://hermes.example.test",
        follow_redirects=False,
    )
    hostile_headers = {
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "http",
    }
    try:
        response = client.get(
            "/auth/native/authorize",
            params={
                "provider": "entra",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "redirect_uri": "http://127.0.0.1:51000/callback",
                "state": "desktop-state",
            },
            headers=hostile_headers,
        )
        assert response.status_code == 302
        callback = urlparse(response.headers["location"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == configured_redirect
        query = parse_qs(callback.query)
        completed = client.get(
            "/auth/callback",
            params={"code": query["code"][0], "state": query["state"][0]},
            cookies=response.cookies,
            headers=hostile_headers,
        )
        assert completed.status_code == 302
        assert seen_redirects == [configured_redirect, configured_redirect]
    finally:
        dashboard_auth.clear_providers()
        native_flow._reset_for_tests()
        web_server.app.state.bound_host = previous_host
        web_server.app.state.auth_required = previous_required

def test_managed_native_authorize_rejects_unbound_provider(monkeypatch):
    from fastapi import HTTPException
    from hermes_cli.dashboard_auth import routes as auth_routes

    monkeypatch.setenv("HERMES_MANAGED_STAFF_MODE", "1")
    monkeypatch.setattr(
        "hermes_cli.managed_staff.load_managed_staff_config",
        lambda: {
            "oidc": {
                "provider": "entra",
                "client_id": "client",
                "subject": "subject",
                "issuer": "https://login.example.test",
                "audience": "api://hermes",
                "redirect_uri": "https://hermes.example.test/auth/callback",
            }
        },
    )
    monkeypatch.setattr(
        auth_routes,
        "get_provider",
        lambda name: SimpleNamespace(
            name=name,
            supports_session=True,
            supports_password=False,
            issuer="https://wrong.example.test",
            client_id="client",
            audience="api://hermes",
            redirect_uri="https://hermes.example.test/auth/callback",
        ),
    )
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            auth_routes.auth_native_authorize(
                _request(),
                provider="entra",
                code_challenge="challenge",
                code_challenge_method="S256",
                redirect_uri="http://127.0.0.1:51000/callback",
            )
        )
    assert rejected.value.status_code == 503
    assert "OIDC" in rejected.value.detail

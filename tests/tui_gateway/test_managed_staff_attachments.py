"""Behavior checks for opaque managed staff file admission."""

import base64

from tui_gateway import server


def test_managed_attach_admits_bytes_and_returns_opaque_receipt(monkeypatch):
    session = {"session_id": "sess-1"}
    calls = {}
    monkeypatch.setattr(server, "_is_managed_staff_mode", lambda: True)
    monkeypatch.setattr(server, "_sess", lambda params, rid: (session, None))

    def admit(_session, **kwargs):
        calls.update(kwargs)
        return {
            "attachment_id": "att_" + "a" * 43,
            "name": "invoice.pdf",
            "size_bytes": len(kwargs["data"]),
            "mime_type": kwargs["declared_mime"],
            "purpose": kwargs["purpose"],
            "supplier_id": kwargs["supplier_id"],
            "supplier_domain": kwargs["supplier_domain"],
            "state": "admitted",
        }

    monkeypatch.setattr(server, "_staff_admit_attachment", admit)
    result = server._methods["file.attach"](
        "req-1",
        {
            "session_id": "sess-1",
            "data": base64.b64encode(b"Hello").decode(),
            "name": "../invoice.pdf",
            "mime_type": "application/pdf",
            "purpose": "supplier",
            "supplier_id": "supplier-1",
            "supplier_domain": "example.test",
        },
    )

    descriptor = result["result"]
    assert set(descriptor) == {
        "attached", "attachment_id", "name", "size", "mime_type", "metadata"
    }
    assert descriptor["attached"] is True
    assert descriptor["attachment_id"] == "att_" + "a" * 43
    assert descriptor["name"] == "invoice.pdf"
    assert descriptor["size"] == 5
    assert descriptor["metadata"]["purpose"] == "supplier"
    assert descriptor["metadata"]["supplier_id"] == "supplier-1"
    assert descriptor["metadata"]["supplier_domain"] == "example.test"
    assert "path" not in descriptor
    assert calls["data"] == b"Hello"


def test_managed_attach_rejects_client_path(monkeypatch):
    monkeypatch.setattr(server, "_is_managed_staff_mode", lambda: True)
    monkeypatch.setattr(server, "_sess", lambda params, rid: ({"session_id": "sess-1"}, None))
    result = server._methods["file.attach"](
        "req-2", {"session_id": "sess-1", "path": "/tmp/secret.txt"}
    )
    assert result["error"]["code"] == 4015

def test_managed_prompt_attachments_are_opaque_and_path_free(monkeypatch):
    monkeypatch.setattr(server, "_staff_identity_for_session", lambda session: {})
    session = {"session_id": "sess-1"}
    assert server._managed_prompt_attachments(
        session,
        [{
            "attachment_id": "att_" + "a" * 43,
            "name": "invoice.pdf",
            "size": 5,
            "mime_type": "application/pdf",
            "metadata": {},
        }],
    )[0]["name"] == "invoice.pdf"

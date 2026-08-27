"""Behavior checks for the managed staff tool inventory boundary."""

import pytest

from tui_gateway import server


def test_managed_loader_returns_only_explicit_toolsets(monkeypatch):
    monkeypatch.setenv("HERMES_MANAGED_STAFF_MODE", "1")
    monkeypatch.setattr(server, "_install_managed_staff_adapters", lambda: None)
    monkeypatch.setattr(
        server,
        "_load_managed_staff_inventory",
        lambda: ("internal", ["memory_guard", "staff_runtime"], {"scope_run_job"}),
    )
    assert server._load_enabled_toolsets() == ["memory_guard", "staff_runtime"]


@pytest.mark.parametrize(
    ("plane", "platform", "toolsets", "expected"),
    [
        (
            "external",
            "desktop",
            ["staff_runtime", "web"],
            ["desktop_ui", "staff_runtime", "web"],
        ),
        ("external", "tui", ["staff_runtime", "web"], ["staff_runtime", "web"]),
        (
            "internal",
            "desktop",
            ["memory_guard", "staff_runtime"],
            ["memory_guard", "staff_runtime"],
        ),
    ],
)
def test_managed_loader_adds_desktop_ui_only_to_external_desktop_sessions(
    monkeypatch, plane, platform, toolsets, expected
):
    monkeypatch.setenv("HERMES_MANAGED_STAFF_MODE", "1")
    monkeypatch.setattr(server, "_install_managed_staff_adapters", lambda: None)
    monkeypatch.setattr(
        server,
        "_load_managed_staff_inventory",
        lambda: (plane, toolsets, {"scope_run_job"}),
    )

    assert server._load_enabled_toolsets(platform) == expected


def test_managed_inventory_rejects_empty_server_mapping(monkeypatch):
    monkeypatch.setattr(
        server,
        "_managed_staff_config",
        lambda: {
            "plane": "internal",
            "product_id": "uk.co.scopefurnishing.hermes.internal",
            "toolsets": {},
        },
    )
    with pytest.raises(ValueError, match="inventory"):
        server._load_managed_staff_inventory()


def test_managed_inventory_rejects_project_server(monkeypatch):
    monkeypatch.setattr(
        server,
        "_managed_staff_config",
        lambda: {
            "plane": "internal",
            "product_id": "uk.co.scopefurnishing.hermes.internal",
            "toolsets": {"project": ["run"]},
        },
    )
    with pytest.raises(ValueError, match="inventory"):
        server._load_managed_staff_inventory()

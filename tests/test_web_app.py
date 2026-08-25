"""Tests for runtime:web app panel serving and file I/O."""
from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import pytest

from senza_agent.webserver import apps


@pytest.fixture(autouse=True)
def _isolated_apps_dir(tmp_path, monkeypatch):
    """Redirect apps dir to tmp_path for each test."""
    monkeypatch.setenv("APPS_DIR", str(tmp_path / "apps"))
    yield


# ── Panel HTML ─────────────────────────────────────────────────────────────


def test_web_app_panel_html():
    apps.register_app(name="Test", description="", runtime="web", script="<h1>Hi</h1>")
    app_id = "Test"
    html = apps.get_app_panel_html(app_id)
    assert html is not None
    assert "__QEVOS__" in html
    assert "qevos-bridge.js" in html
    assert "qevos-theme.css" in html
    assert "<h1>Hi</h1>" in html


def test_web_app_panel_html_with_root():
    apps.register_app(name="Test", description="", runtime="web", script="<p>ok</p>")
    html = apps.get_app_panel_html("Test", root="/tmp/fakeproj")
    assert html is not None
    assert '"root": "/tmp/fakeproj"' in html


def test_non_web_app_returns_none():
    apps.register_app(name="Script", description="", runtime="python", script="print('hi')")
    assert apps.get_app_panel_html("Script") is None


def test_nonexistent_app_returns_none():
    assert apps.get_app_panel_html("no_such_app") is None


def test_web_app_entry_file(tmp_path):
    """If entry is set, panel HTML reads from the project folder."""
    entry_html = "<canvas id='c'></canvas>"
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "panel.html").write_text(entry_html, encoding="utf-8")
    apps.register_app(name="Entry", description="", runtime="web", script="ignored")
    # Manually add entry field to the app file
    app_file = Path(os.environ["APPS_DIR"]) / "Entry.md"
    content = app_file.read_text(encoding="utf-8")
    content = content.replace("enabled: true", "enabled: true\nentry: panel.html")
    app_file.write_text(content, encoding="utf-8")
    html = apps.get_app_panel_html("Entry", root=str(proj))
    assert html is not None
    assert entry_html in html


# ── File I/O ───────────────────────────────────────────────────────────────


def test_write_and_read_file():
    apps.register_app(name="IO", description="", runtime="web", script="")
    apps.write_app_file("IO", "note.md", content="hello")
    r = apps.read_app_file("IO", "note.md")
    assert r["content"] == "hello"
    assert r["exists"] is True


def test_read_nonexistent_file():
    apps.register_app(name="IO", description="", runtime="web", script="")
    r = apps.read_app_file("IO", "missing.md")
    assert "error" in r
    assert r["exists"] is False


def test_write_binary_file():
    apps.register_app(name="IO", description="", runtime="web", script="")
    data = b"\x89PNG\r\n\x1a\n"
    b64 = base64.b64encode(data).decode()
    apps.write_app_file("IO", "img.png", content_b64=b64)
    result = apps.read_app_file_binary("IO", "img.png")
    assert result == data


def test_delete_file():
    apps.register_app(name="IO", description="", runtime="web", script="")
    apps.write_app_file("IO", "temp.txt", content="bye")
    d = apps.delete_app_file("IO", "temp.txt")
    assert d == {"ok": True}
    r = apps.read_app_file("IO", "temp.txt")
    assert "error" in r


def test_list_files():
    apps.register_app(name="IO", description="", runtime="web", script="")
    apps.write_app_file("IO", "a.txt", content="a")
    apps.write_app_file("IO", "sub/b.txt", content="b")
    lst = apps.list_app_files("IO")
    paths = [f["path"] for f in lst["files"]]
    assert "a.txt" in paths
    assert "sub/b.txt" in paths


def test_list_files_with_dir():
    apps.register_app(name="IO", description="", runtime="web", script="")
    apps.write_app_file("IO", "a.txt", content="a")
    apps.write_app_file("IO", "sub/b.txt", content="b")
    lst = apps.list_app_files("IO", dir_path="sub")
    paths = [f["path"] for f in lst["files"]]
    assert "sub/b.txt" in paths
    assert "a.txt" not in paths


# ── Path traversal protection ─────────────────────────────────────────────


def test_path_traversal_read_blocked():
    apps.register_app(name="IO", description="", runtime="web", script="")
    r = apps.read_app_file("IO", "../../../etc/passwd")
    assert "error" in r
    assert "traversal" in r["error"]


def test_path_traversal_write_blocked():
    apps.register_app(name="IO", description="", runtime="web", script="")
    r = apps.write_app_file("IO", "../../../tmp/evil", content="hack")
    assert "error" in r
    assert "traversal" in r["error"]


def test_path_traversal_delete_blocked():
    apps.register_app(name="IO", description="", runtime="web", script="")
    r = apps.delete_app_file("IO", "../../../tmp/evil")
    assert "error" in r
    assert "traversal" in r["error"]


def test_path_traversal_list_blocked():
    apps.register_app(name="IO", description="", runtime="web", script="")
    r = apps.list_app_files("IO", dir_path="../../../etc")
    assert "error" in r
    assert "traversal" in r["error"]

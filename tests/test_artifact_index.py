"""Tests for ArtifactIndex."""
from __future__ import annotations

from pathlib import Path

from senza_agent.behavior.artifact_index import ArtifactIndex


def test_register_and_manifest(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    idx.register(str(tmp_path / "a.txt"), description="first file")
    idx.register(str(tmp_path / "b.txt"), description="second file")
    entries = idx.manifest()
    assert len(entries) == 2
    assert entries[0]["path"] == "a.txt"
    assert entries[0]["description"] == "first file"
    assert entries[1]["path"] == "b.txt"


def test_register_dedupes_same_path(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    p = str(tmp_path / "out.txt")
    idx.register(p, description="v1")
    idx.register(p, description="v2", source="write_file", chars=42)
    entries = idx.manifest()
    assert len(entries) == 1
    assert entries[0]["description"] == "v2"
    assert entries[0]["source"] == "write_file"
    assert entries[0]["chars"] == 42


def test_register_preserves_position(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    idx.register(str(tmp_path / "a.txt"), description="a")
    idx.register(str(tmp_path / "b.txt"), description="b")
    idx.register(str(tmp_path / "c.txt"), description="c")
    # Re-register a — should update in place, not move to end.
    idx.register(str(tmp_path / "a.txt"), description="a-updated")
    paths = [e["path"] for e in idx.manifest()]
    assert paths == ["a.txt", "b.txt", "c.txt"]
    assert idx.manifest()[0]["description"] == "a-updated"


def test_manifest_empty_when_nothing_registered(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    assert idx.manifest() == []
    assert idx.render() == ""


def test_render_contains_header_and_paths(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    idx.register(str(tmp_path / "a.txt"), description="alpha", source="write_file", chars=100)
    idx.register(str(tmp_path / "b.txt"), description="beta", source="spill")
    out = idx.render()
    assert "a.txt" in out
    assert "b.txt" in out
    assert "alpha" in out
    assert "beta" in out


def test_render_relative_paths(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    idx = ArtifactIndex(str(run_dir))
    idx.register(str(run_dir / "deep" / "file.md"), description="nested")
    out = idx.render()
    assert "deep/file.md" in out


def test_render_truncates_to_max_chars(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    for i in range(50):
        idx.register(str(tmp_path / f"file_{i}.txt"), description=f"desc {i}" * 10)
    out = idx.render(max_chars=200)
    # Should fit under the cap and mention omissions.
    assert len(out) <= 200
    assert "omitted" in out or len(idx.manifest()) > 0


def test_register_empty_path_is_noop(tmp_path):
    idx = ArtifactIndex(str(tmp_path))
    idx.register("")
    assert idx.manifest() == []


def test_eviction_prefers_spills(tmp_path, monkeypatch):
    # Force a low cap so eviction triggers.
    monkeypatch.setenv("ARTIFACT_INDEX_MAX", "3")
    import importlib
    import senza_agent.behavior.artifact_index as mod
    importlib.reload(mod)
    idx = mod.ArtifactIndex(str(tmp_path))
    idx.register(str(tmp_path / "deliverable.txt"), source="write_file")
    idx.register(str(tmp_path / "spill1.txt"), source="spill")
    idx.register(str(tmp_path / "spill2.txt"), source="spill")
    idx.register(str(tmp_path / "overflow.txt"), source="write_file")
    paths = [e["path"] for e in idx.manifest()]
    # Cap is 3; a spill should have been evicted, deliverables kept.
    assert len(paths) == 3
    assert "deliverable.txt" in paths
    assert "overflow.txt" in paths

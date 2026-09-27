"""Cache busting: static URLs carry a version that changes when files change."""

from app.static_version import STATIC_VERSION


def test_static_version_is_stable_and_meaningful():
    # Non-empty and stable within one process lifetime.
    assert STATIC_VERSION
    from app import static_version
    assert static_version.STATIC_VERSION == STATIC_VERSION


def test_version_changes_when_a_file_changes(tmp_path, monkeypatch):
    import importlib

    from app import static_version as sv

    fake_static = tmp_path / "static"
    (fake_static / "js").mkdir(parents=True)
    (fake_static / "js" / "app.js").write_text("a = 1;")
    monkeypatch.setattr(sv, "_STATIC_DIR", fake_static)

    first = sv._static_version()

    # Same content, newer mtime -> version changes (the git-pull case).
    import os
    st = fake_static / "js" / "app.js"
    os.utime(st, (st.stat().st_atime + 10, st.stat().st_mtime + 10))
    second = sv._static_version()

    assert first != second

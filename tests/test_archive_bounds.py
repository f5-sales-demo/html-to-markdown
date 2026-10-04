"""Producer archive limits and exact-boundary verification."""

import io
import tarfile

import pytest

from html_to_markdown import package


def test_paired_limits():
    assert package.MAX_ARCHIVE_BYTES == 1024 * 1024 * 1024
    assert package.MAX_EXPANDED_BYTES == 1024 * 1024 * 1024
    assert package.MAX_MEMBERS == 20_000


@pytest.mark.parametrize("field,bound", [("MAX_EXPANDED_BYTES", 7), ("MAX_MEMBERS", 2)])
def test_read_archive_boundary(tmp_path, monkeypatch, field, bound):
    path = tmp_path / "bounds.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in [("manifest.json", b"abc"), ("quality-report.json", b"defg")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(package, field, bound)
    assert package._read_archive_files(path) == {
        "manifest.json": b"abc",
        "quality-report.json": b"defg",
    }
    monkeypatch.setattr(package, field, bound - 1)
    with pytest.raises(ValueError):
        package._read_archive_files(path)


def test_compressed_boundary_accepts_then_one_over_rejects(tmp_path, monkeypatch):
    path = tmp_path / "bounds.tar.gz"
    path.write_bytes(b"1234567")
    monkeypatch.setattr(package, "MAX_ARCHIVE_BYTES", 7)
    monkeypatch.setattr(package, "_read_archive_files", lambda archive: {})
    with pytest.raises(ValueError, match="missing required metadata"):
        package.verify_archive(path)
    monkeypatch.setattr(package, "MAX_ARCHIVE_BYTES", 6)
    with pytest.raises(ValueError, match="archive exceeds"):
        package.verify_archive(path)

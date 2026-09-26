import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from html_to_markdown import package as package_module
from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.package import (
    build_manifest,
    sha256_file,
    verify_archive,
    verify_publication_receipt,
    write_publication_receipt,
    write_release,
)
from html_to_markdown.render import content_hash, serialize_document, split_document
from html_to_markdown.state import StateStore


def prepared_snapshot(tmp_path: Path, *, with_asset: bool = True) -> tuple[Path, StateStore]:
    output = tmp_path / "out"
    target = output / "content/docs-cloud-f5-com/a/index.md"
    target.parent.mkdir(parents=True)
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="A",
        slug="a",
        url="https://docs.cloud.f5.com/docs-v2/a",
        category="guide",
        tags=[],
    )
    target.write_text(serialize_document(metadata, "# A\n\nUseful body."), encoding="utf-8")
    if with_asset:
        asset = target.parent / "assets" / f"{'a' * 64}.png"
        asset.parent.mkdir()
        asset.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    store = StateStore(output / "state.sqlite")
    store.discover([DiscoveredPage(source_id=metadata.source_id, url=metadata.url)])
    store.mark(
        metadata.url,
        PageStatus.SUCCESS,
        output_path="content/docs-cloud-f5-com/a/index.md",
        digest=metadata.content_hash,
    )
    return output, store


def test_manifest_v2_hashes_body_and_complete_file_and_sizes_assets(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    manifest = build_manifest(output, store, "start", "end")

    assert manifest["schema_version"] == 2
    document = manifest["documents"][0]
    document_path = output / document["path"]
    _, body = split_document(document_path.read_text(encoding="utf-8"))
    assert document["body_sha256"] == content_hash(body)
    assert document["file_sha256"] == sha256_file(document_path)
    assert document["size_bytes"] == document_path.stat().st_size
    assert "sha256" not in document

    asset = manifest["assets"][0]
    asset_path = output / asset["path"]
    assert asset["sha256"] == sha256_file(asset_path)
    assert asset["media_type"] == "image/png"
    assert asset["size_bytes"] == asset_path.stat().st_size
    store.close()


def test_sha256sums_covers_every_other_archive_member_once(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    archive = write_release(output, build_manifest(output, store, "start", "end"))

    with tarfile.open(archive, "r:gz") as packaged:
        regular = [member.name for member in packaged.getmembers() if member.isfile()]
        sums = packaged.extractfile("SHA256SUMS")
        assert sums is not None
        rows = sums.read().decode().splitlines()
    listed = [row.split("  ", 1)[1] for row in rows]
    assert len(listed) == len(set(listed))
    assert set(listed) == set(regular) - {"SHA256SUMS"}
    assert {"manifest.json", "quality-report.json", "quality-report.md"} <= set(listed)
    store.close()


def test_publication_receipt_binds_closed_asset_set(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    write_release(output, build_manifest(output, store, "start", "end"))
    receipt_path = write_publication_receipt(
        output,
        tag="content-20260926T200000Z",
        source_commit="0123456789abcdef0123456789abcdef01234567",
        created_at="2026-09-26T19:59:00Z",
        published_at="2026-09-26T20:00:00Z",
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert receipt["schema_version"] == 1
    assert receipt["release_tag"] == "content-20260926T200000Z"
    assert receipt["source_commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert [asset["name"] for asset in receipt["assets"]] == [
        "html-to-markdown-content.tar.gz",
        "html-to-markdown-content.tar.gz.sha256",
        "manifest.json",
        "quality-report.json",
        "quality-report.md",
    ]
    verify_publication_receipt(
        output,
        receipt,
        expected_tag="content-20260926T200000Z",
        actual_asset_names={asset["name"] for asset in receipt["assets"]} | {"publication.json"},
    )

    augmented = {asset["name"] for asset in receipt["assets"]} | {
        "publication.json",
        "unexpected.bin",
    }
    with pytest.raises(ValueError, match="asset set"):
        verify_publication_receipt(
            output,
            receipt,
            expected_tag="content-20260926T200000Z",
            actual_asset_names=augmented,
        )
    store.close()


def test_receipt_rejects_tag_size_and_digest_mismatches(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    write_release(output, build_manifest(output, store, "start", "end"))
    receipt = json.loads(
        write_publication_receipt(
            output,
            tag="content-20260926T200000Z",
            source_commit="0123456789abcdef0123456789abcdef01234567",
            created_at="2026-09-26T19:59:00Z",
            published_at="2026-09-26T20:00:00Z",
        ).read_text(encoding="utf-8")
    )
    names = {asset["name"] for asset in receipt["assets"]} | {"publication.json"}
    receipt_digest = sha256_file(output / "publication.json")

    with pytest.raises(ValueError, match="tag"):
        verify_publication_receipt(output, receipt, expected_tag="wrong", actual_asset_names=names)
    with pytest.raises(ValueError, match="source commit mismatch"):
        verify_publication_receipt(
            output,
            receipt,
            expected_tag="content-20260926T200000Z",
            expected_source_commit="f" * 40,
            actual_asset_names=names,
        )
    with pytest.raises(ValueError, match="receipt digest mismatch"):
        verify_publication_receipt(
            output,
            receipt,
            expected_tag="content-20260926T200000Z",
            expected_receipt_sha256="0" * 64,
            actual_asset_names=names,
        )
    verify_publication_receipt(
        output,
        receipt,
        expected_tag="content-20260926T200000Z",
        expected_source_commit="0123456789abcdef0123456789abcdef01234567",
        expected_receipt_sha256=receipt_digest,
        actual_asset_names=names,
    )
    receipt["assets"][0]["size_bytes"] += 1
    with pytest.raises(ValueError, match="size"):
        verify_publication_receipt(
            output,
            receipt,
            expected_tag="content-20260926T200000Z",
            actual_asset_names=names,
        )
    receipt["assets"][0]["size_bytes"] -= 1
    receipt["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        verify_publication_receipt(
            output,
            receipt,
            expected_tag="content-20260926T200000Z",
            actual_asset_names=names,
        )
    store.close()


def test_archive_limits_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output, store = prepared_snapshot(tmp_path, with_asset=False)
    monkeypatch.setattr(package_module, "MAX_MARKDOWN_BYTES", 1)
    with pytest.raises(ValueError, match="Markdown member exceeds"):
        write_release(output, build_manifest(output, store, "start", "end"))
    store.close()


def test_archive_verification_rejects_links_traversal_and_unknown_members(tmp_path: Path) -> None:
    for name, member in (
        ("link", tarfile.TarInfo("content/docs-cloud-f5-com/a/index.md")),
        ("traversal", tarfile.TarInfo("../outside")),
        ("unknown", tarfile.TarInfo("unknown.bin")),
    ):
        archive = tmp_path / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as packaged:
            if name == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "target"
            else:
                payload = b"x"
                member.size = len(payload)
                packaged.addfile(member, fileobj=__import__("io").BytesIO(payload))
                continue
            packaged.addfile(member)
        with pytest.raises(ValueError):
            verify_archive(archive)


def test_outer_checksum_is_canonical(tmp_path: Path) -> None:
    output, store = prepared_snapshot(tmp_path)
    archive = write_release(output, build_manifest(output, store, "start", "end"))
    checksum = archive.with_name(f"{archive.name}.sha256").read_text(encoding="utf-8")
    assert checksum == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    store.close()

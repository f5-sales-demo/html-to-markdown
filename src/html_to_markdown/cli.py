"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from .logging import configure_logging
from .package import (
    sha256_file,
    verify_archive,
    verify_publication_receipt,
    write_publication_receipt,
)
from .pipeline import Pipeline, run_pipeline
from .quality import write_quality_reports
from .state import StateStore
from .validation import validate_snapshot

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
Source = Annotated[str, typer.Option(help="all, docs-cloud-f5-com, or my-f5-com")]
Output = Annotated[Path, typer.Option(file_okay=False, help="Working/output directory")]


def _check_source(source: str) -> str:
    if source not in {"all", "docs-cloud-f5-com", "my-f5-com"}:
        raise typer.BadParameter("must be all, docs-cloud-f5-com, or my-f5-com")
    return source


@app.command()
# Typer exposes each option as a command function parameter.
# pylint: disable-next=too-many-arguments
def run(
    source: Source = "all",
    output: Output = Path("build"),
    url: Annotated[str | None, typer.Option(help="One allowed URL")] = None,
    concurrency: Annotated[int, typer.Option(min=1, max=20)] = 8,
    retries: Annotated[int, typer.Option(min=1, max=10)] = 3,
    timeout: Annotated[float, typer.Option(min=1)] = 30,
    headed: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option(help="Refresh successful pages")] = False,
    previous_manifest: Annotated[Path | None, typer.Option()] = None,
    quality_reference: Annotated[Path | None, typer.Option(file_okay=False)] = None,
    benchmark: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    inventory_only: Annotated[
        bool, typer.Option(help="Scrape only URLs pinned by --benchmark")
    ] = False,
    verbose: Annotated[bool, typer.Option()] = False,
) -> None:
    """Discover, scrape, validate, and package a snapshot."""
    configure_logging(verbose)
    archive = asyncio.run(
        run_pipeline(
            source=_check_source(source),
            output=output,
            url=url,
            concurrency=concurrency,
            retries=retries,
            timeout=timeout,
            headed=headed,
            force=force,
            previous_manifest=previous_manifest,
            quality_reference=quality_reference,
            benchmark=benchmark,
            inventory_only=inventory_only,
        )
    )
    typer.echo(archive)


@app.command()
def discover(
    source: Source = "all",
    output: Output = Path("build"),
    url: Annotated[str | None, typer.Option()] = None,
    timeout: Annotated[float, typer.Option(min=1)] = 30,
    headed: Annotated[bool, typer.Option()] = False,
) -> None:
    """Discover source URLs into resumable state."""
    configure_logging()

    async def execute() -> int:
        async with Pipeline(output, timeout=timeout, headed=headed) as pipeline:
            return await pipeline.discover(_check_source(source), url)

    typer.echo(asyncio.run(execute()))


@app.command()
def scrape(
    source: Source = "all",
    output: Output = Path("build"),
    concurrency: Annotated[int, typer.Option(min=1, max=20)] = 8,
    retries: Annotated[int, typer.Option(min=1, max=10)] = 3,
    timeout: Annotated[float, typer.Option(min=1)] = 30,
    headed: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option()] = False,
) -> None:
    """Scrape pending URLs already present in state."""
    configure_logging()

    async def execute() -> int:
        async with Pipeline(
            output, concurrency=concurrency, retries=retries, timeout=timeout, headed=headed
        ) as pipeline:
            return len(await pipeline.scrape(_check_source(source), force=force))

    typer.echo(asyncio.run(execute()))


@app.command()
def status(output: Output = Path("build")) -> None:
    """Show state counts as JSON."""
    store = StateStore(output / "state.sqlite")
    try:
        typer.echo(json.dumps(store.counts(), sort_keys=True))
    finally:
        store.close()


@app.command("validate")
def validate_command(
    output: Output = Path("build"),
) -> None:
    """Validate hard document and artifact integrity."""
    store = StateStore(output / "state.sqlite")
    try:
        validate_snapshot(output, store)
    finally:
        store.close()
    typer.echo("valid")


@app.command()
def quality(
    candidate: Annotated[Path, typer.Option(file_okay=False)] = Path("build"),
    reference: Annotated[Path | None, typer.Option(file_okay=False)] = None,
    benchmark: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Write deterministic advisory JSON and Markdown quality reports."""
    json_path, markdown_path = write_quality_reports(candidate, reference, benchmark)
    typer.echo(f"{json_path}\n{markdown_path}")


@app.command("publication")
def publication_command(
    output: Output = Path("build"),
    release_tag: Annotated[str, typer.Option()] = "",
    source_commit: Annotated[str, typer.Option()] = "",
    created_at: Annotated[str, typer.Option()] = "",
    published_at: Annotated[str, typer.Option()] = "",
) -> None:
    """Write the immutable release publication receipt."""
    typer.echo(
        write_publication_receipt(
            output,
            tag=release_tag,
            source_commit=source_commit,
            created_at=created_at,
            published_at=published_at,
        )
    )


@app.command("verify-publication")
def verify_publication_command(
    output: Output = Path("build"),
    release_tag: Annotated[str, typer.Option()] = "",
) -> None:
    """Verify a downloaded release and its closed publication asset set."""
    receipt = json.loads((output / "publication.json").read_text(encoding="utf-8"))
    verify_publication_receipt(
        output,
        receipt,
        expected_tag=release_tag,
        actual_asset_names={path.name for path in output.iterdir() if path.is_file()},
    )
    archive = output / "html-to-markdown-content.tar.gz"
    checksum = output / "html-to-markdown-content.tar.gz.sha256"
    expected_checksum = f"{sha256_file(archive)}  {archive.name}\n"
    if checksum.read_text(encoding="utf-8") != expected_checksum:
        raise ValueError("outer archive checksum mismatch")
    verify_archive(archive)
    typer.echo("valid")


if __name__ == "__main__":
    app()

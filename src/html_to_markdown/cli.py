"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from .logging import configure_logging
from .pipeline import Pipeline, run_pipeline
from .validation import validate_snapshot

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
Source = Annotated[str, typer.Option(help="all, docs-cloud-f5-com, or my-f5-com")]
Output = Annotated[Path, typer.Option(file_okay=False, help="Working/output directory")]


def _check_source(source: str) -> str:
    if source not in {"all", "docs-cloud-f5-com", "my-f5-com"}:
        raise typer.BadParameter("must be all, docs-cloud-f5-com, or my-f5-com")
    return source


@app.command()
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
    acknowledge_page_drop: Annotated[bool, typer.Option()] = False,
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
            acknowledge_page_drop=acknowledge_page_drop,
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
    from .state import StateStore

    store = StateStore(output / "state.sqlite")
    try:
        typer.echo(json.dumps(store.counts(), sort_keys=True))
    finally:
        store.close()


@app.command("validate")
def validate_command(
    output: Output = Path("build"),
    previous_manifest: Annotated[Path | None, typer.Option()] = None,
    acknowledge_page_drop: Annotated[bool, typer.Option()] = False,
) -> None:
    """Validate documents, assets, state, and publication gates."""
    from .state import StateStore

    store = StateStore(output / "state.sqlite")
    try:
        validate_snapshot(
            output,
            store,
            previous_manifest=previous_manifest,
            acknowledge_page_drop=acknowledge_page_drop,
        )
    finally:
        store.close()
    typer.echo("valid")


if __name__ == "__main__":
    app()

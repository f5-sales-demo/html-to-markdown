"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from .adapters import ADAPTERS
from .adapters.community import validate_community_publication
from .content_policy import load_content_policy, migrate_content
from .enrichment.analysis import write_analysis
from .enrichment.engine import prepare, replay, verify_enrichment
from .enrichment.evaluation import evaluate
from .enrichment.generate import generate
from .logging import configure_logging
from .metadata import enrich_snapshot
from .models import DiscoveredPage, PageStatus
from .package import (
    build_manifest,
    sha256_file,
    verify_archive,
    verify_publication_receipt,
    write_publication_receipt,
    write_release,
)
from .pipeline import Pipeline, run_pipeline
from .quality import write_quality_reports
from .render import split_document
from .state import StateStore
from .validation import validate_snapshot

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
Source = Annotated[
    str, typer.Option(help="all, docs-cloud-f5-com, my-f5-com, www-f5-com, or community-f5-com")
]
Output = Annotated[Path, typer.Option(file_okay=False, help="Working/output directory")]


def _check_source(source: str) -> str:
    if source != "all" and source not in ADAPTERS:
        raise typer.BadParameter(
            "must be all, docs-cloud-f5-com, my-f5-com, www-f5-com, or community-f5-com"
        )
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
    retain_previous: Annotated[
        bool, typer.Option(help="Retain verified prior documents when currently unavailable")
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
            retain_previous=retain_previous,
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
        validate_community_publication(output, store)
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


@app.command("examine-content")
def examine_content_command(
    output: Output = Path("build"),
    policy: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Print deterministic mapping and impact evidence without modifying content."""
    evidence = migrate_content(output, policy=load_content_policy(policy), apply=False)
    typer.echo(json.dumps(evidence, indent=2, sort_keys=True))


@app.command("curate-content")
@app.command("migrate-content")
def migrate_content_command(
    output: Output = Path("build"),
    policy: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Apply pinned policy offline, then rebuild metadata, reports and release files."""
    # Preserve manifest provenance when importing an offline corpus into state.
    # pylint: disable=too-many-locals
    active = load_content_policy(policy)
    store = StateStore(output / "state.sqlite")
    try:
        prior_path = output / "manifest.json"
        prior_manifest = json.loads(prior_path.read_text()) if prior_path.is_file() else {}
        prior_documents = {item["url"]: item for item in prior_manifest.get("documents", [])}
        existing = {row["canonical_url"] for row in store.rows()}
        for path in sorted(output.glob("content/*/**/index.md")):
            metadata, _ = split_document(path.read_text(encoding="utf-8"))
            url = str(metadata["url"])
            if url not in existing:
                store.discover([DiscoveredPage(source_id=str(metadata["sourceId"]), url=url)])
                prior_document = prior_documents.get(url, {})
                provenance = prior_document.get("provenance", {})
                freshness = provenance.get("freshness", "fresh")
                status = {
                    "fresh": PageStatus.SUCCESS,
                    "carried_forward": PageStatus.CARRIED_FORWARD,
                    "removal_candidate": PageStatus.REMOVAL_CANDIDATE,
                }.get(freshness, PageStatus.SUCCESS)
                failure = provenance.get("current_failure") or {}
                store.reconcile(
                    url,
                    status,
                    output_path=path.relative_to(output).as_posix(),
                    digest=None,
                    freshness=freshness,
                    failure_classification=failure.get("classification"),
                    last_success_at=provenance.get("last_success_at"),
                    consecutive_failures=provenance.get("consecutive_failure_count", 0),
                    terminal_confirmations=provenance.get("terminal_confirmation_count", 0),
                )
                with store.connection:
                    store.connection.execute(
                        "UPDATE pages SET error_class=?, error_message=? WHERE canonical_url=?",
                        (failure.get("error_class"), failure.get("error_message"), url),
                    )
                related = metadata.get("related_documents", [])
                if isinstance(related, list):
                    store.replace_candidate_links(
                        url,
                        [str(item["canonical_url"]) for item in related if isinstance(item, dict)],
                    )
        evidence = migrate_content(output, store, policy=active)
        enrich_snapshot(output, store)
        write_quality_reports(output)
        validate_snapshot(output, store)
        prior = output / "manifest.json"
        dates = json.loads(prior.read_text()) if prior.is_file() else {}
        archive = write_release(
            output,
            build_manifest(output, store, dates.get("started_at", ""), dates.get("ended_at", "")),
        )
        typer.echo(
            json.dumps(
                {"archive": str(archive), "migration_counts": evidence["counts"]},
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        store.close()


@app.command("analyze-corpus")
def analyze_corpus_command(
    output: Output = Path("build"),
    report: Annotated[Path, typer.Option(dir_okay=False)] = Path("corpus-analysis.json"),
) -> None:
    """Inventory every document and structural span for semantic review."""
    result = write_analysis(output, report)
    typer.echo(
        json.dumps({"documents": len(result["documents"]), "report": str(report)}, sort_keys=True)
    )


@app.command("enrich-content")
# Each public option is an independent CLI argument.
# pylint: disable-next=too-many-arguments
def enrich_content_command(
    output: Output = Path("build"),
    decisions: Annotated[Path, typer.Option(dir_okay=False)] = Path("enrichment-decisions.json"),
    mode: Annotated[
        str, typer.Option(help="prepare, media, editor, validator, or replay")
    ] = "replay",
    decisions_sha256: Annotated[str, typer.Option()] = "",
    baseline: Annotated[Path | None, typer.Option(file_okay=False)] = None,
    baseline_tag: Annotated[str, typer.Option()] = "",
    publication_sha256: Annotated[str, typer.Option()] = "",
    journal: Annotated[Path, typer.Option(file_okay=False)] = Path(".enrichment-batches"),
    wait: Annotated[bool, typer.Option()] = False,
) -> None:
    """Generate decisions in resumable batches or replay an exact artifact offline."""
    if mode == "prepare":
        if baseline is None:
            raise typer.BadParameter("prepare requires --baseline")
        artifact = prepare(
            output, baseline, decisions, tag=baseline_tag, receipt_sha256=publication_sha256
        )
        typer.echo(
            json.dumps(
                {"documents": len(artifact.documents), "decisions": str(decisions)}, sort_keys=True
            )
        )
    elif mode in {"media", "editor", "validator"}:
        typer.echo(
            json.dumps(
                generate(output, decisions, phase=mode, journal=journal, wait=wait), sort_keys=True
            )
        )
    elif mode == "replay":
        if not decisions_sha256:
            raise typer.BadParameter("replay requires --decisions-sha256")
        report = replay(output, decisions, decisions_sha256)
        store = StateStore(output / "state.sqlite")
        try:
            write_quality_reports(output)
            validate_snapshot(output, store)
            prior = json.loads((output / "manifest.json").read_text())
            write_release(
                output, build_manifest(output, store, prior["started_at"], prior["ended_at"])
            )
        finally:
            store.close()
        typer.echo(
            json.dumps(
                {"counts": report["counts"], "input_documents": report["input_documents"]},
                sort_keys=True,
            )
        )
    else:
        raise typer.BadParameter("invalid enrichment mode")


@app.command("verify-enrichment")
def verify_enrichment_command(output: Output = Path("build")) -> None:
    """Verify pinned decisions, protected facts, upstream chain and final bytes."""
    state = verify_enrichment(output)
    typer.echo(
        json.dumps({"valid": True, "artifact_sha256": state["artifact_sha256"]}, sort_keys=True)
    )


@app.command("evaluate-enrichment")
def evaluate_enrichment_command(
    baseline: Annotated[Path, typer.Option(file_okay=False)],
    output: Output,
    questions: Annotated[Path, typer.Option(dir_okay=False)],
    report: Annotated[Path, typer.Option(dir_okay=False)],
    journal: Annotated[Path, typer.Option(file_okay=False)] = Path(".enrichment-evaluation"),
) -> None:
    """Compare corpus retrieval correctness, evidence, relevance and context size."""
    result = evaluate(baseline, output, questions, journal, report)
    typer.echo(json.dumps({"passed": result["passed"], "failures": result["failures"]}))


if __name__ == "__main__":
    app()

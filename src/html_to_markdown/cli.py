"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from .adapters import ADAPTERS
from .adapters.community import validate_community_publication
from .combined_sources import combine_sources
from .content_inventory import collect_content, repair_captured_extraction
from .content_policy import load_content_policy, migrate_content
from .curation_renewal_inputs import prepare_renewals
from .curation_renewal_jobs import review_batch
from .curation_renewal_publication import stage_policy, validate_planner
from .docs_inventory import collect_docs
from .enrichment.analysis import write_analysis
from .enrichment.codex_client import CodexClient
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
from .refresh import combine_docs, verify_refresh
from .render import split_document
from .safe_reports import sanitize_inventory
from .source_inputs import seal_source, verify_source
from .source_review_jobs import run_reviews, verify_reviews
from .state import StateStore
from .validation import validate_snapshot
from .work_queue import export_queue, list_queues, receive_enrichment, receive_source

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)


@app.command("sanitize-inventory")
def sanitize_inventory_command(
    input_path: Annotated[Path, typer.Option(dir_okay=False)],
    output: Annotated[Path, typer.Option(dir_okay=False)],
) -> None:
    """Write an allowlisted CI receipt without captured text, media or diagnostics."""
    result = sanitize_inventory(input_path, output)
    typer.echo(json.dumps({"documents": len(result["documents"]), "counts": result["counts"]}))


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


@app.command("list-review-queues")
def list_review_queues(queue_root: Annotated[Path, typer.Option(file_okay=False)]) -> None:
    """List private captured queues for the MacBook worker."""
    typer.echo(json.dumps(list_queues(queue_root), sort_keys=True))


@app.command("scheduled-collection")
def scheduled_collection_command(
    queue_root: Annotated[Path, typer.Option(file_okay=False)],
    source: Annotated[str, typer.Option()] = "all",
    full: Annotated[bool, typer.Option()] = False,
    install: Annotated[bool, typer.Option()] = False,
) -> None:
    """Collect privately on Ubuntu independently of MacBook review availability."""
    from .collection_service import (  # pylint: disable=import-outside-toplevel
        install_collectors,
        scheduled_collection,
    )

    if install:
        typer.echo(json.dumps({"timers": install_collectors(queue_root)}))
    else:
        typer.echo(
            json.dumps(
                asyncio.run(scheduled_collection(queue_root, source=source, full=full)),
                sort_keys=True,
            )
        )


@app.command("export-review-queue")
def export_review_queue(
    queue_root: Annotated[Path, typer.Option(file_okay=False)],
    queue: Annotated[str, typer.Option()],
) -> None:
    """Stream one private verified capture snapshot to the MacBook."""
    export_queue(queue_root, queue, sys.stdout.buffer)


@app.command("review-worker")
# Each public option is an independent CLI argument.
# pylint: disable-next=too-many-arguments
def review_worker(
    host: Annotated[str, typer.Option()],
    remote_executable: Annotated[str, typer.Option()],
    remote_queue: Annotated[str, typer.Option()],
    output: Output,
    transport: Annotated[Path, typer.Option(dir_okay=False)],
    install: Annotated[bool, typer.Option()] = False,
    source_commit: Annotated[str | None, typer.Option()] = None,
    monitor: Annotated[bool, typer.Option()] = False,
) -> None:
    """Check Ubuntu queues and run up to four isolated MacBook inference jobs."""
    from .mac_worker import (  # pylint: disable=import-outside-toplevel
        install_launch_agent,
        monitored_worker,
        worker_once,
    )

    if install:
        typer.echo(
            install_launch_agent(
                host=host,
                remote_executable=remote_executable,
                remote_queue=remote_queue,
                output=output,
                transport=transport,
                source_commit=source_commit,
            )
        )
    else:
        execute = monitored_worker if monitor else worker_once
        result = execute(
            host=host,
            remote_executable=remote_executable,
            remote_queue=remote_queue,
            output=output,
            transport=transport,
            source_commit=source_commit,
        )
        typer.echo(json.dumps({"state": result["state"], "queues": len(result.get("queues", []))}))


@app.command("receive-review-input")
def receive_review_input(
    queue_root: Annotated[Path, typer.Option(file_okay=False)],
    queue: Annotated[str, typer.Option()],
    review_sha256: Annotated[str, typer.Option()],
) -> None:
    """Receive and independently replay a private MacBook admission bundle."""
    typer.echo(receive_source(queue_root, queue, review_sha256, sys.stdin.buffer))


@app.command("receive-enrichment-input")
def receive_enrichment_input(
    queue_root: Annotated[Path, typer.Option(file_okay=False)],
    queue: Annotated[str, typer.Option()],
    decisions_sha256: Annotated[str, typer.Option()],
) -> None:
    """Replay MacBook enrichment against the preserved private source chain."""
    typer.echo(receive_enrichment(queue_root, queue, decisions_sha256, sys.stdin.buffer))


@app.command("prepare-publication")
def prepare_publication_command(
    queue_root: Annotated[Path, typer.Option(file_okay=False)], output: Output
) -> None:
    """Assemble reviewed source inputs against the latest verified deployed baseline."""
    from .publication_queue import prepare_publication  # pylint: disable=import-outside-toplevel

    typer.echo(json.dumps(prepare_publication(queue_root, output), sort_keys=True))


@app.command("advance-publication")
def advance_publication_command(
    queue_root: Annotated[Path, typer.Option(file_okay=False)],
    source_commit: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Progress the private review-to-publication chain with visible pending gates."""
    from .publication_queue import advance_publication  # pylint: disable=import-outside-toplevel

    typer.echo(
        json.dumps(advance_publication(queue_root, source_commit=source_commit), sort_keys=True)
    )


@app.command("stage-publication")
def stage_publication_command(
    output: Output, source_commit: Annotated[str, typer.Option()]
) -> None:
    """Stage consumer assets for promotion under the existing snapshot lock."""
    from .publication_queue import stage_publication  # pylint: disable=import-outside-toplevel

    typer.echo(json.dumps(stage_publication(output, source_commit=source_commit), sort_keys=True))


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


@app.command("inventory-docs")
def inventory_docs(
    output: Output = Path("docs-inventory"),
    previous_manifest: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    concurrency: Annotated[int, typer.Option(min=1, max=20)] = 4,
    retries: Annotated[int, typer.Option(min=1, max=10)] = 3,
    timeout: Annotated[float, typer.Option(min=1)] = 30,
) -> None:
    """Discover and freshly capture official docs independently of Community reviews."""
    configure_logging()
    report = asyncio.run(
        collect_docs(
            output,
            previous_manifest=previous_manifest,
            concurrency=concurrency,
            retries=retries,
            timeout=timeout,
        )
    )
    typer.echo(json.dumps(report["counts"], sort_keys=True))


@app.command("inventory-content")
# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-arguments
def inventory_content(
    source: Source = "all",
    output: Output = Path("content-inventory"),
    previous_manifest: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    resume: Annotated[bool, typer.Option(help="Continue the exact interrupted capture")] = False,
    full: Annotated[bool, typer.Option(help="Record a full reconciliation pass")] = False,
    concurrency: Annotated[int, typer.Option(min=1, max=20)] = 4,
    retries: Annotated[int, typer.Option(min=1, max=10)] = 3,
    timeout: Annotated[float, typer.Option(min=1)] = 30,
) -> None:
    """Capture all discovered and published candidates into a private review queue."""
    report = asyncio.run(
        collect_content(
            output,
            source=_check_source(source),
            previous_manifest=previous_manifest,
            resume=resume,
            full=full,
            concurrency=concurrency,
            retries=retries,
            timeout=timeout,
        )
    )
    typer.echo(
        json.dumps(
            {"counts": report["counts"], "completeness": report["completeness"]}, sort_keys=True
        )
    )
    if not report["completeness"]["enumeration"] or not report["completeness"]["capture"]:
        raise typer.Exit(1)


@app.command("repair-captured-extraction")
def repair_captured_extraction_command(output: Output) -> None:
    """Apply corrected extraction to preserved captures without repeating collection."""
    typer.echo(json.dumps(repair_captured_extraction(output), sort_keys=True))


@app.command("review-curation")
# The CLI exposes separate paths for preserved inputs and public staging.
# pylint: disable-next=too-many-arguments
def review_curation(
    capture: Annotated[Path, typer.Option(file_okay=False)],
    source_review: Annotated[Path, typer.Option(file_okay=False)],
    output: Output = Path("curation-review"),
    phase: Annotated[str, typer.Option(help="prepare, all, replay, qualify or stage")] = "prepare",
    journal: Annotated[Path, typer.Option(file_okay=False)] = Path(".curation-review-journal"),
    baseline: Annotated[Path | None, typer.Option(file_okay=False)] = None,
    publication_sha256: Annotated[str | None, typer.Option()] = None,
    staging_dir: Annotated[Path | None, typer.Option(file_okay=False)] = None,
) -> None:
    """Prepare private catalog proposals; source admission remains mandatory."""
    if phase not in {"prepare", "all", "replay", "qualify", "stage"}:
        raise typer.BadParameter("phase must be prepare, all, replay, qualify or stage")
    if phase in {"qualify", "stage"} and (baseline is None or publication_sha256 is None):
        raise typer.BadParameter("planner phases require the verified baseline and receipt digest")
    if phase == "stage" and staging_dir is None:
        raise typer.BadParameter("stage requires --staging-dir")
    inventory = prepare_renewals(capture, source_review, output)
    if phase == "prepare":
        typer.echo(json.dumps({"documents": len(inventory["documents"]), "phase": phase}))
        return
    result = review_batch(
        output, inventory, phase if phase in {"all", "replay"} else "replay", journal
    )
    typer.echo(json.dumps({key: value for key, value in result.items() if key != "documents"}))
    if result["exceptions"]:
        raise typer.Exit(1)
    if phase in {"qualify", "stage"}:
        contexts = [
            json.loads((output / item["context_path"]).read_bytes())
            for item in inventory["documents"]
        ]
        if baseline is None or publication_sha256 is None:
            raise typer.BadParameter(
                "planner phases require the verified baseline and receipt digest"
            )
        if phase == "stage":
            if staging_dir is None:
                raise typer.BadParameter("stage requires --staging-dir")
            evidence = stage_policy(output, contexts, baseline, publication_sha256, staging_dir)
        else:
            client = CodexClient(journal, concurrency=4)
            try:
                evidence = validate_planner(output, contexts, baseline, publication_sha256, client)
            finally:
                client.close()
        typer.echo(
            json.dumps({"post_planner_validated": True, "policy_sha256": evidence["policy_sha256"]})
        )


@app.command("receive-curation-renewal")
def receive_curation_renewal(
    queue_root: Annotated[Path, typer.Option(file_okay=False)],
    package_sha256: Annotated[str, typer.Option()],
    repository: Annotated[Path, typer.Option(file_okay=False)] = Path(
        "/data/robin-GIT/html-to-markdown"
    ),
) -> None:
    """Replay private Mac evidence and prepare a governed catalog PR on Ubuntu."""
    from .curation_renewal_worker import receive_renewals  # pylint: disable=import-outside-toplevel

    typer.echo(
        json.dumps(receive_renewals(queue_root, package_sha256, sys.stdin.buffer, repository))
    )


@app.command("review-content")
def review_content(
    capture: Annotated[Path, typer.Option(file_okay=False)],
    output: Output = Path("source-review"),
    phase: Annotated[
        str, typer.Option(help="prepare, relevance, privacy, validation, replay, all or verify")
    ] = "all",
    journal: Annotated[Path, typer.Option(file_okay=False)] = Path(".source-review-journal"),
    limit: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Review complete captured sources through isolated Codex or offline replay."""
    if phase == "verify":
        result = verify_reviews(capture, output)
        typer.echo(json.dumps({"accepted_files": len(result["accepted_files"])}))
        return
    if phase == "all":
        run_reviews(capture, output, phase="prepare", journal=journal)
    result = run_reviews(capture, output, phase=phase, journal=journal, limit=limit)
    typer.echo(
        json.dumps(
            {
                "phase": phase,
                "documents": len(result["documents"]),
                "exceptions": len(result.get("exceptions", {})),
            }
        )
    )


@app.command("combine-content")
# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-arguments
def combine_content(
    baseline: Annotated[Path, typer.Option(file_okay=False)],
    publication_sha256: Annotated[str, typer.Option()],
    output: Output = Path("combined"),
    current: Annotated[Path | None, typer.Option(file_okay=False)] = None,
    source_input: Annotated[
        list[str] | None, typer.Option(help="Repeat SOURCE=sealed-directory")
    ] = None,
    review_sha256: Annotated[
        list[str] | None, typer.Option(help="Repeat SOURCE=admission-receipt-sha256")
    ] = None,
    enrichment_input: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    enrichment_sha256: Annotated[str, typer.Option()] = "",
) -> None:
    """Combine independently reviewed sources against an exact verified baseline."""
    if source_input:

        def pairs(values: list[str]) -> dict[str, str]:
            result = {}
            for value in values:
                if "=" not in value:
                    raise typer.BadParameter("source input must be SOURCE=VALUE")
                source, target = value.split("=", 1)
                if source not in ADAPTERS or source in result or not target:
                    raise typer.BadParameter("unknown, duplicate or empty source input")
                result[source] = target
            return result

        directories, pins = pairs(source_input), pairs(review_sha256 or [])
        if directories.keys() != pins.keys() or current is not None:
            raise typer.BadParameter("each selected source requires its exact review pin")
        state = combine_sources(
            baseline,
            {source: (Path(path), pins[source]) for source, path in directories.items()},
            output,
            publication_sha256,
        )
    else:
        if current is None:
            raise typer.BadParameter(
                "supply --current or repeat --source-input and --review-sha256"
            )
        state = combine_docs(baseline, current, output, publication_sha256)
    if enrichment_input is not None:
        if not enrichment_sha256:
            raise typer.BadParameter("enrichment input requires --enrichment-sha256")
        replay(output, enrichment_input, enrichment_sha256)
        store = StateStore(output / "state.sqlite")
        try:
            write_quality_reports(output)
            validate_snapshot(output, store)
            prior = json.loads((output / "manifest.json").read_bytes())
            write_release(
                output, build_manifest(output, store, prior["started_at"], prior["ended_at"])
            )
        finally:
            store.close()
    typer.echo(json.dumps({"documents": len(state["documents"]), "aliases": len(state["aliases"])}))


@app.command("seal-source-input")
def seal_source_input(
    capture: Annotated[Path, typer.Option(file_okay=False)],
    review: Annotated[Path, typer.Option(file_okay=False)],
    output: Output,
) -> None:
    """Seal a private replayable source admission bundle for governed publication."""
    seal_source(capture, review, output)
    typer.echo(sha256_file(output / "source-review-receipt.json"))


@app.command("verify-source-input")
def verify_source_input(output: Output, review_sha256: Annotated[str, typer.Option()]) -> None:
    """Verify captured bytes, privacy transformations and independent admission."""
    verify_source(output, review_sha256)
    typer.echo("valid")


@app.command("verify-refresh")
def verify_refresh_command(output: Output = Path("combined")) -> None:
    """Verify the source refresh inputs and preserved published content."""
    verify_refresh(output)
    typer.echo("valid")


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
    publication_sha256: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Verify a downloaded release and its closed publication asset set."""
    receipt = json.loads((output / "publication.json").read_text(encoding="utf-8"))
    verify_publication_receipt(
        output,
        receipt,
        expected_tag=release_tag,
        expected_receipt_sha256=publication_sha256,
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
    prompt_version: Annotated[
        str, typer.Option(help="article-enrichment/1 or article-enrichment/2")
    ] = "article-enrichment/2",
) -> None:
    """Generate decisions in resumable batches or replay an exact artifact offline."""
    if mode == "prepare":
        if prompt_version not in {"article-enrichment/1", "article-enrichment/2"}:
            raise typer.BadParameter("unsupported enrichment prompt version")
        if baseline is None:
            raise typer.BadParameter("prepare requires --baseline")
        artifact = prepare(
            output,
            baseline,
            decisions,
            tag=baseline_tag,
            receipt_sha256=publication_sha256,
            version=2 if prompt_version == "article-enrichment/2" else 1,
        )
        typer.echo(
            json.dumps(
                {"documents": len(artifact.documents), "decisions": str(decisions)}, sort_keys=True
            )
        )
    elif mode in {"media", "editor", "validator", "all"}:
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


@app.command("export-policy-runtime")
def export_policy_runtime(repository: Path, state_root: Path) -> None:
    """Export only current protected-main qualified public policy inputs on Ubuntu."""
    from .policy_transport import export_policy  # pylint: disable=import-outside-toplevel

    typer.echo(json.dumps(export_policy(repository, state_root)))


@app.command("activate-policy-runtime")
def activate_policy_runtime(repository: Path, state_root: Path, queue_root: Path) -> None:
    """Activate a verified candidate under the existing Ubuntu publication lock."""
    import fcntl  # pylint: disable=import-outside-toplevel

    from .policy_transport import remote_activate  # pylint: disable=import-outside-toplevel

    candidate = json.load(sys.stdin)
    directory = queue_root / ".publication"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "coordinator.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = remote_activate(repository, state_root, candidate)
    typer.echo(json.dumps(result))


@app.command("confirm-policy-runtime")
def confirm_policy_runtime(state_root: Path, queue_root: Path) -> None:
    """Confirm matching active policy pins before Ubuntu can publish again."""
    import fcntl  # pylint: disable=import-outside-toplevel

    from .policy_activation import confirm_cohort  # pylint: disable=import-outside-toplevel

    directory = queue_root / ".publication"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "coordinator.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = confirm_cohort(state_root, json.load(sys.stdin))
    typer.echo(json.dumps(result))

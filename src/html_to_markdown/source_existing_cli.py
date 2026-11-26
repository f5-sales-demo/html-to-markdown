"""Manual original source recovery commands; no recurring task installation."""

import json
from pathlib import Path
from typing import Annotated

import typer

from .enrichment.clients import client_for_host
from .enrichment.codex_client import CodexClient
from .source_existing_delivery import admit_existing_source, prepare_existing_source
from .source_existing_validation import review_source_selection

Output = Annotated[Path, typer.Option(file_okay=False)]


def register_existing_source_commands(app: typer.Typer) -> None:
    @app.command("prepare-existing-source")
    def prepare_existing_source_command(
        capture: Annotated[Path, typer.Option(exists=True, file_okay=False)],
        record: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
        history_root: Annotated[list[Path], typer.Option(exists=True, file_okay=False)],
        output: Output,
    ) -> None:
        """Seal complete original source candidates without another privacy proposal."""
        path = prepare_existing_source(
            capture.absolute(),
            record.absolute(),
            [p.absolute() for p in history_root],
            output.absolute(),
        )
        typer.echo(
            json.dumps(
                {"input_journal": str(path), "new_editor_proposals": 0, "admission_approved": False}
            )
        )

    @app.command("review-existing-source")
    def review_existing_source_command(
        input_journal: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
        output: Output,
        replay: Annotated[bool, typer.Option()] = False,
        journal: Annotated[Path, typer.Option(file_okay=False)] = Path(".source-review-journal"),
    ) -> None:
        """Independently validate every strongest existing source proposal; never edit it."""
        client = (
            None if replay else client_for_host(journal, concurrency=4, local_client=CodexClient)
        )
        try:
            result = review_source_selection(input_journal.absolute(), output.absolute(), client)
        finally:
            if client is not None:
                client.close()
        typer.echo(
            json.dumps(
                {
                    "selected_record_sha256": result["selected_record_sha256"],
                    "strongest_records_checked": len(result["results"]),
                    "new_editor_proposals": 0,
                    "admission_approved": False,
                }
            )
        )

    @app.command("admit-existing-source")
    def admit_existing_source_command(
        capture: Annotated[Path, typer.Option(exists=True, file_okay=False)],
        review: Annotated[Path, typer.Option(exists=True, file_okay=False)],
        record: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
        input_journal: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
        validation_output: Annotated[Path, typer.Option(exists=True, file_okay=False)],
        worker_root: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    ) -> None:
        """Replay and admit one selected source under the existing Ubuntu worker lock."""
        import platform  # pylint: disable=import-outside-toplevel

        from .source_delivery import private_lock  # pylint: disable=import-outside-toplevel

        record = record.absolute()
        review = review.absolute()
        worker_root = worker_root.absolute()
        if (
            platform.system() != "Linux"
            or not record.is_relative_to(review)
            or not review.is_relative_to(worker_root)
        ):
            raise typer.BadParameter(
                "source admission requires the owning Ubuntu worker root and review record"
            )
        with private_lock(worker_root / "worker.lock"):
            result = admit_existing_source(
                capture.absolute(),
                review,
                record,
                input_journal.absolute(),
                validation_output.absolute(),
            )
        typer.echo(
            json.dumps(
                {
                    "disposition": result["disposition"],
                    "output_sha256": result["output_sha256"],
                    "new_editor_proposals": 0,
                    "snapshot_publication_required": True,
                }
            )
        )

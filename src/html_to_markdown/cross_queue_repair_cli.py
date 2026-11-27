"""Explicit offline evidence repair, separate from content review and publication."""

from pathlib import Path
from typing import Annotated

import typer

from .cross_queue_seed_repair import SeedRepair, repair_seed


def register_seed_repair(app: typer.Typer) -> None:
    @app.command("repair-review-seed")
    def repair_review_seed(
        worker_root: Annotated[Path, typer.Option(file_okay=False)],
        capture: Annotated[Path, typer.Option(file_okay=False)],
        review: Annotated[Path, typer.Option(file_okay=False)],
        inference_queue: Annotated[Path, typer.Option(file_okay=False)],
        prior_record: Annotated[Path, typer.Option(dir_okay=False)],
        current_sha256: Annotated[str, typer.Option()],
        prior_sha256: Annotated[str, typer.Option()],
    ) -> None:
        """Restore a preserved unused-cache transaction without inference or admission."""
        result = repair_seed(
            SeedRepair(
                worker_root,
                capture,
                review,
                inference_queue,
                prior_record,
                current_sha256,
                prior_sha256,
            )
        )
        typer.echo(
            "Original evidence restored; source review and independent replay remain required. "
            f"Record SHA-256: {result['repaired_sha256']}"
        )

"""Private commands for Ubuntu processing and bounded Mac inference."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from .inference_queue import claim, queue_status, receive
from .inference_services import (
    activate_inference_agent,
    install_processing_service,
    stage_inference_agent,
)
from .mac_inference import monitored_inference
from .mac_worker import worker_once


def register_inference_commands(app: typer.Typer) -> None:
    """Keep inference transport separate from consumer and publication commands."""

    @app.command("claim-inference-jobs")
    def claim_jobs(
        queue_root: Annotated[Path, typer.Option(file_okay=False)],
        owner: Annotated[str, typer.Option()],
        executor_sha256: Annotated[str, typer.Option()],
    ) -> None:
        typer.echo(json.dumps(claim(queue_root, owner, executor_sha256), sort_keys=True))

    @app.command("inference-queue-status")
    def inference_status(
        queue_root: Annotated[Path, typer.Option(file_okay=False)],
        owner: Annotated[str, typer.Option()],
    ) -> None:
        typer.echo(json.dumps(queue_status(queue_root, owner), sort_keys=True))

    @app.command("receive-inference-result")
    def receive_result(
        queue_root: Annotated[Path, typer.Option(file_okay=False)],
        job: Annotated[str, typer.Option()],
        owner: Annotated[str, typer.Option()],
        archive_sha256: Annotated[str, typer.Option()],
    ) -> None:
        typer.echo(
            json.dumps(
                receive(queue_root, job, owner, archive_sha256, sys.stdin.buffer), sort_keys=True
            )
        )

    @app.command("mac-inference-worker")
    def mac_worker(
        host: Annotated[str, typer.Option()],
        remote_executable: Annotated[str, typer.Option()],
        remote_queue: Annotated[str, typer.Option()],
        output: Annotated[Path, typer.Option(file_okay=False)],
        transport: Annotated[Path, typer.Option(dir_okay=False)],
    ) -> None:
        report = monitored_inference(
            host=host,
            remote_executable=remote_executable,
            remote_queue=remote_queue,
            output=output,
            transport=transport,
        )
        typer.echo(json.dumps(report, sort_keys=True))

    @app.command("processing-worker")
    def processing_worker(
        queue_root: Annotated[Path, typer.Option(file_okay=False)],
        inference_queue: Annotated[Path, typer.Option(file_okay=False)],
        output: Annotated[Path, typer.Option(file_okay=False)],
        executable: Annotated[str, typer.Option()],
        transport: Annotated[Path, typer.Option(dir_okay=False)],
        source_commit: Annotated[str | None, typer.Option()] = None,
        batch_size: Annotated[int, typer.Option(min=1)] = 8,
    ) -> None:
        if not inference_queue.is_absolute():
            raise typer.BadParameter("inference queue must be absolute")
        previous = os.environ.get("HTML_TO_MARKDOWN_INFERENCE_QUEUE")
        previous_history = os.environ.get("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT")
        os.environ["HTML_TO_MARKDOWN_INFERENCE_QUEUE"] = str(inference_queue)
        os.environ["HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT"] = str(output)
        try:
            report = worker_once(
                host="local",
                remote_executable=executable,
                remote_queue=str(queue_root),
                output=output,
                transport=transport,
                source_commit=source_commit,
                batch_size=batch_size,
            )
        finally:
            if previous is None:
                os.environ.pop("HTML_TO_MARKDOWN_INFERENCE_QUEUE", None)
            else:
                os.environ["HTML_TO_MARKDOWN_INFERENCE_QUEUE"] = previous
            if previous_history is None:
                os.environ.pop("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", None)
            else:
                os.environ["HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT"] = previous_history
        typer.echo(json.dumps(report, sort_keys=True))


def register_service_commands(app: typer.Typer) -> None:
    @app.command("stage-inference-agent")
    def stage_agent(
        host: Annotated[str, typer.Option()],
        remote_executable: Annotated[str, typer.Option()],
        remote_queue: Annotated[str, typer.Option()],
        output: Annotated[Path, typer.Option(file_okay=False)],
        transport: Annotated[Path, typer.Option(dir_okay=False)],
    ) -> None:
        typer.echo(
            stage_inference_agent(
                host=host,
                remote_executable=remote_executable,
                remote_queue=remote_queue,
                output=output,
                transport=transport,
            )
        )

    @app.command("activate-inference-agent")
    def activate_agent(output: Annotated[Path, typer.Option(file_okay=False)]) -> None:
        typer.echo(json.dumps(activate_inference_agent(output), sort_keys=True))

    @app.command("install-processing-service")
    def install_service(
        queue_root: Annotated[Path, typer.Option(file_okay=False)],
        inference_queue: Annotated[Path, typer.Option(file_okay=False)],
        output: Annotated[Path, typer.Option(file_okay=False)],
        executable: Annotated[str, typer.Option()],
        transport: Annotated[Path, typer.Option(dir_okay=False)],
        source_commit: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        typer.echo(
            json.dumps(
                install_processing_service(
                    queue_root=queue_root,
                    inference_queue=inference_queue,
                    output=output,
                    executable=executable,
                    transport=transport,
                    source_commit=source_commit,
                ),
                sort_keys=True,
            )
        )

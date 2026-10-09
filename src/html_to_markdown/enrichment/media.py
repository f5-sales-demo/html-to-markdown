"""Digest-cached OCR plus visual analysis, preserving captured image bytes."""

import base64
import io
import shutil

# Fixed OCR executable with argv transport; scraped text is never a command.
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from PIL import Image

from .analysis import sha


def image_input(path: Path) -> tuple[str, dict[str, Any]]:
    raw = path.read_bytes()
    digest = sha(raw)
    ocr: dict[str, Any] = {"sha256": digest, "engine": None, "text": "", "uncertain": True}
    # Captured media may carry a different encoding from its filename. Give
    # OCR the same decoded, normalized PNG derivative supplied to the model.
    visual = raw
    if b"<svg" in raw[:4096]:
        rasterizer = shutil.which("rsvg-convert")
        if rasterizer is None:
            raise ValueError("SVG visual review requires rsvg-convert")
        rendered = subprocess.run(
            [rasterizer, "--format", "png", "--width", "2048"],
            input=raw,
            capture_output=True,
            timeout=60,
            check=True,
        )  # nosec B603
        visual = rendered.stdout
    with Image.open(io.BytesIO(visual)) as original:
        image = original.convert("RGBA").convert("RGB")
        image.thumbnail((2048, 2048))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    executable = shutil.which("tesseract")
    if executable:
        try:
            result = subprocess.run(  # nosec B603
                [executable, "stdin", "stdout"],
                input=buffer.getvalue(),
                capture_output=True,
                timeout=90,
                check=False,
            )
            ocr.update(
                engine="tesseract",
                text=result.stdout.decode("utf-8", errors="replace"),
                uncertain=result.returncode != 0,
            )
        except subprocess.TimeoutExpired:
            ocr["error"] = "ocr_timeout"
    ocr["visual_input_sha256"] = sha(buffer.getvalue())
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(), ocr

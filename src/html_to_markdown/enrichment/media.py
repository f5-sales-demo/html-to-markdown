"""Digest-cached OCR plus visual analysis, preserving captured image bytes."""

import base64
import io
import re
import shutil

# Fixed OCR executable with argv transport; scraped text is never a command.
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from PIL import Image

from .analysis import sha


def validate_svg(raw: bytes) -> None:
    """Prevent document-controlled SVG resources from reading local or remote data."""
    root = ElementTree.fromstring(raw)
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() in {"script", "foreignobject"}:
            raise ValueError("SVG includes an active document element")
        for key, value in node.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                raise ValueError("SVG includes an event handler")
            if (
                name in {"href", "src"}
                and value
                and not value.startswith("#")
                and not re.fullmatch(
                    r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+", value
                )
            ):
                raise ValueError("SVG includes an external resource")
        text = " ".join([node.text or "", *node.attrib.values()])
        if "@import" in text.casefold() or any(
            not match.strip(" '\"").startswith("#")
            for match in re.findall(r"url\(([^)]+)\)", text, re.I)
        ):
            raise ValueError("SVG includes an external stylesheet resource")


def image_input(path: Path, *, perform_ocr: bool = True) -> tuple[str, dict[str, Any]]:
    raw = path.read_bytes()
    digest = sha(raw)
    ocr: dict[str, Any] = {"sha256": digest, "engine": None, "text": "", "uncertain": True}
    # Captured media may carry a different encoding from its filename. Give
    # OCR the same decoded, normalized PNG derivative supplied to the model.
    visual = raw
    if b"<svg" in raw[:4096]:
        validate_svg(raw)
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
    if executable and perform_ocr:
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


def all_frame_inputs(path: Path, *, perform_ocr: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Supply every animation frame, with its original timing and media digest."""
    raw = path.read_bytes()
    if b"<svg" in raw[:4096]:
        return [image_input(path, perform_ocr=perform_ocr)]
    results = []
    executable = shutil.which("tesseract")
    with Image.open(io.BytesIO(raw)) as source:
        count = getattr(source, "n_frames", 1)
        metadata = {str(key): str(value) for key, value in source.info.items()}
        metadata.update({str(key): str(value) for key, value in source.getexif().items()})
        for tag in (34665, 34853, 40965):
            try:
                nested = source.getexif().get_ifd(tag)
                if nested:
                    metadata[str(tag)] = str(nested)
            except (KeyError, ValueError, TypeError):
                continue
        for index in range(count):
            source.seek(index)
            image = source.convert("RGBA").convert("RGB")
            # OCR sees the complete native-resolution frame. The model receives
            # the same full frame rather than a lossy thumbnail.
            stream = io.BytesIO()
            image.save(stream, format="PNG", optimize=False)
            visual = stream.getvalue()
            ocr: dict[str, Any] = {
                "sha256": sha(raw),
                "frame": index,
                "frame_count": count,
                "duration": source.info.get("duration", 0),
                "width": source.width,
                "height": source.height,
                "loop": source.info.get("loop"),
                "metadata": metadata,
                "visual_input_sha256": sha(visual),
                "text": "",
                "uncertain": True,
            }
            if executable and perform_ocr:
                try:
                    process = subprocess.run(
                        [executable, "stdin", "stdout"],
                        input=visual,
                        capture_output=True,
                        timeout=90,
                        check=False,
                    )  # nosec B603
                    ocr.update(
                        engine="tesseract",
                        text=process.stdout.decode("utf-8", errors="replace"),
                        uncertain=process.returncode != 0,
                    )
                except subprocess.TimeoutExpired:
                    ocr["error"] = "ocr_timeout"
            results.append(("data:image/png;base64," + base64.b64encode(visual).decode(), ocr))
    return results

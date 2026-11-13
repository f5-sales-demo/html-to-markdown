"""Hash-bound, reviewed pixel replacement for public community diagrams."""

from __future__ import annotations

import hashlib
import io
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .errors import PublicationBlockedError
from .media_encoding import png_bytes


def gif_subblocks_end(content: bytes, offset: int) -> int:
    """Read the exact end of a GIF data-block chain."""
    while offset < len(content):
        size = content[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
        if offset > len(content):
            break
    raise ValueError("truncated GIF subblocks")


def strip_gif_metadata(content: bytes) -> bytes:
    """Remove comment and unknown application blocks without decoding frames."""
    if len(content) < 14 or content[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid GIF header")
    offset = 13
    if content[10] & 128:
        offset += 3 * (2 ** ((content[10] & 7) + 1))
    output = bytearray(content[:offset])
    while offset < len(content):
        start = offset
        marker = content[offset]
        offset += 1
        if marker == 0x3B:
            if offset != len(content):
                raise ValueError("unexpected GIF trailing bytes")
            output.append(marker)
            return bytes(output)
        if marker == 0x21:
            if offset >= len(content):
                raise ValueError("truncated GIF extension")
            kind = content[offset]
            offset += 1
            end = gif_subblocks_end(content, offset)
            # Preserve control, plain-text rendering, and standard loop timing only.
            preserve = kind in {0xF9, 0x01} or (
                kind == 0xFF
                and content[offset : offset + 12] in {b"\x0bNETSCAPE2.0", b"\x0bANIMEXTS1.0"}
            )
            if preserve:
                output.extend(content[start:end])
            offset = end
        elif marker == 0x2C:
            if offset + 9 >= len(content):
                raise ValueError("truncated GIF image descriptor")
            packed = content[offset + 8]
            offset += 9
            if packed & 128:
                offset += 3 * (2 ** ((packed & 7) + 1))
            # Skip the LZW minimum code-size byte, preserving compressed bytes.
            offset = gif_subblocks_end(content, offset + 1)
            output.extend(content[start:offset])
        else:
            raise ValueError("invalid GIF block marker")
    raise ValueError("missing GIF trailer")


def label_style(label: dict[str, Any]) -> tuple[Any, Any, bool]:
    """Validate explicit reviewed RGB styling; absent styles preserve historical bytes."""
    style = label.get("style")
    if style is None:
        return "black", "white", False
    if not isinstance(style, dict) or set(style) != {"foreground", "background", "underline"}:
        raise ValueError("invalid synthetic label style")
    colors = []
    for key in ("foreground", "background"):
        color = style[key]
        if (
            not isinstance(color, list)
            or len(color) != 3
            or any(
                not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255
                for value in color
            )
        ):
            raise ValueError("invalid synthetic label color")
        colors.append(tuple(color))
    if not isinstance(style["underline"], bool):
        raise ValueError("invalid synthetic label underline")
    if colors[0] == colors[1]:
        raise ValueError("synthetic label has no color contrast")
    return colors[0], colors[1], style["underline"]


def _label_lines(draw: Any, text: str, font: Any, width: int) -> list[str] | None:
    """Wrap display pixels while retaining every character of the guarded value."""
    lines = []
    remaining = text
    while remaining:
        count = 0
        for end in range(1, len(remaining) + 1):
            bounds = draw.textbbox((0, 0), remaining[:end], font=font)
            if bounds[2] - bounds[0] > width:
                break
            count = end
        if not count:
            return None
        if count < len(remaining):
            boundary = max(
                (i + 1 for i, char in enumerate(remaining[:count]) if char in " .-_/"), default=0
            )
            count = boundary or count
        lines.append(remaining[:count])
        remaining = remaining[count:]
    return lines


def _fitted_label(
    draw: Any, label: dict[str, Any], font: Any, foreground: Any, underline: bool
) -> bool:
    left, top, right, bottom = label["rectangle"]
    lines = _label_lines(draw, label["text"], font, right - left)
    if not lines:
        return False
    bounds = [draw.textbbox((0, 0), line, font=font) for line in lines]
    height = max(box[3] - box[1] for box in bounds)
    extra = 2 if underline else 0
    total = len(lines) * (height + extra) + (len(lines) - 1) * 2
    if total > bottom - top:
        return False
    pad_x = min(2, (right - left - max(box[2] - box[0] for box in bounds)) // 2)
    pad_y = min(2, (bottom - top - total) // 2)
    for index, (line, box) in enumerate(zip(lines, bounds, strict=True)):
        y = top + pad_y + index * (height + extra + 2)
        draw.text((left + pad_x - box[0], y - box[1]), line, fill=foreground, font=font)
        if underline and box[2] > box[0]:
            draw.line(
                (left + pad_x, y + height + 1, left + pad_x + box[2] - box[0] - 1, y + height + 1),
                fill=foreground,
            )
    return True


def draw_label(draw: Any, label: dict[str, Any]) -> None:
    """Draw a reviewed synthetic label only when it fits its cleared area."""
    text, size = label["text"], label["size"]
    if not isinstance(text, str) or not text or not text.isascii():
        raise ValueError("invalid synthetic label text")
    if any(ord(character) < 32 for character in text):
        raise ValueError("invalid synthetic label control character")
    if not isinstance(size, int) or isinstance(size, bool) or not 6 <= size <= 72:
        raise ValueError("invalid synthetic label size")
    fit = label.get("fit", False)
    if not isinstance(fit, bool):
        raise ValueError("invalid synthetic label fit mode")
    layout = label.get("layout")
    if layout not in {None, "wrapped/2"} or (layout is not None and not fit):
        raise ValueError("invalid synthetic label layout")
    left, top, right, bottom = label["rectangle"]
    foreground, _, underline = label_style(label)
    for selected_size in range(size, 5 if fit else size - 1, -1):
        font = ImageFont.load_default(size=selected_size)
        if fit:
            if layout == "wrapped/2":
                if _fitted_label(draw, label, font, foreground, underline):
                    return
                continue
            bounds = draw.textbbox((0, 0), text, font=font)
            width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            if width <= right - left and height + (2 if underline else 0) <= bottom - top:
                pad_x = min(2, (right - left - width) // 2)
                pad_y = min(2, (bottom - top - height - (2 if underline else 0)) // 2)
                draw.text(
                    (left + pad_x - bounds[0], top + pad_y - bounds[1]),
                    text,
                    fill=foreground,
                    font=font,
                )
                if underline:
                    y = top + pad_y + height + 1
                    draw.line((left + pad_x, y, left + pad_x + width - 1, y), fill=foreground)
                return
        else:
            bounds = draw.textbbox((left + 2, top + 2), text, font=font)
            if bounds[2] < right and bounds[3] + (2 if underline else 0) < bottom:
                draw.text((left + 2, top + 2), text, fill=foreground, font=font)
                if underline:
                    draw.line(
                        (bounds[0], bounds[3] + 1, bounds[2] - 1, bounds[3] + 1), fill=foreground
                    )
                return
    raise ValueError("synthetic label does not fit")


def apply_labels_and_rectangles(image: Any, transform: dict[str, Any]) -> None:
    """Clear reviewed areas before drawing any synthetic labels."""
    labels = transform.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(label, dict) for label in labels):
        raise ValueError("invalid synthetic labels")
    rectangles = [(rectangle, "white") for rectangle in transform.get("rectangles", [])]
    rectangles.extend((label["rectangle"], label_style(label)[1]) for label in labels)
    draw = ImageDraw.Draw(image)
    for rectangle, background in rectangles:
        if (
            not isinstance(rectangle, list)
            or len(rectangle) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) for value in rectangle)
        ):
            raise ValueError("invalid rectangle")
        left, top, right, bottom = rectangle
        if not 0 <= left < right <= image.width or not 0 <= top < bottom <= image.height:
            raise ValueError("rectangle is outside image")
        draw.rectangle((left, top, right - 1, bottom - 1), fill=background)
    for label in labels:
        draw_label(draw, label)


def encode_exact_gif(frames: list[Any], durations: list[int], repeats: int | None) -> bytes:
    """Use a common exact palette; reject any color approximation or transparency loss."""
    colors: set[tuple[int, ...]] = set()
    for frame in frames:
        found = frame.getcolors(256)
        if found is None or any(color[3] != 255 for _, color in found):
            raise ValueError("GIF output requires at most 256 opaque colors")
        colors.update(color for _, color in found)
    if len(colors) > 256:
        raise ValueError("GIF output requires a common exact palette")
    ordered = sorted(colors)
    mapping = {color: index for index, color in enumerate(ordered)}
    palette = [component for color in ordered for component in color[:3]]
    palette.extend([0] * (768 - len(palette)))
    indexed = []
    for frame in frames:
        image = Image.new("P", frame.size)
        image.putpalette(palette)
        pixels = frame.get_flattened_data()
        image.putdata([mapping[color] for color in pixels])
        indexed.append(image)
    output = io.BytesIO()
    options = {"loop": repeats} if repeats is not None else {}
    indexed[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=indexed[1:],
        duration=durations,
        disposal=1,
        optimize=True,
        **options,
    )
    return output.getvalue()


def validate_animation_playback(
    result: bytes, frames: list[Any], durations: list[int], expected_loop: int | None
) -> None:
    """Verify lossless pixels and equivalent playback after encoding."""
    with Image.open(io.BytesIO(result)) as published:
        if (
            getattr(published, "n_frames", 1) != len(frames)
            or published.info.get("loop") != expected_loop
        ):
            raise ValueError("animated output changed frame count or loop")
        for index, frame in enumerate(frames):
            published.seek(index)
            if published.info.get("duration", 0) != durations[index]:
                raise ValueError("animated output changed frame timing")
            if published.convert("RGBA").tobytes() != frame.tobytes():
                raise ValueError("animated output changed reviewed pixels")


def apply_animation_frame(image: Any, frame_transform: dict[str, Any], exact_gif: bool) -> None:
    """Apply frame edits with solid glyphs for an exact GIF palette."""
    apply_labels_and_rectangles(image, frame_transform)
    if not exact_gif:
        return
    draw = ImageDraw.Draw(image)
    for label in frame_transform["labels"]:
        left, top, right, bottom = label["rectangle"]
        draw.rectangle((left, top, right - 1, bottom - 1), fill=label_style(label)[1])
    draw.fontmode = "1"
    for label in frame_transform["labels"]:
        draw_label(draw, label)


def transform_animation(content: bytes, transform: dict[str, Any]) -> bytes:
    """Encode every reviewed frame losslessly and verify equivalent playback."""
    with Image.open(io.BytesIO(content)) as source:
        if source.format != "GIF" or getattr(source, "n_frames", 1) < 2:
            raise ValueError("animation transform requires an animated GIF")
        frame_policies = transform.get("frame_transforms", {})
        frame_count = getattr(source, "n_frames", 1)
        if not isinstance(frame_policies, dict) or any(
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or str(int(key)) != key
            or not 0 <= int(key) < frame_count
            or not isinstance(value, dict)
            or set(value) - {"rectangles", "labels"}
            for key, value in frame_policies.items()
        ):
            raise ValueError("invalid animation frame policy")
        frames, durations = [], []
        # GIF counts repeats; APNG counts total plays. Zero means infinite in both.
        repeats = source.info.get("loop")
        plays = 0 if repeats == 0 else (repeats + 1 if repeats is not None else 1)
        for index in range(getattr(source, "n_frames", 1)):
            source.seek(index)
            frame = source.convert("RGBA")
            clean = Image.new("RGBA", frame.size)
            clean.paste(frame)
            specific = frame_policies.get(str(index), {})
            frame_transform = {
                "rectangles": transform.get("rectangles", []) + specific.get("rectangles", []),
                "labels": transform.get("labels", []) + specific.get("labels", []),
            }
            apply_animation_frame(
                clean, frame_transform, transform.get("animation_format") == "gif"
            )
            frames.append(clean)
            durations.append(source.info.get("duration", 0))
    expected_loop = plays
    if transform.get("animation_format") == "gif":
        result = encode_exact_gif(frames, durations, repeats)
        expected_loop = repeats
    else:
        output = io.BytesIO()
        frames[0].save(
            output,
            format="PNG",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=plays,
            disposal=0,
            blend=0,
            optimize=False,
        )
        result = output.getvalue()
    validate_animation_playback(result, frames, durations, expected_loop)
    return result


def privacy_png(image: Image.Image, transform: dict[str, Any]) -> bytes:
    """Retain historical encodings and version new portable privacy outputs."""
    if transform.get("png_encoding") == "portable-1":
        return png_bytes(image)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def reviewed_media(content: bytes, url: str, review: dict[str, Any]) -> tuple[bytes, str | None]:
    """Approve original bytes or apply the explicitly reviewed opaque rectangles."""
    digest = hashlib.sha256(content).hexdigest()
    if review.get("assets", {}).get(url) != digest:
        raise PublicationBlockedError("community image bytes differ from visual privacy review")
    transform = review.get("asset_redactions", {}).get(url)
    if transform is None:
        return content, None
    if not isinstance(transform, dict):
        raise PublicationBlockedError("community image redaction is not a policy")
    pixel_edits = bool(transform.get("rectangles") or transform.get("labels"))
    frame_edits = transform.get("animation") is True and transform.get("frame_transforms")
    metadata_edits = (
        transform.get("strip_metadata") is True or transform.get("strip_gif_metadata") is True
    )
    if not (pixel_edits or frame_edits or metadata_edits):
        raise PublicationBlockedError("community image redaction has no reviewed rectangles")
    if transform.get("animation") is True:
        if transform.get("strip_gif_metadata"):
            raise PublicationBlockedError("animation policy mixes output formats")
        try:
            result = transform_animation(content, transform)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise PublicationBlockedError("community animation transform is invalid") from error
        if hashlib.sha256(result).hexdigest() != transform.get("output_sha256"):
            raise PublicationBlockedError("community animation differs from reviewed output")
        return result, "image/gif" if transform.get("animation_format") == "gif" else "image/png"
    if transform.get("strip_gif_metadata") is True:
        if transform.get("rectangles") or transform.get("labels"):
            raise PublicationBlockedError("GIF metadata policy cannot alter frame pixels")
        try:
            result = strip_gif_metadata(content)
        except ValueError as error:
            raise PublicationBlockedError("community GIF metadata transform is invalid") from error
        if hashlib.sha256(result).hexdigest() != transform.get("output_sha256"):
            raise PublicationBlockedError("community GIF differs from reviewed output")
        return result, "image/gif"
    try:
        with Image.open(io.BytesIO(content)) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise PublicationBlockedError(
                    "community animated media needs a frame-preserving reviewed transform"
                )
            mode = (
                "RGBA"
                if not transform.get("rectangles")
                and ("A" in source.mode or "transparency" in source.info)
                else "RGB"
            )
            converted = source.convert(mode)
            image = Image.new(mode, converted.size)
            image.paste(converted)
        apply_labels_and_rectangles(image, transform)
        result = privacy_png(image, transform)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PublicationBlockedError("community image redaction is invalid") from error
    if hashlib.sha256(result).hexdigest() != transform.get("output_sha256"):
        raise PublicationBlockedError("community redacted image differs from reviewed output")
    return result, "image/png"

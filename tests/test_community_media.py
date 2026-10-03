"""Reviewed media edits remove pixels and bind both input and published output."""

import hashlib
import io

import pytest
from PIL import Image

from html_to_markdown.community_media import reviewed_media
from html_to_markdown.errors import PublicationBlockedError

URL = "https://community.f5.com/diagram.png"


def image_bytes():
    image = Image.new("RGB", (10, 10), "red")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_unchanged_approved_media():
    content = image_bytes()
    review = {"assets": {URL: hashlib.sha256(content).hexdigest()}}
    assert reviewed_media(content, URL, review) == (content, None)
    with pytest.raises(PublicationBlockedError, match="bytes differ"):
        reviewed_media(content + b"changed", URL, review)


def test_redaction_replaces_pixels_and_strips_metadata():
    content = image_bytes()
    expected = Image.new("RGB", (10, 10), "red")
    from PIL import ImageDraw

    ImageDraw.Draw(expected).rectangle((0, 0, 4, 4), fill="white")
    output = io.BytesIO()
    expected.save(output, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "rectangles": [[0, 0, 5, 5]],
                "output_sha256": hashlib.sha256(output.getvalue()).hexdigest(),
            }
        },
    }
    result, media = reviewed_media(content, URL, review)
    assert result == output.getvalue()
    assert media == "image/png"
    image = Image.open(io.BytesIO(result))
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((9, 9)) == (255, 0, 0)
    assert not image.info
    review["asset_redactions"][URL]["output_sha256"] = "0" * 64
    with pytest.raises(PublicationBlockedError, match="reviewed output"):
        reviewed_media(content, URL, review)


@pytest.mark.parametrize(
    "rectangles", [[], [[0, 0, 11, 11]], [[1, 1, 0, 0]], [[0, 0, 1]], [["0", 0, 1, 1]]]
)
def test_bad_redactions_fail_closed(rectangles):
    content = image_bytes()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {URL: {"rectangles": rectangles}},
    }
    with pytest.raises(PublicationBlockedError):
        reviewed_media(content, URL, review)


def test_metadata_only_policy_preserves_pixels_and_binds_output():
    from PIL.PngImagePlugin import PngInfo

    image = Image.new("RGB", (10, 10), "blue")
    metadata = PngInfo()
    metadata.add_text("comment", "Synthetic private annotation")
    source = io.BytesIO()
    image.save(source, format="PNG", pnginfo=metadata)
    expected = io.BytesIO()
    image.save(expected, format="PNG", optimize=False)
    content = source.getvalue()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_metadata": True,
                "rectangles": [],
                "output_sha256": hashlib.sha256(expected.getvalue()).hexdigest(),
            }
        },
    }
    result, media = reviewed_media(content, URL, review)
    assert result == expected.getvalue()
    assert media == "image/png"
    published = Image.open(io.BytesIO(result))
    assert not published.info
    assert published.mode == image.mode
    assert published.tobytes() == image.tobytes()


def test_metadata_policy_preserves_transparent_diagram():
    image = Image.new("RGBA", (10, 10), (0, 0, 255, 0))
    content = io.BytesIO()
    image.save(content, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(content.getvalue()).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_metadata": True,
                "rectangles": [],
                "output_sha256": hashlib.sha256(content.getvalue()).hexdigest(),
            }
        },
    }
    result, _ = reviewed_media(content.getvalue(), URL, review)
    assert Image.open(io.BytesIO(result)).getpixel((0, 0)) == (0, 0, 255, 0)


def test_animated_media_transform_cannot_discard_later_frames():
    source = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(
        source,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "red")],
        duration=[100, 200],
        loop=0,
    )
    content = source.getvalue()
    still = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(still, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_metadata": True,
                "rectangles": [],
                "output_sha256": hashlib.sha256(still.getvalue()).hexdigest(),
            }
        },
    }
    with pytest.raises(PublicationBlockedError, match="animated"):
        reviewed_media(content, URL, review)
    del review["asset_redactions"]
    assert reviewed_media(content, URL, review) == (content, None)


def test_reviewed_label_preserves_diagram_relationships():
    from PIL import ImageDraw, ImageFont

    image = Image.new("RGB", (160, 40), "blue")
    source = io.BytesIO()
    image.save(source, format="PNG")
    expected = Image.new("RGB", image.size, "blue")
    draw = ImageDraw.Draw(expected)
    draw.rectangle((2, 2, 157, 27), fill="white")
    draw.text((4, 4), "blue.example.com", fill="black", font=ImageFont.load_default(size=12))
    output = io.BytesIO()
    expected.save(output, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(source.getvalue()).hexdigest()},
        "asset_redactions": {
            URL: {
                "rectangles": [],
                "labels": [{"rectangle": [2, 2, 158, 28], "text": "blue.example.com", "size": 12}],
                "output_sha256": hashlib.sha256(output.getvalue()).hexdigest(),
            }
        },
    }
    result, _ = reviewed_media(source.getvalue(), URL, review)
    assert result == output.getvalue()
    assert Image.open(io.BytesIO(result)).getpixel((159, 39)) == (0, 0, 255)
    review["asset_redactions"][URL]["labels"][0]["text"] = "example.com " * 30
    with pytest.raises(PublicationBlockedError, match="invalid"):
        reviewed_media(source.getvalue(), URL, review)


@pytest.mark.parametrize(
    "labels",
    [
        "invalid",
        [None],
        [{}],
        [{"rectangle": [0, 0, 10, 10], "text": 3, "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "", "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "é", "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "\n", "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "x", "size": True}],
        [{"rectangle": [0, 0, 10, 10], "text": "x", "size": "8"}],
        [{"rectangle": [0, 0, 10, 10], "text": "x", "size": 73}],
    ],
)
def test_malformed_labels_fail_closed(labels):
    content = image_bytes()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {URL: {"strip_metadata": True, "labels": labels}},
    }
    with pytest.raises(PublicationBlockedError, match="invalid"):
        reviewed_media(content, URL, review)


def test_gif_metadata_removal_preserves_all_frames_and_timing():
    from html_to_markdown.community_media import strip_gif_metadata

    source = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(
        source,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "red")],
        duration=[100, 200],
        loop=2,
        comment=b"Synthetic private annotation",
    )
    content = source.getvalue()
    output = strip_gif_metadata(content)
    assert b"Synthetic private annotation" not in output
    assert len(output) < len(content)
    original, published = Image.open(io.BytesIO(content)), Image.open(io.BytesIO(output))
    assert original.n_frames == published.n_frames == 2
    assert published.info["loop"] == 2
    for frame in range(2):
        original.seek(frame)
        published.seek(frame)
        assert original.info["duration"] == published.info["duration"]
        assert original.convert("RGBA").tobytes() == published.convert("RGBA").tobytes()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_gif_metadata": True,
                "output_sha256": hashlib.sha256(output).hexdigest(),
            }
        },
    }
    assert reviewed_media(content, URL, review) == (output, "image/gif")
    review["asset_redactions"][URL]["output_sha256"] = "0" * 64
    with pytest.raises(PublicationBlockedError, match="reviewed output"):
        reviewed_media(content, URL, review)
    review["assets"][URL] = hashlib.sha256(b"invalid").hexdigest()
    with pytest.raises(PublicationBlockedError, match="invalid"):
        reviewed_media(b"invalid", URL, review)
    review["asset_redactions"][URL]["rectangles"] = [[0, 0, 1, 1]]
    with pytest.raises(PublicationBlockedError):
        reviewed_media(content, URL, review)


@pytest.mark.parametrize("content", [b"", b"GIF89a", b"GIF89a" + bytes(20)])
def test_invalid_gif_metadata_transform_fails_closed(content):
    from html_to_markdown.community_media import strip_gif_metadata

    with pytest.raises(ValueError):
        strip_gif_metadata(content)


def test_gif_unknown_application_metadata_is_removed():
    from html_to_markdown.community_media import strip_gif_metadata

    source = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(source, format="GIF")
    original = source.getvalue()
    annotated = original[:-1] + b"\x21\xff\x0bXMP DataXMP\x07private\x00" + original[-1:]
    assert strip_gif_metadata(annotated) == original
    for malformed in (original[:-1], original + b"trailing", original[:-1] + b"\x21"):
        with pytest.raises(ValueError):
            strip_gif_metadata(malformed)


def test_frame_preserving_transform_keeps_pixels_timing_and_loop():
    from html_to_markdown.community_media import transform_animation

    frames = [Image.new("RGB", (20, 20), color) for color in ("blue", "red", "green")]
    stream = io.BytesIO()
    frames[0].save(
        stream,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=2,
        comment=b"private annotation",
    )
    content = stream.getvalue()
    transform = {"animation": True, "rectangles": [[0, 0, 5, 5]]}
    result = transform_animation(content, transform)
    original, output = Image.open(io.BytesIO(content)), Image.open(io.BytesIO(result))
    assert output.format == "PNG"
    assert output.n_frames == original.n_frames == 3
    assert output.info["loop"] == 3
    assert b"private annotation" not in result
    for n in range(3):
        original.seek(n)
        output.seek(n)
        assert output.info["duration"] == original.info["duration"]
        assert output.convert("RGBA").getpixel((0, 0)) == (255, 255, 255, 255)
        assert (
            output.convert("RGBA").crop((5, 5, 20, 20)).tobytes()
            == original.convert("RGBA").crop((5, 5, 20, 20)).tobytes()
        )
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {**transform, "output_sha256": hashlib.sha256(result).hexdigest()}
        },
    }
    assert reviewed_media(content, URL, review) == (result, "image/png")
    review["asset_redactions"][URL]["output_sha256"] = "0" * 64
    with pytest.raises(PublicationBlockedError, match="reviewed output"):
        reviewed_media(content, URL, review)


def test_animation_transform_rejects_invalid_input_and_unsafe_policy():
    from html_to_markdown.community_media import transform_animation

    with pytest.raises(ValueError, match="animated GIF"):
        transform_animation(image_bytes(), {"rectangles": [[0, 0, 1, 1]]})
    with pytest.raises((ValueError, OSError)):
        transform_animation(b"GIF89a", {"rectangles": [[0, 0, 1, 1]]})


def test_animation_transform_rejects_collapsed_frames():
    from html_to_markdown.community_media import transform_animation

    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (20, 20), "red")],
        duration=[100, 200],
        loop=0,
    )
    transform = {"animation": True, "rectangles": [[0, 0, 20, 20]]}
    with pytest.raises(ValueError, match="frame count"):
        transform_animation(stream.getvalue(), transform)


def test_animation_policy_rejects_conflicting_format_and_bad_coordinates():
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (20, 20), "red")],
        duration=[100, 200],
    )
    content = stream.getvalue()
    transform = {"animation": True, "rectangles": [[0, 0, 21, 21]]}
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {URL: transform},
    }
    with pytest.raises(PublicationBlockedError, match="invalid"):
        reviewed_media(content, URL, review)
    transform["strip_gif_metadata"] = True
    with pytest.raises(PublicationBlockedError, match="mixes"):
        reviewed_media(content, URL, review)


def test_animation_without_loop_plays_once():
    from html_to_markdown.community_media import transform_animation

    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (20, 20), "red")],
        duration=[100, 200],
    )
    output = transform_animation(stream.getvalue(), {"rectangles": [[0, 0, 5, 5]]})
    assert Image.open(io.BytesIO(output)).info["loop"] == 1


def test_exact_palette_animation_preserves_pixels_and_gif_loop():
    from html_to_markdown.community_media import transform_animation

    stream = io.BytesIO()
    Image.new("RGB", (40, 40), "blue").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (40, 40), "red")],
        duration=[100, 200],
        loop=2,
    )
    policy = {"animation": True, "animation_format": "gif", "rectangles": [[0, 0, 5, 5]]}
    content = stream.getvalue()
    result = transform_animation(content, policy)
    published = Image.open(io.BytesIO(result))
    assert published.format == "GIF"
    assert published.n_frames == 2
    assert published.info["loop"] == 2
    for n, color in enumerate([(0, 0, 255, 255), (255, 0, 0, 255)]):
        published.seek(n)
        assert published.convert("RGBA").getpixel((9, 9)) == color
        assert published.convert("RGBA").getpixel((0, 0)) == (255, 255, 255, 255)
        assert published.info["duration"] == [100, 200][n]
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {URL: {**policy, "output_sha256": hashlib.sha256(result).hexdigest()}},
    }
    assert reviewed_media(content, URL, review) == (result, "image/gif")


def test_exact_gif_rejects_transparency_and_excess_colors():
    from html_to_markdown.community_media import encode_exact_gif

    with pytest.raises(ValueError, match="opaque"):
        encode_exact_gif([Image.new("RGBA", (2, 2), (1, 2, 3, 0))], [100], 0)
    image = Image.new("RGBA", (300, 1))
    image.putdata([(n % 256, n // 256, 0, 255) for n in range(300)])
    with pytest.raises(ValueError, match="256"):
        encode_exact_gif([image], [100], 0)
    first = Image.new("RGBA", (200, 1))
    second = Image.new("RGBA", (200, 1))
    first.putdata([(n, 0, 0, 255) for n in range(200)])
    second.putdata([(n, 1, 0, 255) for n in range(200)])
    with pytest.raises(ValueError, match="common exact"):
        encode_exact_gif([first, second], [100, 100], 0)


def test_animation_frame_specific_labels_preserve_other_frames():
    from html_to_markdown.community_media import transform_animation

    source = io.BytesIO()
    Image.new("RGB", (160, 40), "blue").save(
        source,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (160, 40), "red")],
        duration=[100, 200],
        loop=0,
    )
    content = source.getvalue()
    result = transform_animation(
        content,
        {
            "animation": True,
            "rectangles": [],
            "frame_transforms": {
                "1": {"labels": [{"rectangle": [2, 2, 158, 28], "text": "192.0.2.1", "size": 12}]}
            },
        },
    )
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "animation": True,
                "frame_transforms": {
                    "1": {
                        "labels": [{"rectangle": [2, 2, 158, 28], "text": "192.0.2.1", "size": 12}]
                    }
                },
                "output_sha256": hashlib.sha256(result).hexdigest(),
            }
        },
    }
    assert reviewed_media(content, URL, review) == (result, "image/png")
    original, published = Image.open(io.BytesIO(content)), Image.open(io.BytesIO(result))
    assert published.n_frames == 2
    assert published.convert("RGBA").tobytes() == original.convert("RGBA").tobytes()
    original.seek(1)
    published.seek(1)
    assert published.getpixel((3, 3)) == (255, 255, 255, 255)
    assert published.getpixel((159, 39)) == (255, 0, 0, 255)
    assert published.info["duration"] == 200


@pytest.mark.parametrize("frames", [[], {"2": {}}, {"-1": {}}, {"01": {}}, {"0": None}])
def test_animation_frame_policies_reject_unapplied_targets(frames):
    from html_to_markdown.community_media import transform_animation

    source = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(
        source,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "red")],
        duration=[100, 200],
    )
    with pytest.raises(ValueError, match="frame"):
        transform_animation(source.getvalue(), {"frame_transforms": frames})

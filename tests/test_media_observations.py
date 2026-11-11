"""Visible image values need exact independently confirmed guards."""

import pytest

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.media_observations import (
    MediaObservations,
    ObservationValidation,
    VisibleValue,
    observation_payload,
    verify_observations,
)
from html_to_markdown.source_review import Label, MediaChange, preview_media


def proposal() -> MediaObservations:
    return MediaObservations(
        identity="source:article",
        binding_sha256="b" * 64,
        complete_media_reviewed=True,
        unresolved=[],
        observations=[
            VisibleValue(
                media_sha256="a" * 64,
                frame=0,
                rectangle=[5, 5, 60, 20],
                value="198.51.100.10",
                reason="visible user address omitted by OCR",
            )
        ],
    )


def test_native_value_guard_is_deterministic_and_requires_independent_confirmation() -> None:
    media = {"a" * 64: {"frames": [{"width": 100, "height": 50, "text": ""}]}}
    payload = observation_payload("source:article", "b" * 64, proposal(), media)
    assert payload["values"][0]["value_sha256"] == sha("198.51.100.10")
    validation = ObservationValidation(
        identity="source:article",
        binding_sha256="b" * 64,
        observations_sha256=payload["observations_sha256"],
        approved_indices=[0],
        complete_media_reviewed=True,
        failures=[],
        unresolved=[],
    )
    assert verify_observations("source:article", "b" * 64, payload, validation) == payload["values"]
    with pytest.raises(ValueError, match="independently"):
        verify_observations(
            "source:article",
            "b" * 64,
            payload,
            validation.model_copy(update={"approved_indices": []}),
        )


@pytest.mark.parametrize("change", ["binding", "frame", "rectangle", "unresolved", "empty"])
def test_stale_unreadable_or_invalid_observations_stay_unapproved(change: str) -> None:
    value = proposal()
    if change == "binding":
        value.binding_sha256 = "c" * 64
    elif change == "frame":
        value.observations[0].frame = 1
    elif change == "rectangle":
        value.observations[0].rectangle = [0, 0, 101, 50]
    elif change == "unresolved":
        value.unresolved = ["hidden account characters are unreadable"]
    else:
        value.observations[0].value = ""
    with pytest.raises(ValueError):
        observation_payload(
            "source:article",
            "b" * 64,
            value,
            {"a" * 64: {"frames": [{"width": 100, "height": 50}]}},
        )


def test_clipped_value_binds_media_frame_and_region_without_inventing_text() -> None:
    media = {"a" * 64: {"frames": [{"width": 100, "height": 50}]}}
    value = proposal()
    value.observations[0].value = None
    first = observation_payload("source:article", "b" * 64, value, media)
    observed = first["values"][0]
    assert observed["value"] is None and observed["guard_kind"] == "identifying_region"
    value.observations[0].rectangle = [5, 5, 61, 20]
    changed = observation_payload("source:article", "b" * 64, value, media)
    assert changed["values"][0]["value_sha256"] != observed["value_sha256"]
    value.observations[0].media_sha256 = "c" * 64
    with pytest.raises(ValueError, match="absent frame"):
        observation_payload("source:article", "b" * 64, value, media)


def test_observation_approval_cannot_bypass_text_image_replacement_consistency() -> None:
    change = MediaChange(
        source_sha256="a" * 64,
        rectangles=[],
        labels=[
            Label(rectangle=[5, 5, 60, 20], source_value_sha256="b" * 64, text="demo", size=12)
        ],
        frames=[],
        strip_metadata=True,
        reason="replace confirmed region",
    )
    with pytest.raises(ValueError, match="replacements disagree"):
        preview_media(b"image", change, {"b" * 64: "different"})

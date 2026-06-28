"""Unit tests for the deterministic render mapping (no I/O)."""

from __future__ import annotations

from web_channel import render
from web_channel._gen import core_pb2


def test_status_label_is_from_structured_enum() -> None:
    assert render.status_label(core_pb2.FOUND) == ("found", "status-found")
    assert render.status_label(core_pb2.NOT_FOUND) == ("not found", "status-warn")
    assert render.status_label(core_pb2.PARTIAL)[1] == "status-warn"
    assert render.status_label(core_pb2.ERROR) == (
        "error — knowledge base unavailable",
        "status-error",
    )


def test_status_label_unknown_is_safe() -> None:
    # An unknown/unspecified status must not crash and must not read as "found".
    label, cls = render.status_label(999)
    assert label != "found"
    assert cls == "status-warn"


def test_confidence_class_thresholds_match_cli() -> None:
    assert render.confidence_class(0.7) == "conf-high"
    assert render.confidence_class(0.69) == "conf-mid"
    assert render.confidence_class(0.4) == "conf-mid"
    assert render.confidence_class(0.39) == "conf-low"


def test_show_confidence_only_for_found_and_partial() -> None:
    assert render.show_confidence(core_pb2.FOUND)
    assert render.show_confidence(core_pb2.PARTIAL)
    assert not render.show_confidence(core_pb2.NOT_FOUND)
    assert not render.show_confidence(core_pb2.ERROR)

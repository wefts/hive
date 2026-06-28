"""Deterministic, channel-owned rendering of the answer-result algebra.

Mirrors `swarm/cli` (`_STATUS_LABELS`, `_confidence_style`): the channel maps the
STRUCTURED `status`/`confidence` fields to a fixed label + style. It NEVER infers
the outcome or any value from the answer prose (presentation-determinism). No model
is in this path.

Difference from the CLI on purpose: the CLI suppresses a banner for FOUND; the web
brief wants the status badge ALWAYS visible (found/partial/not_found/error), so
FOUND gets an explicit badge here.
"""

from __future__ import annotations

from web_channel._gen import core_pb2

# status -> (human label, css class). Driven by the structured enum only.
_STATUS_LABELS: dict[int, tuple[str, str]] = {
    core_pb2.FOUND: ("found", "status-found"),
    core_pb2.NOT_FOUND: ("not found", "status-warn"),
    core_pb2.PARTIAL: ("partial — some sources unavailable", "status-warn"),
    core_pb2.ERROR: ("error — knowledge base unavailable", "status-error"),
    core_pb2.ANSWER_STATUS_UNSPECIFIED: ("unspecified", "status-warn"),
}


def status_label(status: int) -> tuple[str, str]:
    """(label, css_class) for a structured AnswerStatus; safe default for unknowns."""
    return _STATUS_LABELS.get(status, ("unspecified", "status-warn"))


def confidence_class(confidence: float) -> str:
    """Semantic color band — same thresholds as the CLI (0.7 / 0.4)."""
    if confidence >= 0.7:
        return "conf-high"
    if confidence >= 0.4:
        return "conf-mid"
    return "conf-low"


def show_confidence(status: int) -> bool:
    """Confidence is meaningful only for FOUND/PARTIAL. For NOT_FOUND/ERROR we
    suppress it — showing a number there would fabricate certainty (brief A.0.2)."""
    return status in (core_pb2.FOUND, core_pb2.PARTIAL)

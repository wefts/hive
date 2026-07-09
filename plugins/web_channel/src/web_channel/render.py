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

from markdown_it import MarkdownIt
from pygments import highlight as _pyg_highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from web_channel._gen import core_pb2

# Server-side syntax highlighting (local-first: no CDN, no client JS — the vendored
# pygments CSS carries the colors). `nowrap` emits bare token spans; markdown-it
# wraps them in <pre><code>. Pygments HTML-escapes the code content itself, so the
# safety property of `markdown()` below is preserved.
_pyg_formatter = HtmlFormatter(nowrap=True)


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """Fence highlighter: a labeled fence uses its language; an unlabeled one is
    guessed (operator: highlighting should be automatic). Returning "" falls back
    to markdown-it's plain escaped rendering."""
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except ClassNotFound:
        return ""
    return _pyg_highlight(code, lexer, _pyg_formatter)


# Chat-style markdown (Mattermost/Slack shape — operator, 2026-07-08): CommonMark
# with `breaks` (a bare newline IS a line break — keeps the kernel's structured
# line-based answers intact) and `linkify` (bare URLs become links). `html=False`
# is the safety line: raw HTML in a question or a corpus-quoting answer is escaped,
# never emitted — so the rendered output is safe to mark |safe in templates.
_md = MarkdownIt(
    "commonmark", {"html": False, "breaks": True, "linkify": True, "highlight": _highlight}
)
_md.enable("linkify")


def markdown(text: str) -> str:
    """Render untrusted markdown text to safe HTML (chat-style: breaks + autolinks)."""
    return _md.render(text or "")


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

"""Logging with secret redaction.

Spec 4.5 forbids writing API keys, cookies or auth tokens to the log but does
not say how (spec-review D-8). A filter is the only place that catches it
regardless of which call site formats the message.
"""

from __future__ import annotations

import logging
import re
import sys

# Order matters. The bearer rule runs before the generic key/value rule,
# because "Authorization: Bearer <token>" would otherwise have the *word*
# "Bearer" masked as the value and leave the token itself in the clear.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Cloudflare Access / JWT shaped values anywhere in the line.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}=*"), "Bearer ***"),
    # token=..., "apiKey": "...", cookie=...
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|token|secret|password|authorization|cookie)"
            r"(\"?\s*[:=]\s*\"?)([^\s\",;&]{6,})"
        ),
        r"\1\2***",
    ),
]

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Redact a known literal (e.g. the configured API key) everywhere."""
    if value and len(value) >= 6:
        _registered_secrets.add(value)


def redact(text: str) -> str:
    for secret in _registered_secrets:
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO", *, stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("websockets.client").setLevel("WARNING")
    logging.getLogger("httpx").setLevel("WARNING")

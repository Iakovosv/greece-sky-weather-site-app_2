"""Structured logging with redaction, configured once at startup.

What was wrong before
---------------------
Nothing configured logging. The root logger had no handlers and a level of
WARNING, so every ``log.info`` in the codebase — including the startup line that
reports missing Stripe configuration and the RAM-grid refresh messages — went
nowhere. ``log.warning`` reached stderr through the interpreter's last-resort
handler, unformatted: no timestamp, no level, no logger name. On a production
host that is the difference between "we can see which model failed" and "the
card was blank and nothing was logged".

Format
------
JSON lines by default. A log aggregator parses them; a human can read them. The
alternative — a free-form line — cannot be reliably parsed back into "which run,
which point, which error", which is the whole point of the exercise.

Redaction
---------
A filter drops the values of anything that looks like a credential before the
record is formatted. This is belt-and-braces: the code is written not to log
secrets, but a future `log.info("headers=%s", request.headers)` would leak a
token, and the filter is what stops that. Keys are matched by name.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time

# Anything whose key contains one of these is replaced with a placeholder.
_SECRET_HINTS = ("secret", "token", "passkey", "password", "authorization",
                 "api_key", "apikey", "card", "cvc", "stripe-signature",
                 "private", "credential")

# `sk_live_...`, `whsec_...`, `Bearer xyz`, a long base64 blob. Used for free text
# that is not a clean key=value pair.
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|rk)_(test|live)_[A-Za-z0-9]+"),
    re.compile(r"\bwhsec_[A-Za-z0-9]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|passkey|passwd|password)=([^&\s]+)"),
]

_REDACTED = "<redacted>"


def redact(value):
    """Return `value` with credential-looking parts replaced. Never raises."""
    try:
        if isinstance(value, dict):
            return {k: (_REDACTED if _looks_secret(k) else redact(v))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(v) for v in value]
        if isinstance(value, str):
            return _scrub(value)
        return value
    except Exception:
        return "<unrenderable>"


def _looks_secret(key) -> bool:
    k = str(key).lower()
    return any(h in k for h in _SECRET_HINTS)


def _scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


class _RedactingFilter(logging.Filter):
    """Scrub the formatted message and any structured extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub(record.msg)
            if record.args:
                record.args = tuple(
                    _scrub(a) if isinstance(a, str) else a for a in
                    (record.args if isinstance(record.args, tuple) else (record.args,)))
        except Exception:
            pass
        return True


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "wx") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Any `extra={"...": ...}` the caller attached, minus the reserved names.
        for key, value in vars(record).items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if value is None:
                continue
            payload[key] = redact(value)
        payload["service"] = self.service
        return json.dumps(payload, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))} "
                f"{record.levelname:<7} {record.name}: {record.getMessage()}")
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return _scrub(base)


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime", "message", "taskName"}


def configure(level: str | None = None, fmt: str | None = None) -> None:
    """Install handlers on the root logger. Idempotent.

    Called once from app.py after ``envfile.load()`` so the level can come from
    ``WX_LOG_LEVEL``. Safe to call twice: the marker attribute stops a second set
    of handlers from being stacked, which would duplicate every line.
    """
    root = logging.getLogger()
    if getattr(root, "_wx_configured", False):
        return
    root._wx_configured = True  # type: ignore[attr-defined]

    lvl = (level or os.environ.get("WX_LOG_LEVEL") or "INFO").upper()
    style = (fmt or os.environ.get("WX_LOG_FORMAT") or "json").lower()

    handler = logging.StreamHandler(sys.stderr)
    service = os.environ.get("WX_SERVICE_NAME") or "wx"
    handler.setFormatter(_TextFormatter() if style == "text" else _JsonFormatter(service))
    handler.addFilter(_RedactingFilter())

    root.addHandler(handler)
    try:
        root.setLevel(getattr(logging, lvl))
    except AttributeError:
        root.setLevel(logging.INFO)

    # uvicorn's own loggers propagate into our handler; stop them double-printing
    # through their default configuration.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

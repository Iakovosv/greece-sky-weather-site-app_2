"""Minimal ``.env`` loader.

The app is configured entirely through environment variables, so a ``.env`` file
next to the code is the natural place to keep them. ``python-dotenv`` would do
this, but it is one more dependency for twenty lines of parsing, and the parsing
has to be done carefully anyway:

* ``.env`` must **not** override a variable that is already in the real
  environment. A systemd unit or a container's ``-e`` is a deliberate deployment
  decision; a stray file in the working directory is not allowed to quietly win.
* Values are never echoed. A tile API key lives in this file, and printing it to
  logs or to the ``/api/health`` payload would leak it to anyone who can read a
  response body.

The parser is deliberately small: ``KEY=value``, one per line, ``#`` comments,
optional surrounding quotes. No interpolation, no ``export``, no multi-line.
Anything more elaborate belongs in the deployment tooling, not here.
"""
from __future__ import annotations

import os
from pathlib import Path


def _strip_inline_comment(v: str) -> str:
    """Drop a trailing ``# comment``, but only when it is unquoted.

    A URL fragment (``.../style#hash``) or a ``#`` inside quotes is data, not a
    comment, and truncating there would produce a broken URL that fails silently
    as a 404 from the tile server.
    """
    for i, ch in enumerate(v):
        if ch == "#" and (i == 0 or v[i - 1].isspace()):
            return v[:i].rstrip()
    return v


def parse(text: str) -> dict[str, str]:
    """Parse ``.env`` text into a mapping. Never raises on malformed input."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]          # quoted: take it literally
        else:
            value = _strip_inline_comment(value)
        out[key] = value
    return out


def load(path: str | os.PathLike | None = None, override: bool = False) -> list[str]:
    """Load a ``.env`` file into ``os.environ``. Returns the keys that were set.

    ``override=False`` is the default and the reason this exists: the real
    environment wins.
    """
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    applied = []
    for key, value in parse(text).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied

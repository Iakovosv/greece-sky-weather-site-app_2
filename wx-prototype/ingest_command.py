"""FFmpeg command construction for the M3 ingest worker, kept testable and inert.

Building a command is separated from running it. This module returns an
:class:`IngestCommand` -- argv, a minimal environment, and a *redacted* summary --
and starts nothing. The tests inspect the command; the worker runs it through the
process abstraction; production eventually runs real FFmpeg. No dependency on
FFmpeg exists here, so importing and testing this module needs nothing installed.

The security rules this builder enforces
----------------------------------------
* **The source is never client input.** ``build_command`` takes a ``source`` dict
  that only :func:`cameras.source_for` produces (the trusted registry). There is
  no parameter anywhere here that a request, query string or body could reach.
* **Host allowlist is re-checked at build time.** A camera can pass the lifecycle
  gate and still have a host that is not allowlisted if configuration changed
  between the check and the launch; the last check before ``execve`` is here.
* **The URL never appears in a summary or an error.** The device path of an RTSP
  URL can carry a token, so the redacted summary shows scheme/host/port only.
* **Credentials never appear in a summary, error or log.** FFmpeg has no
  out-of-band password mechanism for RTSP -- the credential must be in the input
  URL -- so the builder accepts that the *argv* carries it and guarantees it
  appears nowhere else: :attr:`IngestCommand.summary` and every exception are
  built from :func:`redact_url`, and :meth:`IngestCommand.safe_argv` returns a
  copy safe to log. Residual argv visibility (``ps``) is documented as a VPS
  hardening item, not solved here -- see the note on the class.
* **Video only.** ``-an`` is unconditional and explicit video mapping is used, so
  no audio track can be pulled from the camera or pushed to the destination.
* **No recording.** The only output is a single RTMPS destination; there is no
  file output, no tee, no segment muxer, and the builder rejects a source that
  would need one.
* **Stream key redaction.** The destination URL embeds the YouTube stream key.
  It is resolved server-side, placed only in the argv's final argument, and
  excluded from every summary, exception and log by construction.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import cameras as cams

log = logging.getLogger("wx.ingest_command")

# The one output this builder can produce: RTMPS to an ingest endpoint whose
# secret is resolved server-side. Named as an abstraction now; the real value and
# storage belong to the separate VPS secrets milestone.
OUTPUT_RTMPS = "rtmps"

# Closed vocabulary for why a command could not be built. Mirrored into
# stream_control's reason set by the worker; kept here so this module can raise
# without importing the control plane (which would be a cycle).
BUILD_REASONS = frozenset({
    "source_absent",
    "host_not_allowlisted",
    "source_scheme",
    "credential_missing",
    "audio_not_allowed",
    "no_output",
    "not_startable",
})


# The only environment variables a media child inherits. Kept short and explicit:
# anything not named here is withheld, so this process's own secrets cannot leak
# into a child it spawns. PATH/LANG/HOME plus the TLS bundle locations a static
# FFmpeg may consult. Nothing project-specific belongs on this list.
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)


class CommandBuildError(Exception):
    """A command could not be built safely. Carries a closed-vocabulary reason."""

    def __init__(self, reason: str = "unknown") -> None:
        super().__init__("ingest command rejected")
        self.reason = reason if reason in BUILD_REASONS else "unknown"


class IngestCommand:
    """An argv to run, a minimal env, and a redacted form safe to show a human.

    ``summary`` is what an operator (or a log) may see: the tool, the source
    redacted to scheme/host/port, and the destination redacted to its scheme and
    host. It never contains the source URL's path, a username, a password, or the
    stream key.

    ``argv`` is the real argv and *does* contain the RTSP credential (FFmpeg takes
    it in the input URL -- there is no out-of-band mechanism). It is never logged:
    :meth:`safe_argv` is the logger's view, and the worker logs the summary. The
    argv's visibility via ``ps``/``/proc/<pid>/cmdline`` is a known residual and a
    VPS hardening item (restricted process visibility), not something this module
    can remove.
    """

    __slots__ = ("argv", "env", "summary", "_secrets")

    def __init__(self, argv: list[str], env: dict[str, str], summary: str,
                 secrets: tuple[str, ...] = ()) -> None:
        self.argv = argv
        self.env = env
        self.summary = summary
        self._secrets = tuple(s for s in secrets if s)

    def __repr__(self) -> str:  # never let a repr leak the argv
        return f"<IngestCommand {self.summary}>"

    def safe_argv(self) -> list[str]:
        """The argv with every known secret replaced, for logging or diagnostics.

        Every occurrence of a resolved credential or the stream key is replaced,
        so neither can reach a log line even though both are in the real argv.
        """
        out: list[str] = []
        for arg in self.argv:
            for secret in self._secrets:
                if secret and secret in arg:
                    arg = arg.replace(secret, "<redacted>")
            out.append(arg)
        return out

    def scrub(self, text: str) -> str:
        """Remove every known secret from arbitrary text (e.g. a stderr tail)."""
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, "<redacted>")
        return text


def redact_url(url: str) -> str:
    """``scheme://host:port`` with path, query, userinfo and fragment removed.

    An RTSP path can itself be a credential (some vendors put a token in it), so
    the path is dropped, not masked. Credentials in the userinfo are dropped with
    it. Returns ``<unparseable>`` rather than echoing the input on a parse error.
    """
    try:
        parts = urlsplit(str(url or ""))
        scheme, host = parts.scheme, parts.hostname
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<unparseable>"
    if not scheme or not host:
        return "<redacted>"
    return f"{scheme}://{host}{port}"


def _rtsp_transport(url: str) -> str:
    """Force TCP interleave for RTSP. UDP is the common cause of silent stalls."""
    return "tcp"


def _with_credentials(url: str, username: str | None,
                      password: str | None) -> str:
    """The URL with a resolved credential injected into its userinfo, encoded.

    Credentials are held in separate fields precisely so they can be attached
    here, at the last moment, and never sit in the stored/compared ``url``.
    Percent-encoding matters: an ``@`` or ``:`` in a password would otherwise
    reshape the URL and could send the request somewhere unintended.
    """
    if not username:
        return url
    try:
        from urllib.parse import quote
        parts = urlsplit(url)
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{userinfo}@{host}{port}"
        return parts._replace(netloc=netloc).geturl()
    except ValueError:
        raise CommandBuildError("source_scheme")


def _stream_key_of(url: str) -> str | None:
    """The stream key from an RTMPS ingest URL (the last path segment), if any.

    YouTube's ingest URL is ``rtmps://a.rtmps.youtube.com/live2/<KEY>``. The key
    is returned only so the command can redact it from logs; it is never stored in
    an :class:`IngestCommand` attribute of its own, so no accessor can surface it.
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    path = (parts.path or "").strip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


def build_command(camera_id: str, source: dict, *,
                  output: str = OUTPUT_RTMPS,
                  ingest_url: str | None = None,
                  ffmpeg_bin: str | None = None) -> IngestCommand:
    """Build the video-only ingest command for one camera, or raise.

    ``ingest_url`` is the server-side RTMPS destination (its stream key is
    resolved by the caller from a secret reference). It is a parameter so tests
    can pass a placeholder; production resolves it from the private store. When it
    is absent the command is refused -- a stream with no destination would only
    waste a camera connection.
    """
    if not isinstance(source, dict) or not source.get("url"):
        raise CommandBuildError("source_absent")

    url = str(source["url"])
    try:
        parts = urlsplit(url)
    except ValueError:
        raise CommandBuildError("source_scheme")
    scheme = (parts.scheme or "").lower()
    if scheme not in cams._SOURCE_SCHEMES or not parts.hostname:
        raise CommandBuildError("source_scheme")

    # The last allowlist check before a process is launched. A host that is not on
    # a *configured* allowlist is refused; with no allowlist configured this
    # matches the existing runtime rule (any public host) rather than inventing a
    # stricter one here.
    allowed = cams._allowed_hosts()
    if allowed and parts.hostname.lower() not in allowed:
        raise CommandBuildError("host_not_allowlisted")

    # Credentials embedded in the URL are a leak waiting to happen; the private
    # store rejects them, and so does the builder in case one slips through.
    if parts.username or parts.password:
        raise CommandBuildError("source_scheme")

    if source.get("audio") is True:
        raise CommandBuildError("audio_not_allowed")

    if output != OUTPUT_RTMPS or not ingest_url:
        raise CommandBuildError("no_output")

    secret_ref = source.get("secret_ref")
    password = cams.resolve_secret(source) if secret_ref else (
        source.get("password") or None)
    username = source.get("username") or None
    # A named username with nothing resolvable behind it is a misconfiguration:
    # fail closed rather than let FFmpeg prompt or connect anonymously.
    if username and not password:
        raise CommandBuildError("credential_missing")

    input_url = _with_credentials(url, username, password)

    argv = [
        ffmpeg_bin or "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-rtsp_transport", _rtsp_transport(url),
        # A bounded connect/read so a dead camera cannot pin a worker slot.
        "-rw_timeout", "15000000",
        "-i", input_url,
        # Video only, twice over: -an drops any audio, and the explicit map takes
        # exactly the first video stream and nothing else.
        "-an",
        "-map", "0:v:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        # A single live output. No `-f segment`, no tee, no file path: nothing here
        # can record to disk.
        "-f", "flv",
        ingest_url,
    ]

    # A minimal environment, not the web process's own. A media child has no
    # reason to see WX_ADMIN_TOKEN, the Stripe keys or anything else this process
    # holds: inheriting them would widen a compromised demuxer's reach for free.
    # Only the variables a media tool genuinely needs are passed through.
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    secrets = tuple(s for s in (password, _stream_key_of(ingest_url)) if s)
    summary = (f"{argv[0]} in={redact_url(url)} out={redact_url(ingest_url)} "
               f"video-only")
    return IngestCommand(argv, env, summary, secrets)


def resolved_source(camera_id: str) -> dict:
    """The trusted private source for a camera, or raise ``source_absent``.

    The only door to a source in this module. It calls the registry, so no caller
    can supply a URL -- which is the property the whole ingest path rests on.
    """
    src = cams.source_for(camera_id)
    if src is None:
        raise CommandBuildError("source_absent")
    return src

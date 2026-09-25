"""Camera onboarding lifecycle: ``disabled -> configured -> tested -> enabled``.

This is the operator-facing state of a camera *as a source*, kept strictly apart
from the public projection in :mod:`cameras`. Three properties are deliberate:

* **Admin-side only.** Nothing here reaches ``/api/cameras``. The public
  ``status`` field keeps its existing meaning (is a still configured?), and this
  module adds a separate ``lifecycle`` name so the two can never be confused.
* **Derived, never stored.** The state is computed from the trusted config on
  every call. There is no table to drift out of sync and no migration.
* **Sanitised.** A failing check yields a *category* from a closed vocabulary,
  never a URL, host, credential or exception text. An operator can act on
  ``host_not_allowlisted`` without ever being shown what the host is.

The four states
---------------
``disabled``    ``enabled: false`` -- the operator switched the camera off.
``configured``  declared (or enabled) but not yet fully validated: either no
                private source is attached yet, or a check failed.
``tested``      a private source is attached and every applicable check passed.
``enabled``     ``tested`` *and* live is switched on with a valid public id, i.e.
                the camera is activated. This is the gate the M3 stream worker
                will require before it will be asked to start.

The distinction from the existing ``enabled`` boolean is intentional and is what
keeps this a non-breaking addition: ``enabled`` stays the simple public on/off
switch it is today; ``lifecycle`` is the richer, private readiness model.
"""
from __future__ import annotations

import logging

import cameras as cams

log = logging.getLogger("wx.camera_lifecycle")

STATES = ("disabled", "configured", "tested", "enabled")

# A failing check reports one of these and nothing else. Closed on purpose: the
# operator UI must not be able to render attacker-influenced config text, and a
# reason that is not in this set is dropped rather than passed through.
REASONS = frozenset({
    "source_absent",
    "source_scheme",
    "host_not_allowlisted",
    "credential_missing",
    "audio_not_allowed",
    "live_provider_invalid",
})


def _check(name: str, ok: bool, reason: str | None = None) -> dict:
    """One check result, with the reason laundered through the closed set."""
    return {"name": name, "ok": bool(ok),
            "reason": None if ok else (reason if reason in REASONS else None)}


def _checks(cam: dict, source: dict | None,
            declared: dict | None = None) -> list[dict]:
    """Every check that applies to this camera, in evaluation order.

    Checks that cannot apply (nothing to check when no source is declared) are
    simply not emitted, so ``all(ok)`` is meaningful without sentinel entries.

    ``declared`` is the *unvalidated* source entry. It matters when a source was
    declared but rejected by the parser: without it the checks would see only
    ``source is None`` and report ``source_absent``, which is misleading (and, for
    an operator, unfixable). With it the failing rule is named instead.
    """
    checks: list[dict] = []
    has_source = source is not None
    if has_source or declared is None:
        checks.append(_check("source_present", has_source,
                             None if has_source else "source_absent"))
    if not has_source:
        if declared is not None:
            checks.extend(_reject_checks(declared))
        return checks

    url = str(source.get("url") or "")
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    checks.append(_check("source_scheme", scheme in cams._SOURCE_SCHEMES,
                         "source_scheme"))

    allowed = cams._allowed_hosts()
    host = _host_of(url)
    # When an allowlist is configured the host must be on it. When none is
    # configured this check passes, matching the existing runtime behaviour
    # (which permits any public host) rather than inventing a stricter rule here.
    checks.append(_check("host_allowlisted",
                         (not allowed) or (host is not None and host in allowed),
                         "host_not_allowlisted"))

    # A secret reference that cannot be resolved is a misconfiguration: the
    # camera will fail at connect time, so it must not pass as "tested".
    ref = source.get("secret_ref")
    if ref:
        checks.append(_check("credential_resolvable",
                             cams.resolve_secret(source) is not None,
                             "credential_missing"))
    else:
        # An inline password or an anonymous source both count as "nothing
        # missing"; the runtime decides whether the camera accepts it.
        checks.append(_check("credential_resolvable", True))

    checks.append(_check("video_only", source.get("audio") is not True,
                         "audio_not_allowed"))

    if cam.get("live_enabled"):
        checks.append(_check("live_provider", cam.get("live") is not None,
                             "live_provider_invalid"))
    return checks


def _reject_checks(declared: dict) -> list[dict]:
    """The checks that explain a declared-but-rejected source, by named rule.

    Mirrors the rules :func:`cameras._validate_source` applies, in the same order,
    but reports which one failed. Only the rule name leaves this function; the
    URL, host and credentials are read and discarded.
    """
    url = str(declared.get("url") or "")
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
    except ValueError:
        return [_check("source_scheme", False, "source_scheme")]
    scheme = (parts.scheme or "").lower()
    if scheme not in cams._SOURCE_SCHEMES or not parts.hostname:
        return [_check("source_scheme", False, "source_scheme")]
    if parts.username or parts.password:
        return [_check("source_scheme", False, "source_scheme")]
    allowed = cams._allowed_hosts()
    host = (parts.hostname or "").lower()
    if allowed and host not in allowed:
        return [_check("host_allowlisted", False, "host_not_allowlisted")]
    if declared.get("audio"):
        return [_check("video_only", False, "audio_not_allowed")]
    # Rejected for a reason not in the named set (an unparseable port). The
    # source is still not usable, so a generic rule-failure is reported.
    return [_check("source_present", False, "source_absent")]


def _host_of(url: str) -> str | None:
    """The lower-cased host of a source URL, or None. Never logged or returned."""
    try:
        from urllib.parse import urlsplit
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def _raw_camera(camera_id: str) -> dict | None:
    """The sanitized camera entry regardless of ``enabled``, plus its live block.

    Deliberately *not* :func:`cameras.find_camera`, which hides disabled cameras:
    the whole point of the lifecycle view is that an operator can see a camera
    that is switched off and why it is not startable.

    Also deliberately not :func:`cameras.public_camera`: that projection is built
    from ``_PUBLIC_KEYS`` and therefore drops the internal ``enabled`` and
    ``live_enabled`` flags this module needs. The dict from
    :func:`cameras.load_config` is already sanitized (private source material is
    read only through ``source_for``), so nothing private is exposed by using it
    here.
    """
    wanted = str(camera_id or "").strip()
    if not wanted:
        return None
    for cam in cams.load_config():
        if cam.get("id") == wanted:
            out = dict(cam)
            out["live"] = cams._public_live(cam)
            return out
    return None


def lifecycle_of(camera_id: str) -> dict | None:
    """The lifecycle block for one camera, or None when the id is unknown.

    Unlike :func:`cameras.find_camera`, a *disabled* camera still produces a
    block (state ``disabled``) -- the admin view must be able to see it.

    Fail-safe: any unexpected problem degrades to ``configured`` rather than
    raising, so a bad config can never 500 the admin surface. The returned dict
    carries only state names, check names and reason categories.
    """
    cam = _raw_camera(camera_id)
    if cam is None:
        return None
    if not cam.get("enabled", True):
        # A disabled camera is off before any source is considered; reporting its
        # checks would invite an operator to "fix" a camera that is intentionally
        # switched off.
        return {"id": camera_id, "state": "disabled", "checks": [], "reason": None}

    state, checks = _evaluate(cam, camera_id)
    first_fail = next((c["reason"] for c in checks if not c["ok"]), None)
    return {"id": camera_id, "state": state, "checks": checks,
            "reason": first_fail}


def _classify(cam: dict, source: dict | None,
              declared: dict | None) -> tuple[str, list[dict]]:
    """The state and checks for one camera, given its already-resolved source.

    The single place the lifecycle decision is made; both :func:`lifecycle_of`
    and :func:`summary` funnel through it, so the two can never disagree.
    """
    checks = _checks(cam, source, declared)
    if source is None or not all(c["ok"] for c in checks):
        return "configured", checks
    if cam.get("live_enabled") and cam.get("live") is not None:
        return "enabled", checks
    return "tested", checks


def _evaluate(cam: dict, camera_id: str) -> tuple[str, list[dict]]:
    """Resolve the private source for one camera, then classify it."""
    try:
        source = cams.source_for(camera_id)
    except Exception:  # pragma: no cover - defensive; cameras.source_for does not raise
        log.warning("camera lifecycle: source lookup failed for %s", camera_id)
        source = None
    declared = None if source is not None else cams.declared_source(camera_id)
    return _classify(cam, source, declared)


def is_startable(camera_id: str) -> tuple[bool, str | None]:
    """Whether the M3 stream worker may be asked to start for this camera.

    The single gate the control plane consults. ``enabled`` implies ``tested``,
    so a camera can only be started once it is wired *and* activated.
    """
    life = lifecycle_of(camera_id)
    if life is None:
        return False, "unknown_camera"
    if life["state"] == "enabled":
        return True, None
    return False, "not_ready"


def summary() -> dict:
    """Counts per state for /api/health and the admin list. No identifiers.

    Reads the camera config and the private source store *once* each, then
    classifies every entry from those two dicts. Going through
    :func:`lifecycle_of` per id would re-read and re-parse both stores for every
    camera (O(n^2)); the classification itself is the same code (:func:`_classify`),
    so the counts are identical to the per-camera answer.
    """
    counts = {s: 0 for s in STATES}
    cameras = cams.load_config()
    try:
        sources = cams.all_sources()
    except Exception:  # pragma: no cover - defensive; all_sources does not raise
        log.warning("camera lifecycle: source store unavailable for summary")
        sources = {}
    for cam in cameras:
        camera_id = str(cam.get("id", ""))
        if not cam.get("enabled", True):
            counts["disabled"] += 1
            continue
        source = sources.get(camera_id)
        declared = None if source is not None else cams.declared_source(camera_id)
        state, _ = _classify({**cam, "live": cams._public_live(cam)},
                             source, declared)
        counts[state] += 1
    return counts

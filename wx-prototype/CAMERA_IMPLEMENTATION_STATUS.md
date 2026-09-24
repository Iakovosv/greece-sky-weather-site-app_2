# Camera Implementation Status — Handoff

Technical handoff for the **camera infrastructure foundation**. It records the
state exactly as merged on the `camera-infrastructure` branch, so the next
session can resume from GitHub without losing context.

This document is descriptive only. It changes no implementation.

---

## 1. Current branch / baseline

| Item | Value |
|---|---|
| Branch | `camera-infrastructure` |
| Current commit | `98a254a20031edc54cdc2ac36d66df37a7f7271d` (`98a254a`) |
| Commit subject | `Add secure camera infrastructure foundation` |
| Parent / production baseline | `d0c088576d7233309c216b72746cd2102abf58cd` (`d0c0885`) |
| Remote | `origin/camera-infrastructure` = `98a254a` |
| `production-hardening` | `d0c0885` — **unchanged** |
| `origin/production-hardening` | `d0c0885` — **unchanged** |

`98a254a` is a direct descendant of `d0c0885`. The camera work is fully isolated
on `camera-infrastructure`; `production-hardening` was not touched and no merge
has been performed.

---

## 2. What has been implemented

- **Central camera registry** — `cameras.py` is the single place where cameras
  are defined, parsed and published. No camera configuration is scattered across
  frontend and backend.
- **Public/private camera data separation** — public metadata is built from an
  explicit whitelist (`_PUBLIC_KEYS`); private source material lives in a
  separate store and is never merged into a camera dict or payload.
- **Server-side camera source secrets** — a private source store
  (`WX_CAMERA_SOURCES`) holds the RTSP/HTTP URL, username, password reference and
  a `secret_ref` naming the env var that carries the actual password.
- **Host allowlist** — optional `WX_CAMERA_ALLOWED_HOSTS` restricts which hosts a
  private source may name.
- **SSRF-safe architecture** — no endpoint accepts a URL; the server never
  fetches a caller-influenced address on the camera path.
- **No arbitrary client-supplied RTSP/source URL** — the client cannot supply or
  influence a source URL, host, port or scheme.
- **No credentials in public payloads or logs** — responses, the rendered page,
  the health block and log lines never carry a source URL, userinfo, host or
  credential. Logs use a redacted URL form (host kept, userinfo/query dropped).
- **Camera enabled/disabled handling** — `enabled: false` omits a camera from
  both the list and the detail route; unknown and disabled ids are
  indistinguishable (both 404).
- **Snapshot metadata/configuration** — `snapshot_interval_min` is validated
  against a fixed menu (1/2/5/10/15/30/60, default 5).
- **YouTube-only live provider abstraction** — `LIVE_PROVIDERS = ("youtube",)`,
  whitelisted; an unsupported provider yields no live block.
- **YouTube Live ID validation** — shape-bounded (6–24 chars, alphanumeric plus
  `_` and `-`); a malformed id yields no live block.
- **Embedded YouTube player** — official iframe on `youtube-nocookie.com` with
  the public video id; privacy-enhanced mode; the camera's own address never
  reaches the browser.
- **Explicit-click LIVE activation** — no autoplay on page load; playback starts
  only from the user's LIVE click, and closing restores the last snapshot.
- **Video-only requirement** — camera streams carry no audio.
- **Server-side rejection of audio sources** — a source declaring `audio: true`
  is rejected rather than muted downstream. Frontend `mute=1` is a default, not
  the guarantee.
- **No streaming infrastructure installed** — no WebRTC, HLS, MediaMTX, FFmpeg,
  RTMP or TURN. None is required for this foundation.
- **No real camera credentials, IPs or live IDs configured** — only placeholders
  (commented) in `.env.example` and synthetic fixtures inside tests.

---

## 3. Files introduced/changed in `98a254a`

| File | Role |
|---|---|
| `wx-prototype/cameras.py` | Camera registry: public model + whitelist, provider validation, URL safety, private source store, secret resolution, health counts. |
| `wx-prototype/app.py` | `GET /api/cameras`, `GET /api/cameras/{id}`, camera health block, camera UI (snapshot cadence line, guarded LIVE button, YouTube embed, close), camera CSS. |
| `wx-prototype/analytics.py` | One new allowlisted event: `sky_camera_live_opened`. |
| `wx-prototype/.env.example` | Commented placeholders for the camera env vars (`WX_CAMERAS`, `WX_CAMERA_SOURCES`, `WX_CAMERA_ALLOWED_HOSTS`); no active values. |
| `wx-prototype/README.md` | Camera configuration documentation (public/private split, snapshot mode, live provider, future RTSP→YouTube plan). |
| `wx-prototype/tests/test_camera_security.py` | New security test suite for the camera boundary (33 tests). |
| `wx-prototype/tests/test_verify_and_cameras.py` | Updated one camera test to the valid-URL contract; added one snapshot-scheme test. |

---

## 4. Tests

- **Full suite:** `632 passed, 185 warnings`.
- **Camera security tests:** `tests/test_camera_security.py` — 33 test functions.
- **M1 regression tests** (same file, 4 tests): `_redact_url` survives a
  non-numeric port, survives an out-of-range port, a rejected source with a bad
  port leaks nothing to the log, and `/api/health` stays HTTP 200 with a
  malformed camera source.
- **`git diff --check`:** clean.
- **Falsifiability:** the M1 tests fail against the pre-fix code and pass against
  the fix.

What the core security tests cover:

- credentials absent from the public payload, the rendered page, the health block
  and the logs;
- the private source is reachable only through the pipeline accessor, never via a
  route;
- a private key placed in camera config cannot ride along into a response;
- the public YouTube id is served while the private source stays private;
- non-source schemes, embedded userinfo and non-allowlisted hosts are refused;
- audio sources are refused; accepted sources are marked video-only;
- no endpoint accepts a URL or can be asked to fetch; hostile ids / path
  traversal return 404;
- unknown and disabled cameras are refused, in the list and in the detail route;
- a missing secret resolves to `None`; malformed blobs degrade safely;
- the FREE/PRO gate is untouched; a forged token still grants nothing;
- unsupported providers, missing ids, malformed YouTube ids and client-supplied
  live parameters cannot create a live block;
- the snapshot interval is whitelisted;
- the health camera block carries counts and no credentials.

---

## 5. Security guarantees currently implemented

Each of the following has been exercised against the committed code:

- **Credential leakage** — checked across `/api/cameras`,
  `/api/cameras/{id}`, `/api/health`, the rendered page and log output; none.
- **IDOR / path traversal** — `../health`, encoded traversal, null byte,
  `__proto__`, `constructor`, a metadata-IP path and an encoded traversal all
  return 404; ids are looked up in server-side config and never used to build a
  URL.
- **Client injection of provider/source parameters** — query flags such as
  `live_provider`, `url` and `live` are ignored; the server-side configuration is
  the only source of truth; no injected value is reflected.
- **SSRF exposure** — structurally absent: no endpoint accepts a URL and the
  camera module imports only `json`, `logging`, `os`, `time` and `urlsplit`.
- **Disabled / unknown camera access** — the same 404 answer for both; neither
  can reach the private source store.
- **Public/private data separation** — the public payload is built from an
  explicit whitelist, not a dict spread, so a new private key cannot leak.
- **Malformed ports / health endpoint robustness** — `_redact_url` no longer
  raises on a bad port (M1 fix); `/api/health` stays 200 with a malformed source.
- **YouTube ID validation** — shape-bounded; malformed ids yield no live block.
- **Server-side audio rejection** — `audio: true` is refused at the source layer.

---

## 6. Known open items — DO NOT FIX NOW

**M2 — MEDIUM**

- custom app controls currently overlay the YouTube iframe/player

`L6`–`L11` — LOW (documented, not fixed):

- **L6** — `_validate_source` reads `.scheme`/`.hostname` outside the `try`
  guard (`cameras.py`); not reachable today, consistency only.
- **L7** — the card's "Αυτόματη εικόνα: κάθε N λεπτά" text reflects the feed's
  snapshot cadence, while the browser reload stays at a fixed 60 s
  (`refresh_seconds=60`). `snapshot_interval_min` is metadata only; no
  per-camera polling exists.
- **L8** — `enabled` accepts only a literal `False`; the string `"false"` leaves
  a camera enabled.
- **L9** — a snapshot URL carrying embedded userinfo is passed to `<img src>`.
- **L10** — the health camera block is counts-only; a malformed source blob and
  an unset variable both read as `sources: 0`.
- **L11** — camera config is re-parsed from the environment per request;
  negligible cost, accepted.

---

## 7. Not yet built (future, deliberately out of scope)

- No server-side camera fetch, no RTSP proxy, no streaming infrastructure.
- No real camera onboarding or runtime.
- The future `Hikvision RTSP → server-side stream layer → YouTube Live` pipeline
  is architectural only; it is described in `README.md` but not implemented. It
  would require a separate VPS decision and a video-only (`-an`) stream service.

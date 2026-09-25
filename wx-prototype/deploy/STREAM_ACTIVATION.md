# Stream activation — VPS runbook (M3-B)

This is the operator runbook for turning the camera **ingest** path from "code
ready, inert" into "running on the VPS". It is deliberately a checklist of
*documented* steps, because none of them can be validated from a development
machine: they need a real FFmpeg, a real camera, a real ingest key and a real
systemd host. Nothing here is executed by the repository or by tests.

> Scope reminder: the ingest path is **admin-only and off by default**. With
> `WX_STREAM_ENABLED` unset (or `0`) nothing here runs, and enabling it never
> happens as a side effect of a deploy.

---

## 1. What is already true (no action)

- `WX_STREAM_BACKEND` defaults to `mock`; an unknown value fails closed.
- With `WX_STREAM_BACKEND=real` and no `WX_CAMERAS` private source, every start is
  refused (`source_absent` / `not_ready`) — enabling the backend alone starts
  nothing.
- The RTSP credential and the RTMPS stream key are **never on the command line**:
  they are written to 0600 files under `WX_STREAM_SECRET_DIR` and FFmpeg reads
  them with its documented `-/i` / `-/rtmp_playpath` "argument from file" form.
  `/proc/<pid>/cmdline` therefore exposes only paths.
- The secret files are removed on stop, on worker shutdown, and on every
  supervisor terminal path (crash-loop exhausted, disabled, cancelled).

## 2. Prerequisites to install (once)

| Item | Purpose | Command (Debian/Ubuntu) |
|---|---|---|
| FFmpeg | the ingest child (re-encode to H.264) | `apt-get install -y ffmpeg` |
| Python venv + deps | the app itself | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |

Verify: `.venv/bin/python -c "import shutil; print(shutil.which('ffmpeg'))"`
must print a path, and `ffmpeg -version` must succeed.

### 2b. Pre-flight: does *your* FFmpeg honour "argument from file"?

The credential control relies on FFmpeg's documented `-/<opt> <file>` form (an
option whose argument is read from a file). It is documented behaviour, but it is
version-sensitive and cannot be exercised from a development machine without
FFmpeg, so confirm it on the host **before** activating:

```bash
# A tiny value file, no newline (the slurp is verbatim):
printf '%s' 'file:/etc/hostname' > /tmp/preflight.txt
ffmpeg -hide_banner -loglevel error -/i /tmp/preflight.txt -f null - < /dev/null
```

- If FFmpeg reads the URL from the file, this exits quickly with a decode error
  about the "file" input (expected: `/etc/hostname` is not media) or with no
  error � either way it *opened the file as the input URL*, which is the point.
- If instead it prints `Unrecognized option 'i'` / `Option not found`, this build
  does not support the file form. Do **not** enable the stream backend on that
  build: the worker would still refuse to build a command (it never falls back to
  putting the credential in argv), so the failure is safe, but no stream would
  start until FFmpeg is upgraded.

Delete `/tmp/preflight.txt` afterwards.

## 3. Environment (in `/etc/<name>/env`, mode 0600)

```ini
WX_ENV=production
WX_SECRET=<a long random value>            # prod startup fails without it
WX_ADMIN_TOKEN=<a long random value>       # protects the admin endpoints
WX_DB=/var/lib/<name>/wx.db
WX_CACHE_DIR=/var/lib/<name>/cache
WX_STREAM_SECRET_DIR=/var/lib/<name>/stream-secrets

# --- the ingest path, still OFF until the last step ---
WX_STREAM_ENABLED=0
WX_STREAM_BACKEND=mock
WX_CAMERA_INGEST_REF=WX_CAMERA_INGEST_URL
WX_CAMERA_INGEST_URL=rtmps://<ingest-host>/live2/<STREAM_KEY>

# private camera source (never in WX_CAMERAS)
WX_CAMERA_SOURCES=[{"id":"ilioupoli","url":"rtsp://<cam-host>:554/Streaming/Channels/101","username":"<user>","secret_ref":"WX_CAMERA_ILIOUPOLI_PASS"}]
WX_CAMERA_ALLOWED_HOSTS=<cam-host>
WX_CAMERA_ILIOUPOLI_PASS=<camera password>
```

Rules that are not optional:

- `WX_CAMERA_SOURCES`, the camera password and `WX_CAMERA_INGEST_URL` **never**
  appear in `WX_CAMERAS`, in a response, or in a log.
- Credentials are never embedded in a URL (`rtsp://user:pw@host` is rejected);
  the password goes in its own env var via `secret_ref`.
- `WX_CAMERA_ALLOWED_HOSTS` should be set in production so a typo in the source
  cannot point the server at an arbitrary host.

## 4. Filesystem

```bash
install -d -m 0700 -o <WX_USER> -g <WX_GROUP> /var/lib/<name>/stream-secrets
install -d -m 0750 -o <WX_USER> -g <WX_GROUP> /var/lib/<name>/cache
```

The app re-applies `0700` to `WX_STREAM_SECRET_DIR` itself, and refuses a
symlinked directory. It must be local disk — not NFS/SMB, not `/tmp` shared with
other users (`PrivateTmp=yes` in the unit covers `/tmp`).

## 5. systemd

Install the unit from `deploy/wx-stream.service.example`, replacing every
`<PLACEHOLDER>`, then:

```bash
systemctl daemon-reload
systemctl enable --now wx-stream-<name>
systemctl status wx-stream-<name>
systemd-analyze security wx-stream-<name>     # wants a low exposure score
```

The unit already sets `ProtectProc=invisible` and `ProcSubset=pid`. These reduce
the unit's view of *other* processes; they do **not** hide the unit's own child
argv from local users. The argv would only be sensitive if a credential were in
it, and §1 is why it is not — do not treat `ProtectProc` as the credential
control.

## 6. Activation — in this order

1. **Health first.** `curl -s https://<host>/api/health | python3 -m json.tool`
   Look at `streams`: `enabled`, `backend`, and `ready`. `ready` is the whole
   activation suite (backend is `real`, FFmpeg present, secret dir usable, an
   ingest destination resolvable). It is `{"ready": false, "reason": "..."}`
   until every prerequisite is met.
2. **Flake without live.** Set `WX_STREAM_BACKEND=real` and
   `WX_STREAM_ENABLED=1`, restart, and re-check `/api/health`. `ready.ready`
   must be `true` before any start is attempted. If it is false, `reason` names
   the missing piece (`ffmpeg_missing`, `secret_dir_unusable`,
   `no_ingest_destination`).
3. **One camera, admin-only.** `POST /api/admin/streams/ilioupoli/start` with the
   admin header. Expect `{"ok": true, ...}`. Confirm with
   `GET /api/admin/streams` (`observed: live`) and on the ingest side (YouTube
   Studio shows the stream as live).
4. **Prove argv hygiene on the host.** While the stream runs:
   `tr '\0' ' ' < /proc/<ffmpeg-pid>/cmdline` must show `-/i /var/lib/...` and
   `-/rtmp_playpath /var/lib/...` and **no** rtsp userinfo and **no** stream key.
   This is the single most important post-activation check.
5. **Prove cleanup.** `systemctl stop` the unit, then confirm
   `/var/lib/<name>/stream-secrets/` is empty.
6. **Only then** consider a public LIVE surface. That is a *separate* milestone:
   today `start/stop` are admin-only and there is no public endpoint.

## 7. Rollback

Set `WX_STREAM_ENABLED=0` and restart. The control plane refuses every start
before any worker is consulted, and `live_status` returns `running: false`. The
camera snapshots and the forecast are unaffected — the two paths share no state.

## 8. What is *not* covered here

- Real FFmpeg execution against a real camera (needs credentials + a camera).
- A real RTMPS publish (needs a stream key).
- `systemd-analyze security` on the actual host (needs the installed unit).
- Any change to `FREE=72h` / `PRO=240h` — the ingest path does not touch
  entitlements, and neither does this runbook.

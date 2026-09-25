# Project Handoff — Greece Sky and Weather

Technical handoff for the **PRO Ensemble Forecasts** milestone. It records the
state exactly as pushed on the `camera-infrastructure` branch, so the next
session can resume from GitHub without losing context.

This document is descriptive only. It changes no implementation and contains no
secrets, tokens or credential values.

Sibling document: `CAMERA_IMPLEMENTATION_STATUS.md` covers the earlier camera
infrastructure work on the same branch.

---

## 1. Current branch / baseline

| Item | Value |
|---|---|
| Branch | `camera-infrastructure` |
| Current commit | `da8cdf78fd986366d835c80f865fc95b3670048b` (`da8cdf7`) |
| Commit subject | `fix: stop a comment from closing the inline script tag` |
| Previous milestone commit | `4fd4906` — `feat: add pro ensemble forecast summary` |
| Prior commit | `898116a` — `feat: harden camera ingest secret handling` (M3-B) |
| Production baseline | `d0c088576d7233309c216b72746cd2102abf58cd` (`d0c0885`) |
| Remote | `origin/camera-infrastructure` = `da8cdf7` |
| `origin/main` | `5f40288d1f47f2bc3bc1eb302137689ea6d2a1ec` — **unchanged** |
| `production-hardening` / `origin/production-hardening` | `d0c0885` — **unchanged** |

**Working tree is clean.** `HEAD` and `origin/camera-infrastructure` are
synchronized (`ahead/behind = 0 0`). All pushes were fast-forward; no merge,
rebase, amend or force push was performed.

`da8cdf7` is a direct descendant of `4fd4906`, which descends from `898116a` and
`d0c0885`. The camera and ensemble work is fully isolated on
`camera-infrastructure`; `production-hardening` and `main` were not touched.

---

## 2. Completed milestones on this branch

Listed oldest → newest (camera work precedes the ensemble work):

- **Camera infrastructure foundation** — secure server-side camera snapshots.
- **Camera UI mock flow**, public experience, lifecycle and stream control plane,
  ingest worker, secret handling — see `CAMERA_IMPLEMENTATION_STATUS.md`.
- **PRO Ensemble Forecasts** (`4fd4906`) — the milestone this handoff is about.
- **Inline-script fix** (`da8cdf7`) — shipping blocker; see section 6.

---

## 3. What was added — PRO Ensemble Forecasts

New / changed files in `4fd4906` (7 files, +873 / −4):

| File | Change |
|---|---|
| `wx-prototype/ensemble.py` | **new** — GEFS mean/spread fetch, cache, decode |
| `wx-prototype/tests/test_ensemble.py` | **new** — 28 tests, fully offline |
| `wx-prototype/cachestore.py` | +29 — `get_stale()` bounded stale-read |
| `wx-prototype/app.py` | +36 / −4 — import, PRO-gated task, `/api/brief` wiring, UI line |
| `wx-prototype/README.md` | +35 / −1 — dataset/epistemology section |
| `wx-prototype/LICENSES.md` | +1 — GEFS licence entry |
| `wx-prototype/AGENTS.md` | +1 — `ensemble.py` row |

### Server side

- `ensemble.gefs_ensemble_point(client, lat, lon, step=24, run=None)` returns
  `t2m_mean_c`, `t2m_spread_c`, `members` (= 30), `run`, `step`; `{}` when
  neither field is retrievable.
- `app.ensemble_task()` in `/api/brief` is **PRO-only**: it returns `{}` *before*
  any network call when `not entl.is_pro`, so a FREE caller triggers no upstream
  request and writes nothing to the ensemble cache.
- Fetched fields are additive: if NOMADS does not answer, the existing 3-model
  «Συμφωνία μοντέλων» card stands in unchanged and the forecast still succeeds.
  Exceptions inside the task are caught and never fail the request.

### UI

The ensemble figure is one extra `<p class="note">` line rendered **inside the
existing «Συμφωνία μοντέλων» verdict block** in the «Εξειδικευμένα» (Expert) tab.
It reads, e.g.:

> **Διασπορά GEFS (30 μελών): ±0.3°C** — …

No separate card, no new section, no layout change. The line is omitted entirely
when `expert.ensemble` is absent.

---

## 4. How the GEFS ensemble works

- **Source:** NOAA/NWS GEFS, through the NOMADS grib filter
  (`nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p50a.pl`), subset server-side
  to a Greece-sized box (18–30 E, 34–42 N) so a step costs a few KB instead of
  ~470 MB for the member files.
- **Fields:** precomputed `geavg` (ensemble mean) and `gespr` (ensemble spread),
  both 2 m temperature, `pgrb2a`, 0.5°. Variable is `var_TMP` /
  `lev_2_m_above_ground`.
- **Member count is 30, not 31.** Verified from the GRIB metadata
  (`GRIB_dataType="pf"`, `GRIB_totalNumber=30`) and NCEP documentation, which
  states `geavg`/`gespr` are generated **only from the perturbed members** — the
  control run is not included. This is why the constant is
  `GEFS_PERTURBED_MEMBERS = 30`, not the 31 the full system runs.
- **`gespr` is a spread "similar to standard deviation"**, per NCEP's own
  definition. It is **not the same statistic** as the 3-model max-minus-min
  range, so the two are deliberately **not** compared and the ensemble figure is
  **not** graded high/moderate/low.
- **No probabilities, no percentiles, no per-member values, no confidence or
  reliability score.** Percentiles cannot be recovered from mean+spread, and
  presenting them would pass off a narrower quantity as the full distribution.
  The module returns a number, its unit, its member count and a plain-language
  explanation that it is agreement between members, never the probability that a
  forecast verifies.
- **Run discovery:** `gefs_latest_run()` probes the `geavg` object for candidate
  cycles (same pattern as `wx._probe_latest_gfs_run`), memoised. A bucket-listing
  approach was tried first and **does not work** — see section 6.
- **Licence:** NOAA/NWS products are US public domain (17 USC §105), usable for
  any lawful purpose; no endorsement implied, no NWS material presented as
  official. Same `LICENSES.md` entry as GFS. **No paid data source is used.**

### Caching / concurrency

- Cache key is `provider|kind|run|step|variable` — **never the coordinate** — so
  every PRO user asking about any Greek location on the same run shares one
  download. TTL `ENSEMBLE_TTL_S = 6 h`, no longer than the run cadence.
- Concurrent cold requests are collapsed by the existing `app._single_flight`
  helper, which wraps the whole ensemble request (tested: 3 concurrent callers →
  1 upstream fetch).
- **Stale fallback:** `cachestore.get_stale(key, max_age)` returns a cached field
  past its TTL but within `ENSEMBLE_MAX_STALE_S = 24 h`. Used **only** after a
  fresh fetch has already failed (e.g. run rollout), and it shares `get`'s
  corruption tolerance.
- Temp GRIB files are per-process and removed in `finally`, so a crash cannot
  leave a half-written file that a later read decodes as garbage.

---

## 5. Tests and last results

- **Full suite: 992 passed, 0 failed** (325 warnings) — collected 992 across 43
  test files.
- **`tests/test_ensemble.py`: 28 passed** (~6 s, fully offline).
- **Page/UI subset** (`-k "page or html or index or script or smoke or ui"`):
  **114 passed**.
- `python -m py_compile` clean; `git diff --check` clean.

`tests/test_ensemble.py` covers, with no network: mean/spread in Celsius; member
count is 30; partial/undecodable data yields an honest result rather than a
fabricated one; a missing half leaves the other half `None`; both missing →
`{}`; the wording is asserted to contain no `%`, "confidence", "probability",
"reliability" or grade label; requests are a Greece-sized subregion and the point
falls inside it; the cache key excludes the coordinate (a nearby point refetches
nothing); a second identical request hits the cache; the TTL is ≤ the cadence;
stale is used only when a fresh fetch fails and is refused past max-age; a
truncated cache entry is discarded; single-flight collapses concurrent callers;
no secret/internal host is embedded; the PRO payload carries the ensemble block;
a FREE caller makes zero ensemble requests and writes no cache; and an upstream
failure leaves the rest of the brief intact.

Entitlement/forecast invariants are unchanged and still asserted:
`FREE_HOURS = 72`, `PRO_HOURS = 240`.

### Real end-to-end check (not a mock)

Run once against live NOMADS with the real `cfgrib` decode, before the milestone
commit:

- `gefs_latest_run()` resolved run `2026092506`.
- `geavg.t06z.pgrb2a.0p50.f024` and `gespr…f024` both returned HTTP 200.
- Real decode produced `mean = 18.35 °C`, `spread = 0.3 °C`.
- Both fields carried `dataType=pf`, `totalNumber=30`.
- Cold call = 2 upstream requests; **warm call = 0 upstream requests**, identical
  payload (cache proven by absence of a request).
- Leak scan over the payload and the module's real logs: none.

---

## 6. The inline-script fix (`da8cdf7`) — and why it was necessary

**Symptom:** on the live site, the whole Expert tab did nothing when a location
was selected, and a large block of raw JavaScript text was printed at the bottom
of the page.

**Cause:** the page ships one big inline `<script>` block. Inside a **JS comment**
in `app.py`, the source contained the literal characters `</script>` while
explaining the `jsq()` escaping rule. The HTML parser closes a `<script>` at the
first `</script` it sees, so the script element ended ~1.2 KB into a ~80 KB
block; everything after it was parsed as page text and no handler ever ran.

**Fix:** one comment-only change in `wx-prototype/app.py` — the phrase
`` `<` so no `</script>` can form `` became
`` `<` so no script-closing sequence can form ``. No functional, aesthetic or
structural change; 2 insertions, 2 deletions, one file.

**Verification:** after the fix the inline `<script>` runs to the real closing tag
(102,370 bytes, immediately before `</body>`), instead of ~1.2 KB.

**Provenance:** the bad comment predates this milestone — it was introduced by
`eb0bd469` and is present in `898116a`. It was **not** introduced by the ensemble
commit; the ensemble hunks in `app.py` are at lines 48, 2777, 4530 and 4615,
nowhere near the offending comment.

---

## 7. Confirmed by manual testing on the site

After the fix, a human verified on the live site:

- location selection works;
- PRO mode works;
- the «Εξειδικευμένα» tab works;
- «Συμφωνία μοντέλων» renders;
- **«Διασπορά GEFS (30 μελών): ±0.3°C»** appears, in the correct place;
- the rest of the Expert tab content renders.

Conclusion: the ensemble appears correctly and in the intended location. No
further UI/code change was requested at this point.

---

## 8. What has NOT changed

Untouched in this milestone, and to be treated as invariants:

- `main` and `production-hardening` branches.
- `entitlements.py`, `billing.py`, `promo.py`, `verify.py`, `bias.py`,
  `legal.py`, `cameras.py`, `stream_control.py`, `config.py`, `wx.py` — zero diff.
- FREE / PRO forecast limits (`FREE_HOURS = 72`, `PRO_HOURS = 240`).
- Existing forecast logic and weather calculations.
- Camera and stream work (covered by its own handoff document).
- Dependencies: no change to `requirements.txt` / project metadata.
- `WX_STREAM_ENABLED` remains **off** by default.

---

## 9. Open items to remember

### Security — must be revoked

An **exposed GitHub PAT** was pasted into the chat during this work and is also
present in plaintext in the local `origin` remote URL. It **must be revoked and
rotated**: revoke the token, issue a new one, and update the git remote. No token
value is recorded here, and none should ever be committed.

### Cosmetic — stale "31-member" comments

Two comments still say "31-member" while the correct figure is 30 perturbed
members (the code, payload, UI, README and LICENSES all correctly say 30):

- `wx-prototype/app.py:4540` — `# GEFS mean/spread: a genuinely 31-member spread…`
- `wx-prototype/AGENTS.md:19` — `| ensemble.py | GEFS 31-member mean/spread… |`

These are **comment/doc text only** and affect nothing at runtime. Deferred.

### Deferred by explicit decision — not touched

- `gePPpt` (GEFS bias-corrected percentile product) — a separate dataset; would
  be the only sound way to offer a qualitative ensemble signal.
- Live observations (METAR / SYNOP) and model-vs-observation verification.
- Any new provider.
- Persisted ensemble forecast history (e.g. how the spread moved across runs).

---

## 10. Next steps

### Already complete — do not redo

- ✅ Camera infrastructure (see `CAMERA_IMPLEMENTATION_STATUS.md`).
- ✅ PRO Ensemble Forecasts: server side, PRO gating, caching, single-flight,
  stale fallback, UI line, licence entry, tests, real E2E check.
- ✅ Inline-script shipping blocker fixed and manually confirmed on the site.

### Discussed for the future — not started, not approved

> These are ideas raised while working on the ensemble. **None of them is
> implemented**, and none should be begun without an explicit request.

1. **`gePPpt` percentiles** — for a qualitative ensemble signal, if the numeric
   spread proves useful in practice.
2. **Live observations / model-vs-observation verification** — METAR/SYNOP or
   similar, to extend `verify.py` beyond ERA5.
3. **Ensemble forecast history** — persisted spread across model runs.
4. **Cosmetic cleanup** — the two stale "31-member" comments in section 9.

### Immediate next action

Use the application as-is for a while and judge whether the Ensemble, and the
Expert tab overall, are actually useful. **No development is planned until that
real-world feedback is evaluated.**

As of this handoff: branch `camera-infrastructure`, HEAD `da8cdf7`,
working tree clean, synchronized with origin, production and main untouched.

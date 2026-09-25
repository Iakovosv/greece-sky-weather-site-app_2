# Project Handoff — Greece Sky and Weather

Technical handoff for the **PRO Ensemble Forecasts** milestone. It records the
state as pushed on the `camera-infrastructure` branch, so the next session can
resume from GitHub without losing context.

This document is descriptive only. It changes no implementation and contains no
secrets, tokens or credential values.

Sibling document: `CAMERA_IMPLEMENTATION_STATUS.md` covers the earlier camera
infrastructure work on the same branch.

---

## 1. Current branch / baseline

| Item | Value |
|---|---|
| Branch | `camera-infrastructure` |
| Current commit | recorded in the chat checkpoint at the end of this session |
| Previous commit | `1769de29fdad3c4b2613d8c3a70da11b44772817` (`1769de2`) |
| Earlier milestone commits | `4fd4906` (first ensemble summary), `da8cdf7` (inline-script fix), `898116a` (camera secret handling) |
| Production baseline | `d0c088576d7233309c216b72746cd2102abf58cd` (`d0c0885`) |
| `origin/main` | `5f40288d1f47f2bc3bc1eb302137689ea6d2a1ec` — **unchanged** |
| `production-hardening` / `origin/production-hardening` | `d0c0885` — **unchanged** |

**Working tree is clean.** `HEAD` and `origin/camera-infrastructure` are
synchronized (`ahead/behind = 0 0`). All pushes were fast-forward; no merge,
rebase, amend or force push was performed.

Work is fully isolated on `camera-infrastructure`; `main` and
`production-hardening` were not touched. No production or VPS change was made.

---

## 2. Completed milestones on this branch

Listed oldest → newest (camera work precedes the ensemble work):

- **Camera infrastructure foundation** — secure server-side camera snapshots.
- **Camera UI mock flow**, public experience, lifecycle and stream control plane,
  ingest worker, secret handling — see `CAMERA_IMPLEMENTATION_STATUS.md`.
- **PRO Ensemble Forecasts, first pass** (`4fd4906`) — a single 2 m temperature
  mean/spread line inside the agreement block.
- **Inline-script fix** (`da8cdf7`) — shipping blocker; see §9.
- **Regression audit** (`1769de2`) — read-only; produced W1–W4, see §10.
- **PRO Ensemble Forecasts, completed** (this session) — multi-variable series in
  its own PRO section; see §3–§8.

---

## 3. What this session added — completed PRO Ensemble Forecasts

Changed files: **6** (`ensemble.py`, `app.py`, `tests/test_ensemble.py`,
`README.md`, `LICENSES.md`, `AGENTS.md`). No new file, no new dependency. The
`4fd4906` pass already created `ensemble.py`; this session reworked and extended
it.

| File | Change |
|---|---|
| `wx-prototype/ensemble.py` | Reworked: multi-variable `gefs_ensemble_series()` beside the behaviourally-unchanged 2 m `gefs_ensemble_point()`; gust path; unique temp-file decode; `describe_series()` |
| `wx-prototype/app.py` | Ensemble task now calls the series; new PRO section `x-ens` («Σύνολο GEFS») + FREE locked block; removed the old single line from the agreement block; `ensemble_opened` analytics event |
| `wx-prototype/tests/test_ensemble.py` | 28 existing tests retargeted to the new contract; +13 new series tests → **41 total** |
| `wx-prototype/README.md` | «Σύνολο GEFS» section rewritten (variables, step types, honesty rules) |
| `wx-prototype/LICENSES.md` | GEFS entry now names the 0.5° `pgrb2ap5` and 0.25° `pgrb2sp25` (gust) products |
| `wx-prototype/AGENTS.md` | `ensemble.py` row: "GEFS 30-member mean/spread: t2m point path + multi-variable PRO series" |

### Server side

- `ensemble.gefs_ensemble_series(client, lat, lon, leads=ENSEMBLE_LEADS, run=None)`
  returns `{"run", "members": 30, "leads": [...]}` — one row per lead.
- `ensemble.gefs_ensemble_point(client, lat, lon, step=24, run=None)` is kept
  **behaviourally identical** to before; it is the smaller public API and the
  original 28 tests still pass against it.
- `app.ensemble_task()` in `/api/brief` is **PRO-only**: it returns `{}` *before*
  any network call when `not entl.is_pro`, so a FREE caller triggers no upstream
  request and writes nothing to the ensemble cache.
- Every variable degrades independently: a gust outage does not remove
  temperature, wind, precipitation or cloud.
- Exceptions inside the task are caught and never fail the request; the 3-model
  «Συμφωνία μοντέλων» card always stands.

### UI

The ensemble is now its **own Expert-tab section** (`x-ens`, «Σύνολο GEFS»), not
a footnote under the 3-model agreement. Reason, stated in the section text: the
two answer different questions — agreement is how far three deterministic models
sit from each other; the ensemble is how far 30 perturbed members of the *same*
model sit from their own mean.

- One table row per lead; columns are value ± spread (or `—` when unavailable,
  never `0`).
- FREE tier sees a PRO locked block (`lockedBlock('ens', …)`), the same pattern
  as the other Expert sections.
- On failure the section renders as «μη διαθέσιμο» rather than disappearing.
- Opening it fires the `ensemble_opened` analytics event.

---

## 4. What data it supports today

All availability below was verified against **live** products (`.idx` listings
and GRIB metadata), not assumed.

| Variable | GEFS product | Level | Step type |
|---|---|---|---|
| 2 m temperature | `pgrb2ap5` (0.5°) | 2 m above ground | instantaneous |
| 10 m wind (U, V) | `pgrb2ap5` | 10 m above ground | instantaneous |
| total precipitation (APCP) | `pgrb2ap5` | surface | **6-hour accumulation** |
| total cloud cover (TCDC) | `pgrb2ap5` | entire atmosphere | **6-hour average** |
| **wind gust (GUST)** | **`pgrb2sp25` (0.25°)** | surface | instantaneous |

**Leads available in the panel:** `ENSEMBLE_LEADS = (24, 72, 120, 168, 240)` —
+24, +72, +120, +168, +240 h. All are multiples of 24, so the 6-hour
precipitation window is identical on every row, and every lead is ≤
`ent.PRO_HOURS` (240). The panel can show up to 5 rows.

### GEFS / 30 perturbed members

- `geavg` (mean) and `gespr` (spread) are prefetched products covering 30
  perturbed members. Both carry `GRIB_dataType="pf"` and `GRIB_totalNumber=30`.
- NCEP states they are generated **only from the perturbed members** — the
  control run is not included — so the count is **30, not 31**. The constant is
  `GEFS_PERTURBED_MEMBERS = 30` and the UI shows "30 διαταραγμένων μελών".
- Using the precomputed mean/spread is both cheaper (a few KB per step via the
  NOMADS grib filter, versus hundreds of MB for 30 member files) and identical in
  meaning to recomputing the spread ourselves.

### mean / spread — what the spread actually is

- `gespr` is labelled by NCEP **"ens std dev"**: the ensemble standard deviation
  about the ensemble mean, in the unit of the field. This is a standard
  deviation, and the UI says so explicitly.
- **It is not the same statistic as the 3-model figure.** The agreement card is a
  max-minus-min range across three deterministic models; the ensemble spread is a
  standard-deviation-like quantity over 30 members. The two are therefore
  **never** compared, and the ensemble is **not** graded high/moderate/low.
- **Wind spread is the vector magnitude** `hypot(spread_u, spread_v)`, in km/h —
  not a spread of wind speed. A wind that changes direction strongly can have a
  large vector spread with a small speed change. The UI labels it "διανυσματικού
  σφάλματος, όχι διασπορά ταχύτητας".
- **No probabilities, no percentiles, no per-member values, no confidence or
  reliability score.** Percentiles cannot be recovered from mean+spread, and
  presenting them would pass off a narrower quantity as the full distribution.
- Documented in the module docstring and in `describe_series()`; pinned by a test
  that fails if the panel text contains `%`, "confidence", "probability",
  "reliability", «πιθανότητ», «αξιοπιστία», or a grade label.

### APCP — 6-hour accumulation and how aggregation is handled

- GEFS APCP is a **6-hour accumulation with mixed/overlapping windows**. Confirmed
  from live `.idx` files: f024 = `18-24 hour acc fcst`, f030 = `24-30 hour`,
  f048 = `42-48 hour`, f072 = `66-72 hour` — i.e. regular 6-hour windows at steps
  that are multiples of 6.
- **Naive summation across steps would double-count** (f018 covers 12–18 h while
  f024 covers 18–24 h, etc.).
- The design therefore avoids summation entirely: every reported lead is a
  multiple of 6, so each row shows **one** 6-hour accumulation window, and the
  column is labelled «Υετός 6h». Two leads are never silently added together.
- The GRIB itself does **not** expose the accumulation window (checked: the
  decoded `tp` coordinates carry only `step`, `surface`, `valid_time`, no
  `stepRange`); the 6-hour window is established from the `.idx` and has been
  checked on every lead used.
- `tp` is in `kg m**-2`, which equals mm.

### TCDC

- Total cloud cover is a **6-hour average**, in percent, on the entire-atmosphere
  level. Reported as-is with its spread, and the UI states it is a 6-hour average
  and is not aggregated across leads.

### Wind / gust and the differing resolution

- **Wind** (10 m U/V, mean and spread) comes from the 0.5° `pgrb2ap5` product in
  the same single request as temperature, precipitation and cloud.
- **Gust does not exist in the 0.5° product at all** — verified from the `.idx`
  of both `geavg` and `gespr` 0.5°, and from an HTTP 500 from the filter. It is
  therefore fetched from the **0.25° `pgrb2sp25`** product through its own filter
  endpoint (`filter_gefs_atmos_0p25s.pl`). The regional subset is tiny
  (~1.6 kB per field).
- Consequence: the gust column has a different native resolution from the other
  columns. This is a property of the GEFS products, not a choice, and it is
  documented here and in `LICENSES.md`.
- Gust speed is converted m/s → km/h; gust spread likewise.

### Cache / TTL / single-flight / stale fallback

- Cache key is `ensemble|gefs|{kind}|{run}|{step}|{mix|gust}` — **never the
  coordinate**. Every PRO user asking about any Greek location on the same run
  shares one download.
- The request box is `_GEFS_BOX = (18, 30, 34, 42)` (west, east, south, north) —
  wider than the Greek bbox on purpose, so a coastal or island point still has
  all four interpolation neighbours inside the request. One box serves all of
  Greece.
- Per lead the cost is **4 upstream GETs**: `geavg` + `gespr` on the 0.5° filter
  and `geavg` + `gespr` on the 0.25° gust filter — never one per variable. Leads
  run in sequence and the requests within a lead run concurrently, keeping the
  burst to a bounded width against NOMADS.
- TTL `ENSEMBLE_TTL_S = 6 h`, no longer than the run cadence.
- Concurrent cold requests collapse through the existing `app._single_flight`.
- **Stale fallback:** `cachestore.get_stale(key, max_age)` returns a cached field
  past its TTL but within `ENSEMBLE_MAX_STALE_S = 24 h`, used **only after a fresh
  fetch has already failed** (e.g. run rollout). It shares `get`'s corruption
  tolerance. A fresh success never reads the stale copy.
- **Temp GRIB files** are created with `tempfile.mkstemp` (unique name, so two
  parallel decodes cannot collide) in the cache dir, matching `wx.py`, and removed
  on exit. A test asserts no scratch file is left behind.
- Run discovery: `gefs_latest_run()` probes the `geavg` object for candidate
  cycles (same pattern as `wx._probe_latest_gfs_run`), memoised for
  `WX_RUN_LOOKUP_TTL_S`. A bucket listing does **not** work — see §9. A probe
  failure falls back to the 00Z cycle of the current date rather than raising.

---

## 5. Entitlement invariants (unchanged)

| Item | Value |
|---|---|
| FREE | `FREE_HOURS = 72` |
| PRO | `PRO_HOURS = 240` (10 days) |

**FREE = 0 upstream requests.** The ensemble task returns `{}` before any network
call when the caller is not PRO, so a FREE caller neither downloads nor caches
GEFS data and receives no ensemble block (the Expert tab is locked). Verified
live: a FREE `/api/brief` produced an empty ensemble-hit list.

`entitlements.py`, `billing.py`, `promo.py`, `verify.py`, `bias.py`, `legal.py`,
`cameras.py`, `stream_control.py`, `config.py`, `wx.py` — **zero diff**.

### Failure isolation

- Ensemble failures never fail `/api/brief`; the request still returns 200.
- Each variable degrades independently.
- Whole-panel failure renders «μη διαθέσιμο» inside the Expert tab only.
- Cache corruption: a truncated entry is discarded rather than decoded.
- A failed run probe falls back to a nominal cycle.

---

## 6. Tests and results

- **Full suite: 1005 passed, 0 failed** (325 warnings) — ~42 s.
- **`tests/test_ensemble.py`: 41 passed** (~8 s, fully offline) — 28 original + 13
  new series tests.
- `python -m py_compile app.py ensemble.py` clean.
- `git diff --check` clean.
- Inline page JS: `<script>` tags balanced (2/2) and `node --check` passes.

`tests/test_ensemble.py` covers, with no network: mean/spread in Celsius; member
count is 30; partial/undecodable data yields an honest result rather than a
fabricated one; a missing half leaves the other half `None`; both missing → `{}`;
the wording carries no `%`, "confidence", "probability", "reliability" or grade
label; requests are a Greece-sized subregion containing the point; the cache key
excludes the coordinate; a second identical request hits the cache; the TTL is ≤
the cadence; stale is used only when a fresh fetch fails and refused past
max-age; a truncated cache entry is discarded; single-flight collapses concurrent
callers; the PRO payload carries the ensemble block; a FREE caller makes zero
ensemble requests and writes no cache; an upstream failure leaves the rest of the
brief intact. The new series tests add: one row per lead with every variable; wind
spread is the vector magnitude (a spread-of-speed implementation would return 0
there); exactly 4 requests per lead; both GEFS filter hosts are hit and both
filters used; all variables and the Greek box are requested; the cache key is
run/step not point; a gust failure leaves the other variables; missing everything
returns no leads; no scratch file is left behind.

Entitlement/forecast invariants are asserted: `FREE_HOURS = 72`,
`PRO_HOURS = 240`.

### Real end-to-end checks (not mocks)

Run against live NOMADS with the real `cfgrib` decode:

1. **Module level** — run resolved to `2026092506`; three leads returned:
   - `+24h` t2m 18.4 °C ± 0.3, wind 9.3 km/h (from 25°), gust 11.9 km/h,
     precip 0.5 mm, cloud 60 %
   - `+72h` t2m 18.1 °C ± 0.48, wind 21.8 km/h, gust 31.0 km/h, cloud 1 %
   - `+120h` t2m 17.8 °C ± 0.65, wind 21.0 km/h, gust 32.0 km/h, cloud 10 %
   - Spread **increases with lead time**, the physically expected behaviour and a
     good sign the field is real rather than constant.
2. **Full `/api/brief` with a real PRO token** — status 200; `expert.ensemble`
   available with all 5 leads (24/72/120/168/240) and all columns; spread rose
   0.3 → 0.48 → 0.65 → 0.8 → 1.83 °C; `expert.agreement` still present;
   `tier.hours = 240`; panel text contains no `%`.
3. **Full `/api/brief` as FREE** — status 200; `tier.hours = 72`; expert locked;
   no ensemble key; **zero** ensemble upstream hits.

---

## 7. What was NOT implemented (and must not be assumed)

- No high/moderate/low or numeric "confidence" grade for the ensemble.
- No ensemble percentiles / probabilities (only `geavg`+`gespr` are used).
- No per-member data.
- No new data provider; no paid data source.
- No change to forecast logic, precipitation computation, or the deterministic
  GFS / ICON-EU / ECMWF paths.
- No change to entitlements, Stripe/billing, promo codes, verification, bias
  correction, analytics backend, cameras or streaming.
- No ensemble forecast-history persistence.
- No new dependency; `requirements.txt` untouched.
- W1 and W2 (see §10) were **not** fixed.

---

## 8. Remaining future work (not started, not approved)

> Ideas only. None is implemented, and none should begin without an explicit
> request.

1. **`gePPpt` percentiles** — GEFS bias-corrected percentile product; the only
   sound route to a qualitative ensemble signal. A separate dataset.
2. **Live observations / model-vs-observation verification** — METAR/SYNOP, to
   extend `verify.py` beyond ERA5.
3. **Persisted ensemble forecast history** — how the spread moved across runs.
4. **W1 fix** — see §10.
5. **W2 investigation** — see §10.

---

## 9. Earlier fixes still relevant

### The inline-script fix (`da8cdf7`)

**Symptom:** on the live site the Expert tab did nothing and a large block of raw
JavaScript printed at the bottom of the page.

**Cause:** the page ships one inline `<script>` block. Inside a **JS comment** in
`app.py`, the source contained the literal characters `</script>` while
explaining the `jsq()` escaping rule. The HTML parser closes a `<script>` at the
first `</script` it sees, so the script element ended ~1.2 KB into a ~80 KB block.

**Fix:** one comment-only change. Afterwards the inline `<script>` runs to the
real closing tag (102,370 bytes, immediately before `</body>`). The bad comment
predates the ensemble work (introduced by `eb0bd469`, present in `898116a`).

**Why it matters for this session:** `app.py` still ships that one inline script.
**Any new string added to the page JS must not contain a literal `</script`.**
The ensemble section added this session contains none, and tag balance is checked
(2/2).

### Run discovery

`gefs_latest_run()` probes the `geavg` object for candidate cycles. The NODD
bucket **listing** looks like the obvious route but does not work: with
`delimiter=/` it returns only the date level (`gefs.YYYYMMDD/`), not the cycle
hour. The run is therefore settled by whether the actual object exists.

---

## 10. W1–W4 from the previous regression audit

Recorded in substance from the `1769de2` read-only audit. **None was introduced
by the ensemble work.**

**W1 — UI says «Σύγκριση 3 μοντέλων» while the spread uses 2 models.**
*(pre-existing, baseline)*
- Point: `temperature_series_for_agreement(gfs_rows, icon, ec)` — the `ec`
  (ECMWF) parameter is **accepted but never used**. The series is
  `{"GFS", "ICON-EU"}`.
- Result: `agreement.models == 2`, while the FREE unlocks text says «Σύγκριση
  **3** μοντέλων». ECMWF is used in `model_grid`, not in the spread.
- Provenance: `c396fb9`, baseline; also present in `d0c0885`. **Not** introduced
  by the recent commits.
- Severity: low (2 is not a wrong value — it is honestly 2 models), but the copy
  promises 3. The `agreement()` comment also still says "Single source of truth
  for **reliability**", wording worth revisiting alongside the honesty rules added
  for the ensemble.
- **Not fixed this session.**

**W2 — `free(): invalid pointer` / `double free` → SIGABRT (exit 134) at process
shutdown.** *(environmental, baseline)*
- Point: Python teardown after any cfgrib decode (ICON/ECMWF/GFS/ensemble).
- Proof it is not ours: it reproduces with a bare
  `xr.open_dataset(..., engine="cfgrib")` on a DWD download, with **zero app
  code**. `wx.py` blob `ab951df…` is byte-identical across `d0c0885`, `898116a`
  and HEAD.
- Impact: in E2E the output was **complete and correct** before the abort — it
  happens at teardown.
- Severity: low for correctness, **worth attention for production**: heap
  corruption in the eccodes/C layer could affect restart or graceful shutdown of a
  uvicorn worker. Not investigated here.
- **Not fixed this session.**

**W3 — two stale "31-member" comments.** `app.py:4540` and `AGENTS.md:19` said
"31-member". Both were **corrected this session** (the ensemble task comment now
describes the series; the `AGENTS.md` row now says 30-member). No other stale
"31" remains in the ensemble path.

**W4 — exposed GitHub PAT.** See §12 — an action for the user. **Not resolved
this session.**

*(Self-correction from that audit: an apparent `/health` → 404 was a wrong path in
the audit script; the correct endpoint is `/api/health`, verified 200.)*

---

## 11. This session's commit

| Item | Value |
|---|---|
| Branch | `camera-infrastructure` |
| Commit | recorded in the chat checkpoint at the end of this session |
| Parent | `1769de2` |
| Contents | the 6 files in §3 plus this handoff |
| Remote | pushed to `origin/camera-infrastructure` (fast-forward only) |

No commit, push, merge, rebase, amend or force push touched `main`,
`production-hardening`, or production. No VPS change.

---

## 12. Security actions for the user

### Exposed GitHub PAT — must be revoked

A GitHub personal access token was pasted into the chat during this work, and an
earlier token is also present in plaintext in the local `origin` remote URL.
**Both must be revoked and rotated.** A token that has passed through chat
history must be treated as compromised regardless of scope.

1. Revoke the pasted token: GitHub → Settings → Developer settings → Personal
   access tokens → Revoke.
2. Issue a new token with the minimum scope needed.
3. Update the git remote so no token is stored in `.git/config` in plaintext;
   prefer a credential helper or an SSH remote over an embedded token URL.

No token value is recorded in this document, and none should ever be committed.

---

## 13. Environment notes

- The cfgrib/eccodes `free(): invalid pointer` abort at shutdown (W2) still
  appears after live decodes. It is environmental and pre-existing. A future live
  E2E script should treat a trailing `exit 134` as expected **only when the
  script's own output is complete**.
- `WX_STREAM_ENABLED` remains **off** by default.
- Cache dir default is `/tmp/wx-cache`; tests use their own `cache_dir` fixture.

---

## 14. Invariants — do not break

- FREE 72 h / PRO 240 h.
- `main` and `production-hardening` untouched.
- Production/VPS untouched.
- The single inline `<script>` block must never contain a literal `</script`
  (not even inside a comment).
- The ensemble is never presented as forecast reliability or confidence.
- No token, secret or credential in the repository.

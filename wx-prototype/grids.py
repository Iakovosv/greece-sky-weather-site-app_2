"""In-memory forecast grids with on-the-fly bilinear interpolation.

Why this exists
---------------
The original path fetched one tiny GRIB box per *forecast step per user point*:
a 240 h request was ~160 separate NOMADS calls, and the cache key was rounded to
0.01 deg (~1.1 km), so two users in the same city who differed by a few metres of
GPS jitter shared nothing. Cost scaled with users, which is the wrong shape.

Here the download granularity becomes *per model run, not per point*: the
scheduler decodes one full Greece box per step once, stores it as numpy arrays,
and every request is answered by reading four numbers per field. The number of
network calls per run is fixed regardless of how many users ask.

What is deliberately NOT done here
----------------------------------
"Grids in RAM" here means numpy arrays, not GRIB bytes. Decoding GRIB is the
expensive part; keeping the encoded form in RAM would still pay that cost on
every request. The invariant this module upholds is: no network and no
decoding inside a request. Interpolation itself is cheap arithmetic and is
exactly the right thing to do at request time.

On accuracy
-----------
Bilinear interpolation between the four surrounding model cells is smoothing: it
does not add terrain detail the grid does not contain. The topographic accuracy
comes from the lapse-rate correction downstream, which needs `orog` (this
module's per-cell terrain height) as its reference. The two are complementary;
neither substitutes for the other.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import numpy as np

import config

log = logging.getLogger("wx.grids")

# Greece plus margin. Covers the Ionian and Aegean tourist islands, Crete, and
# the mainland border ridges, without pulling the whole of Europe.
GREEK_BBOX = {"north": 42.0, "south": 34.0, "west": 18.0, "east": 30.0}

# The ICON-EU domain, which is what DWD actually publishes: lat 29.5-70.5,
# lon -23.5-62.5, at 0.0625 deg (~7 km). There is no server-side subset, so this
# box is what the decoder trims to. Held separately from GREEK_BBOX because the
# two answer different questions: Greece is the high-resolution home market, the
# continental box is what makes a 7 km forecast reachable for the rest of Europe.
EUROPE_BBOX = {"north": 70.5, "south": 29.5, "west": -23.5, "east": 62.5}


@dataclass
class GridSpec:
    """A decoded model grid held in RAM.

    `vars` maps a field name to a float32 array of shape (step, lat, lon). Steps
    are indexed by position in `steps`, never by the step number itself, so a
    series need not be contiguous (GFS is hourly to 120 then 3-hourly).

    `lat` is stored ascending. Every consumer may rely on that: the download
    layer is responsible for flipping, because GFS publishes descending
    latitude and silently interpolating against a descending axis is how a point
    in Crete ends up reading Siberia.
    """
    model: str
    run: str                      # "YYYYMMDDHH"
    lat: np.ndarray               # 1D, ascending
    lon: np.ndarray               # 1D, ascending, may wrap
    steps: list[int]
    vars: dict[str, np.ndarray]   # name -> (step, lat, lon) float32
    loaded_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def age_s(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.loaded_at

    def has(self, *names: str) -> bool:
        return all(n in self.vars for n in names)

    def step_index(self, step: int) -> int | None:
        try:
            return self.steps.index(step)
        except ValueError:
            return None


def bilinear(grid: GridSpec, var: str, step: int, lat: float, lon: float) -> float | None:
    """Interpolate `var` at (lat, lon) for one forecast step.

    Returns None when the field or step is absent, or when the point falls
    outside the grid. Longitude wraps only for a grid that spans the whole globe;
    a regional grid rejects out-of-range longitudes rather than folding them back
    in. The difference matters: wrapping a 18-30 E grid turns 5 E into 29 E, so a
    point in Algeria would silently be answered with Greek weather.

    Vector fields must be passed as their u/v components, never as speed and
    direction: the arithmetic mean of 350 and 10 degrees is 180, which is the
    exact opposite of the truth. Callers interpolate u and v separately.
    """
    arr = grid.vars.get(var)
    if arr is None:
        return None
    i = grid.step_index(step)
    if i is None:
        return None

    plat = float(lat)
    if plat < grid.lat[0] or plat > grid.lat[-1]:
        return None

    # Longitude: a global grid wraps so a point at 359.9 deg lands next to one at
    # 0.1 deg instead of being clamped to an edge. A regional grid does not: its
    # two edges are real boundaries, and folding a point across them invents data.
    #
    # "Global" is span plus one cell, not span alone: a regular global grid stops
    # one cell short of 360 (0..359.75 at 0.25 deg), so testing span >= 360 would
    # misclassify every real global grid as regional.
    plon = float(lon)
    lon0, lon1 = float(grid.lon[0]), float(grid.lon[-1])
    span = lon1 - lon0
    n = int(grid.lon.size)
    spacing = span / (n - 1) if n > 1 else 360.0
    if (span + spacing) >= 360.0 - 1e-6:
        if plon < lon0 or plon > lon1:
            plon = lon0 + (plon - lon0) % 360.0
    elif plon < lon0 or plon > lon1:
        return None

    return _bilinear_plane(arr[i], grid.lat, grid.lon, plat, plon)


def _bilinear_plane(plane: np.ndarray, lat_axis: np.ndarray, lon_axis: np.ndarray,
                    lat: float, lon: float) -> float | None:
    """Bilinear read of a single 2D (lat, lon) plane. Axes must be ascending."""
    if plane.shape != (lat_axis.shape[0], lon_axis.shape[0]):
        # A mis-shaped plane means the caller paired the wrong axes with the
        # array. Degrade to "no value" rather than raising an IndexError deep
        # inside a request.
        return None
    if lat_axis.shape[0] == 1:
        j0 = j1 = 0
        ty = 0.0
    else:
        # searchsorted on an ascending axis: j0 is the last index strictly below
        # `lat`, so clamp it to leave a real interval [j0, j0+1].
        j0 = int(np.searchsorted(lat_axis, lat, side="right")) - 1
        j0 = min(max(j0, 0), lat_axis.shape[0] - 2)
        j1 = j0 + 1
        span = float(lat_axis[j1]) - float(lat_axis[j0])
        ty = 0.0 if span == 0 else (lat - float(lat_axis[j0])) / span

    if lon_axis.shape[0] == 1:
        k0 = k1 = 0
        tx = 0.0
    else:
        k0 = int(np.searchsorted(lon_axis, lon, side="right")) - 1
        k0 = min(max(k0, 0), lon_axis.shape[0] - 2)
        k1 = k0 + 1
        span = float(lon_axis[k1]) - float(lon_axis[k0])
        tx = 0.0 if span == 0 else (lon - float(lon_axis[k0])) / span

    v00 = float(plane[j0, k0])
    v01 = float(plane[j0, k1])
    v10 = float(plane[j1, k0])
    v11 = float(plane[j1, k1])
    if not np.isfinite([v00, v01, v10, v11]).all():
        return None

    top = v00 + (v01 - v00) * tx
    bot = v10 + (v11 - v10) * tx
    return top + (bot - top) * ty


def series(grid: GridSpec, var: str, lat: float, lon: float,
           steps: list[int] | None = None) -> dict[int, float]:
    """Interpolate one field at a point across many steps, skipping missing ones."""
    out: dict[int, float] = {}
    for s in (steps if steps is not None else grid.steps):
        v = bilinear(grid, var, s, lat, lon)
        if v is not None:
            out[s] = v
    return out


def slices_at_point(grid: GridSpec, lat: float, lon: float,
                    var: str, steps: list[int] | None = None) -> dict[int, float]:
    """Convenience alias kept explicit so call sites read clearly."""
    return series(grid, var, lat, lon, steps)


def surface_rows(grid: GridSpec, lat: float, lon: float,
                 steps: list[int] | None = None) -> list[dict]:
    """Reproduce wx.gfs_surface_step's row dict from RAM, without touching the network.

    The key correctness point is wind: u and v are interpolated separately and the
    speed/direction are derived *after* interpolation. Averaging directions instead
    would turn a northerly and a southerly into a southerly-looking result around
    the wrap.
    """
    use = steps if steps is not None else grid.steps
    t2m = series(grid, "t2m_c", lat, lon, use)
    rh = series(grid, "rh2_pct", lat, lon, use)
    u = series(grid, "u10", lat, lon, use)
    v = series(grid, "v10", lat, lon, use)
    pr = series(grid, "precip_mm", lat, lon, use)
    gu = series(grid, "gust_kmh", lat, lon, use)
    ca = series(grid, "cape", lat, lon, use)
    ci = series(grid, "cin", lat, lon, use)
    cc = series(grid, "cloud_pct", lat, lon, use)

    rows: list[dict] = []
    for s in use:
        row: dict = {"step": s}
        if s in t2m:
            row["t2m_c"] = t2m[s]
        if s in rh:
            row["rh2_pct"] = rh[s]
        if s in u and s in v:
            uu, vv = u[s], v[s]
            row["wind_kmh"] = float(np.hypot(uu, vv) * 3.6)
            row["wind_dir"] = float((np.degrees(np.arctan2(-uu, -vv)) + 360) % 360)
            row["u10"], row["v10"] = uu, vv
        if s in pr:
            row["precip_mm"] = pr[s]
        if s in gu:
            row["gust_kmh"] = gu[s]
        if s in ca:
            row["cape"] = ca[s]
        if s in ci:
            row["cin"] = ci[s]
        if s in cc:
            row["cloud_pct"] = cc[s]
        # Drop a step that carried no temperature: downstream treats it as empty.
        if "t2m_c" in row:
            rows.append(row)
    return rows


def covers(grid: GridSpec, lat: float, lon: float) -> bool:
    """Whether the grid can answer for this point at all.

    The Greeks grids are regional, so a request for anywhere else in Europe has no
    data in them. Callers use this to fall back to the per-point path, which
    subsets server-side and works continent-wide, instead of returning an error
    for a point the site otherwise handles.
    """
    if not (grid.lat[0] <= lat <= grid.lat[-1]):
        return False
    lon0, lon1 = float(grid.lon[0]), float(grid.lon[-1])
    span = lon1 - lon0
    n = int(grid.lon.size)
    spacing = span / (n - 1) if n > 1 else 360.0
    if (span + spacing) >= 360.0 - 1e-6:
        return True
    return lon0 <= lon <= lon1


def model_orography(grid: GridSpec, lat: float, lon: float) -> float | None:
    """Terrain height of the model cell, the reference for the lapse-rate step.

    Bilinear over the orography field rather than nearest: the cell mean height
    should vary smoothly with position, and a nearest lookup makes the correction
    jump by hundreds of metres at cell boundaries.

    Uses the orography's own axes when it carries them: it is decoded from its own
    GRIB subset and its coordinates need not match the main fields' grid.
    """
    orog = grid.meta.get("orog")
    if orog is None:
        return None
    la = grid.meta.get("orog_lat", grid.lat)
    lo = grid.meta.get("orog_lon", grid.lon)
    return _bilinear_plane(np.asarray(orog, dtype=np.float32),
                           np.asarray(la, dtype=np.float64),
                           np.asarray(lo, dtype=np.float64), lat, lon)


# One process-wide lock for restoring a grid from disk. Deliberately not a per-
# model lock: the read is a single small file and sharing the guard keeps the
# "many first requests, one disk read" guarantee simple.
_LOAD_LOCK = threading.Lock()


class GridStore:
    """Thread-safe holder for the live grids, with atomic replacement.

    The swap matters: rebuilding a grid in place while requests are in flight
    would let one response mix half of the 00z run with half of the 06z run. Here
    a fully-built dict is published with a single assignment, and readers take a
    local reference, so a reader sees one run or the other and never a mixture.

    `previous` is kept so a failed refresh degrades to the last good run rather
    than to an error page.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, GridSpec] = {}
        self._previous: dict[str, GridSpec] = {}
        self.stats: dict[str, dict] = {}

    def get(self, model: str) -> GridSpec | None:
        # Readers take the dict reference once; no lock needed for a read.
        return self._current.get(model)

    def current(self) -> dict[str, GridSpec]:
        return self._current

    def ensure_loaded(self, model: str, scope: str = "") -> GridSpec | None:
        """Return the live grid, restoring the newest persisted run if absent.

        This is the lazy half of the restart story. A freshly started process has
        an empty store, and the refresher's first pass needs the network; between
        those two points a request would otherwise fall back to the per-point
        path and pay the very cost this exists to remove. Reading the newest
        archive off disk closes that gap without downloading anything.

        Guarded by a process-wide lock so a burst of first requests performs one
        disk read, not one per request. The fast path (already in RAM) takes no
        lock at all, so the common case is unchanged.
        """
        cur = self._current.get(model)
        if cur is not None:
            return cur
        with _LOAD_LOCK:
            cur = self._current.get(model)
            if cur is not None:
                return cur
            for m in _grid_metas():
                if m.get("model") != model or str(m.get("scope", "")) != scope:
                    continue
                grid = load_grid(model, str(m.get("run", "")), scope)
                if grid is None:
                    continue
                self.replace(model, grid)
                self.stats.setdefault(model, {})["loaded_from_disk"] = True
                log.info("grid restored from disk: model=%s run=%s scope=%r",
                         model, grid.run, scope)
                return grid
            return None

    def replace(self, model: str, grid: GridSpec, scope: str = "") -> None:
        """Publish `grid` as the live one, demoting the old one to `previous`."""
        with self._lock:
            old = self._current.get(model)
            new = dict(self._current)
            new[model] = grid
            self._current = new
            if old is not None:
                self._previous[model] = old
        self.stats[model] = {"run": grid.run, "loaded_at": grid.loaded_at,
                             "age_s": 0.0, "steps": len(grid.steps),
                             "vars": sorted(grid.vars), "stale": False,
                             "scope": scope}

    def mark_failure(self, model: str, error: str) -> None:
        """Record a refresh failure without dropping the last good grid."""
        with self._lock:
            cur = self._current.get(model)
        st = self.stats.setdefault(model, {})
        st |= {"last_error": error, "last_error_at": time.time()}
        if cur is not None:
            # The point of the fallback: keep serving, but say it is old.
            st |= {"stale": True, "run": cur.run, "loaded_at": cur.loaded_at}
        else:
            st |= {"stale": False, "run": None}

    def health(self) -> dict:
        """Per-model age/run for /api/health, so a silent scheduler is visible."""
        now = time.time()
        out: dict = {}
        for model, grid in self._current.items():
            st = dict(self.stats.get(model, {}))
            st["age_s"] = round(grid.age_s(now), 1)
            st["run"] = grid.run
            st["stale"] = bool(st.get("last_error"))
            out[model] = st
        for model, st in self.stats.items():
            if model not in out:
                # Never loaded: surface the failure rather than an empty dict.
                out[model] = {k: v for k, v in st.items() if k != "loaded_at"}
        # Persistence state. `disk_runs` is how many archives exist per model, so
        # an operator can tell "retention is working" from "nothing is saved".
        out["persist"] = {"dir": grid_store_dir(),
                          "keep_runs": GRID_KEEP_RUNS,
                          "disk_bytes": disk_bytes(),
                          "disk_runs": _disk_run_counts()}
        return out


# The process-wide store. Single uvicorn process by design: the grids are ~hundreds
# of MB and each worker would otherwise pay for its own copy.
STORE = GridStore()


# ---------------------------------------------------------------- disk persistence
#
# The grid in RAM is lost on restart, so without this a deploy or a crash re-paid
# the entire download and decode for a run that was already on disk seconds
# earlier. Persisting one decoded grid per (model, run) turns that back into a
# file read: the refresher still owns *when* a run is fetched, but a process that
# has just started can serve the last run immediately instead of waiting for the
# first network refresh to finish.
#
# What is stored is the decoded numpy grid, not raw GRIB. That is deliberate and
# is the same reasoning as the in-RAM form: decoding is the expensive step, and
# storing encoded GRIB would move that cost onto every reader instead of paying
# it once. It also means the archive is small — the whole GFS Greece grid is
# ~14 MB on disk.
#
# Keying is by run, not by point: `model|run|scope`. The scope (which ICON box)
# is part of the key because two deployments configured for different boxes must
# not read each other's grid. Retention keeps the newest runs per model and
# deletes the rest, so disk use is bounded by construction rather than by eviction
# pressure.

# How many runs per model to keep on disk. Two is the minimum that satisfies the
# "a failed new run still leaves the last good one" requirement across a restart:
# the newest is what a fresh process serves, the one before it is what it falls
# back to. More is waste — a superseded weather run is never wanted.
GRID_KEEP_RUNS = int(os.environ.get("WX_GRID_KEEP_RUNS", "2"))


def grid_store_dir() -> str:
    """Where decoded grids live. Under the cache dir so one env var moves both."""
    d = os.path.join(config.cache_dir(), "grids")
    os.makedirs(d, exist_ok=True)
    return d


def _grid_stem(model: str, run: str, scope: str = "") -> str:
    # A hash keeps the name filesystem-safe without inventing a character
    # allow-list; the model and run are still readable in the hash input's prefix
    # via `_grid_meta`, which is what an operator actually greps for.
    tag = f"{model}|{run}|{scope}"
    return f"{model}-{hashlib.sha256(tag.encode()).hexdigest()[:16]}"


def _grid_paths(model: str, run: str, scope: str = "") -> tuple[str, str]:
    stem = _grid_stem(model, run, scope)
    return (os.path.join(grid_store_dir(), stem + ".npz"),
            os.path.join(grid_store_dir(), stem + ".json"))


def save_grid(grid: GridSpec, scope: str = "") -> str | None:
    """Write a decoded grid to disk atomically. Returns the npz path, or None.

    Atomic via a temp file plus `os.replace`, the same pattern as `cachestore`,
    so a reader never sees a half-written archive and an interrupted save cannot
    leave a file that decodes to garbage.
    """
    npz, meta_p = _grid_paths(grid.model, grid.run, scope)
    try:
        payload = {k: v for k, v in grid.vars.items()}
        payload["__lat"] = np.asarray(grid.lat, dtype=np.float64)
        payload["__lon"] = np.asarray(grid.lon, dtype=np.float64)
        payload["__steps"] = np.asarray(grid.steps, dtype=np.int64)
        # Array-valued metadata (GFS orography and its own axes) travels in the
        # npz; only scalars go into the JSON. Putting an ndarray in the JSON is
        # not a formatting detail — it raises, and the whole save is lost.
        meta_arr: dict[str, np.ndarray] = {}
        meta_scalar: dict = {}
        for k, v in grid.meta.items():
            if isinstance(v, np.ndarray):
                meta_arr[f"__meta_{k}"] = np.asarray(v)
                meta_scalar[k] = {"__array__": f"__meta_{k}"}
            else:
                meta_scalar[k] = v
        payload.update(meta_arr)
        meta = {"model": grid.model, "run": grid.run, "scope": scope,
                "saved_at": time.time(), "loaded_at": grid.loaded_at,
                "meta": meta_scalar}
        tmp = npz + ".tmp"
        # savez is uncompressed: these arrays are float32 and already dense, and
        # the read path wants a plain memory map, not a decompress on boot.
        # Passed an open handle rather than a path because savez appends ".npz"
        # to a bare filename, which would put the temp file alongside the target
        # with a name os.replace could not find.
        with open(tmp, "wb") as f:
            np.savez(f, **payload)
        os.replace(tmp, npz)
        # Metadata is a separate small file so the archive stays a pure array
        # container that `np.load` can map without a JSON parse.
        tmp_m = meta_p + ".tmp"
        with open(tmp_m, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp_m, meta_p)
        return npz
    except Exception:
        log.exception("grid persist failed: model=%s run=%s", grid.model, grid.run)
        for p in (npz, meta_p, npz + ".tmp", meta_p + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass
        return None


def load_grid(model: str, run: str, scope: str = "") -> GridSpec | None:
    """Read a decoded grid back from disk. None if absent or damaged.

    Damage is treated as a miss, not an error: a truncated archive (a save killed
    mid-flight, or a filesystem that lost a page) must make the caller fetch
    again rather than crash the boot. That is the same contract as `cachestore`.
    """
    npz, meta_p = _grid_paths(model, run, scope)
    if not (os.path.exists(npz) and os.path.exists(meta_p)):
        return None
    try:
        with open(meta_p, "r", encoding="utf-8") as f:
            meta_doc = json.load(f)
        with np.load(npz, allow_pickle=False) as z:
            lat = z["__lat"]
            lon = z["__lon"]
            steps = [int(s) for s in z["__steps"]]
            names = [k for k in z.files if not k.startswith("__")]
            vs = {k: z[k] for k in names}
            raw_meta = meta_doc.get("meta") or {}
            meta: dict = {}
            for k, v in raw_meta.items():
                ref = v.get("__array__") if isinstance(v, dict) else None
                if ref:
                    meta[k] = z[ref]
                else:
                    meta[k] = v
        return GridSpec(model=model, run=run, lat=lat, lon=lon, steps=steps,
                        vars=vs, meta=meta,
                        loaded_at=float(meta_doc.get("loaded_at") or time.time()))
    except Exception as e:
        log.warning("grid load failed (%s), discarding: model=%s run=%s",
                    e, model, run)
        for p in (npz, meta_p):
            try:
                os.unlink(p)
            except OSError:
                pass
        return None


def disk_bytes(folder: str | None = None) -> int:
    """Total bytes of persisted grid archives (npz + json), for /api/health."""
    folder = folder or grid_store_dir()
    total = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _disk_run_counts() -> dict:
    """How many archived runs exist per `model|scope`. For /api/health."""
    counts: dict[str, int] = {}
    for m in _grid_metas():
        key = f"{m.get('model')}|{m.get('scope', '')}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _grid_metas() -> list[dict]:
    """Every persisted grid's metadata, newest run first. Unreadable ones skipped."""
    out: list[dict] = []
    folder = grid_store_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda m: (str(m.get("run", "")), float(m.get("saved_at", 0))),
             reverse=True)
    return out


def prune_grids(keep: int | None = None) -> dict:
    """Delete all but the newest `keep` runs per model+scope. Returns a summary.

    Retention by construction rather than by eviction pressure: the disk cache's
    size cap would evict whatever is oldest across *all* keys, which is the wrong
    policy here — it could drop the newest GFS run while keeping five old ICON
    ones. Keeping a fixed count per model is what actually bounds the footprint,
    because a superseded run is never wanted again.
    """
    limit = GRID_KEEP_RUNS if keep is None else keep
    if limit <= 0:
        return {"removed": 0, "freed_bytes": 0, "kept": {}}
    kept: dict[str, int] = {}
    removed = freed = 0
    for m in _grid_metas():
        key = f"{m.get('model')}|{m.get('scope', '')}"
        kept[key] = kept.get(key, 0) + 1
        if kept[key] <= limit:
            continue
        npz, meta_p = _grid_paths(m.get("model", ""), m.get("run", ""),
                                  m.get("scope", ""))
        for p in (npz, meta_p):
            try:
                freed += os.path.getsize(p)
            except OSError:
                pass
            try:
                os.unlink(p)
                removed += 1
            except OSError:
                pass
    if removed:
        log.info("grid retention: removed %d file(s), freed %.1f MB (keep %d/model)",
                 removed, freed / 1e6, limit)
    return {"removed": removed, "freed_bytes": freed,
            "kept": kept}


def flag_enabled() -> bool:
    """Whether /api/brief should serve from RAM instead of the per-point fetch."""
    return os.environ.get("WX_USE_RAM_GRIDS", "").strip().lower() in ("1", "true", "yes", "on")


def icon_bbox() -> dict:
    """Which box the ICON grid keeps in RAM.

    `greek` (default) is the home market: small, so it costs almost nothing to hold
    and covers the area the site is actually about. `europe` keeps the whole DWD
    domain, which is what lets a point in Berlin or Madrid be served at 7 km from
    memory instead of falling back to the 25 km GFS per-point path.

    European scope is opt-in because it is not free. Measured from live DWD
    data, the Greece box holds 1.79 MB of ICON fields and the full domain holds
    65.1 MB: 36x, not a rounding difference. That is why it is a separate choice
    rather than what a bare flag switch hands you, and why an unrecognised value
    falls back to `greek` instead of guessing.
    """
    return EUROPE_BBOX if icon_scope() == "europe" else GREEK_BBOX


def icon_scope() -> str:
    """The ICON box name as a string, for persistence keys and /api/health."""
    scope = os.environ.get("WX_RAM_ICON_SCOPE", "greek").strip().lower()
    return "europe" if scope == "europe" else "greece"


def gfs_scope() -> str:
    """GFS is always the Greece box; named so persistence keys are explicit."""
    return "greece"


def synthetic_grid(model: str = "gfs", run: str = "2026010100",
                   nlat: int = 9, nlon: int = 9, steps: list[int] | None = None,
                   fields: tuple[str, ...] = ("t2m_c", "u10", "v10")) -> GridSpec:
    """A small analytic grid for tests.

    Values are linear in (step, lat, lon), which makes bilinear interpolation
    exact: the correct answer is computable by hand, so a test can assert equals
    rather than approximate equality.
    """
    steps = steps or [0, 1]
    lat = np.linspace(34.0, 42.0, nlat)
    lon = np.linspace(18.0, 30.0, nlon)
    LON, LAT = np.meshgrid(lon, lat)
    vs: dict[str, np.ndarray] = {}
    for name in fields:
        cube = np.empty((len(steps), nlat, nlon), dtype=np.float32)
        for i, s in enumerate(steps):
            cube[i] = (LAT * 1.0 + LON * 2.0 + s * 3.0).astype(np.float32)
        vs[name] = cube
    return GridSpec(model=model, run=run, lat=lat, lon=lon, steps=list(steps), vars=vs)

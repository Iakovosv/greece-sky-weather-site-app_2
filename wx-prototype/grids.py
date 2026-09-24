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

import os
import threading
import time
from dataclasses import dataclass, field

import numpy as np

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

    def replace(self, model: str, grid: GridSpec) -> None:
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
                             "vars": sorted(grid.vars), "stale": False}

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
        return out


# The process-wide store. Single uvicorn process by design: the grids are ~hundreds
# of MB and each worker would otherwise pay for its own copy.
STORE = GridStore()


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
    scope = os.environ.get("WX_RAM_ICON_SCOPE", "greek").strip().lower()
    return EUROPE_BBOX if scope == "europe" else GREEK_BBOX


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

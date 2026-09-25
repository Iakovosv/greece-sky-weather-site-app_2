"""Disk cache with a size cap, eviction, and recovery from damaged entries.

The shape of the cache is unchanged from `wx.py`: a key hashed to a filename, a
TTL checked against mtime, and an atomic rename so a reader never sees a partial
write. What is added here is the part that was missing, and the omission was a
production bug rather than an oversight in polish:

* **No ceiling.** `cache_put` wrote until the disk filled. A single malformed
  request (`lat=999`) fetched a whole-global GRIB field and left a 419 MB file
  behind; six of them filled a gigabyte and a half.
* **No eviction.** The only lifetime was the TTL check on read, which never
  deletes anything. An entry that is never read again stays forever.
* **No recovery from a truncated entry.** A write killed mid-flight, or a
  filesystem that lost a page, leaves a short file that `xr.open_dataset` then
  fails on — with the failure presented as "the model is down", which is wrong
  and undiagnosable.

So this module is a small, self-contained, dependency-free cache that is safe to
point at a real disk. It is deliberately not a library: the whole thing is a few
dozen lines of straightforward file handling.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time

import config

log = logging.getLogger("wx.cache")

# A file this small is almost certainly a truncated write rather than a real
# payload. Used to reject a suspiciously short entry on read as well as to avoid
# rewriting an empty blob.
MIN_REAL_BYTES = 8


def cache_dir() -> str:
    d = config.cache_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _path(key: str) -> str:
    return os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:32])


def total_bytes(folder: str | None = None) -> int:
    """Size of the cache directory in bytes. Missing files count as zero."""
    folder = folder or cache_dir()
    total = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def stats() -> dict:
    """Files, bytes and the configured ceiling, for /api/health."""
    folder = cache_dir()
    limit = config.cache_max_bytes()
    count = 0
    used = 0
    oldest = None
    now = time.time()
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_file(follow_symlinks=False):
                    count += 1
                    used += st.st_size
                    age = now - st.st_mtime
                    oldest = age if oldest is None else max(oldest, age)
    except OSError:
        pass
    return {
        "dir": folder,
        "files": count,
        "bytes": used,
        "limit_bytes": limit or None,
        "oldest_age_s": None if oldest is None else round(oldest, 1),
    }


def get(key: str, ttl: int) -> bytes | None:
    """Cached bytes younger than `ttl`, or None.

    A damaged entry (unreadable, or shorter than `MIN_REAL_BYTES`) is removed and
    reported as a miss, so the caller regenerates instead of propagating a decode
    error that looks like a model outage.
    """
    p = _path(key)
    try:
        age = time.time() - os.path.getmtime(p)
        size = os.path.getsize(p)
    except OSError:
        return None
    if age >= ttl:
        return None
    if size < MIN_REAL_BYTES:
        log.warning("cache entry too small (%d B), discarding: key=%s", size, key)
        _unlink(p)
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError as e:
        log.warning("cache read failed (%s), discarding: key=%s", e, key)
        _unlink(p)
        return None


def get_stale(key: str, max_age: int) -> bytes | None:
    """Cached bytes that are past the TTL but younger than `max_age`, or None.

    A deliberate escape hatch for the case where the fresh fetch has *already*
    failed: an old but real field is better than an empty card during a model run
    rollout or a brief upstream outage. It shares `get`'s corruption tolerance, so
    a truncated entry is still discarded rather than decoded.
    """
    p = _path(key)
    try:
        age = time.time() - os.path.getmtime(p)
        size = os.path.getsize(p)
    except OSError:
        return None
    if age >= max_age:
        return None
    if size < MIN_REAL_BYTES:
        log.warning("stale cache entry too small (%d B), discarding: key=%s", size, key)
        _unlink(p)
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError as e:
        log.warning("stale cache read failed (%s), discarding: key=%s", e, key)
        _unlink(p)
        return None


def put(key: str, blob: bytes) -> None:
    """Write atomically, then enforce the size cap.

    The write goes to a per-process temporary file in the same directory and is
    renamed into place: a concurrent reader sees either the old file or the new
    one, never a half-written one. `os.replace` is atomic on POSIX and on NTFS.
    """
    if not blob or len(blob) < MIN_REAL_BYTES:
        return
    folder = cache_dir()
    p = _path(key)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, p)
    except OSError as e:
        log.warning("cache write failed (%s): key=%s", e, key)
        _unlink(tmp)
        return
    evict(folder)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def evict(folder: str | None = None, target: int | None = None) -> dict:
    """Delete oldest-first until the directory is at or below `target` bytes.

    Oldest-first (by mtime) rather than random or largest-first: the entries
    about to be recreated by the next refresh are the *newest*, and evicting by
    size would preferentially drop the multi-MB surface series that the RAM path
    and the per-point path both rely on. The moving part here is small: this runs
    only when a write has pushed the directory over the cap.
    """
    folder = folder or cache_dir()
    limit = config.cache_max_bytes() if target is None else target
    if not limit:
        return {"evicted": 0, "freed_bytes": 0}
    entries: list[tuple[float, int, str]] = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if e.is_file(follow_symlinks=False):
                    entries.append((st.st_mtime, st.st_size, e.path))
    except OSError:
        return {"evicted": 0, "freed_bytes": 0}

    total = sum(size for _m, size, _p in entries)
    if total <= limit:
        return {"evicted": 0, "freed_bytes": 0}

    entries.sort()  # oldest mtime first
    evicted = freed = 0
    for mtime, size, path in entries:
        if total <= limit:
            break
        _unlink(path)
        total -= size
        evicted += 1
        freed += size
    if evicted:
        log.info("cache eviction: removed %d file(s), freed %.1f MB (cap %.1f MB)",
                 evicted, freed / 1e6, limit / 1e6)
    return {"evicted": evicted, "freed_bytes": freed}

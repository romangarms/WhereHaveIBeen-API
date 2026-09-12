"""
Per-user track and heatmap cache behind /api/me/track and /api/me/heatmap.

Entries live under TRACK_CACHE_DIR/<username>/<key>.pkl with an index.json
per user. Open-ended entries (no `to`) keep the streaming DeviceTracker state
so a request only fetches points newer than latest_tst and unions the new
corridor pieces into the stored geometry. Closed entries are computed once and
expire after TRACK_CLOSED_TTL_SECONDS because late uploads can add points to
a past range.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import pickle
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import shapely
from shapely.geometry import mapping

import recorder
import track
from config import Config

log = logging.getLogger("track_cache")

CACHE_DIR = Config.TRACK_CACHE_DIR
CLOSED_TTL_SECONDS = Config.TRACK_CLOSED_TTL_SECONDS
MAX_ENTRIES_PER_USER = Config.TRACK_MAX_ENTRIES_PER_USER
INLINE_WINDOW_DAYS = Config.TRACK_INLINE_WINDOW_DAYS
MIN_REFRESH_SECONDS = Config.TRACK_MIN_REFRESH_SECONDS

FETCH_WINDOW_DAYS = 30
# Windows are fetched concurrently (I/O bound) and consumed in order.
FETCH_WORKERS = 4
# The recorder reads from/to as UTC but at minute granularity; fetch wider and
# bound by tst exactly.
FETCH_MARGIN = timedelta(days=1)
SIMPLIFY_M = 15.0
OUT_SIMPLIFY_M = 30.0
DEFAULT_BUFFER_M = 500
MIN_BUFFER_M, MAX_BUFFER_M = 100, 5000
RETRY_AFTER_SECONDS = 1
COORD_DECIMALS = 6

FLIGHT_PARAMS = track.DEFAULT_FLIGHT_PARAMS
PARAMS_FINGERPRINT = "|".join(str(x) for x in [
    "track-v2", SIMPLIFY_M, OUT_SIMPLIFY_M, track.HEATMAP_CELL_DEG, track.CHUNK_KM, track.DENSIFY_KM,
    FLIGHT_PARAMS.entry_kmh, FLIGHT_PARAMS.entry_alt_m, FLIGHT_PARAMS.entry_jump_km,
    FLIGHT_PARAMS.exit_kmh, FLIGHT_PARAMS.lookback_s, FLIGHT_PARAMS.stale_s,
    FLIGHT_PARAMS.acc_max_m, FLIGHT_PARAMS.min_dist_m,
])

_SAFE_USERNAME = re.compile(r'^[A-Za-z0-9-]{1,64}$')

_meta_lock = threading.Lock()
_key_locks = {}
_computing = {}
_errors = {}
_entries = {}
_indexes = {}


class Spec:
    """Identifies one cache entry."""

    def __init__(self, kind, username, devices, from_ts=None, to_ts=None, buffer_m=None):
        if not _SAFE_USERNAME.match(username):
            raise ValueError("username is not a safe path segment")
        self.kind = kind
        self.username = username
        self.devices = sorted(devices)
        self.from_ts = from_ts
        self.to_ts = to_ts
        self.buffer_m = buffer_m if kind == "track" else None
        raw = json.dumps([username, self.devices, self.buffer_m, from_ts, to_ts, kind,
                          PARAMS_FINGERPRINT])
        self.key = hashlib.sha256(raw.encode()).hexdigest()[:24]

    @property
    def open_ended(self):
        return self.to_ts is None

    @property
    def all_time(self):
        return self.from_ts is None and self.to_ts is None

    def index_row(self):
        return {"kind": self.kind, "devices": self.devices, "buffer_m": self.buffer_m,
                "from": self.from_ts, "to": self.to_ts}


class Entry:
    def __init__(self, spec):
        self.spec = spec
        self.created_at = time.time()
        self.computed_at = None
        self.latest_tst = None
        self.earliest_tst = None
        # In-memory only: when the recorder last confirmed there is nothing
        # newer, and the serialized response for the current content.
        self.checked_at = 0.0
        self.body = None
        self.progress = None
        self.trackers = {d: track.DeviceTracker(FLIGHT_PARAMS) for d in spec.devices}
        self.driving = None
        self.flights_buffer = None
        self.driving_area = 0.0
        self.flights_area = 0.0
        self.cells = {}

    def to_disk(self):
        return {
            "fingerprint": PARAMS_FINGERPRINT,
            "created_at": self.created_at,
            "computed_at": self.computed_at,
            "latest_tst": self.latest_tst,
            "earliest_tst": self.earliest_tst,
            "trackers": {d: t.to_state() for d, t in self.trackers.items()},
            "driving": shapely.to_wkb(self.driving) if self.driving is not None else None,
            "flights_buffer": (shapely.to_wkb(self.flights_buffer)
                               if self.flights_buffer is not None else None),
            "driving_area": self.driving_area,
            "flights_area": self.flights_area,
            "cells": self.cells,
        }

    @classmethod
    def from_disk(cls, spec, data):
        if data.get("fingerprint") != PARAMS_FINGERPRINT:
            return None
        e = cls(spec)
        e.created_at = data["created_at"]
        e.computed_at = data["computed_at"]
        e.latest_tst = data["latest_tst"]
        e.earliest_tst = data.get("earliest_tst")
        e.checked_at = e.computed_at or 0.0
        e.trackers = {d: track.DeviceTracker.from_state(s, FLIGHT_PARAMS)
                      for d, s in data["trackers"].items()}
        e.driving = shapely.from_wkb(data["driving"]) if data["driving"] else None
        e.flights_buffer = (shapely.from_wkb(data["flights_buffer"])
                            if data["flights_buffer"] else None)
        e.driving_area = data["driving_area"]
        e.flights_area = data["flights_area"]
        e.cells = data["cells"]
        return e


# ---------------------------------------------------------------------------
# Disk layout
# ---------------------------------------------------------------------------
def _user_dir(username):
    return os.path.join(CACHE_DIR, username)


def _entry_path(spec):
    return os.path.join(_user_dir(spec.username), spec.key + ".pkl")


def _index_path(username):
    return os.path.join(_user_dir(username), "index.json")


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _index(username):
    """Index dict for a user; loaded once and kept in memory. Call under _meta_lock."""
    idx = _indexes.get(username)
    if idx is None:
        try:
            with open(_index_path(username)) as fh:
                idx = json.load(fh)
        except FileNotFoundError:
            idx = {}
        except Exception as e:
            log.warning("track_cache: unreadable index for %s: %s", username, e)
            idx = {}
        _indexes[username] = idx
    return idx


def _write_index(username):
    _atomic_write(_index_path(username), json.dumps(_indexes[username]).encode())


def _touch(spec):
    with _meta_lock:
        row = _index(spec.username).get(spec.key)
        if row is not None:
            row["last_access"] = time.time()
            _write_index(spec.username)


def load_entry(spec):
    entry = _entries.get(spec.key)
    if entry is not None:
        return entry
    try:
        with open(_entry_path(spec), "rb") as fh:
            entry = Entry.from_disk(spec, pickle.load(fh))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("track_cache: unreadable entry %s: %s", spec.key, e)
        return None
    if entry is not None:
        _entries[spec.key] = entry
    return entry


def save_entry(entry):
    spec = entry.spec
    _atomic_write(_entry_path(spec), pickle.dumps(entry.to_disk(), protocol=pickle.HIGHEST_PROTOCOL))
    _entries[spec.key] = entry
    with _meta_lock:
        idx = _index(spec.username)
        row = idx.get(spec.key) or spec.index_row()
        row["last_access"] = time.time()
        row["computed_at"] = entry.computed_at
        idx[spec.key] = row
        _evict_locked(spec.username, keep=spec.key)
        _write_index(spec.username)


def delete_entry(spec):
    _entries.pop(spec.key, None)
    with _meta_lock:
        idx = _index(spec.username)
        if idx.pop(spec.key, None) is not None:
            _write_index(spec.username)
    try:
        os.remove(_entry_path(spec))
    except FileNotFoundError:
        pass


def _evict_locked(username, keep):
    idx = _index(username)
    while len(idx) > MAX_ENTRIES_PER_USER:
        candidates = [k for k in idx if k != keep]
        if not candidates:
            return
        smaller = [k for k in candidates
                   if not (idx[k]["from"] is None and idx[k]["to"] is None)]
        pool = smaller or candidates
        victim = min(pool, key=lambda k: idx[k].get("last_access", 0))
        idx.pop(victim)
        _entries.pop(victim, None)
        try:
            os.remove(os.path.join(_user_dir(username), victim + ".pkl"))
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------
def _dt(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def iso(ts):
    return _dt(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply(entry, inc):
    """Buffer new polylines into the stored geometry. Areas are recomputed
    once by the caller when the batch is complete."""
    changed = False
    if inc.driving_lines:
        polys = track.buffer_polylines(inc.driving_lines, entry.spec.buffer_m,
                                       SIMPLIFY_M, OUT_SIMPLIFY_M)
        entry.driving = track.merge_geometry(entry.driving, polys)
        changed = True
    if inc.flight_lines:
        polys = track.buffer_polylines(inc.flight_lines, entry.spec.buffer_m,
                                       SIMPLIFY_M, OUT_SIMPLIFY_M)
        entry.flights_buffer = track.merge_geometry(entry.flights_buffer, polys)
        changed = True
    return changed


def update(entry, now_ts):
    """Fetch every point newer than the entry knows about and fold it in."""
    spec = entry.spec
    t0 = time.time()
    upper = spec.to_ts if spec.to_ts is not None else now_ts
    if entry.latest_tst is not None:
        lower, lower_inclusive = entry.latest_tst, False
    else:
        lower, lower_inclusive = spec.from_ts, True
    user = spec.username.lower()
    latest = entry.latest_tst
    earliest = entry.earliest_tst
    fetched = 0
    changed = False

    plan = []
    for device in spec.devices:
        months = recorder.list_rec_months(user, device)
        if not months:
            continue
        start = recorder.month_start(*months[0])
        if lower is not None:
            start = max(start, _dt(lower))
        # .rec files are per month, so nothing exists past the last one.
        end = min(_dt(upper), recorder.month_start(*months[-1]) + timedelta(days=32))
        if start >= end:
            continue
        for ws, we in recorder.windows(start - FETCH_MARGIN, end + FETCH_MARGIN, FETCH_WINDOW_DAYS):
            plan.append((device, ws, we))

    entry.progress = {"stage": "fetching", "done": 0, "total": len(plan) + 1}
    try:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = [pool.submit(recorder.fetch_points, user, d, ws, we) for d, ws, we in plan]
            for (device, ws, we), future in zip(plan, futures):
                fixes = track.fixes_from_points(future.result())
                fixes = [f for f in fixes if f.tst <= upper and (
                    lower is None or f.tst > lower or (lower_inclusive and f.tst == lower))]
                if fixes:
                    fetched += len(fixes)
                    latest = fixes[-1].tst if latest is None else max(latest, fixes[-1].tst)
                    earliest = fixes[0].tst if earliest is None else min(earliest, fixes[0].tst)
                    if spec.kind == "track":
                        changed |= _apply(entry, entry.trackers[device].feed(fixes))
                    else:
                        track.add_heat_cells(fixes, entry.cells, FLIGHT_PARAMS.acc_max_m)
                entry.progress["done"] += 1

        entry.progress["stage"] = "building"
        if spec.kind == "track":
            if not spec.open_ended:
                for tracker in entry.trackers.values():
                    changed |= _apply(entry, tracker.flush())
            if changed or entry.computed_at is None:
                entry.driving_area = track.geometry_area_km2(entry.driving)
                entry.flights_area = track.geometry_area_km2(entry.flights_buffer)
    finally:
        entry.progress = None

    entry.latest_tst = latest
    entry.earliest_tst = earliest
    entry.computed_at = now_ts
    entry.checked_at = now_ts
    entry.body = None
    log.info("track_cache: %s %s/%s updated with %d points in %.1fs",
             spec.kind, spec.username, spec.key, fetched, time.time() - t0)


def _settled(entry):
    """Trackers with the pending tail applied, plus the geometry that adds."""
    if not entry.spec.open_ended:
        return entry.trackers, entry.driving, entry.driving_area, \
            entry.flights_buffer, entry.flights_area
    trackers = {}
    inc = track.Increment()
    for device, tracker in entry.trackers.items():
        piece, settled = tracker.preview()
        trackers[device] = settled
        inc.extend(piece)
    driving, driving_area = entry.driving, entry.driving_area
    flights, flights_area = entry.flights_buffer, entry.flights_area
    if inc.driving_lines:
        polys = track.buffer_polylines(inc.driving_lines, entry.spec.buffer_m,
                                       SIMPLIFY_M, OUT_SIMPLIFY_M)
        driving = track.merge_geometry(driving, polys)
        driving_area = track.geometry_area_km2(driving)
    if inc.flight_lines:
        polys = track.buffer_polylines(inc.flight_lines, entry.spec.buffer_m,
                                       SIMPLIFY_M, OUT_SIMPLIFY_M)
        flights = track.merge_geometry(flights, polys)
        flights_area = track.geometry_area_km2(flights)
    return trackers, driving, driving_area, flights, flights_area


def _feature(geom):
    if geom is None or geom.is_empty:
        return {"type": "Feature", "properties": {}, "geometry": None}
    rounded = shapely.transform(geom, lambda c: np.round(c, COORD_DECIMALS))
    return {"type": "Feature", "properties": {}, "geometry": mapping(rounded)}


def _range(spec, computed_at):
    return {"from": iso(spec.from_ts) if spec.from_ts is not None else None,
            "to": iso(spec.to_ts if spec.to_ts is not None else computed_at)}


def payload(entry):
    spec = entry.spec
    base = {"range": _range(spec, entry.computed_at),
            "computed_at": int(entry.computed_at),
            "latest_tst": int(entry.latest_tst) if entry.latest_tst is not None else None,
            "earliest_tst": int(entry.earliest_tst) if entry.earliest_tst is not None else None}
    if spec.kind == "heatmap":
        base["cell_deg"] = track.HEATMAP_CELL_DEG
        base["cells"] = [[gx, gy, n] for (gx, gy), n in sorted(entry.cells.items())]
        return base

    trackers, driving, driving_area, flights_buf, flights_area = _settled(entry)
    flights = []
    for t in trackers.values():
        for fl in t.flights:
            flights.append({
                "type": "Feature",
                "properties": {"start_tst": int(fl["start_tst"]), "end_tst": int(fl["end_tst"]),
                               "distance_km": round(fl["distance_km"], 1)},
                "geometry": {"type": "LineString",
                             "coordinates": [[round(x, COORD_DECIMALS), round(y, COORD_DECIMALS)]
                                             for x, y in fl["coords"]]},
            })
    flights.sort(key=lambda f: f["properties"]["start_tst"])
    ts = list(trackers.values())
    base.update({
        "buffer_m": spec.buffer_m,
        "driving": _feature(driving),
        "flights": {"type": "FeatureCollection", "features": flights},
        "flights_buffer": _feature(flights_buf),
        "stats": {
            "driving": {
                "distance_km": round(sum(t.driving_km for t in ts), 1),
                "area_km2": round(driving_area, 1),
                "max_alt_m": round(max([t.max_alt_driving for t in ts] or [0.0]), 1),
                "max_vel_kmh": round(max([t.max_vel_driving for t in ts] or [0.0]), 1),
            },
            "flying": {
                "distance_km": round(sum(t.flying_km for t in ts), 1),
                "area_km2": round(flights_area, 1),
                "max_alt_m": round(max([t.max_alt_flying for t in ts] or [0.0]), 1),
                "max_vel_kmh": round(max([t.max_vel_flying for t in ts] or [0.0]), 1),
            },
        },
    })
    return base


def response_bytes(entry):
    """JSON body for the entry, built once per content change."""
    if entry.body is None:
        entry.body = json.dumps(payload(entry), separators=(",", ":")).encode()
    return entry.body


def etag(entry):
    """Changes when the entry is recreated (refresh, expiry, new parameters)
    or when newer fixes have been folded in; stable across freshness checks
    that found nothing new."""
    return '"%s-%d-%d"' % (entry.spec.key, int(entry.created_at),
                          int(entry.latest_tst or 0))


def _recorder_has_newer(entry):
    """One cheap /api/0/last call instead of re-fetching the tail windows.
    Any doubt (probe failure, unknown device) counts as "newer" so the full
    path runs."""
    try:
        rows = recorder.last_fixes(entry.spec.username.lower())
    except recorder.RecorderError as e:
        log.warning("track_cache: last-fix probe failed for %s: %s", entry.spec.username, e)
        return True
    latest = entry.latest_tst if entry.latest_tst is not None else float("-inf")
    for row in rows:
        if row.get("device") in entry.spec.devices:
            tst = row.get("tst")
            if tst is None or tst > latest:
                return True
    return False


def empty_payload(spec, now_ts):
    entry = Entry(spec)
    entry.computed_at = now_ts
    return payload(entry)


# ---------------------------------------------------------------------------
# Request entry point
# ---------------------------------------------------------------------------
def _lock_for(key):
    with _meta_lock:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def _run_background(entry, lock):
    key = entry.spec.key
    try:
        update(entry, time.time())
        save_entry(entry)
    except Exception as e:
        log.exception("track_cache: background compute failed for %s", key)
        _errors[key] = str(e)
    finally:
        _computing.pop(key, None)
        lock.release()


def get_or_compute(spec, refresh=False):
    """
    Returns (status, value): status is "ok" (value is the Entry; see
    response_bytes/etag), "computing" (a compute holds the key; value is its
    progress dict or None; poll again) or "error" (a background compute
    failed; value is the message).
    """
    lock = _lock_for(spec.key)
    if not lock.acquire(blocking=False):
        running = _computing.get(spec.key)
        return "computing", dict(running.progress) if running is not None and running.progress else None
    handed_off = False
    try:
        now = time.time()
        if refresh:
            delete_entry(spec)
        entry = load_entry(spec)
        if entry is not None and not spec.open_ended and now - entry.computed_at >= CLOSED_TTL_SECONDS:
            delete_entry(spec)
            entry = None
        if entry is not None and not spec.open_ended:
            _touch(spec)
            return "ok", entry
        if entry is not None and (now - entry.checked_at < MIN_REFRESH_SECONDS
                                  or not _recorder_has_newer(entry)):
            entry.checked_at = now
            _touch(spec)
            return "ok", entry

        if entry is not None:
            window_lower = entry.computed_at
        else:
            window_lower = spec.from_ts
        upper = spec.to_ts if spec.to_ts is not None else now
        inline = window_lower is not None and (upper - window_lower) <= INLINE_WINDOW_DAYS * 86400

        if inline:
            entry = entry or Entry(spec)
            update(entry, now)
            save_entry(entry)
            return "ok", entry

        err = _errors.pop(spec.key, None)
        if err is not None:
            return "error", err
        entry = entry or Entry(spec)
        _computing[spec.key] = entry
        handed_off = True
        threading.Thread(target=_run_background, args=(entry, lock), daemon=True).start()
        return "computing", None
    finally:
        if not handed_off:
            lock.release()


def parse_iso(value):
    """ISO 8601 with an offset or Z -> unix seconds. Raises ValueError otherwise."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset or Z")
    return dt.timestamp()

"""
Privacy-preserving aggregation of ALL users' location history into a single
dissolved "visited roads" shape.

The OwnTracks recorder stores every user's points per-user/per-device. Reading
those individually is exactly the cross-account capability we do NOT want to
hand the frontend. Instead, this module runs entirely inside the trusted
usermanagement-api: it reads every user's track over the internal docker
network, buffers EACH PERSON's track separately (so point ordering is correct
per person), then dissolves all the buffers into one geometry with
shapely.unary_union. The result is a single GeoJSON Feature with empty
properties -- no usernames, no counts, no timestamps -- so there is no per-user
structure left to filter down to an individual.

Accepted residual risk (k=1, per product decision): a road that only one person
ever drove still appears as a spur, so presence on a unique road is inferable.
The merged shape only guarantees you cannot *select* one person's data out of it.

Computing this over ~1M+ points takes far too long to do per-request, so the
result is cached (in memory + persisted to the /data volume) with a
stale-while-revalidate refresh.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from shapely.geometry import mapping

import imports
import recorder
import track
from config import Config

# --- tunables resolved from config ---
FROM_DATE = Config.AGGREGATE_FROM
WINDOW_DAYS = Config.AGGREGATE_WINDOW_DAYS
BUFFER_M = Config.AGGREGATE_BUFFER_M
SIMPLIFY_M = Config.AGGREGATE_SIMPLIFY_M
OUT_SIMPLIFY_M = Config.AGGREGATE_OUT_SIMPLIFY_M
ACC_MAX_M = Config.AGGREGATE_ACC_MAX_M
MIN_DIST_M = Config.AGGREGATE_MIN_DIST_M
FLIGHT_SPEED_KMH = Config.AGGREGATE_FLIGHT_SPEED_KMH
FLIGHT_JUMP_KM = Config.AGGREGATE_FLIGHT_JUMP_KM
TTL_SECONDS = Config.AGGREGATE_TTL_SECONDS
CACHE_PATH = Config.AGGREGATE_CACHE_PATH

# A fingerprint of every config value that changes the OUTPUT geometry. If any
# of these change, a persisted cache from an older config is treated as stale.
PARAMS_FINGERPRINT = "|".join(str(x) for x in [
    "stats-v4",  # bump when the output shape changes (e.g. added stats properties)
    FROM_DATE, WINDOW_DAYS, BUFFER_M, SIMPLIFY_M, OUT_SIMPLIFY_M,
    ACC_MAX_M, MIN_DIST_M, FLIGHT_SPEED_KMH, FLIGHT_JUMP_KM,
])

_EMPTY_FEATURE = {"type": "Feature", "properties": {"empty": True}, "geometry": None}

# --- in-memory cache + concurrency guards ---
_cache = None            # dict: {"geojson", "computed_at", "params_fingerprint"}
_lock = threading.Lock()
_computing = False        # True while a (foreground or background) compute runs

log = logging.getLogger("aggregate")


def _windows(from_date, to_dt):
    """Yield (from, to) datetime windows of WINDOW_DAYS spanning [from_date, to_dt]."""
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return recorder.windows(start, to_dt, WINDOW_DAYS)


def filter_to_segments(points):
    return track.filter_to_segments(points, ACC_MAX_M, MIN_DIST_M, FLIGHT_SPEED_KMH, FLIGHT_JUMP_KM)


def _segments_to_feature(segments):
    """Buffer each segment in metres, dissolve all, simplify, return (Feature, area_km2).
    The area is geodesic, as in the per-user track, so a user's share of the
    shape compares like with like; the single AEQD used for buffering
    inflates area far from its centre."""
    geom, _ = track.dissolve_segments(segments, BUFFER_M, SIMPLIFY_M, OUT_SIMPLIFY_M)
    if geom is None:
        return dict(_EMPTY_FEATURE), 0.0
    return ({"type": "Feature", "properties": {}, "geometry": mapping(geom)},
            track.geometry_area_km2(geom))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _fix_points(fixes):
    return [{"lat": f.lat, "lon": f.lon, "tst": f.tst, "vel": f.vel, "alt": f.alt, "acc": f.acc}
            for f in fixes]


def compute_union():
    """Read every user's track, buffer per-user, dissolve into one Feature."""
    t0 = time.time()
    now = datetime.now(timezone.utc)
    all_segments = []
    # Population-level scalars (not per-user, not attributable) for the stats panel.
    max_vel = 0.0
    max_alt = 0.0
    users = recorder.list_users()
    covered = {}
    log.info("aggregate: computing over %d users", len(users))
    for user in users:
        buckets = covered.setdefault(user.lower(), set())
        try:
            devices = recorder.list_devices(user)
        except Exception as e:
            log.warning("aggregate: list_devices failed for %s: %s", user, e)
            continue
        for device in devices:
            # Buffer each person's track separately -> fetch all their windows,
            # build segments, and keep them grouped (we union everything at the
            # end; per-segment buffering already preserves per-person ordering).
            for f_dt, t_dt in _windows(FROM_DATE, now):
                try:
                    pts = recorder.fetch_points(user, device, f_dt, t_dt)
                except Exception as e:
                    log.warning("aggregate: fetch failed %s/%s %s..%s: %s",
                                user, device, f_dt, t_dt, e)
                    continue
                if not pts:
                    continue
                for p in pts:
                    v, a = p.get("vel"), p.get("alt")
                    if v is not None and v > max_vel:
                        max_vel = v
                    if a is not None and a > max_alt:
                        max_alt = a
                    if p.get("tst") is not None:
                        buckets.add(imports.bucket(p["tst"]))
                all_segments.extend(filter_to_segments(pts))

    # Imported history joins under the same recorder-wins rule as the per-user
    # track, so nothing is counted twice on a day the recorder covered.
    for user in imports.usernames():
        for source in imports.list_imports(user):
            fixes, _ = imports.select(imports.load_fixes(user, source), None, True,
                                      now.timestamp(), covered.get(user.lower(), set()))
            if not fixes:
                continue
            all_segments.extend(filter_to_segments(_fix_points(fixes)))

    feature, area_km2 = _segments_to_feature(all_segments)
    # Attach aggregate stats. These are single scalars across the whole
    # population (max speed/altitude anyone reached, combined distance, covered
    # area) — no per user breakdown, so nothing here can be traced to an individual.
    feature["properties"] = {
        "max_vel": round(float(max_vel), 1),
        "max_alt": round(float(max_alt), 1),
        "distance_km": round(track.segments_distance_km(all_segments), 1),
        "area_km2": round(area_km2, 1),
    }
    log.info("aggregate: done in %.1fs (%d segments)", time.time() - t0, len(all_segments))
    return feature


# ---------------------------------------------------------------------------
# Cache (in memory + persisted to /data) with stale-while-revalidate
# ---------------------------------------------------------------------------
def _load_cache():
    global _cache
    try:
        with open(CACHE_PATH, "r") as fh:
            data = json.load(fh)
        if data.get("params_fingerprint") == PARAMS_FINGERPRINT:
            _cache = data
            log.info("aggregate: loaded cache (age %.0fs)", time.time() - data["computed_at"])
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("aggregate: failed to load cache: %s", e)


def _save_cache(entry):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(entry, fh)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        log.warning("aggregate: failed to persist cache: %s", e)


def _run_compute():
    """Compute and store the result. Clears the _computing flag when done."""
    global _cache, _computing
    try:
        feature = compute_union()
        entry = {
            "geojson": feature,
            "computed_at": time.time(),
            "params_fingerprint": PARAMS_FINGERPRINT,
        }
        _cache = entry
        _save_cache(entry)
    except Exception:
        log.exception("aggregate: compute failed")
    finally:
        with _lock:
            _computing = False


def _start_background_compute():
    """Kick a single background compute if one isn't already running."""
    global _computing
    with _lock:
        if _computing:
            return
        _computing = True
    threading.Thread(target=_run_compute, daemon=True).start()


def etag():
    return '"agg-%d"' % int(_cache["computed_at"])


_body = (None, None)  # (computed_at, serialized feature)


def response_bytes():
    """Serialized once per compute; the feature can run to hundreds of KB.
    Kept outside the cache dict so the persisted JSON stays plain."""
    global _body
    entry = _cache
    if _body[0] != entry["computed_at"]:
        _body = (entry["computed_at"], json.dumps(entry["geojson"], separators=(",", ":")).encode())
    return _body[1]


def get_cached_or_compute(force=False):
    """
    Return (geojson, ready).

    - fresh cache            -> (geojson, True)
    - stale cache            -> serve stale, kick background refresh -> (geojson, True)
    - no cache (cold)/force  -> kick background compute -> (None, False)  [caller sends 503]
    """
    if force:
        _start_background_compute()
        return None, False

    entry = _cache
    if entry is not None:
        age = time.time() - entry["computed_at"]
        if age >= TTL_SECONDS:
            _start_background_compute()  # stale-while-revalidate
        return entry["geojson"], True

    _start_background_compute()
    return None, False


# Warm on import: load any persisted cache; if none, start computing so the
# first user rarely hits a cold endpoint.
_load_cache()
if _cache is None:
    _start_background_compute()

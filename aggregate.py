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
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from shapely.geometry import LineString, mapping
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union
from pyproj import Transformer

from config import Config

# --- tunables resolved from config ---
RECORDER_URL = Config.RECORDER_URL.rstrip('/')
RECORDER_TIMEOUT = Config.RECORDER_TIMEOUT
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
    FROM_DATE, WINDOW_DAYS, BUFFER_M, SIMPLIFY_M, OUT_SIMPLIFY_M,
    ACC_MAX_M, MIN_DIST_M, FLIGHT_SPEED_KMH, FLIGHT_JUMP_KM,
])

_EMPTY_FEATURE = {"type": "Feature", "properties": {"empty": True}, "geometry": None}

# --- in-memory cache + concurrency guards ---
_cache = None            # dict: {"geojson", "computed_at", "params_fingerprint"}
_lock = threading.Lock()
_computing = False        # True while a (foreground or background) compute runs

import logging
log = logging.getLogger("aggregate")


# ---------------------------------------------------------------------------
# Recorder client
# ---------------------------------------------------------------------------
def _recorder_get(path, **params):
    """GET a recorder JSON endpoint over the internal network. Returns parsed JSON."""
    url = RECORDER_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=RECORDER_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_users():
    """All users the recorder knows about (no exclusions, per product decision)."""
    return _recorder_get("/api/0/list").get("results", [])


def list_devices(user):
    return _recorder_get("/api/0/list", user=user).get("results", [])


def _windows(from_date, to_dt):
    """Yield (from_iso, to_iso) windows of WINDOW_DAYS spanning [from_date, to_dt]."""
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    step = timedelta(days=WINDOW_DAYS)
    while start < to_dt:
        end = min(start + step, to_dt)
        yield start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
        start = end


def fetch_track(user, device, f_iso, t_iso):
    """One window of raw points for a user/device. format=json so we get acc/vel/tst."""
    data = _recorder_get(
        "/api/0/locations",
        user=user, device=device, **{"from": f_iso, "to": t_iso}, format="json",
    )
    return data.get("data", [])


# ---------------------------------------------------------------------------
# Filtering -> segments  (mirrors the frontend's drawOnMap/manageData logic)
# ---------------------------------------------------------------------------
def _haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def filter_to_segments(points):
    """
    Turn raw recorder points into a list of polyline segments [[(lon,lat),...], ...].

    - drop points with poor accuracy
    - thin points closer than MIN_DIST_M to the last kept point
    - start a NEW segment on a "flight"/teleport (too fast or too far) so we never
      draw a buffer corridor across a flight or a GPS jump
    - drop segments shorter than 2 points
    """
    pts = [p for p in points if p.get("lat") is not None and p.get("lon") is not None
           and p.get("tst") is not None]
    pts.sort(key=lambda p: p["tst"])

    segments = []
    current = []
    last = None  # (lon, lat, tst)
    for p in pts:
        if p.get("acc") is not None and p["acc"] > ACC_MAX_M:
            continue
        lon, lat, tst = float(p["lon"]), float(p["lat"]), float(p["tst"])
        if last is not None:
            dist = _haversine_m(last[0], last[1], lon, lat)
            if dist < MIN_DIST_M:
                continue  # thin: too close to keep
            dt = tst - last[2]
            speed_kmh = (dist / 1000.0) / (dt / 3600.0) if dt > 0 else float("inf")
            if dist > FLIGHT_JUMP_KM * 1000 or speed_kmh > FLIGHT_SPEED_KMH:
                # teleport/flight -> break the line here
                if len(current) >= 2:
                    segments.append(current)
                current = []
                last = (lon, lat, tst)
                current.append((lon, lat))
                continue
        current.append((lon, lat))
        last = (lon, lat, tst)

    if len(current) >= 2:
        segments.append(current)
    return segments


# ---------------------------------------------------------------------------
# Buffer + union
# ---------------------------------------------------------------------------
def _build_transformers(centroid_lon, centroid_lat):
    """Local Azimuthal Equidistant centered on the data, so buffers in metres are accurate."""
    aeqd = f"+proj=aeqd +lat_0={centroid_lat} +lon_0={centroid_lon} +datum=WGS84 +units=m +no_defs"
    to_metric = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform
    return to_metric, to_wgs84


def _union_batched(polys, batch=1000):
    """unary_union in batches to bound peak memory on large inputs."""
    if not polys:
        return None
    merged = []
    for i in range(0, len(polys), batch):
        merged.append(unary_union(polys[i:i + batch]))
    return unary_union(merged) if len(merged) > 1 else merged[0]


def _segments_to_feature(segments):
    """Buffer each segment in metres, dissolve all, simplify, return a GeoJSON Feature."""
    if not segments:
        return dict(_EMPTY_FEATURE)

    # centroid of all coords -> projection center
    sx = sy = n = 0
    for seg in segments:
        for lon, lat in seg:
            sx += lon
            sy += lat
            n += 1
    to_metric, to_wgs84 = _build_transformers(sx / n, sy / n)

    polys = []
    for seg in segments:
        line = LineString(seg)
        line_m = shapely_transform(to_metric, line)
        if SIMPLIFY_M > 0:
            line_m = line_m.simplify(SIMPLIFY_M, preserve_topology=False)
        if line_m.is_empty:
            continue
        polys.append(line_m.buffer(BUFFER_M, quad_segs=2))

    dissolved = _union_batched(polys)
    if dissolved is None or dissolved.is_empty:
        return dict(_EMPTY_FEATURE)

    if OUT_SIMPLIFY_M > 0:
        dissolved = dissolved.simplify(OUT_SIMPLIFY_M)

    dissolved_wgs = shapely_transform(to_wgs84, dissolved)
    return {"type": "Feature", "properties": {}, "geometry": mapping(dissolved_wgs)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def compute_union():
    """Read every user's track, buffer per-user, dissolve into one Feature."""
    t0 = time.time()
    now = datetime.now(timezone.utc)
    all_segments = []
    users = list_users()
    log.info("aggregate: computing over %d users", len(users))
    for user in users:
        try:
            devices = list_devices(user)
        except Exception as e:
            log.warning("aggregate: list_devices failed for %s: %s", user, e)
            continue
        for device in devices:
            # Buffer each person's track separately -> fetch all their windows,
            # build segments, and keep them grouped (we union everything at the
            # end; per-segment buffering already preserves per-person ordering).
            for f_iso, t_iso in _windows(FROM_DATE, now):
                try:
                    pts = fetch_track(user, device, f_iso, t_iso)
                except Exception as e:
                    log.warning("aggregate: fetch failed %s/%s %s..%s: %s",
                                user, device, f_iso, t_iso, e)
                    continue
                if pts:
                    all_segments.extend(filter_to_segments(pts))

    feature = _segments_to_feature(all_segments)
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

"""
Shared track geometry pipeline: recorder points -> flight classification ->
drawable polylines -> buffered, dissolved corridors and stats.

Two callers with different rules share this module:

- aggregate.py keeps its per-point speed/jump cutoff and buffers everything in
  one projection in a single pass (filter_to_segments + dissolve_segments).
- The per-user endpoints use the episodic flight detection ported from the web
  app (static/js/manageData.js: detectFlights / filterData) through
  DeviceTracker, which streams fixes so an open-ended cache entry can be
  extended without recomputing history.

All distances are haversine kilometres; coordinates are (lon, lat) WGS84.
"""

import copy
import math
from dataclasses import dataclass
from typing import NamedTuple, Optional

import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

EARTH_RADIUS_KM = 6371.0
KM_PER_MI = 1.609344


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def js_round(x):
    """JavaScript Math.round: halves round toward +infinity, unlike Python's round()."""
    return int(math.floor(x + 0.5))


class Fix(NamedTuple):
    lat: float
    lon: float
    tst: float
    vel: float
    alt: float
    acc: Optional[float]


def fixes_from_points(points):
    """Recorder JSON points -> Fix list sorted by tst. Missing vel/alt count as 0,
    missing acc as accurate."""
    out = []
    for p in points:
        lat, lon, tst = p.get("lat"), p.get("lon"), p.get("tst")
        if lat is None or lon is None or tst is None:
            continue
        acc = p.get("acc")
        out.append(Fix(
            float(lat), float(lon), float(tst),
            float(p.get("vel") or 0), float(p.get("alt") or 0),
            None if acc is None else float(acc),
        ))
    out.sort(key=lambda f: f.tst)
    return out


def polyline_km(fixes):
    total = 0.0
    for i in range(1, len(fixes)):
        a, b = fixes[i - 1], fixes[i]
        total += haversine_km(a.lat, a.lon, b.lat, b.lon)
    return total


# ---------------------------------------------------------------------------
# Episodic flight detection (port of the web app's detectFlights)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FlightParams:
    entry_kmh: float = 200 * KM_PER_MI
    entry_alt_m: float = 6000.0
    entry_jump_km: float = 100.0
    exit_kmh: float = 100.0
    lookback_s: float = 30 * 60
    stale_s: float = 30 * 60
    acc_max_m: float = 100.0
    min_dist_m: float = 20.0


DEFAULT_FLIGHT_PARAMS = FlightParams()


def classify_flights(fixes, params=DEFAULT_FLIGHT_PARAMS, prev=None, airborne=False,
                     last_signal=None):
    """
    Forward pass over fixes sorted by tst. Returns (flying, airborne_after,
    signal_after): the per-fix flying flag after the take-off walk-back, and the
    forward-pass state after each fix, which lets a later batch resume from any
    index. prev/airborne/last_signal seed the pass from an earlier batch.
    """
    n = len(fixes)
    flying = [False] * n
    airborne_after = [False] * n
    signal_after = [None] * n
    for i, f in enumerate(fixes):
        jump = haversine_km(prev.lat, prev.lon, f.lat, f.lon) if prev is not None else 0.0
        jumped = jump > params.entry_jump_km
        signal = f.vel > params.entry_kmh or f.alt > params.entry_alt_m or jumped
        if not airborne and signal:
            airborne = True
            ref = f.tst
            j = i - 1
            while j >= 0 and not flying[j]:
                if fixes[j].vel <= params.exit_kmh:
                    break
                before_jump = jumped and j == i - 1
                if not before_jump and ref - fixes[j].tst > params.lookback_s:
                    break
                flying[j] = True
                ref = fixes[j].tst
                j -= 1
        elif airborne and not signal:
            stale = last_signal is not None and f.tst - last_signal > params.stale_s
            if f.vel < params.exit_kmh or stale:
                airborne = False
        if signal:
            last_signal = f.tst
        flying[i] = airborne
        airborne_after[i] = airborne
        signal_after[i] = last_signal
        prev = f
    return flying, airborne_after, signal_after


def flight_intervals(fixes, flying):
    intervals = []
    i = 0
    while i < len(fixes):
        j = i
        while j < len(fixes) and flying[j] == flying[i]:
            j += 1
        if flying[i]:
            intervals.append([fixes[i].tst, fixes[j - 1].tst])
        i = j
    return intervals


def detect_flights(fixes, params=DEFAULT_FLIGHT_PARAMS):
    """
    One-pass classification of a whole device track. Returns
    (intervals, kept, kept_flying): flight time windows, the drawable subset
    (accuracy + thinning filters) and its per-fix flying flags.
    """
    flying, _, _ = classify_flights(fixes, params)
    kept, kept_flying = [], []
    last = None
    for f, fl in zip(fixes, flying):
        if f.acc is not None and f.acc >= params.acc_max_m:
            continue
        gap = haversine_km(last.lat, last.lon, f.lat, f.lon) if last is not None else 0.0
        if last is not None and gap * 1000 <= params.min_dist_m:
            continue
        kept.append(f)
        kept_flying.append(fl or gap > params.entry_jump_km)
        last = f
    return flight_intervals(fixes, flying), kept, kept_flying


def group_segments(kept, kept_flying):
    """
    Port of filterData: runs of equal flying flag become segments. A flying run
    is extended by its neighbouring driving fix on each side so the flight line
    meets the road. Returns (driving_groups, flying_groups) as Fix lists.
    """
    driving, flying = [], []
    i = 0
    while i < len(kept):
        mode = kept_flying[i]
        j = i
        while j < len(kept) and kept_flying[j] == mode:
            j += 1
        group = kept[i:j]
        if mode:
            if i > 0:
                group = [kept[i - 1]] + group
            if j < len(kept):
                group = group + [kept[j]]
        if len(group) > 1:
            (flying if mode else driving).append(group)
        i = j
    return driving, flying


# ---------------------------------------------------------------------------
# Streaming per-device tracker
# ---------------------------------------------------------------------------
# Long driving runs are buffered in pieces so the pending state stays small.
MAX_RUN_FIXES = 2000


@dataclass
class Increment:
    """Polylines produced by one feed() that still need buffering."""
    driving_lines: list
    flight_lines: list

    def __init__(self):
        self.driving_lines = []
        self.flight_lines = []

    def extend(self, other):
        self.driving_lines.extend(other.driving_lines)
        self.flight_lines.extend(other.flight_lines)


class DeviceTracker:
    """
    Streams one device's fixes through detect_flights + group_segments while
    keeping only the state needed to continue.

    A later signal can retroactively mark earlier fixes as flying, but only the
    trailing run of not-yet-flying fixes faster than the exit speed, and only
    while consecutive gaps stay within the lookback. Those fixes are the
    "tail": they are held back (not drawn, not counted) until a later fix
    settles them, so feeding a track in pieces yields exactly the one-pass
    classification, distances and maxima. flush() settles the tail when no
    more data will come.
    """

    def __init__(self, params=DEFAULT_FLIGHT_PARAMS):
        self.p = params
        self.prev = None
        self.airborne = False
        self.last_signal = None
        self.tail = []
        self.last_tst = None
        self.last_kept = None
        self.run = []
        self.run_mode = None
        self.run_prev = None
        self.run_started = False
        self.run_start_tst = None
        self.interval_open = False
        self.intervals = []
        self.flights = []
        self.flight_open = False
        self.driving_km = 0.0
        self.flying_km = 0.0
        self.max_alt_driving = 0.0
        self.max_vel_driving = 0.0
        self.max_alt_flying = 0.0
        self.max_vel_flying = 0.0

    # -- persistence -------------------------------------------------------
    def to_state(self):
        d = dict(self.__dict__)
        d.pop("p")
        return d

    @classmethod
    def from_state(cls, state, params=DEFAULT_FLIGHT_PARAMS):
        t = cls(params)
        for k, v in state.items():
            setattr(t, k, v)
        t.prev = Fix(*t.prev) if t.prev is not None else None
        t.last_kept = Fix(*t.last_kept) if t.last_kept is not None else None
        t.run_prev = Fix(*t.run_prev) if t.run_prev is not None else None
        t.tail = [Fix(*f) for f in t.tail]
        t.run = [Fix(*f) for f in t.run]
        return t

    # -- feeding -----------------------------------------------------------
    def feed(self, fixes, final=False):
        """Process fixes newer than anything seen. Returns an Increment."""
        new = sorted((f for f in fixes if self.last_tst is None or f.tst > self.last_tst),
                     key=lambda f: f.tst)
        deduped = []
        for f in new:
            if deduped and f.tst <= deduped[-1].tst:
                continue
            deduped.append(f)
        if deduped:
            self.last_tst = deduped[-1].tst

        inc = Increment()
        allf = self.tail + deduped
        if allf:
            flying, air_after, sig_after = classify_flights(
                allf, self.p, self.prev, self.airborne, self.last_signal)
            t = len(allf) if final else self._tail_start(allf, flying)
            self._finalize(allf[:t], flying[:t], inc)
            if t > 0:
                self.prev = allf[t - 1]
                self.airborne = air_after[t - 1]
                self.last_signal = sig_after[t - 1]
            self.tail = allf[t:]
        self._flush_run(inc)
        return inc

    def flush(self):
        return self.feed([], final=True)

    def preview(self):
        """What flush() would add, without changing this tracker.
        Returns (increment, settled tracker copy)."""
        settled = copy.deepcopy(self)
        return settled.flush(), settled

    def _tail_start(self, fixes, flying):
        n = len(fixes)
        t = n
        while t > 0 and not flying[t - 1] and fixes[t - 1].vel > self.p.exit_kmh:
            t -= 1
        for k in range(n - 1, t, -1):
            if fixes[k].tst - fixes[k - 1].tst > self.p.lookback_s:
                return k
        return t

    def _finalize(self, fixes, flags, inc):
        p = self.p
        for f, fl in zip(fixes, flags):
            if fl:
                if self.interval_open:
                    self.intervals[-1][1] = f.tst
                else:
                    self.intervals.append([f.tst, f.tst])
                    self.interval_open = True
            else:
                self.interval_open = False
            in_flight = fl or (bool(self.intervals)
                               and self.intervals[-1][0] <= f.tst <= self.intervals[-1][1])
            if in_flight:
                self.max_alt_flying = max(self.max_alt_flying, f.alt)
                self.max_vel_flying = max(self.max_vel_flying, f.vel)
            else:
                self.max_alt_driving = max(self.max_alt_driving, f.alt)
                self.max_vel_driving = max(self.max_vel_driving, f.vel)

            if f.acc is not None and f.acc >= p.acc_max_m:
                continue
            last = self.last_kept
            gap = haversine_km(last.lat, last.lon, f.lat, f.lon) if last is not None else 0.0
            if last is not None and gap * 1000 <= p.min_dist_m:
                continue
            self._add_kept(f, fl or gap > p.entry_jump_km, inc)
            self.last_kept = f

    def _add_kept(self, f, mode, inc):
        if self.run_mode is None:
            self._start_run(f, mode, None)
        elif mode != self.run_mode:
            self._close_run(f, inc)
        else:
            self.run.append(f)
            if not mode and len(self.run) >= MAX_RUN_FIXES:
                self._flush_run(inc)

    def _start_run(self, f, mode, prev):
        self.run = [f]
        self.run_mode = mode
        self.run_prev = prev
        self.run_started = False
        self.run_start_tst = f.tst

    def _group(self, next_fix):
        group = list(self.run)
        if self.run_mode:
            if self.run_prev is not None and not self.run_started:
                group.insert(0, self.run_prev)
            if next_fix is not None:
                group.append(next_fix)
        return group

    def _close_run(self, next_fix, inc):
        self._emit(self._group(next_fix), inc)
        if self.run_mode:
            self.flight_open = False
        self._start_run(next_fix, not self.run_mode, self.run[-1])

    def _flush_run(self, inc):
        if not self.run:
            return
        self._emit(self._group(None), inc)
        self.run_started = True
        self.run = [self.run[-1]]

    def _emit(self, group, inc):
        if len(group) < 2:
            return
        coords = [(f.lon, f.lat) for f in group]
        km = polyline_km(group)
        if not self.run_mode:
            inc.driving_lines.append(coords)
            self.driving_km += km
            return
        inc.flight_lines.append(coords)
        self.flying_km += km
        end_tst = self.run[-1].tst
        if self.flight_open:
            fl = self.flights[-1]
            fl["coords"].extend(coords[1:])
            fl["distance_km"] += km
            fl["end_tst"] = end_tst
        else:
            self.flights.append({
                "coords": coords, "start_tst": self.run_start_tst,
                "end_tst": end_tst, "distance_km": km,
            })
            self.flight_open = True


# ---------------------------------------------------------------------------
# Aggregate-style filtering (per-point cutoff, one pass)
# ---------------------------------------------------------------------------
def filter_to_segments(points, acc_max_m, min_dist_m, flight_speed_kmh, flight_jump_km):
    """
    Raw recorder points -> polyline segments [[(lon, lat), ...], ...].

    Drops poor-accuracy points, thins points closer than min_dist_m to the last
    kept point, and starts a new segment on a teleport (too fast or too far)
    so no corridor is drawn across a flight. Segments under 2 points are dropped.
    """
    pts = [p for p in points if p.get("lat") is not None and p.get("lon") is not None
           and p.get("tst") is not None]
    pts.sort(key=lambda p: p["tst"])

    segments = []
    current = []
    last = None
    for p in pts:
        if p.get("acc") is not None and p["acc"] > acc_max_m:
            continue
        lon, lat, tst = float(p["lon"]), float(p["lat"]), float(p["tst"])
        if last is not None:
            dist = haversine_km(last[1], last[0], lat, lon) * 1000
            if dist < min_dist_m:
                continue
            dt = tst - last[2]
            speed_kmh = (dist / 1000.0) / (dt / 3600.0) if dt > 0 else float("inf")
            if dist > flight_jump_km * 1000 or speed_kmh > flight_speed_kmh:
                if len(current) >= 2:
                    segments.append(current)
                current = [(lon, lat)]
                last = (lon, lat, tst)
                continue
        current.append((lon, lat))
        last = (lon, lat, tst)

    if len(current) >= 2:
        segments.append(current)
    return segments


def segments_distance_km(segments):
    total = 0.0
    for seg in segments:
        for i in range(1, len(seg)):
            total += haversine_km(seg[i - 1][1], seg[i - 1][0], seg[i][1], seg[i][0])
    return total


# ---------------------------------------------------------------------------
# Projection, buffering, dissolving
# ---------------------------------------------------------------------------
def build_transformers(centroid_lon, centroid_lat):
    """Local Azimuthal Equidistant centred on the data, so metre buffers are accurate."""
    aeqd = f"+proj=aeqd +lat_0={centroid_lat} +lon_0={centroid_lon} +datum=WGS84 +units=m +no_defs"
    to_metric = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform
    return to_metric, to_wgs84


def union_batched(polys, batch=1000):
    """unary_union in batches to bound peak memory on large inputs."""
    if not polys:
        return None
    merged = []
    for i in range(0, len(polys), batch):
        merged.append(unary_union(polys[i:i + batch]))
    return unary_union(merged) if len(merged) > 1 else merged[0]


def _polygon_parts(geom):
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def _as_multipolygon(geom):
    parts = _polygon_parts(geom)
    return MultiPolygon(parts) if parts else None


def dissolve_segments(segments, buffer_m, simplify_m, out_simplify_m):
    """
    Buffer every segment in one AEQD centred on all coordinates, dissolve,
    simplify. Returns (geometry in WGS84 or None, area_km2 measured in that
    projection).
    """
    if not segments:
        return None, 0.0
    sx = sy = n = 0
    for seg in segments:
        for lon, lat in seg:
            sx += lon
            sy += lat
            n += 1
    to_metric, to_wgs84 = build_transformers(sx / n, sy / n)

    polys = []
    for seg in segments:
        line_m = shapely_transform(to_metric, LineString(seg))
        if simplify_m > 0:
            line_m = line_m.simplify(simplify_m, preserve_topology=False)
        if line_m.is_empty:
            continue
        polys.append(line_m.buffer(buffer_m, quad_segs=2))

    dissolved = union_batched(polys)
    if dissolved is None or dissolved.is_empty:
        return None, 0.0
    if out_simplify_m > 0:
        dissolved = dissolved.simplify(out_simplify_m)
    return shapely_transform(to_wgs84, dissolved), dissolved.area / 1e6


_GEOD = Geod(ellps="WGS84")
# A local AEQD is only accurate near its centre, so long polylines (flights,
# road trips) are densified along the geodesic and buffered in chunks.
CHUNK_KM = 1000.0
DENSIFY_KM = 200.0


def _chunks(coords):
    chunks, cur, acc = [], [coords[0]], 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        d = haversine_km(y1, x1, y2, x2)
        pts = [(x2, y2)]
        if d > DENSIFY_KM:
            pts = [tuple(p) for p in _GEOD.npts(x1, y1, x2, y2, int(d // DENSIFY_KM))] + pts
        for x, y in pts:
            step = haversine_km(cur[-1][1], cur[-1][0], y, x)
            if acc + step > CHUNK_KM and len(cur) >= 2:
                chunks.append(cur)
                cur, acc = [cur[-1]], 0.0
            cur.append((x, y))
            acc += step
    if len(cur) >= 2:
        chunks.append(cur)
    return chunks


def _shift_lons(geom, offset, only_negative=False):
    def f(c):
        lon = c[:, 0]
        lon = np.where(lon < 0, lon + offset, lon) if only_negative else lon + offset
        return np.column_stack([lon, c[:, 1]])
    return shapely.transform(geom, f)


def _split_antimeridian(poly):
    """A corridor whose longitudes wrap from 179 to -179 comes back from the
    projection spanning the whole map; cut it at the antimeridian instead."""
    minx, _, maxx, _ = poly.bounds
    if maxx - minx <= 180:
        return [poly]
    unwrapped = _shift_lons(poly, 360, only_negative=True)
    west = unwrapped.intersection(box(0, -90, 180, 90))
    east = _shift_lons(unwrapped.intersection(box(180, -90, 360, 90)), -360)
    return _polygon_parts(west) + _polygon_parts(east)


def buffer_polylines(lines, buffer_m, simplify_m, out_simplify_m):
    """Buffer each polyline in local AEQD chunks. Returns WGS84 polygons."""
    polys = []
    for coords in lines:
        if len(coords) < 2:
            continue
        for chunk in _chunks(coords):
            line = LineString(chunk)
            cx, cy = chunk[len(chunk) // 2]
            to_metric, to_wgs84 = build_transformers(cx, cy)
            line_m = shapely_transform(to_metric, line)
            if simplify_m > 0:
                line_m = line_m.simplify(simplify_m, preserve_topology=False)
            if line_m.is_empty:
                continue
            poly = line_m.buffer(buffer_m, quad_segs=2)
            if out_simplify_m > 0:
                poly = poly.simplify(out_simplify_m)
            for part in _polygon_parts(shapely_transform(to_wgs84, poly)):
                polys.extend(_split_antimeridian(part))
    return polys


def _bounds_overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def merge_geometry(base, new_polys):
    """
    Union new polygons into a dissolved MultiPolygon, re-unioning only the
    existing parts whose bounds touch the new ones so extending a large
    history stays cheap. Returns a MultiPolygon or None.
    """
    if not new_polys:
        return base
    fresh = unary_union(new_polys)
    if base is None or base.is_empty:
        return _as_multipolygon(fresh)
    fb = fresh.bounds
    touched, untouched = [], []
    for part in _polygon_parts(base):
        (touched if _bounds_overlap(part.bounds, fb) else untouched).append(part)
    merged = unary_union(touched + [fresh])
    return _as_multipolygon(MultiPolygon(untouched + _polygon_parts(merged)))


def geometry_area_km2(geom):
    """Geodesic area of a WGS84 geometry on the ellipsoid, in km²."""
    total = 0.0
    for part in _polygon_parts(geom):
        area, _ = _GEOD.geometry_area_perimeter(part)
        total += abs(area)
    return total / 1e6


# ---------------------------------------------------------------------------
# Heatmap grid
# ---------------------------------------------------------------------------
HEATMAP_CELL_DEG = 0.0006


def add_heat_cells(fixes, cells, acc_max_m=100.0, cell_deg=HEATMAP_CELL_DEG):
    """Count fixes with acc < acc_max_m into cells keyed (gx, gy). Mutates and returns cells."""
    for f in fixes:
        if f.acc is not None and f.acc >= acc_max_m:
            continue
        key = (js_round(f.lon / cell_deg), js_round(f.lat / cell_deg))
        cells[key] = cells.get(key, 0) + 1
    return cells

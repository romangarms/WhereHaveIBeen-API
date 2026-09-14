"""
Google Maps Timeline exports -> Fix list.

Two layouts are accepted:

- The phone export ("Export Timeline data" in the Google Maps app): a JSON
  list of semantic segments, each with startTime/endTime and one of
  `timelinePath` (raw points with a minute offset from the start), `visit`
  (one place for the whole span), `activity` (a trip with start/end
  points), or `timelineMemory` (ignored). Some exports wrap the list as
  {"semanticSegments": [...]}.
- Google Takeout's Records.json: {"locations": [{latitudeE7, longitudeE7,
  timestamp, accuracy?, altitude?, velocity?}, ...]}.

timelinePath is the recorded position and is always used. visit and
activity segments carry Google's inferred places, which in real exports are
sometimes hundreds of kilometres off, so they only stand in for spans that no
path point covers. A point that would need more than OUTLIER_MAX_KMH to reach
from the last kept one is dropped unless the following points agree with it,
and so is a point mid-flight that the track leaves as fast as it arrived while
sitting far off the line between its neighbours: Google slips the phone's
stale location into a flight path, which would draw the flight doubling back.

The export carries no speed, and a plane with the phone awake leaves path
points every few minutes that are closer together than the 100 km jump rule,
so each fix gets a speed from its displacement over at least SPEED_WINDOW_S
(the slower of before and across it). It only steers flight detection: the
per-user stats report speed and altitude from the recorder alone. Timestamps are made strictly
increasing (ties pushed forward one second) so the streaming tracker keeps
points that share a minute offset.
"""

import bisect
import re
from datetime import datetime

from track import Fix, haversine_km

OUTLIER_MIN_KM = 50.0
OUTLIER_MAX_KMH = 1500.0
OUTLIER_LOOKAHEAD = 3
BACKTRACK_MIN_KMH = 400.0
BACKTRACK_DETOUR = 1.5
# Path offsets are whole minutes, so speed is measured over at least this
# long to keep two points a minute apart from reading as a jet.
SPEED_WINDOW_S = 300.0

_GEO = re.compile(r'^\s*geo:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$')


class TimelineFormatError(ValueError):
    pass


def _ts(value):
    if not isinstance(value, str):
        raise TimelineFormatError("segment has no startTime/endTime")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise TimelineFormatError(f"bad timestamp {value!r}") from e
    if dt.tzinfo is None:
        raise TimelineFormatError(f"timestamp {value!r} has no offset")
    return dt.timestamp()


def _geo(value):
    m = _GEO.match(value) if isinstance(value, str) else None
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _fix(lat, lon, tst, vel=0.0, alt=0.0, acc=None):
    return Fix(float(lat), float(lon), float(tst), float(vel), float(alt), acc)


def _spans(segments):
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            yield seg, _ts(seg.get("startTime")), _ts(seg.get("endTime"))
        except TimelineFormatError:
            continue


def _parse_segments(segments):
    fixes = []
    counts = {"timelinePath": 0, "visit": 0, "activity": 0, "flying": 0, "skipped": 0}
    path_tsts = []
    for seg, start, end in _spans(segments):
        path = seg.get("timelinePath")
        if not isinstance(path, list):
            continue
        for p in path:
            geo = _geo(p.get("point")) if isinstance(p, dict) else None
            if geo is None:
                continue
            try:
                offset = float(p.get("durationMinutesOffsetFromStartTime") or 0)
            except (TypeError, ValueError):
                offset = 0.0
            tst = start + offset * 60
            fixes.append(_fix(geo[0], geo[1], tst))
            path_tsts.append(tst)
    counts["timelinePath"] = len(fixes)
    path_tsts.sort()

    def path_covers(a, b):
        i = bisect.bisect_left(path_tsts, a)
        return i < len(path_tsts) and path_tsts[i] <= b

    for seg, start, end in _spans(segments):
        if "timelinePath" in seg:
            continue
        visit = seg.get("visit")
        activity = seg.get("activity")
        if isinstance(visit, dict):
            geo = _geo((visit.get("topCandidate") or {}).get("placeLocation"))
            if geo is None or str(visit.get("hierarchyLevel", "0")) != "0":
                counts["skipped"] += 1
            elif not path_covers(start, end):
                fixes.append(_fix(geo[0], geo[1], start))
                fixes.append(_fix(geo[0], geo[1], end))
                counts["visit"] += 1
        elif isinstance(activity, dict):
            a, b = _geo(activity.get("start")), _geo(activity.get("end"))
            if a is None or b is None:
                counts["skipped"] += 1
            elif not path_covers(start, end):
                fixes.append(_fix(a[0], a[1], start))
                fixes.append(_fix(b[0], b[1], end))
                counts["activity"] += 1
                if (activity.get("topCandidate") or {}).get("type") == "flying":
                    counts["flying"] += 1
        else:
            counts["skipped"] += 1
    return fixes, counts


def _parse_records(records):
    fixes = []
    counts = {"records": 0, "skipped": 0}
    for r in records:
        if not isinstance(r, dict):
            counts["skipped"] += 1
            continue
        lat_e7, lon_e7, ts = r.get("latitudeE7"), r.get("longitudeE7"), r.get("timestamp")
        if lat_e7 is None or lon_e7 is None or ts is None:
            counts["skipped"] += 1
            continue
        try:
            tst = _ts(ts)
            lat, lon = float(lat_e7) / 1e7, float(lon_e7) / 1e7
        except (TimelineFormatError, TypeError, ValueError):
            counts["skipped"] += 1
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            counts["skipped"] += 1
            continue
        acc = r.get("accuracy")
        vel = r.get("velocity") or 0.0
        fixes.append(_fix(lat, lon, tst, float(vel) * 3.6, r.get("altitude") or 0.0,
                          None if acc is None else float(acc)))
        counts["records"] += 1
    return fixes, counts


def _monotonic(fixes):
    fixes.sort(key=lambda f: f.tst)
    out = []
    for f in fixes:
        if out and f.tst <= out[-1].tst:
            f = f._replace(tst=out[-1].tst + 1)
        out.append(f)
    return out


def _implausible(a, b):
    km = haversine_km(a.lat, a.lon, b.lat, b.lon)
    if km <= OUTLIER_MIN_KM:
        return False
    hours = (b.tst - a.tst) / 3600.0
    return hours <= 0 or km / hours > OUTLIER_MAX_KMH


def _kmh(a, b):
    km = haversine_km(a.lat, a.lon, b.lat, b.lon)
    hours = (b.tst - a.tst) / 3600.0
    return km / hours if hours > 0 else float("inf")


def _detour(a, b, c):
    ab = haversine_km(a.lat, a.lon, b.lat, b.lon)
    bc = haversine_km(b.lat, b.lon, c.lat, c.lon)
    if ab <= OUTLIER_MIN_KM or bc <= OUTLIER_MIN_KM:
        return False
    ac = haversine_km(a.lat, a.lon, c.lat, c.lon)
    return (ab + bc > BACKTRACK_DETOUR * ac
            and _kmh(a, b) > BACKTRACK_MIN_KMH and _kmh(b, c) > BACKTRACK_MIN_KMH)


def _backtrack(a, b, window):
    """Three points alone cannot say whether b or the point after it is the
    stale one, so b must be a detour against each of the next few."""
    return bool(window) and all(_detour(a, b, c) for c in window)


def _drop_outliers(fixes):
    """Drop a fix the last kept one could not have reached, unless most of the
    next few fixes sit near it (then it is a real relocation and the earlier
    fix was the odd one), and a fix the track only visits at jet speed as a
    detour between its neighbours."""
    kept = []
    dropped = 0
    for i, f in enumerate(fixes):
        if kept and _implausible(kept[-1], f):
            window = fixes[i + 1:i + 1 + OUTLIER_LOOKAHEAD]
            near = sum(1 for g in window
                       if haversine_km(f.lat, f.lon, g.lat, g.lon) <= OUTLIER_MIN_KM)
            if not window or near * 2 < len(window):
                dropped += 1
                continue
        if kept and _backtrack(kept[-1], f, fixes[i + 1:i + 1 + OUTLIER_LOOKAHEAD]):
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped


def _speed(a, b):
    hours = max(b.tst - a.tst, SPEED_WINDOW_S) / 3600.0
    return haversine_km(a.lat, a.lon, b.lat, b.lon) / hours


def _derive_speeds(fixes):
    """Speed of each fix: the smaller of the displacement speeds over the
    SPEED_WINDOW_S before it and across it. A real flight is fast on both; a
    stray point is slow across itself, and slow before it when it is the far
    endpoint of an earlier fix's window. The leg after the fix is left out so
    a flight's last point before a quiet gap still reads as airborne.
    Anything faster than a jet is a data error and reads as 0; the jump rule
    still classifies the gap."""
    n = len(fixes)
    out = []
    for i, f in enumerate(fixes):
        k = i
        while k > 0 and f.tst - fixes[k].tst < SPEED_WINDOW_S:
            k -= 1
        m = i
        while m < n - 1 and fixes[m].tst - f.tst < SPEED_WINDOW_S:
            m += 1
        kmh = _speed(fixes[k], fixes[m])
        if k < i:
            kmh = min(kmh, _speed(fixes[k], f))
        out.append(f._replace(vel=kmh if kmh <= OUTLIER_MAX_KMH else 0.0))
    return out


def with_speeds(fixes):
    """Fixes with estimated speeds when the source supplied none."""
    if fixes and not any(f.vel for f in fixes):
        return _derive_speeds(fixes)
    return fixes


def parse(data):
    """Decoded export JSON -> (fixes sorted by strictly increasing tst, meta).
    Raises TimelineFormatError when the layout is not recognised."""
    if isinstance(data, dict):
        if isinstance(data.get("semanticSegments"), list):
            layout, items = "semantic", data["semanticSegments"]
        elif isinstance(data.get("locations"), list):
            layout, items = "records", data["locations"]
        else:
            raise TimelineFormatError(
                "Expected a list of Timeline segments, or an object with semanticSegments or locations")
    elif isinstance(data, list):
        layout, items = "semantic", data
    else:
        raise TimelineFormatError("Expected a JSON list or object")

    if layout == "records":
        fixes, counts = _parse_records(items)
    else:
        fixes, counts = _parse_segments(items)
    fixes, counts["outliers"] = _drop_outliers(_monotonic(fixes))
    fixes = with_speeds(fixes)
    meta = {"layout": layout, "segments": len(items), "counts": counts, "points": len(fixes)}
    if fixes:
        meta["first_tst"] = int(fixes[0].tst)
        meta["last_tst"] = int(fixes[-1].tst)
    return fixes, meta

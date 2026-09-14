import json
import time
from datetime import datetime, timezone

import pytest
from conftest import basic
from test_api import fake_recorder  # noqa: F401

import google_timeline
import imports
import track_cache

DAY = 86400


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def geo(lat, lon):
    return f"geo:{lat:.6f},{lon:.6f}"


def path_segment(start, minutes_and_points):
    return {"startTime": iso(start), "endTime": iso(start + 7200),
            "timelinePath": [{"point": geo(lat, lon), "durationMinutesOffsetFromStartTime": str(m)}
                             for m, lat, lon in minutes_and_points]}


def visit(start, end, lat, lon, level="0"):
    return {"startTime": iso(start), "endTime": iso(end),
            "visit": {"hierarchyLevel": level, "probability": "0.9",
                      "topCandidate": {"placeLocation": geo(lat, lon), "semanticType": "Home"}}}


def activity(start, end, a, b, kind="in passenger vehicle"):
    return {"startTime": iso(start), "endTime": iso(end),
            "activity": {"start": geo(*a), "end": geo(*b), "distanceMeters": "1000",
                         "topCandidate": {"type": kind, "probability": "0.5"}}}


T0 = 1_500_000_000


def sample_export():
    return [
        # A drive whose path has two points sharing one minute offset.
        path_segment(T0, [(0, 47.60, -122.33), (5, 47.61, -122.33), (5, 47.62, -122.33),
                          (10, 47.63, -122.33)]),
        # A visit fully inside the path's span: redundant with the path.
        visit(T0 + 60, T0 + 500, 47.605, -122.33),
        # An activity after the path: no path point covers it, so its ends are used.
        activity(T0 + 3 * 3600, T0 + 4 * 3600, (47.63, -122.33), (47.70, -122.30)),
        # A flight the next day, uncovered.
        activity(T0 + DAY, T0 + DAY + 5 * 3600, (47.45, -122.31), (25.80, -80.20), kind="flying"),
        # A level-1 visit is a trip container, ignored.
        visit(T0 + DAY, T0 + 3 * DAY, 25.80, -80.20, level="1"),
        # A visit two days later at the destination, uncovered.
        visit(T0 + 2 * DAY, T0 + 2 * DAY + 3600, 25.81, -80.19),
        {"startTime": iso(T0), "endTime": iso(T0 + DAY), "timelineMemory": {"destinations": []}},
    ]


def test_parse_semantic_segments():
    fixes, meta = google_timeline.parse(sample_export())
    assert meta["layout"] == "semantic"
    assert meta["counts"]["timelinePath"] == 4
    assert meta["counts"]["visit"] == 1
    assert meta["counts"]["activity"] == 2
    assert meta["counts"]["flying"] == 1
    assert meta["counts"]["skipped"] == 2          # memory + level-1 visit
    assert meta["points"] == 4 + 2 + 2 + 2
    assert all(b.tst > a.tst for a, b in zip(fixes, fixes[1:]))
    assert fixes[0].tst == T0 and fixes[1].tst == T0 + 300 and fixes[2].tst == T0 + 301
    assert meta["first_tst"] == T0
    assert all(f.alt == 0 and f.acc is None for f in fixes)


def test_parse_wrapped_and_records():
    fixes, meta = google_timeline.parse({"semanticSegments": sample_export()})
    assert meta["points"] == 10
    records = {"locations": [
        {"latitudeE7": 476000000, "longitudeE7": -1223300000, "timestamp": iso(T0),
         "accuracy": 12, "altitude": 30, "velocity": 10},
        {"latitudeE7": 476100000, "longitudeE7": -1223300000, "timestamp": iso(T0 + 60)},
        {"latitudeE7": 476100000, "timestamp": iso(T0 + 120)},
    ]}
    fixes, meta = google_timeline.parse(records)
    assert meta["layout"] == "records"
    assert meta["counts"] == {"records": 2, "skipped": 1, "outliers": 0}
    assert fixes[0].acc == 12 and fixes[0].alt == 30 and fixes[0].vel == pytest.approx(36)
    assert fixes[1].acc is None


@pytest.mark.parametrize("bad", [{"foo": []}, "text", 42, {"locations": "x"}])
def test_parse_rejects_unknown_layout(bad):
    with pytest.raises(google_timeline.TimelineFormatError):
        google_timeline.parse(bad)


def test_outlier_spike_dropped_but_relocation_kept():
    home, away = (47.60, -122.33), (20.90, -156.70)
    segs = [path_segment(T0, [(0, *away), (1, *home), (2, *away), (3, *away)]),
            # 6 hours later the phone really is home and stays there.
            path_segment(T0 + 6 * 3600, [(0, *home), (1, *home), (2, *home)])]
    fixes, meta = google_timeline.parse(segs)
    assert meta["counts"]["outliers"] == 1
    assert [round(f.lat, 2) for f in fixes] == [20.9, 20.9, 20.9, 47.6, 47.6, 47.6]


def test_derived_speed_flags_inflight_path_points():
    """Points every 6 minutes at cruise speed are closer than the jump rule but
    must still read as a flight; a freeway drive must not."""
    cruise = path_segment(T0, [(6 * i, 47.0 + i * 0.75, -122.0) for i in range(8)])  # ~83 km/6 min
    # The drive away from the airport starts where the flight ended, half an hour later.
    freeway = path_segment(T0 + 72 * 60, [(i, 52.25 + i * 0.018, -122.0) for i in range(30)])  # ~2 km/min
    fixes, _ = google_timeline.parse([cruise, freeway])
    cruise_fixes, freeway_fixes = fixes[:8], fixes[8:]
    assert all(f.vel > 800 for f in cruise_fixes[:-1])
    assert 100 < cruise_fixes[-1].vel < 322      # slow across the wait, still above exit speed
    assert all(f.vel < 130 for f in freeway_fixes[1:])
    from track import detect_flights
    intervals, _, _ = detect_flights(fixes)
    assert intervals == [[cruise_fixes[0].tst, cruise_fixes[-1].tst]]


def test_derived_speed_is_measured_across_a_stray_point():
    # A 30 km stray point in the middle of a town drive: the speed is taken
    # between its neighbours, so it neither reads as a jet nor lifts the
    # driving top speed; it is dropped by the thinning as a spike instead.
    pts = [(i, 47.0 + i * 0.003, -122.0) for i in range(20)]
    pts[10] = (10, 47.30, -122.0)
    fixes, _ = google_timeline.parse([path_segment(T0, pts)])
    from track import DEFAULT_FLIGHT_PARAMS, detect_flights
    assert max(f.vel for f in fixes) < DEFAULT_FLIGHT_PARAMS.entry_kmh
    assert detect_flights(fixes)[0] == []
    # A physically impossible reading (bad timestamps) is zeroed, not reported.
    far = [(0, 47.0, -122.0), (1, 47.0, -122.0), (2, 47.0, -122.0),
           (60, 60.0, -100.0), (61, 60.0, -100.0), (62, 60.0, -100.0)]
    fixes, _ = google_timeline.parse([path_segment(T0, far)])
    assert max(f.vel for f in fixes) == 0


def test_derived_speed_ignores_same_minute_hops():
    # Two points 2 km apart that share a minute offset must not read as 7200 km/h.
    seg = path_segment(T0, [(0, 47.0, -122.0), (5, 47.02, -122.0), (5, 47.038, -122.0),
                            (10, 47.05, -122.0)])
    fixes, _ = google_timeline.parse([seg])
    assert max(f.vel for f in fixes) < 60


def test_payload_stats_ignore_imported_speeds():
    spec = track_cache.Spec("track", "carol", ["phone"], import_sources=["google-timeline"])
    entry = track_cache.Entry(spec)
    entry.computed_at = 1.0
    entry.trackers["phone"].max_vel_driving = 120.0
    entry.trackers["import:google-timeline"].max_vel_driving = 999.0
    entry.trackers["import:google-timeline"].max_vel_flying = 990.0
    entry.trackers["import:google-timeline"].driving_km = 50.0
    stats = track_cache.payload(entry)["stats"]
    assert stats["driving"]["max_vel_kmh"] == 120.0
    assert stats["flying"]["max_vel_kmh"] == 0.0
    assert stats["driving"]["distance_km"] == 50.0


def test_select_skips_recorder_days():
    fixes, _ = google_timeline.parse(sample_export())
    covered = imports.covered_buckets([fixes[0]._replace(tst=T0 + DAY + 100)])
    kept, skipped = imports.select(fixes, None, True, T0 + 10 * DAY, covered)
    assert skipped == 2                       # the two flight fixes on day 1
    assert all(imports.bucket(f.tst) != imports.bucket(T0 + DAY) for f in kept)
    kept, skipped = imports.select(fixes, T0 + 2 * DAY, True, T0 + 2 * DAY + 60, set())
    assert [f.tst for f in kept] == [T0 + 2 * DAY]


def test_load_upgrades_speedless_import_in_place():
    cruise = path_segment(T0, [(6 * i, 47.0 + i * 0.75, -122.0) for i in range(8)])
    fixes, meta = google_timeline.parse([cruise])
    stale = [f._replace(vel=0.0) for f in fixes]
    imports.save("dave", "google-timeline", stale, meta)
    try:
        loaded = imports.load_fixes("dave", "google-timeline")
        assert loaded == fixes
        with open(imports._paths("dave", "google-timeline")[0], "rb") as fh:
            import pickle
            assert [f.vel for f in loaded] == [row[3] for row in pickle.load(fh)]
    finally:
        imports.delete("dave", "google-timeline")


def test_storage_roundtrip_and_fingerprint():
    fixes, meta = google_timeline.parse(sample_export())
    assert imports.list_imports("carol") == {}
    assert imports.fingerprint("carol") == ""
    row = imports.save("carol", "google-timeline", fixes, meta)
    assert row["source"] == "google-timeline" and row["points"] == 10
    assert imports.list_imports("carol")["google-timeline"]["imported_at"] == row["imported_at"]
    assert imports.fingerprint("carol") == f"google-timeline:{row['imported_at']}"
    assert imports.load_fixes("carol", "google-timeline") == fixes
    assert imports.delete("carol", "google-timeline") is True
    assert imports.delete("carol", "google-timeline") is False
    assert imports.list_imports("carol") == {}
    with pytest.raises(ValueError):
        imports.save("carol", "other", fixes, meta)


def _export_bytes(segments):
    return json.dumps(segments).encode()


def get_settled(client, path, **query):
    """GET, polling through the 202s of a background compute."""
    r = client.get(path, headers=basic("alice"), query_string=query or None)
    for _ in range(400):
        if r.status_code != 202:
            break
        time.sleep(0.02)
        r = client.get(path, headers=basic("alice"), query_string=query or None)
    return r


def old_history(base):
    """Two years before the recorder's data: a drive, and a fix on the recorder's
    first day that must lose to OwnTracks."""
    old = base - 2 * 365 * DAY
    return [
        path_segment(old, [(0, 47.60, -122.33), (10, 47.65, -122.33), (20, 47.70, -122.33)]),
        visit(base + 3600, base + 7200, 47.60, -122.33),
    ]


def test_import_endpoints(client, fake_recorder):  # noqa: F811
    base = fake_recorder["base"]
    old_range = {"from": iso(base - 3 * 365 * DAY), "to": iso(base - 365 * DAY)}
    imports.delete("alice", "google-timeline")

    assert client.get("/api/me/imports").status_code == 401
    assert client.get("/api/me/imports", headers=basic("alice")).json == {"imports": []}
    assert client.put("/api/me/imports/nope", data=b"[]", headers=basic("alice")).status_code == 404
    r = client.put("/api/me/imports/google-timeline", data=b"{not json", headers=basic("alice"))
    assert r.status_code == 400 and "JSON" in r.json["error"]
    r = client.put("/api/me/imports/google-timeline", data=b'{"foo": 1}', headers=basic("alice"))
    assert r.status_code == 400 and "Timeline" in r.json["error"]
    r = client.put("/api/me/imports/google-timeline", data=b"[]", headers=basic("alice"))
    assert r.status_code == 400 and "no usable" in r.json["error"]

    before = get_settled(client, "/api/me/track")
    assert before.status_code == 200 and before.json["imports"] == {}

    r = client.put("/api/me/imports/google-timeline", data=_export_bytes(old_history(base)),
                   headers=basic("alice"))
    assert r.status_code == 200
    assert r.json["points"] == 5 and r.json["source"] == "google-timeline"
    listed = client.get("/api/me/imports", headers=basic("alice")).json["imports"]
    assert [row["source"] for row in listed] == ["google-timeline"]

    # The cache was cleared, so this is a fresh all-time entry.
    r = get_settled(client, "/api/me/track")
    assert r.status_code == 200
    body = r.json
    assert body["imports"] == {"google-timeline": {"points": 3, "skipped": 2}}
    assert body["earliest_tst"] == base - 2 * 365 * DAY
    assert body["stats"]["driving"]["distance_km"] > before.json["stats"]["driving"]["distance_km"]
    assert body["latest_tst"] == before.json["latest_tst"]

    # A closed range in the imported years is computed from the import alone.
    r = get_settled(client, "/api/me/track", **old_range)
    assert r.status_code == 200
    assert r.json["imports"] == {"google-timeline": {"points": 3, "skipped": 0}}
    assert r.json["stats"]["driving"]["distance_km"] == pytest.approx(11.1, abs=0.2)

    # Heatmap counts the imported fixes too.
    r = get_settled(client, "/api/me/heatmap", **old_range)
    assert r.status_code == 200 and len(r.json["cells"]) == 3

    # Selecting one device keeps the import: it belongs to the account.
    r = get_settled(client, "/api/me/track", device="ipad", **old_range)
    assert r.status_code == 200 and r.json["imports"]["google-timeline"]["points"] == 3

    r = client.delete("/api/me/imports/google-timeline", headers=basic("alice"))
    assert r.status_code == 200 and r.json == {"removed": True}
    r = get_settled(client, "/api/me/track", **old_range)
    assert r.status_code == 200 and r.json["imports"] == {} and r.json["latest_tst"] is None
    assert client.delete("/api/me/imports/google-timeline", headers=basic("alice")).json == {"removed": False}


def test_import_without_recorder_devices(client, fake_recorder, monkeypatch):  # noqa: F811
    import recorder
    monkeypatch.setattr(recorder, "list_devices", lambda user: [])
    base = fake_recorder["base"]
    imports.delete("alice", "google-timeline")
    r = get_settled(client, "/api/me/track")
    assert r.status_code == 200 and r.json["latest_tst"] is None

    r = client.put("/api/me/imports/google-timeline", data=_export_bytes(old_history(base)),
                   headers=basic("alice"))
    assert r.status_code == 200
    r = get_settled(client, "/api/me/track", **{"from": iso(base - 3 * 365 * DAY)})
    assert r.status_code == 200
    # Nothing in the recorder, so the fix on the recorder's first day is kept too.
    assert r.json["imports"] == {"google-timeline": {"points": 5, "skipped": 0}}
    imports.delete("alice", "google-timeline")


def test_upload_limit(client, monkeypatch):
    monkeypatch.setitem(client.application.config, "MAX_CONTENT_LENGTH", 100)
    r = client.put("/api/me/imports/google-timeline", data=b"[" + b" " * 200 + b"]",
                   headers=basic("alice"))
    assert r.status_code == 413 and "too large" in r.json["error"]

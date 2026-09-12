import time
from datetime import datetime, timezone

import pytest
from conftest import basic
from synthetic import journey, points

import aggregate
import recorder
import track_cache

DAY = 86400


@pytest.fixture
def fake_recorder(monkeypatch):
    """A recorder with one user 'alice' whose phone recorded journey() starting 10 days ago."""
    fixes, marks = journey()
    base = int(time.time()) - 10 * DAY
    shifted = [f._replace(tst=f.tst + base) for f in fixes]
    pts = points(shifted)

    def list_devices(user):
        assert user == "alice"
        return ["phone", "ipad"]

    def list_rec_months(user, device):
        assert user == "alice"
        d = datetime.fromtimestamp(base, timezone.utc)
        return [(d.year, d.month)] if device == "phone" else []

    def fetch_points(user, device, from_dt, to_dt):
        assert user == "alice"
        lo, hi = from_dt.timestamp(), to_dt.timestamp()
        return [p for p in pts if device == "phone" and lo <= p["tst"] <= hi]

    calls = {"fetch": 0, "last": 0}

    def counted_fetch(*a, **k):
        calls["fetch"] += 1
        return fetch_points(*a, **k)

    def last_fixes(user):
        assert user == "alice"
        calls["last"] += 1
        return [{"username": "alice", "device": "phone", "tst": pts[-1]["tst"]}]

    monkeypatch.setattr(recorder, "list_devices", list_devices)
    monkeypatch.setattr(recorder, "list_rec_months", list_rec_months)
    monkeypatch.setattr(recorder, "fetch_points", counted_fetch)
    monkeypatch.setattr(recorder, "last_fixes", last_fixes)
    monkeypatch.setattr(track_cache, "MIN_REFRESH_SECONDS", 0)
    return {"base": base, "marks": marks, "fixes": shifted, "pts": pts, "calls": calls}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("path", ["/api/me/devices", "/api/me/track", "/api/me/heatmap"])
def test_missing_credentials_401(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == 'Basic realm="WhereHaveIBeen"'


def test_wrong_password_401(client):
    assert client.get("/api/me/devices", headers=basic("alice", "nope")).status_code == 401


@pytest.mark.parametrize("path", ["/api/me/devices", "/api/me/track", "/api/me/heatmap"])
def test_inactive_account_403(client, path):
    assert client.get(path, headers=basic("bob")).status_code == 403


def test_devices(client, fake_recorder):
    r = client.get("/api/me/devices", headers=basic("alice"))
    assert r.status_code == 200
    assert r.get_json() == {"username": "alice", "devices": ["phone", "ipad"]}


def test_devices_empty_when_recorder_has_none(client, monkeypatch):
    monkeypatch.setattr(recorder, "list_devices", lambda u: [])
    assert client.get("/api/me/devices", headers=basic("alice")).get_json()["devices"] == []


@pytest.mark.parametrize("query,message", [
    ("from=2024-01-01T00:00:00", "Invalid from/to"),
    ("from=yesterday", "Invalid from/to"),
    ("from=2024-02-01T00:00:00Z&to=2024-01-01T00:00:00Z", "from must not be after to"),
    ("device=watch", "Unknown device"),
    ("buffer_m=big", "buffer_m must be an integer"),
])
def test_bad_parameters_400(client, fake_recorder, query, message):
    r = client.get(f"/api/me/track?{query}", headers=basic("alice"))
    assert r.status_code == 400
    assert message in r.get_json()["error"]


def test_no_user_query_parameter_is_honoured(client, fake_recorder):
    r = client.get("/api/me/devices?user=someone-else", headers=basic("alice"))
    assert r.get_json()["username"] == "alice"


def test_closed_range_track_inline(client, fake_recorder):
    base = fake_recorder["base"]
    frm, to = iso(base - DAY), iso(base + 2 * DAY)
    r = client.get(f"/api/me/track?from={frm}&to={to}&buffer_m=50", headers=basic("alice"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["range"] == {"from": frm, "to": to}
    assert body["buffer_m"] == 100
    assert body["latest_tst"] == fake_recorder["fixes"][-1].tst
    assert body["earliest_tst"] == fake_recorder["fixes"][0].tst
    assert body["driving"]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert body["flights_buffer"]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert len(body["flights"]["features"]) == 1
    flight = body["flights"]["features"][0]
    assert flight["properties"]["start_tst"] == fake_recorder["marks"]["roll_start"] + 150 + base
    assert flight["geometry"]["type"] == "LineString"
    stats = body["stats"]
    assert set(stats["driving"]) == {"distance_km", "area_km2", "max_alt_m", "max_vel_kmh"}
    assert stats["driving"]["max_vel_kmh"] == 120
    assert stats["flying"]["max_vel_kmh"] == 850 and stats["flying"]["max_alt_m"] == 11000
    assert stats["driving"]["area_km2"] > 0 and stats["flying"]["distance_km"] > 100

    again = client.get(f"/api/me/track?from={frm}&to={to}&buffer_m=50", headers=basic("alice"))
    assert again.get_json()["computed_at"] == body["computed_at"]


def test_open_ended_all_time_polls_then_serves(client, fake_recorder):
    r = client.get("/api/me/track?device=phone", headers=basic("alice"))
    assert r.status_code == 202
    assert r.get_json() == {"status": "computing"}
    assert int(r.headers["Retry-After"]) > 0
    for _ in range(100):
        r = client.get("/api/me/track?device=phone", headers=basic("alice"))
        if r.status_code == 200:
            break
        time.sleep(0.1)
    assert r.status_code == 200
    body = r.get_json()
    assert body["range"]["from"] is None
    assert body["stats"]["driving"]["max_vel_kmh"] == 120
    assert body["stats"]["flying"]["max_vel_kmh"] == 850
    assert len(body["flights"]["features"]) == 1

    # Warm entry: a second request refreshes incrementally and stays consistent.
    r = client.get("/api/me/track?device=phone", headers=basic("alice"))
    assert r.status_code == 200
    assert r.get_json()["stats"] == body["stats"]


def test_heatmap_closed_range(client, fake_recorder):
    base = fake_recorder["base"]
    r = client.get(f"/api/me/heatmap?from={iso(base - DAY)}&to={iso(base + 2 * DAY)}",
                   headers=basic("alice"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["cell_deg"] == 0.0006
    assert sum(c[2] for c in body["cells"]) == sum(
        1 for f in fake_recorder["fixes"] if f.acc is None or f.acc < 100)
    assert all(len(c) == 3 for c in body["cells"])


def test_refresh_recomputes_closed_entry(client, fake_recorder):
    base = fake_recorder["base"]
    q = f"from={iso(base - DAY)}&to={iso(base + 2 * DAY)}"
    first = client.get(f"/api/me/track?{q}", headers=basic("alice")).get_json()
    time.sleep(1.1)
    second = client.get(f"/api/me/track?{q}&refresh=1", headers=basic("alice")).get_json()
    assert second["computed_at"] > first["computed_at"]
    assert second["stats"] == first["stats"]


def test_no_devices_gives_empty_payload(client, monkeypatch):
    monkeypatch.setattr(recorder, "list_devices", lambda u: [])
    r = client.get("/api/me/track", headers=basic("alice"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["latest_tst"] is None and body["earliest_tst"] is None
    assert body["driving"]["geometry"] is None
    assert body["stats"]["driving"]["distance_km"] == 0.0


def test_unsafe_username_rejected():
    with pytest.raises(ValueError):
        track_cache.Spec("track", "../etc", ["phone"])


def test_aggregate_gains_area_km2(monkeypatch):
    fixes, _ = journey()
    pts = points(fixes)
    monkeypatch.setattr(recorder, "list_users", lambda: ["alice"])
    monkeypatch.setattr(recorder, "list_devices", lambda u: ["phone"])
    monkeypatch.setattr(recorder, "fetch_points",
                        lambda u, d, f, t: pts if f.year == 2024 and f.month == 8 else [])
    feature = aggregate.compute_union()
    assert set(feature["properties"]) == {"max_vel", "max_alt", "distance_km", "area_km2"}
    assert feature["properties"]["area_km2"] > 0
    assert feature["properties"]["max_vel"] == 850
    assert "stats-v3" in aggregate.PARAMS_FINGERPRINT


def _warm_all_time(client):
    for _ in range(100):
        r = client.get("/api/me/track?device=phone", headers=basic("alice"))
        if r.status_code == 200:
            return r
        time.sleep(0.1)
    raise AssertionError("never warmed")


def test_warm_request_probes_last_fix_instead_of_refetching(client, fake_recorder):
    _warm_all_time(client)
    calls = fake_recorder["calls"]
    fetched, probed = calls["fetch"], calls["last"]
    r = client.get("/api/me/track?device=phone", headers=basic("alice"))
    assert r.status_code == 200
    assert calls["fetch"] == fetched
    assert calls["last"] == probed + 1


def test_min_refresh_window_skips_the_probe(client, fake_recorder, monkeypatch):
    _warm_all_time(client)
    monkeypatch.setattr(track_cache, "MIN_REFRESH_SECONDS", 60)
    probed = fake_recorder["calls"]["last"]
    client.get("/api/me/track?device=phone", headers=basic("alice"))
    assert fake_recorder["calls"]["last"] == probed


def test_etag_304_and_invalidation(client, fake_recorder, monkeypatch):
    r = _warm_all_time(client)
    etag = r.headers["ETag"]
    assert r.headers["Cache-Control"] == "private, no-cache"

    r2 = client.get("/api/me/track?device=phone", headers={**basic("alice"), "If-None-Match": etag})
    assert r2.status_code == 304 and r2.headers["ETag"] == etag and r2.data == b""

    # refresh=1 recreates the entry: always a body, and a new tag afterwards.
    r3 = client.get("/api/me/track?device=phone&refresh=1",
                    headers={**basic("alice"), "If-None-Match": etag})
    while r3.status_code == 202:
        time.sleep(0.1)
        r3 = client.get("/api/me/track?device=phone", headers={**basic("alice"), "If-None-Match": etag})
    assert r3.status_code == 200 and r3.headers["ETag"] != etag
    etag = r3.headers["ETag"]

    # A newer fix on the device: the probe sees it, the entry extends, the tag changes.
    pts = fake_recorder["pts"]
    last = pts[-1]
    pts.append({**last, "tst": last["tst"] + 600, "lat": last["lat"] + 0.01})
    r4 = client.get("/api/me/track?device=phone", headers={**basic("alice"), "If-None-Match": etag})
    assert r4.status_code == 200
    assert r4.headers["ETag"] != etag
    assert r4.get_json()["latest_tst"] == last["tst"] + 600


def test_aggregate_etag(client, fake_recorder, monkeypatch):
    feature = {"type": "Feature", "properties": {}, "geometry": None}
    monkeypatch.setattr(aggregate, "_cache", {"geojson": feature, "computed_at": 1700000000.0,
                                              "params_fingerprint": aggregate.PARAMS_FINGERPRINT})
    r = client.get("/api/aggregate-roads", headers=basic("alice"))
    assert r.status_code == 200 and r.headers["ETag"] == '"agg-1700000000"'
    r2 = client.get("/api/aggregate-roads", headers={**basic("alice"), "If-None-Match": r.headers["ETag"]})
    assert r2.status_code == 304


def test_update_reports_progress_per_window(fake_recorder, monkeypatch):
    seen = []
    real_fetch = recorder.fetch_points

    def spy(*a, **k):
        seen.append(dict(entry.progress))
        return real_fetch(*a, **k)

    monkeypatch.setattr(recorder, "fetch_points", spy)
    spec = track_cache.Spec("track", "alice", ["phone"], None, None, 500)
    entry = track_cache.Entry(spec)
    track_cache.update(entry, time.time())
    assert seen and all(p["stage"] == "fetching" for p in seen)
    assert seen[0]["total"] == len(seen) + 1
    assert entry.progress is None
    assert entry.driving_area > 0
    # journey() spans one month, so a single window (plus margins) covers it.
    assert len(seen) <= 3


def test_202_body_carries_progress_when_available(client, fake_recorder):
    # buffer_m=1234 makes a key no earlier test has warmed.
    r = client.get("/api/me/track?device=phone&buffer_m=1234", headers=basic("alice"))
    assert r.status_code == 202
    body = r.get_json()
    assert body["status"] == "computing"
    if "progress" in body:
        assert set(body["progress"]) == {"stage", "done", "total"}
    for _ in range(100):
        if client.get("/api/me/track?device=phone&buffer_m=1234", headers=basic("alice")).status_code == 200:
            break
        time.sleep(0.1)

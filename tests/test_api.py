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

    monkeypatch.setattr(recorder, "list_devices", list_devices)
    monkeypatch.setattr(recorder, "list_rec_months", list_rec_months)
    monkeypatch.setattr(recorder, "fetch_points", fetch_points)
    return {"base": base, "marks": marks, "fixes": shifted}


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
    assert body["latest_tst"] is None
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

import pytest
from synthetic import journey

import track
from track import DeviceTracker

BUFFER_M = 500


def run(batches):
    """Feed batches through one tracker, buffering as the cache would. Returns
    (tracker, driving geom, flights geom)."""
    t = DeviceTracker()
    driving = flights = None
    for batch in batches + [None]:
        inc = t.flush() if batch is None else t.feed(batch)
        driving = track.merge_geometry(
            driving, track.buffer_polylines(inc.driving_lines, BUFFER_M, 15, 30))
        flights = track.merge_geometry(
            flights, track.buffer_polylines(inc.flight_lines, BUFFER_M, 15, 30))
    return t, driving, flights


def split_at(fixes, tst):
    return [[f for f in fixes if f.tst < tst], [f for f in fixes if f.tst >= tst]]


@pytest.mark.parametrize("marker", ["mid_flight", "roll_start", "landed"])
def test_two_halves_match_one_pass(marker):
    fixes, marks = journey()
    split = marks[marker] + (200 if marker == "roll_start" else 1)
    one, d1, f1 = run([fixes])
    two, d2, f2 = run(split_at(fixes, split))

    assert one.intervals == two.intervals
    assert one.intervals[0][0] == marks["roll_start"] + 150   # roll pulled in from 150 km/h fix
    assert one.driving_km == pytest.approx(two.driving_km, abs=1e-9)
    assert one.flying_km == pytest.approx(two.flying_km, abs=1e-9)
    assert (one.max_vel_driving, one.max_alt_driving) == (two.max_vel_driving, two.max_alt_driving)
    assert (one.max_vel_flying, one.max_alt_flying) == (two.max_vel_flying, two.max_alt_flying)
    assert (one.max_vel_flying, one.max_alt_flying) == (850, 11000)
    assert one.max_vel_driving == 120
    assert [(fl["start_tst"], fl["end_tst"], fl["coords"]) for fl in one.flights] == \
        [(fl["start_tst"], fl["end_tst"], fl["coords"]) for fl in two.flights]

    a1, a2 = track.geometry_area_km2(d1), track.geometry_area_km2(d2)
    assert a1 == pytest.approx(a2, rel=0.01)
    b1, b2 = track.geometry_area_km2(f1), track.geometry_area_km2(f2)
    assert b1 == pytest.approx(b2, rel=0.01)


def test_split_inside_lookback_defers_roll_until_settled():
    fixes, marks = journey()
    first, second = split_at(fixes, marks["roll_start"] + 200)
    t = DeviceTracker()
    t.feed(first)
    assert t.tail and t.tail[0].vel == 150
    assert t.max_vel_driving == 120
    t.feed(second)
    t.flush()
    assert t.max_vel_driving == 120
    assert t.intervals[0][0] == marks["roll_start"] + 150


def test_preview_settles_tail_without_mutating():
    fixes, marks = journey()
    first, _ = split_at(fixes, marks["roll_start"] + 200)
    t = DeviceTracker()
    t.feed(first)
    before = t.to_state()
    inc, settled = t.preview()
    assert not settled.tail
    assert t.to_state() == before
    assert inc.driving_lines


def test_state_round_trip():
    fixes, marks = journey()
    first, second = split_at(fixes, marks["mid_flight"])
    t = DeviceTracker()
    t.feed(first)
    restored = DeviceTracker.from_state(t.to_state())
    restored.feed(second)
    restored.flush()
    one, _, _ = run([fixes])
    assert restored.intervals == one.intervals
    assert restored.flying_km == pytest.approx(one.flying_km)

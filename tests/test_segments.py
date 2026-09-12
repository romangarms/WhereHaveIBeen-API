from synthetic import fix

from track import DeviceTracker, group_segments


def _kept(n):
    return [fix(i * 60, i * 0.01, 0) for i in range(n)]


def tsts(groups):
    return [[f.tst for f in g] for g in groups]


def test_runs_become_segments_and_flights_bridge_one_fix_each_side():
    kept = _kept(6)
    driving, flying = group_segments(kept, [False, False, True, True, False, False])
    assert tsts(driving) == [[0, 60], [240, 300]]
    assert tsts(flying) == [[60, 120, 180, 240]]


def test_single_flying_fix_is_bridged_and_single_driving_fixes_are_dropped():
    kept = _kept(3)
    driving, flying = group_segments(kept, [False, True, False])
    assert driving == []
    assert tsts(flying) == [[0, 60, 120]]


def test_flight_at_track_edges_bridges_only_where_a_neighbour_exists():
    kept = _kept(4)
    driving, flying = group_segments(kept, [True, True, False, False])
    assert tsts(flying) == [[0, 60, 120]]
    assert tsts(driving) == [[120, 180]]
    driving, flying = group_segments(kept, [False, False, True, True])
    assert tsts(flying) == [[60, 120, 180]]


def test_tracker_grouping_matches_group_segments():
    fixes = [fix(0, 0, 0, vel=50), fix(60, 0.01, 0, vel=50), fix(120, 0.02, 0, vel=400),
             fix(180, 0.05, 0, vel=400), fix(240, 0.06, 0, vel=20), fix(300, 0.07, 0, vel=50)]
    t = DeviceTracker()
    inc = t.feed(fixes)
    inc.extend(t.flush())
    assert inc.driving_lines == [[(0, 0), (0, 0.01)], [(0, 0.06), (0, 0.07)]]
    assert inc.flight_lines == [[(0, 0.01), (0, 0.02), (0, 0.05), (0, 0.06)]]
    assert len(t.flights) == 1
    assert (t.flights[0]["start_tst"], t.flights[0]["end_tst"]) == (120, 180)
    assert t.flights[0]["coords"] == inc.flight_lines[0]

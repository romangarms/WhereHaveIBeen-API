from synthetic import drive, fix

import track
from track import DeviceTracker, classify_flights, detect_flights

ENTRY = track.DEFAULT_FLIGHT_PARAMS.entry_kmh


def flags(fixes):
    return classify_flights(fixes)[0]


def test_entry_speed_is_exact_mph_conversion():
    assert ENTRY == 200 * 1.609344


def test_freeway_drive_never_opens_flight():
    fixes = drive(0, 60, 60, 120, km_per_step=2.0)
    intervals, kept, kept_flying = detect_flights(fixes)
    assert intervals == []
    assert not any(kept_flying)
    assert len(kept) == 60


def test_takeoff_roll_pulled_in_from_first_fix_above_exit_speed():
    fixes = [fix(0, 0, 0, vel=60), fix(200, 0.002, 0, vel=150),
             fix(400, 0.004, 0, vel=250), fix(600, 0.008, 0, vel=330)]
    assert flags(fixes) == [False, True, True, True]
    intervals, _, _ = detect_flights(fixes)
    assert intervals == [[200, 600]]


def test_fix_older_than_lookback_not_pulled_in_even_if_fast():
    fixes = [fix(0, 0, 0, vel=150), fix(2000, 0.002, 0, vel=150), fix(2100, 0.004, 0, vel=330)]
    assert flags(fixes) == [False, True, True]


def test_slow_point_with_recent_signal_stays_airborne_then_stale_lands():
    fixes = [fix(0, 0, 0, vel=400), fix(300, 0.03, 0, vel=400), fix(600, 0.06, 0, vel=400),
             fix(700, 0.07, 0, vel=180),      # below entry, above exit, signal 100 s ago
             fix(900, 0.09, 0, vel=400),
             fix(2800, 0.1, 0, vel=180)]      # 1900 s of silence -> stale
    assert flags(fixes) == [True, True, True, True, True, False]
    intervals, _, _ = detect_flights(fixes)
    assert intervals == [[0, 900]]


def test_approach_below_entry_above_exit_keeps_flight_open():
    fixes = [fix(0, 0, 0, vel=400), fix(300, 0.03, 0, vel=180), fix(600, 0.04, 0, vel=180),
             fix(900, 0.05, 0, vel=180), fix(1000, 0.051, 0, vel=20)]
    assert flags(fixes) == [True, True, True, True, False]


def test_altitude_alone_opens_flight_and_slow_high_fix_does_not_close_it():
    fixes = [fix(0, 0, 0, vel=50, alt=7000), fix(300, 0.01, 0, vel=30, alt=7000),
             fix(600, 0.02, 0, vel=30, alt=100)]
    assert flags(fixes) == [True, True, False]
    intervals, _, _ = detect_flights(fixes)
    assert intervals == [[0, 300]]


def test_jump_carried_by_poor_accuracy_fixes_still_marks_drawable_link_flying():
    fixes = [fix(0, 0.0, 0, acc=10), fix(100, 0.5, 0, acc=150), fix(200, 1.0, 0, acc=150),
             fix(300, 1.5, 0, acc=10)]
    intervals, kept, kept_flying = detect_flights(fixes)
    assert intervals == []
    assert [k.tst for k in kept] == [0, 300]
    assert kept_flying == [False, True]


def test_single_fix_before_jump_pulled_in_regardless_of_lookback():
    jumped = [fix(0, 0, 0, vel=150), fix(10000, 2.0, 0, vel=400)]
    assert flags(jumped) == [True, True]
    intervals, _, _ = detect_flights(jumped)
    assert intervals == [[0, 10000]]

    no_jump = [fix(0, 0, 0, vel=150), fix(10000, 0.001, 0, vel=400)]
    assert flags(no_jump) == [False, True]


def test_thinning_drops_fixes_within_20m():
    fixes = [fix(0, 0, 0), fix(10, 0.0001, 0), fix(20, 0.0003, 0)]  # 11 m, 33 m from first
    _, kept, _ = detect_flights(fixes)
    assert [k.tst for k in kept] == [0, 20]


def test_poor_accuracy_fix_in_flight_feeds_flying_maxima_only():
    fixes = [fix(0, 0, 0, vel=50, alt=10, acc=10),
             fix(60, 0.01, 0, vel=400, alt=3000, acc=10),
             fix(120, 0.05, 0, vel=500, alt=9000, acc=500),
             fix(180, 0.08, 0, vel=20, alt=10, acc=10)]
    t = DeviceTracker()
    t.feed(fixes)
    t.flush()
    assert (t.max_vel_flying, t.max_alt_flying) == (500, 9000)
    assert (t.max_vel_driving, t.max_alt_driving) == (50, 10)
    assert t.intervals == [[60, 120]]


def test_missing_vel_alt_treated_as_zero_and_missing_acc_as_accurate():
    fixes = track.fixes_from_points([{"lat": 0, "lon": 0, "tst": 5},
                                     {"lat": 0.001, "lon": 0, "tst": 1, "vel": None}])
    assert [f.tst for f in fixes] == [1, 5]
    assert fixes[0].vel == 0 and fixes[0].alt == 0 and fixes[0].acc is None
    _, kept, _ = detect_flights(fixes)
    assert len(kept) == 2

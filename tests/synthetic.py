"""Builders for synthetic tracks. Latitude degrees ~ 111 km; 0.009 deg ~ 1 km."""

from track import Fix

KM_LAT = 1 / 111.0


def fix(tst, lat=0.0, lon=0.0, vel=0.0, alt=0.0, acc=10.0):
    return Fix(float(lat), float(lon), float(tst), float(vel), float(alt), acc)


def drive(start, count, step_s, vel, lat0=0.0, lon0=0.0, km_per_step=1.0, alt=0.0, acc=10.0):
    """count fixes step_s apart moving north km_per_step per step."""
    return [fix(start + i * step_s, lat0 + i * km_per_step * KM_LAT, lon0, vel, alt, acc)
            for i in range(count)]


def points(fixes):
    """Fix list -> recorder JSON points."""
    out = []
    for f in fixes:
        p = {"lat": f.lat, "lon": f.lon, "tst": int(f.tst), "vel": f.vel, "alt": f.alt}
        if f.acc is not None:
            p["acc"] = f.acc
        out.append(p)
    return out


def journey():
    """
    A full synthetic day: freeway drive, stop, take-off roll, flight with
    periodic signals and a poor-accuracy fix at altitude, approach, landing,
    drive away. Returns (fixes, markers) where markers names key timestamps.
    """
    fixes = []
    t = 0
    fixes += drive(t, 60, 60, 120, km_per_step=2.0)             # 1 h freeway
    t = fixes[-1].tst + 60
    fixes += [fix(t, fixes[-1].lat, 0.0, vel=0.0)]              # stopped
    t += 600
    roll_start = t
    lat = fixes[-1].lat
    roll = [(60, 0.2), (150, 0.5), (250, 1.0), (330, 2.0)]
    for vel, km in roll:
        lat += km * KM_LAT
        fixes.append(fix(t, lat, 0.0, vel=vel, alt=50))
        t += 150
    takeoff_signal = fixes[-1].tst
    cruise = []
    for i in range(12):                                          # 1 h cruise
        lat += 12 * KM_LAT
        acc = 300.0 if i == 6 else 10.0
        alt = 11000 if i == 6 else 9000
        cruise.append(fix(t, lat, 0.0, vel=850, alt=alt, acc=acc))
        t += 300
    fixes += cruise
    mid_flight = cruise[6].tst
    for _ in range(4):                                           # approach 180 km/h
        lat += 3 * KM_LAT
        fixes.append(fix(t, lat, 0.0, vel=180, alt=800))
        t += 120
    lat += 0.5 * KM_LAT
    fixes.append(fix(t, lat, 0.0, vel=30, alt=20))               # landed
    landed = t
    t += 600
    fixes += drive(t, 30, 60, 90, lat0=lat + KM_LAT, km_per_step=1.5)
    return fixes, {"roll_start": roll_start, "takeoff_signal": takeoff_signal,
                   "mid_flight": mid_flight, "landed": landed}

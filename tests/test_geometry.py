import math

import track

# 1 km due east along the equator.
LINE = [(0.0, 0.0), (1 / 111.319, 0.0)]


def expected_corridor_km2(length_km, radius_m):
    return (length_km * 1000 * 2 * radius_m + math.pi * radius_m ** 2) / 1e6


def test_polyline_distance_matches_haversine():
    assert abs(track.segments_distance_km([LINE]) - 1.0) < 0.002


def test_buffer_union_of_short_line_has_expected_area():
    polys = track.buffer_polylines([LINE], 100, 0, 0)
    geom = track.merge_geometry(None, polys)
    area = track.geometry_area_km2(geom)
    assert abs(area - expected_corridor_km2(1.0, 100)) / expected_corridor_km2(1.0, 100) < 0.05


def test_dissolve_segments_area_matches_buffer_polylines():
    geom, area = track.dissolve_segments([LINE], 100, 0, 0)
    assert geom is not None
    assert abs(area - expected_corridor_km2(1.0, 100)) / expected_corridor_km2(1.0, 100) < 0.05
    assert track.dissolve_segments([], 100, 15, 30) == (None, 0.0)


def test_overlapping_lines_dissolve_without_double_counting():
    half = [(0.0, 0.0), (0.5 / 111.319, 0.0)]
    polys = track.buffer_polylines([LINE, half], 100, 0, 0)
    geom = track.merge_geometry(None, polys)
    assert geom.geom_type == "MultiPolygon" and len(geom.geoms) == 1
    area = track.geometry_area_km2(geom)
    assert abs(area - expected_corridor_km2(1.0, 100)) / expected_corridor_km2(1.0, 100) < 0.05


def test_merge_geometry_keeps_distant_parts_and_merges_touching_ones():
    far = [(10.0, 10.0), (10.0, 10.01)]
    base = track.merge_geometry(None, track.buffer_polylines([LINE, far], 100, 0, 0))
    assert len(base.geoms) == 2
    extension = [(1 / 111.319, 0.0), (2 / 111.319, 0.0)]
    merged = track.merge_geometry(base, track.buffer_polylines([extension], 100, 0, 0))
    assert len(merged.geoms) == 2
    area = track.geometry_area_km2(merged)
    expected = expected_corridor_km2(2.0, 100) + expected_corridor_km2(1.1119, 100)
    assert abs(area - expected) / expected < 0.05


def test_long_flight_corridor_is_valid_and_split_at_antimeridian():
    sfo_tokyo = [(-122.4, 37.6), (139.8, 35.5)]
    polys = track.buffer_polylines([sfo_tokyo], 500, 15, 30)
    assert polys and all(p.is_valid for p in polys)
    assert all(-180 <= p.bounds[0] and p.bounds[2] <= 180 and p.bounds[2] - p.bounds[0] < 180
               for p in polys)
    geom = track.merge_geometry(None, polys)
    length_km = track.haversine_km(37.6, -122.4, 35.5, 139.8)
    area = track.geometry_area_km2(geom)
    assert abs(area - length_km * 1.0) / (length_km * 1.0) < 0.05


def test_chunking_keeps_polyline_connected():
    line = [(-122.4, 37.6), (-73.8, 40.6)]  # SFO -> NYC, ~4130 km
    polys = track.buffer_polylines([line], 500, 15, 30)
    assert len(polys) > 1
    geom = track.merge_geometry(None, polys)
    assert len(geom.geoms) == 1
    length_km = track.haversine_km(37.6, -122.4, 40.6, -73.8)
    assert abs(track.geometry_area_km2(geom) - length_km) / length_km < 0.05


def test_piecewise_buffer_matches_whole_line_buffer():
    # A self-crossing local drive far longer than one buffer piece.
    line = [(-122.4 + 0.002 * math.sin(i / 3.0), 37.6 + 0.002 * math.cos(i / 7.0))
            for i in range(4 * track.BUFFER_PIECE_VERTICES)]
    pieced = track.merge_geometry(None, track.buffer_polylines([line], 100, 0, 0))
    to_metric, to_wgs84 = track.build_transformers(*line[len(line) // 2])
    whole = track.shapely_transform(
        to_wgs84, track.shapely_transform(to_metric, track.LineString(line)).buffer(100, quad_segs=2))
    assert pieced.symmetric_difference(whole).area / whole.area < 1e-3

from synthetic import fix

import track


def test_cell_indexing_uses_js_rounding():
    cells = track.add_heat_cells([fix(0, lat=-0.0003, lon=0.00031)], {})
    assert cells == {(1, 0): 1}
    assert track.js_round(-2.5) == -2 and track.js_round(2.5) == 3


def test_accuracy_filter_and_missing_acc():
    fixes = [fix(0, 0, 0, acc=100), fix(1, 0, 0, acc=99.9), fix(2, 0, 0, acc=None)]
    assert track.add_heat_cells(fixes, {}) == {(0, 0): 2}


def test_merge_adds_counts_across_devices():
    cells = track.add_heat_cells([fix(0, 0, 0), fix(1, 0.0006, 0)], {})
    track.add_heat_cells([fix(2, 0, 0)], cells)
    assert cells == {(0, 0): 2, (0, 1): 1}

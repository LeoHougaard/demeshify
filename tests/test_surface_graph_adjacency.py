from types import SimpleNamespace

import numpy as np
import pytest

from app.surface_graph import _freeform_charts


@pytest.mark.parametrize("selected,expected", [
    ([0, 1], {(0, 1)}),
    ([0, 1, 2, 3], {(0, 1), (2, 3)}),
    ([0, 1, 3, 4], {(0, 1), (3,), (4,)}),
])
def test_chart_uses_internal_creases_without_crossing_unselected_faces(selected, expected):
    mesh = SimpleNamespace(
        faces=np.zeros((5, 3), dtype=int),
        face_adjacency=np.array(((0, 1), (1, 2), (2, 3), (3, 4))),
        face_normals=np.array(((0, 0, 1), (0, 0, 1), (1, 0, 0), (1, 0, 0), (0, 0, -1))),
        area_faces=np.arange(1, 6, dtype=float),
    )
    charts = _freeform_charts(mesh, np.array(selected), smooth_angle_degrees=30)
    assert {tuple(chart) for chart in charts} == expected
    np.testing.assert_array_equal(np.sort(np.concatenate(charts)), selected)

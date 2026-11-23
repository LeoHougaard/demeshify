import sys

import numpy as np
import pytest
import rtree as native
import trimesh

from web.browser_python import rtree as browser


@pytest.mark.parametrize("dimension", [2, 3])
def test_intersections_match_native(dimension):
    rng = np.random.default_rng(43)
    low = rng.normal(size=(5003, dimension))
    high = low + rng.uniform(0, 0.15, low.shape)
    high[0] = low[0]
    low[1], high[1] = -100, 100
    boxes = np.c_[low, high]
    ids = rng.integers(-2000, 2000, len(low))

    def build(module):
        entries = ((i, box, None) for i, box in zip(ids, boxes, strict=True))
        return module.index.Index(entries, properties=module.index.Property(dimension=dimension))

    tree, ref = build(browser), build(native)
    np.testing.assert_array_equal(tree.bounds, ref.bounds)
    for query in [*boxes[:100], *high[:100], np.full(dimension, 105.0)]:
        assert sorted(tree.intersection(query)) == sorted(ref.intersection(query))


def test_containment_matches_native(monkeypatch):
    mesh = trimesh.creation.icosphere(subdivisions=4)
    probes = np.random.default_rng(25).uniform(-1.2, 1.2, (300, 3))

    ref_inside = mesh.contains(probes)
    assert ref_inside.any() and not ref_inside.all()
    monkeypatch.setitem(sys.modules, "rtree", browser)
    copy = mesh.copy()
    inside = copy.contains(probes)
    assert isinstance(copy.triangles_tree, browser.Index)
    np.testing.assert_array_equal(inside, ref_inside)

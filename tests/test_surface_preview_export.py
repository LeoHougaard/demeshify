import cadquery as cq
import numpy as np
import pytest
import trimesh
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location

from app.step import load_step_shape
from app.surface_brep import SurfaceBRepResult, export_surface_brep
from app.surface_graph import SurfaceGraph


def test_complete_export_preserves_native_geometry_and_unmeshed_source(tmp_path):
    shape = cq.Workplane("XY").box(12, 8, 4).faces(">Z").workplane().hole(2).val()
    shape = shape.translate((100, 200, 300))
    vertices = np.asarray([vertex.Center().toTuple() for vertex in shape.Vertices()])
    volume = shape.Volume()
    assert all(BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()) is None
               for face in shape.Faces())
    result = SurfaceBRepResult(
        shape=shape, graph=SurfaceGraph(), surface_counts={}, face_count=len(shape.Faces()),
        solid_count=1, free_edge_count=0, sewing_tolerance=.02, valid=True, closed=True,
    )

    export_surface_brep(result, tmp_path)

    delivered = load_step_shape(tmp_path / "reconstruction.step")
    preview = trimesh.load(tmp_path / "reconstruction.stl", force="mesh")
    assert result.closed and delivered.isValid() and len(delivered.Solids()) == 1
    assert delivered.Volume() == pytest.approx(volume)
    assert preview.is_watertight and preview.is_winding_consistent
    assert preview.euler_number == 0 and len(preview.split()) == 1
    np.testing.assert_allclose(preview.extents, [12, 8, 4], atol=1e-6)
    np.testing.assert_allclose(preview.bounds.mean(axis=0), [100, 200, 300], atol=1e-6)
    np.testing.assert_array_equal(
        [vertex.Center().toTuple() for vertex in shape.Vertices()], vertices,
    )
    assert shape.Volume() == volume
    assert all(BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()) is None
               for face in shape.Faces())

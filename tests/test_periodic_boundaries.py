import cadquery as cq
import numpy as np
import pytest

from app.surface_brep import _replace_periodic_boundary_edges, _TrimEdge


def test_periodic_end_edges_match_position_when_circumferences_are_equal(tmp_path):
    face = next(
        face for face in cq.Solid.makeCylinder(5, 10).Faces()
        if face.geomType() == "CYLINDER"
    )
    ends = [edge for edge in face.Edges() if edge.geomType() == "CIRCLE"]
    trims = []
    for end in reversed(ends):
        center = end.Center().toTuple()
        edge = cq.Edge.makeCircle(5, center)
        angles = np.linspace(0, 2 * np.pi, 17)
        points = np.column_stack((5 * np.cos(angles), 5 * np.sin(angles), angles * 0))
        points += np.asarray(center)
        points[-1] = points[0]
        trims.append(_TrimEdge(edge.wrapped, points))

    replaced = _replace_periodic_boundary_edges(face.wrapped, trims, 1e-5)
    result = cq.Face(replaced)

    assert result.isValid()
    for trim in trims:
        assert any(edge.wrapped.IsSame(trim.edge) for edge in result.Edges())
    assert result.Area() == pytest.approx(face.Area(), rel=1e-8)
    path = tmp_path / "periodic-face.step"
    cq.exporters.export(result, str(path))
    imported = cq.importers.importStep(str(path)).val()
    assert imported.isValid()
    assert imported.Area() == pytest.approx(face.Area(), rel=1e-8)

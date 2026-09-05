import cadquery as cq
import numpy as np
import pytest
import trimesh
from OCP.BRep import BRep_Tool
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

from app.surface_brep import _faceted_residual_faces, _TrimEdge
from app.surface_graph import FreeformPatch


@pytest.mark.parametrize("width,amplitude", [(0.05, 0.002), (0.02, 0.001), (0.01, 0.0005)])
def test_thin_transition_preserves_its_curved_boundary_after_step_export(
    tmp_path, width, amplitude
):
    points = np.array([[0.0, 0.0, 0.0], [1.2, 0.0, 0.0], [1.2, width, 0.0]])
    mesh = trimesh.Trimesh(vertices=points, faces=[[0, 1, 2]], process=False)
    patch = FreeformPatch(
        "thin", np.array([0]), np.arange(3), mesh.area, [np.vstack((points, points[0]))]
    )
    parameters = np.linspace(0, 1, 9)
    curve_points = (
        (1 - parameters[:, None]) * points[0] + parameters[:, None] * points[1]
    )
    curve_points[:, 2] += amplitude * np.sin(np.pi * parameters)
    boundary = cq.Edge.makeSpline([cq.Vector(*point) for point in curve_points])
    trim = _TrimEdge(boundary.wrapped, curve_points, source_points=points[:2])

    faces = _faceted_residual_faces(patch, [trim], mesh, 0.02)

    assert len(faces) == 1
    path = tmp_path / "transition.step"
    cq.exporters.export(cq.Face(faces[0]), str(path))
    imported = cq.importers.importStep(str(path)).val()
    assert imported.isValid()
    assert len(imported.Faces()) == 1
    support = BRep_Tool.Surface_s(imported.Faces()[0].wrapped)
    deviations = [
        GeomAPI_ProjectPointOnSurf(boundary.positionAt(float(parameter)).toPnt(), support)
        .LowerDistance()
        for parameter in np.linspace(0, 1, 41)
    ]
    assert max(deviations) < 0.0001

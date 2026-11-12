from dataclasses import replace

import numpy as np
import pytest
import trimesh
from OCP.BRep import BRep_Tool
from OCP.GeomAPI import GeomAPI_ProjectPointOnCurve

from app.surface_brep import _linear_extrusion_face
from app.surface_graph import LinearExtrusionPatch


@pytest.mark.parametrize('scale', [1.0, 0.04])
@pytest.mark.parametrize('duplicate', [1, 11, 18])
def test_repeated_open_profile_node_does_not_change_extruded_geometry(scale, duplicate):
    angles = np.linspace(-np.pi / 2, np.pi / 2, 22)
    profile = scale * np.column_stack((-5 * np.cos(angles), np.zeros(22), 10 * np.sin(angles)))
    vertices = np.vstack((profile, profile + (0, scale * 0.7, 0)))
    faces = [face for i in range(21)
             for face in ((i, i + 1, i + 23), (i, i + 23, i + 22))]
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    patch = LinearExtrusionPatch(
        'open-ellipse', np.arange(len(faces)), np.arange(len(vertices)), np.array((0, 1, 0)),
        profile, 0, scale * 0.7, mesh.area, [], 0, 0, 0,
    )
    repeated = replace(
        patch, profile_points=np.insert(profile, duplicate, profile[duplicate], axis=0),
    )
    clean_face = _linear_extrusion_face(patch, [], mesh, scale * 0.012)
    repeated_face = _linear_extrusion_face(repeated, [], mesh, scale * 0.012)
    assert clean_face is not None and repeated_face is not None
    curves = [BRep_Tool.Surface_s(face).BasisCurve() for face in (clean_face, repeated_face)]
    for source, target in (curves, curves[::-1]):
        for parameter in np.linspace(source.FirstParameter(), source.LastParameter(), 101):
            point = source.Value(float(parameter))
            projection = GeomAPI_ProjectPointOnCurve(point, target)
            assert projection.NbPoints() and projection.LowerDistance() <= scale * 1e-7

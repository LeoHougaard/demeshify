import numpy as np
import pytest
import trimesh
from OCP.BRep import BRep_Tool
from OCP.GeomAPI import GeomAPI_ProjectPointOnCurve
from OCP.gp import gp_Pnt

from app.surface_brep import _linear_extrusion_face
from app.surface_graph import LinearExtrusionPatch, _point_to_polyline_distances


def uneven_profile(count=15):
    x = np.r_[np.linspace(0, 0.24, count - 2), 10.45, 10.47]
    return np.column_stack((x, 0.04 * x + 0.003 * np.sin(25 * x), np.zeros(len(x))))


def test_extrusion_profile_stays_near_the_whole_source_span():
    profile = uneven_profile()
    vertices = np.vstack((profile, profile + (0, 0, 2)))
    count = len(profile)
    faces = [
        face for i in range(count - 1)
        for face in ([i, i + 1, i + count], [i + 1, i + count + 1, i + count])
    ]
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    patch = LinearExtrusionPatch(
        patch_id="uneven-extrusion", face_indices=np.arange(len(faces)),
        vertex_indices=np.arange(len(vertices)), direction=np.array((0, 0, 1)),
        profile_points=profile, start=0, end=2, area=mesh.area,
        boundary_loops=[], rms_error=0, max_error=0, normal_error_degrees=0,
    )
    tolerance = 0.012
    face = _linear_extrusion_face(patch, [], mesh, tolerance)
    assert face is not None
    curve = BRep_Tool.Surface_s(face).BasisCurve()
    parameters = np.linspace(curve.FirstParameter(), curve.LastParameter(), 4001)
    samples = np.asarray([curve.Value(float(t)).Coord() for t in parameters])
    curve_tolerance = tolerance * 0.5
    assert max(_point_to_polyline_distances(samples, profile)) <= curve_tolerance
    source_samples = np.vstack([
        a + (b - a) * np.linspace(0, 1, 33)[:, None]
        for a, b in zip(profile[:-1], profile[1:], strict=True)
    ])
    assert max(
        GeomAPI_ProjectPointOnCurve(gp_Pnt(*point), curve).LowerDistance()
        for point in source_samples
    ) <= curve_tolerance


def test_balanced_curved_profile_does_not_gain_unnecessary_chord_constraints():
    from app.surface_brep import _profile_chord_anchors

    angle = np.linspace(0, np.pi / 3, 20)
    profile = np.column_stack((10 * np.cos(angle), 10 * np.sin(angle), np.zeros(20)))
    np.testing.assert_array_equal(_profile_chord_anchors(profile), profile)


@pytest.mark.parametrize("count", [15, 96, 127, 128])
def test_chord_anchors_keep_original_points_and_fit_budget(count):
    from app.surface_brep import _profile_chord_anchors

    profile = uneven_profile(count)
    anchored = _profile_chord_anchors(profile)
    assert len(profile) <= len(anchored) <= 128
    assert all(any(np.array_equal(point, candidate) for candidate in anchored) for point in profile)

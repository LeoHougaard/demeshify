import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh

import app.surface_graph as graph
from app.surface_graph import (
    SurfaceOfRevolutionPatch,
    _boundary_loops,
    _build_adjacency,
    _fit_component,
    _make_analytic_patch,
    _piecewise_linear_revolution_regions,
)


def revolved_source(profile, *, sections=48, phases=None):
    profile = np.asarray(profile, dtype=float)
    angles = np.arange(sections) * math.tau / sections
    phases = np.zeros(len(profile)) if phases is None else phases
    vertices = np.asarray([(radius * np.cos(angle), radius * np.sin(angle), axial)
                           for (axial, radius), phase in zip(profile, phases, strict=True)
                           for angle in angles + phase])
    faces = []
    for row in range(len(profile) - 1):
        for column in range(sections):
            low = row * sections + column
            next_low = row * sections + (column + 1) % sections
            faces.extend(((low, next_low, next_low + sections),
                          (low, next_low + sections, low + sections)))
    rotation = trimesh.transformations.rotation_matrix(.63, (1., -2., .5))
    vertices = trimesh.transform_points(vertices, rotation)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    axis = rotation[:3, 2].copy()
    meridian = trimesh.transform_points(np.column_stack((profile[:, 1],
                                                        np.zeros(len(profile)), profile[:, 0])),
                                       rotation)
    indices = np.arange(len(mesh.faces))
    patch = SurfaceOfRevolutionPatch(
        'revolution-source', indices, np.arange(len(vertices)), np.zeros(3), axis,
        rotation[:3, 0].copy(), meridian, float(mesh.area), _boundary_loops(mesh, indices),
        0., 0., 0.,
    )
    return SimpleNamespace(mesh=mesh, diagonal=float(np.linalg.norm(mesh.extents))), patch


def assert_exact_shared_ring(data, pieces, axis, axial, radius):
    patches = [_make_analytic_patch(data.mesh, fit, indices, number)
               for number, (fit, indices) in enumerate(pieces, 1)]
    labels = np.empty(len(data.mesh.faces), dtype=object)
    for patch in patches:
        labels[patch.face_indices] = patch.patch_id
    adjacency = _build_adjacency(data.mesh, labels, patches, 1e-4)
    assert len(adjacency) == 1
    assert len(adjacency[0].boundary_curves) == 1
    ring = adjacency[0].boundary_curves[0]
    assert len(ring) == 49
    assert ring @ axis == pytest.approx(np.full(len(ring), axial), abs=1e-12)
    assert np.linalg.norm(ring - np.outer(ring @ axis, axis), axis=1) == pytest.approx(
        np.full(len(ring), radius), abs=1e-12,
    )
    assert np.array_equal(np.sort(np.concatenate([indices for _, indices in pieces])),
                          np.arange(len(data.mesh.faces)))


@pytest.mark.parametrize('profile', [
    [(0., 2.), (1., 2.), (1.5, 2.25), (2., 2.5)],
    [(0., 2.5), (.5, 2.25), (1., 2.), (2., 2.)],
])
def test_cone_cylinder_breakpoint_retains_exact_source_shared_ring(profile):
    data, patch = revolved_source(profile)
    original_vertices = data.mesh.vertices.copy()
    pieces = _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4)
    assert sorted(fit.kind for fit, _ in pieces) == ['cone', 'cylinder']
    cone = next(fit for fit, _ in pieces if fit.kind == 'cone')
    cylinder = next(fit for fit, _ in pieces if fit.kind == 'cylinder')
    assert math.tan(cone.parameters['semi_angle']) == pytest.approx(.5, abs=1e-8)
    assert cylinder.parameters['radius'] == pytest.approx(2., abs=1e-8)
    assert all(fit.max_error < 1e-8 for fit, _ in pieces)
    assert np.array_equal(data.mesh.vertices, original_vertices)
    assert_exact_shared_ring(data, pieces, patch.axis, 1., 2.)


def test_two_distinct_cone_slopes_preserve_the_existing_meridian_corner():
    data, patch = revolved_source([(0., 2.), (.5, 2.1), (1., 2.2),
                                   (1.5, 2.45), (2., 2.7)])
    pieces = _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4)
    assert [fit.kind for fit, _ in pieces] == ['cone', 'cone']
    slopes = sorted(math.tan(fit.parameters['semi_angle']) for fit, _ in pieces)
    assert slopes == pytest.approx([.2, .5], abs=1e-8)
    assert_exact_shared_ring(data, pieces, patch.axis, 1., 2.2)


def test_staggered_angular_rows_do_not_fragment_an_exact_conical_meridian():
    data, patch = revolved_source(
        [(0., 2.), (1., 2.), (1.5, 2.25), (2., 2.5)], sections=48,
        phases=(0., math.pi / 48, 0., math.pi / 48),
    )
    cone_angles = np.arcsin(np.clip(data.mesh.face_normals[96:] @ patch.axis, -1., 1.))
    assert np.ptp(cone_angles) > math.radians(.5)
    pieces = _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4)
    assert sorted(fit.kind for fit, _ in pieces) == ['cone', 'cylinder']
    assert all(fit.max_error < 1e-8 for fit, _ in pieces)
    assert np.array_equal(np.sort(np.concatenate([indices for _, indices in pieces])),
                          np.arange(len(data.mesh.faces)))


def test_coarse_staggered_rows_do_not_bypass_existing_primitive_validation():
    data, patch = revolved_source(
        [(0., 2.), (1., 2.), (1.5, 2.25), (2., 2.5)], sections=24,
        phases=(0., math.pi / 24, 0., math.pi / 24),
    )
    assert _fit_component(data, np.arange(48), 1e-4, 5.,
                          allowed_kinds=('cylinder',)) is None
    assert _fit_component(data, np.arange(48, 144), 1e-4, 5.,
                          allowed_kinds=('cone',)) is None
    assert _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4) == []


def test_rounded_meridian_is_not_replaced_by_independent_conical_chords():
    angles = np.asarray((0., .2, .4, .6))
    data, patch = revolved_source(np.column_stack((np.sin(angles), 2. - np.cos(angles))))
    # A single tessellated meridian interval can fit a cone, which is why the
    # extra independent interior level is necessary before splitting a curve.
    chord_fit = _fit_component(data, np.arange(96), 1e-4, 5., allowed_kinds=('cone',))
    assert chord_fit is not None and chord_fit.kind == 'cone'
    assert _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4) == []


def test_tangent_cylinder_and_round_meridian_remain_one_original_region():
    angles = np.asarray((.2, .4))
    rounded = np.column_stack((1. + .5 * np.sin(angles), 2. + .5 * (1. - np.cos(angles))))
    data, patch = revolved_source(np.vstack(((0., 2.), (1., 2.), rounded)))
    original = patch.face_indices.copy()
    assert _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4) == []
    assert np.array_equal(patch.face_indices, original)


def test_single_existing_conical_support_is_not_fragmented():
    data, patch = revolved_source([(0., 2.), (.5, 2.25), (1., 2.5)])
    assert _piecewise_linear_revolution_regions(data, patch, 1e-4, 5., 1e-4) == []


def source_cylinders(data, indices):
    cylinders = []
    for number, faces in enumerate(indices, 1):
        fit = graph._fit_component(data, faces, 1e-4, 5., allowed_kinds=('cylinder',))
        assert fit is not None and fit.kind == 'cylinder'
        cylinders.append(graph._make_analytic_patch(data.mesh, fit, faces, number))
    return cylinders


def meridian_with_source_supports(profile):
    data, patch = revolved_source(profile)
    indices = [np.arange(96), np.arange(len(data.mesh.faces) - 96, len(data.mesh.faces))]
    cylinders = source_cylinders(data, indices)
    residual = np.arange(96, len(data.mesh.faces) - 96)
    return data, patch, cylinders, residual


def test_source_coaxial_axis_can_partition_before_generic_revolution_is_fit():
    data, patch, cylinders, residual = meridian_with_source_supports(
        [(0., 2.), (1., 2.), (1.5, 2.1), (2., 2.2),
         (2.5, 2.45), (3., 2.7), (4., 2.7)])
    before = data.mesh.vertices.copy()
    axis = graph._coaxial_cylinder_axis(cylinders, data.diagonal, 1e-4)
    assert axis is not None and abs(float(axis @ patch.axis)) == pytest.approx(1.)
    pieces = graph._coaxial_profile_regions(data, residual, axis, 1e-4, 5., 1e-4)
    assert [fit.kind for fit, _ in pieces] == ['cone', 'cone']
    assert [len(faces) for _, faces in pieces] == [192, 192]
    assert max(fit.max_error for fit, _ in pieces) < 1e-8
    assert np.array_equal(np.sort(np.concatenate([faces for _, faces in pieces])), residual)
    assert np.array_equal(data.mesh.vertices, before)


def test_one_source_cylinder_or_duplicate_ownership_cannot_establish_axis():
    data, _, cylinders, _ = meridian_with_source_supports(
        [(0., 2.), (1., 2.), (1.5, 2.1), (2., 2.2), (3., 2.2)])
    assert graph._coaxial_cylinder_axis(cylinders[:1], data.diagonal, 1e-4) is None
    duplicate = replace(cylinders[0], patch_id='cylinder-duplicate')
    assert graph._coaxial_cylinder_axis(
        [cylinders[0], duplicate], data.diagonal, 1e-4) is None


@pytest.mark.parametrize('alteration', ['offset', 'different_axis'])
def test_independent_real_cylinder_geometry_must_be_coaxial(alteration):
    first, _ = revolved_source([(0., 2.), (1., 2.)])
    other = first.mesh.copy()
    if alteration == 'offset':
        other.apply_translation((5., 0., 0.))
    else:
        other.apply_transform(trimesh.transformations.rotation_matrix(.2, (1., 0., 0.)))
    mesh = trimesh.util.concatenate([first.mesh, other])
    data = SimpleNamespace(mesh=mesh, diagonal=float(np.linalg.norm(mesh.extents)))
    cylinders = source_cylinders(data, [np.arange(96), np.arange(96, 192)])
    assert graph._coaxial_cylinder_axis(cylinders, data.diagonal, 1e-4) is None


def test_source_supported_axis_does_not_turn_rounded_meridian_into_cones():
    data, _, cylinders, residual = meridian_with_source_supports(
        [(0., 2.), (1., 2.), (1.2, 2.02), (1.4, 2.08),
         (1.6, 2.18), (2.6, 2.18)])
    axis = graph._coaxial_cylinder_axis(cylinders, data.diagonal, 1e-4)
    assert axis is not None
    assert graph._coaxial_profile_regions(
        data, residual, axis, 1e-4, 5., 1e-4) == []


def test_whole_source_cone_survives_before_any_local_or_generic_fit():
    data, _, cylinders, residual = meridian_with_source_supports(
        [(0., 2.), (1., 2.), (1.5, 2.25), (2., 2.5), (3., 2.5)])
    axis = graph._coaxial_cylinder_axis(cylinders, data.diagonal, 1e-4)
    assert axis is not None
    assert graph._piecewise_linear_axis_regions(
        data, residual, axis, 1e-4, 5., 1e-4) == []
    pieces = graph._coaxial_profile_regions(data, residual, axis, 1e-4, 5., 1e-4)
    assert len(pieces) == 1 and pieces[0][0].kind == 'cone'
    assert np.array_equal(pieces[0][1], residual)
    assert pieces[0][0].max_error < 1e-8

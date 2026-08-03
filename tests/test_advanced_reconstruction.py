from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
from shapely.geometry import Point
from shapely.geometry import Polygon as ShapelyPolygon

import app.profiles as profiles_module
import app.reconstruction as reconstruction_module
from app.cad import build_plan
from app.mesh import load_mesh
from app.profiles import (
    PlanCandidate,
    _fit_path_profile,
    _mesh_spherical_features,
    _profile_from_ring,
    generate_adaptive_layer_candidates,
    generate_analytic_cylinder_separation_candidates,
    generate_arbitrary_axis_prismatic_candidates,
    generate_change_weighted_layer_candidates,
    generate_circular_end_finish_candidates,
    generate_conical_hole_candidates,
    generate_cross_axis_residual_candidates,
    generate_embedded_circle_promotion_candidates,
    generate_end_finish_candidates,
    generate_feature_round_finish_candidates,
    generate_local_tangent_envelope_candidates,
    generate_oriented_cylinder_candidates,
    generate_profiled_endcap_cylinder_candidates,
    generate_spherical_corner_finish_candidates,
    mesh_has_conical_patch,
    rank_curve_aligned_axes,
)
from app.reconstruction import reconstruct
from app.schemas import (
    ArcSegment,
    Axis,
    BooleanExtrudeFeature,
    CircleProfile,
    ConicalHoleFeature,
    CylinderFeature,
    EdgeFinishFeature,
    ExtrudeFeature,
    LineSegment,
    OrientedBooleanExtrudeFeature,
    OrientedCylinderFeature,
    OrientedExtrudeFeature,
    PathProfile,
    PolygonProfile,
    ReconstructionPlan,
    RevolveFeature,
    RoundHoleFeature,
    SphereFeature,
    SplineProfile,
    SplineSegment,
    TaperedAddFeature,
)


def step_to_stl(model: cq.Workplane, directory: Path, name: str) -> Path:
    """Round-trip through STEP before tessellating, as a real benchmark would."""
    step_path = directory / f"{name}.step"
    stl_path = directory / f"{name}.stl"
    cq.exporters.export(model, str(step_path))
    neutral_model = cq.importers.importStep(str(step_path))
    cq.exporters.export(
        neutral_model,
        str(stl_path),
        tolerance=0.025,
        angularTolerance=0.08,
    )
    return stl_path


def test_shallow_tangent_round_is_not_replaced_by_a_chord() -> None:
    center = np.asarray([0.0, 0.4])
    arc = np.asarray(
        [
            center
            + 0.4
            * np.asarray([math.cos(angle), math.sin(angle)])
            for angle in np.linspace(math.radians(-120), math.radians(-90), 5)
        ]
    )
    ring = np.vstack(([-3, 2], arc, [5, 0], [5, 2], [-3, 2]))

    profile = _fit_path_profile(ring, tolerance=0.008)

    assert profile is not None
    assert any(
        isinstance(segment, ArcSegment)
        and segment.end == pytest.approx((0.0, 0.0), abs=0.002)
        for segment in profile.segments
    )


def test_two_large_radius_shallow_arcs_remain_analytic() -> None:
    points = np.asarray(
        [
            (-3.103224, 3.295958),
            (-1.884283, 2.864182),
            (-0.653081, 2.468719),
            (0.5893, 2.109918),
            (1.841768, 1.788094),
            (3.103224, 1.503529),
            (1.487668, -0.075665),
            (-0.107367, -1.675583),
            (-1.681616, -3.295958),
            (-3.103224, 3.295958),
        ]
    )

    profile = _fit_path_profile(points, tolerance=0.0125)

    assert profile is not None
    assert sum(
        isinstance(segment, ArcSegment) for segment in profile.segments
    ) == 2
    shape = build_plan(
        ReconstructionPlan(
            name="shallow_double_arc",
            base=ExtrudeFeature(
                axis=Axis.Y,
                start=-10.0,
                depth=20.0,
                outer=profile,
            ),
        )
    ).val()
    assert shape.isValid()
    assert sum(face.geomType() == "CYLINDER" for face in shape.Faces()) == 2


def test_change_weighted_layers_reuse_probe_sections(tmp_path: Path) -> None:
    stl_path = step_to_stl(
        cq.Workplane("XY").sphere(5.0),
        tmp_path,
        "weighted_probe_sphere",
    )
    data = load_mesh(stl_path, stl_path.name, "mm")

    candidates = generate_change_weighted_layer_candidates(
        data,
        "weighted_probe_sphere",
        Axis.X,
        sample_count=12,
        layer_budget=6,
    )

    assert candidates
    assert all(
        candidate.representation == "sampled_approximation"
        for candidate in candidates
    )
    assert len(data.section_cache) == 12


def test_curve_axis_ranking_is_driven_by_shallow_arc_geometry(
    tmp_path: Path,
) -> None:
    radius = 20.0
    start_angle = math.radians(-30.0)
    mid_angle = math.radians(-15.0)
    tangent_stock = (
        cq.Workplane("XY")
        .moveTo(-20.0, -10.0)
        .lineTo(radius * math.cos(start_angle), radius * math.sin(start_angle))
        .threePointArc(
            (radius * math.cos(mid_angle), radius * math.sin(mid_angle)),
            (radius, 0.0),
        )
        .lineTo(radius, 10.0)
        .lineTo(-20.0, 10.0)
        .close()
        .extrude(8.0)
    )
    stl_path = step_to_stl(tangent_stock, tmp_path, "generic_tangent_stock")
    data = load_mesh(stl_path, stl_path.name, "mm")

    ranked = rank_curve_aligned_axes(data, excluded_axis=Axis.Y)

    assert ranked
    assert ranked[0][0] == Axis.Z
    assert ranked[0][1] >= 3


def test_curve_axis_ranking_does_not_trigger_for_plain_box(tmp_path: Path) -> None:
    stl_path = step_to_stl(
        cq.Workplane("XY").box(20, 12, 8),
        tmp_path,
        "plain_box",
    )
    data = load_mesh(stl_path, stl_path.name, "mm")

    assert rank_curve_aligned_axes(data, excluded_axis=Axis.Y) == []


def test_equal_cost_revolve_fit_keeps_full_tangent_arc() -> None:
    fillet_radius = 2.5807
    tangent_radius = 10.0
    fillet_center = np.asarray([tangent_radius - fillet_radius, 0.6452])
    arc = np.asarray(
        [
            fillet_center
            + fillet_radius
            * np.asarray([math.cos(angle), math.sin(angle)])
            for angle in np.linspace(-math.pi / 2, 0, 54)
        ]
    )
    profile_points = np.vstack(
        (
            arc,
            [tangent_radius, 1.9355],
            [0.0, 1.9355],
            [0.0, -1.9355],
            arc[0],
        )
    )

    profile = _fit_path_profile(profile_points, tolerance=0.02284)

    assert profile is not None
    arc_segments = [
        segment for segment in profile.segments if isinstance(segment, ArcSegment)
    ]
    assert len(arc_segments) == 1
    assert arc_segments[0].end == pytest.approx((10.0, 0.645), abs=0.008)
    arc_index = profile.segments.index(arc_segments[0])
    tangent = profile.segments[(arc_index + 1) % len(profile.segments)]
    assert isinstance(tangent, LineSegment)
    assert tangent.end[0] == pytest.approx(10.0, abs=0.002)


def test_changing_curvature_tangent_is_an_editable_spline_segment() -> None:
    x = np.linspace(0.0, 8.0, 35)
    # Curvature changes continuously, so no single circle can represent this
    # tapering transition.  Its start tangent joins the horizontal edge.
    y = 0.025 * x**2 + 0.004 * x**3
    ring = np.vstack(
        (
            np.column_stack((x, y)),
            (8.0, 6.0),
            (-2.0, 6.0),
            (-2.0, 0.0),
            (0.0, 0.0),
        )
    )

    profile = _fit_path_profile(ring, tolerance=0.012)

    assert profile is not None
    splines = [
        segment for segment in profile.segments if isinstance(segment, SplineSegment)
    ]
    assert len(splines) == 1
    assert splines[0].start_tangent == pytest.approx((1.0, 0.0), abs=0.002)
    solid = build_plan(
        ReconstructionPlan(
            name="tapering_tangent",
            base=ExtrudeFeature(
                axis=Axis.Z,
                start=0.0,
                depth=2.0,
                outer=profile,
            ),
        )
    ).val()
    assert solid.isValid()


def test_cross_axis_cylinders_become_round_holes_on_x_y_and_z(
    tmp_path: Path,
) -> None:
    model = cq.Workplane("XY").box(30, 24, 18, centered=(True, True, True))
    for origin, direction in (
        ((-16, 0, 0), (1, 0, 0)),
        ((0, -13, 0), (0, 1, 0)),
        ((0, 0, -10), (0, 0, 1)),
    ):
        cutter = cq.Workplane(
            obj=cq.Solid.makeCylinder(
                2.0,
                32.0,
                cq.Vector(*origin),
                cq.Vector(*direction),
            )
        )
        model = model.cut(cutter)
    stl_path = step_to_stl(model, tmp_path, "three_axis_holes")
    data = load_mesh(stl_path, stl_path.name, "mm")
    source = ReconstructionPlan(
        name="three_axis_holes",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-9.0,
            depth=18.0,
            outer=PolygonProfile(
                points=[(-15, -12), (15, -12), (15, 12), (-15, 12)]
            ),
        ),
    )

    candidates = generate_oriented_cylinder_candidates(data, source)

    assert candidates
    holes = [
        operation
        for operation in candidates[0].operations
        if isinstance(operation, RoundHoleFeature)
    ]
    assert len(holes) == 3
    assert {hole.axis for hole in holes} == {Axis.X, Axis.Y, Axis.Z}
    assert all(hole.diameter == pytest.approx(4.0, abs=0.01) for hole in holes)
    assert build_plan(candidates[0]).val().isValid()


def test_complete_multi_plane_holes_replace_only_covered_circle_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ReconstructionPlan(
        name="fragmented-three-axis-holes",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-5,
            depth=10,
            outer=PolygonProfile(
                points=[(-10, -10), (10, -10), (10, 10), (-10, 10)]
            ),
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="cut",
                axis=Axis.X,
                start=-5,
                depth=5,
                outer=CircleProfile(center=(0, 0), radius=2.001),
            ),
            BooleanExtrudeFeature(
                mode="cut",
                axis=Axis.X,
                start=0,
                depth=5,
                outer=CircleProfile(center=(0, 0), radius=1.999),
            ),
            BooleanExtrudeFeature(
                mode="cut",
                axis=Axis.Y,
                start=-5,
                depth=10,
                outer=CircleProfile(center=(0, 0), radius=2.002),
                # A distinct stepped portion must survive promotion of the
                # measured radius-two bore.
                additional_regions=[
                    CircleProfile(center=(4, 0), radius=1.0)
                ],
            ),
            # This short feature used to suppress the complete Z-axis bore
            # because matching ignored start and depth.
            RoundHoleFeature(
                axis=Axis.Z,
                center=(0, 0),
                diameter=4,
                start=-5,
                depth=4,
                through=False,
            ),
            # A path with arcs is not assumed to be a full circle and must not
            # be deleted by circle deduplication.
            BooleanExtrudeFeature(
                mode="cut",
                axis=Axis.Z,
                start=-5,
                depth=10,
                outer=PathProfile(
                    start=(-4, -1),
                    segments=[
                        LineSegment(end=(-2, -1)),
                        ArcSegment(mid=(-1, 0), end=(-2, 1)),
                        LineSegment(end=(-4, 1)),
                        ArcSegment(mid=(-5, 0), end=(-4, -1)),
                    ],
                ),
            ),
        ],
    )
    measured = [
        OrientedCylinderFeature(
            mode="cut",
            origin=(-5, 0, 0),
            direction=(1, 0, 0),
            depth=10,
            radius=2,
        ),
        OrientedCylinderFeature(
            mode="cut",
            origin=(0, -5, 0),
            direction=(0, 1, 0),
            depth=10,
            radius=2,
        ),
        OrientedCylinderFeature(
            mode="cut",
            origin=(0, 0, -5),
            direction=(0, 0, 1),
            depth=10,
            radius=2,
        ),
        OrientedCylinderFeature(
            mode="cut",
            origin=(5, 0, -5),
            direction=(0, 0, 1),
            depth=10,
            radius=2,
        ),
    ]
    monkeypatch.setattr(
        profiles_module,
        "_mesh_cylindrical_features",
        lambda _data: measured,
    )
    data = SimpleNamespace(
        diagonal=30.0,
        mesh=SimpleNamespace(bounds=np.asarray([[-5, -5, -5], [5, 5, 5]])),
    )

    candidate = generate_oriented_cylinder_candidates(
        data,
        source,
        cuts_only=True,
    )[0]

    holes = [
        operation
        for operation in candidate.operations
        if isinstance(operation, RoundHoleFeature)
    ]
    assert len(holes) == 4
    assert {hole.axis for hole in holes} == {Axis.X, Axis.Y, Axis.Z}
    assert all(hole.start == -5 and hole.depth == 10 for hole in holes)
    assert any(
        len(axis_holes) == 2
        and {hole.axis for hole in axis_holes} == {Axis.Z}
        for grouped_candidate in generate_oriented_cylinder_candidates(
            data,
            source,
            cuts_only=True,
        )[1:]
        if (
            axis_holes := [
                operation
                for operation in grouped_candidate.operations
                if isinstance(operation, RoundHoleFeature)
            ]
        )
    )
    circle_cuts = [
        profile
        for operation in candidate.operations
        if isinstance(operation, BooleanExtrudeFeature)
        and operation.mode == "cut"
        for profile in [operation.outer, *operation.additional_regions]
        if isinstance(profile, CircleProfile)
    ]
    assert [(circle.center, circle.radius) for circle in circle_cuts] == [
        ((4.0, 0.0), 1.0)
    ]
    assert any(
        isinstance(operation, BooleanExtrudeFeature)
        and isinstance(operation.outer, PathProfile)
        and operation.axis == Axis.Z
        for operation in candidate.operations
    )


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y, Axis.Z])
def test_arc_sketch_boolean_builds_on_every_cardinal_plane(axis: Axis) -> None:
    capsule = PathProfile(
        start=(-3, -1),
        segments=[
            LineSegment(end=(3, -1)),
            ArcSegment(mid=(4, 0), end=(3, 1)),
            LineSegment(end=(-3, 1)),
            ArcSegment(mid=(-4, 0), end=(-3, -1)),
        ],
    )
    plan = ReconstructionPlan(
        name=f"{axis.value.lower()}-plane-arc-sketch",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-5,
            depth=10,
            outer=PolygonProfile(
                points=[(-10, -10), (10, -10), (10, 10), (-10, 10)]
            ),
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="cut",
                axis=axis,
                start=-11,
                depth=22,
                outer=capsule,
            )
        ],
    )

    result = build_plan(plan).val()

    assert result.isValid()
    assert "CYLINDER" in {face.geomType() for face in result.Faces()}


def test_layer_profile_circles_promote_on_every_cardinal_plane() -> None:
    square = PolygonProfile(points=[(-5, -5), (5, -5), (5, 5), (-5, 5)])
    source = ReconstructionPlan(
        name="three-plane-layer-circles",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-5,
            depth=2,
            outer=square,
            holes=[CircleProfile(center=(0, 0), radius=1)],
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="cut",
                axis=Axis.Y,
                start=-5,
                depth=2,
                outer=CircleProfile(center=(1, 0), radius=1.5),
            ),
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.X,
                start=-5,
                depth=2,
                outer=square,
                holes=[CircleProfile(center=(-1, 0), radius=2)],
            ),
        ],
    )
    data = SimpleNamespace(
        diagonal=20.0,
        mesh=SimpleNamespace(
            bounds=np.asarray([[-5.0, -5.0, -5.0], [5.0, 5.0, 5.0]])
        ),
    )

    candidates = generate_embedded_circle_promotion_candidates(data, source)

    assert len(candidates) == 1
    promoted = candidates[0]
    holes = [
        operation
        for operation in promoted.operations
        if isinstance(operation, RoundHoleFeature)
    ]
    assert {hole.axis for hole in holes} == {Axis.X, Axis.Y, Axis.Z}
    retained_profile_circles = []
    for feature in [promoted.base, *promoted.operations]:
        retained_profile_circles.extend(
            profile
            for profile in getattr(feature, "holes", [])
            if isinstance(profile, CircleProfile)
        )
        if isinstance(feature, BooleanExtrudeFeature) and feature.mode == "cut":
            retained_profile_circles.extend(
                profile
                for profile in [feature.outer, *feature.additional_regions]
                if isinstance(profile, CircleProfile)
            )
    assert not retained_profile_circles


def test_local_tangent_envelope_uses_alternate_plane_spline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    curve = np.asarray(
        [
            (2.0, 10.0),
            (1.6, 9.3),
            (1.2, 8.5),
            (0.8, 7.5),
            (0.4, 6.2),
            (0.15, 4.8),
            (0.03, 3.2),
            (0.0, 1.6),
            (0.0, 0.0),
        ]
    )
    profile = PathProfile(
        start=(2.0, 10.0),
        segments=[
            SplineSegment(
                points=[tuple(point) for point in curve[1:-1]],
                end=(0.0, 0.0),
                start_tangent=(-0.5, -0.8),
                end_tangent=(0.0, -1.0),
            ),
            LineSegment(end=(8.0, 0.0)),
            LineSegment(end=(8.0, 10.0)),
            LineSegment(end=(2.0, 10.0)),
        ],
    )
    polygon = ShapelyPolygon([*curve, (8.0, 0.0), (8.0, 10.0)])
    section = profiles_module.SectionShape(
        outer=profile,
        holes=[],
        additional_regions=[],
        area=polygon.area,
        polygon=polygon,
    )
    monkeypatch.setattr(
        profiles_module,
        "section_shape",
        lambda _data, axis, _location: section if axis == Axis.Z else None,
    )

    centers = np.column_stack(
        (
            np.interp(np.linspace(0, len(curve) - 1, 10), np.arange(len(curve)), curve[:, 0]),
            np.interp(np.linspace(0, len(curve) - 1, 10), np.arange(len(curve)), curve[:, 1]),
            np.linspace(0.4, 5.9, 10),
        )
    )
    vertices = np.repeat(centers, 3, axis=0)
    vertices[:, 2] += np.tile(np.asarray([-0.01, 0.0, 0.01]), len(centers))
    mesh = SimpleNamespace(
        bounds=np.asarray([[0.0, 0.0, 0.0], [8.0, 10.0, 6.0]]),
        triangles_center=centers,
        face_normals=np.tile(np.asarray([1.0, 0.0, 0.0]), (len(centers), 1)),
        faces=np.arange(len(vertices)).reshape(-1, 3),
        vertices=vertices,
    )
    data = SimpleNamespace(diagonal=15.0, mesh=mesh)
    source = ReconstructionPlan(
        name="layered-tangent",
        base=ExtrudeFeature(
            axis=Axis.Y,
            start=0,
            depth=10,
            outer=PolygonProfile(points=[(0, 0), (8, 0), (8, 6), (0, 6)]),
        ),
    )

    candidates = generate_local_tangent_envelope_candidates(data, source)

    assert candidates
    repaired = candidates[0]
    assert any(
        isinstance(operation, BooleanExtrudeFeature)
        and operation.axis == Axis.Z
        for operation in repaired.operations
    )
    assert "tangent boundary curves" in repaired.assumptions[-1]


def test_cone_preservation_can_search_smaller_repeated_hole_boundaries() -> None:
    plan = ReconstructionPlan(
        name="layered_ring",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=1,
            outer=CircleProfile(center=(0, 0), radius=5),
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.Z,
                start=float(index + 1),
                depth=1,
                outer=CircleProfile(center=(0, 0), radius=5),
                holes=[CircleProfile(center=(0, 0), radius=2)],
            )
            for index in range(3)
        ],
    )
    data = SimpleNamespace(diagonal=20.0)

    dominant_only = generate_feature_round_finish_candidates(data, plan)
    every_group = generate_feature_round_finish_candidates(
        data,
        plan,
        include_all_groups=True,
    )

    assert not any(
        isinstance(operation, EdgeFinishFeature) and operation.radius == 2
        for candidate in dominant_only
        for operation in candidate.operations
    )
    assert any(
        isinstance(operation, EdgeFinishFeature)
        and operation.mode == "chamfer"
        and operation.radius == 2
        for candidate in every_group
        for operation in candidate.operations
    )


def test_repeated_oriented_cylinders_offer_one_shared_chamfer_plan() -> None:
    plan = ReconstructionPlan(
        name="repeated-countersinks",
        base=ExtrudeFeature(
            axis=Axis.X,
            start=-10,
            depth=20,
            outer=PolygonProfile(
                points=[
                    (-0.66, -5),
                    (0.66, -5),
                    (0.66, 5),
                    (-0.66, 5),
                ]
            ),
        ),
        operations=[
            OrientedCylinderFeature(
                mode="cut",
                origin=(-9, -0.66, z_position),
                direction=(0, 1, 0),
                depth=1.32,
                radius=0.13,
            )
            for z_position in (2.2, 3.3)
        ],
    )
    data = SimpleNamespace(diagonal=22.4)

    candidates = generate_feature_round_finish_candidates(
        data,
        plan,
        include_all_groups=True,
    )

    repeated = [
        candidate
        for candidate in candidates
        if len(
            additions := [
                operation
                for operation in candidate.operations[len(plan.operations) :]
                if isinstance(operation, EdgeFinishFeature)
                and operation.mode == "chamfer"
                and operation.end == "end"
            ]
        )
        == 2
        and len({operation.size for operation in additions}) == 1
    ]
    assert repeated
    shape = build_plan(repeated[0]).val()
    assert shape.isValid()
    assert sum(face.geomType() == "CONE" for face in shape.Faces()) == 2


def test_spherical_mesh_patch_is_recovered_as_analytic_cut(
    tmp_path: Path,
) -> None:
    stock = cq.Workplane("XY").box(
        10,
        10,
        4,
        centered=(True, True, False),
    )
    sphere = cq.Workplane(
        obj=cq.Solid.makeSphere(1.2, cq.Vector(4.4, 4.4, 3.4))
    )
    stl_path = step_to_stl(
        stock.cut(sphere),
        tmp_path,
        "spherical_patch",
    )

    features = _mesh_spherical_features(
        load_mesh(stl_path, stl_path.name)
    )

    assert any(
        isinstance(feature, SphereFeature)
        and feature.mode == "cut"
        and feature.center == pytest.approx((4.4, 4.4, 3.4), abs=0.04)
        and feature.radius == pytest.approx(1.2, abs=0.04)
        for feature in features
    )


def test_end_finish_search_keeps_single_full_radius_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = Point(0.0, 0.0).buffer(5.0, quad_segs=128)

    def fake_section_shape(
        _data: object,
        _axis: Axis,
        location: float,
    ) -> SimpleNamespace:
        if location < 0.5:
            distance = location
            radius = 0.15
        else:
            distance = 1.0 - location
            radius = 0.5
        inset = (
            radius
            - math.sqrt(max(radius**2 - (distance - radius) ** 2, 0.0))
            if distance < radius
            else 0.0
        )
        return SimpleNamespace(
            polygon=reference.buffer(-inset) if inset else reference
        )

    monkeypatch.setattr(profiles_module, "section_shape", fake_section_shape)
    source = PlanCandidate(
        plan=ReconstructionPlan(
            name="one_real_end_finish",
            base=ExtrudeFeature(
                axis=Axis.Y,
                start=0.0,
                depth=1.0,
                outer=CircleProfile(center=(0.0, 0.0), radius=5.0),
            ),
        ),
        heuristic_score=0.0,
        section_consistency=1.0,
    )
    data = SimpleNamespace(
        diagonal=10.1,
        mesh=SimpleNamespace(extents=np.asarray([10.0, 1.0, 10.0])),
    )

    candidates = generate_end_finish_candidates(data, [source])

    single_finishes = [
        candidate.plan.operations[0]
        for candidate in candidates
        if len(candidate.plan.operations) == 1
        and isinstance(candidate.plan.operations[0], EdgeFinishFeature)
    ]
    assert {finish.end for finish in single_finishes} == {"start", "end"}
    assert any(
        finish.end == "end"
        and finish.mode == "fillet"
        and finish.size == pytest.approx(0.5)
        for finish in single_finishes
    )


def test_spherical_corner_candidates_perturb_equal_radius_brep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="spherical_corner",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-0.667,
            depth=1.333,
            outer=PathProfile(
                start=(-10.0, -4.667),
                segments=[
                    LineSegment(end=(10.0, -4.667)),
                    LineSegment(end=(10.0, 4.533)),
                    ArcSegment(
                        mid=(9.968, 4.62),
                        end=(9.866, 4.667),
                    ),
                    LineSegment(end=(-10.0, 4.667)),
                    LineSegment(end=(-10.0, -4.667)),
                ],
            ),
        ),
    )
    finished = plan.model_copy(deep=True)
    finished.operations.extend(
        [
            EdgeFinishFeature(
                mode="fillet",
                axis=Axis.Z,
                end=end,
                size=0.133,
            )
            for end in ("start", "end")
        ]
    )
    monkeypatch.setattr(
        profiles_module,
        "generate_end_finish_candidates",
        lambda _data, _sources: [
            PlanCandidate(
                plan=finished,
                heuristic_score=0.0,
                section_consistency=1.0,
            )
        ],
    )
    monkeypatch.setattr(
        profiles_module,
        "_mesh_spherical_features",
        lambda _data: [],
    )

    candidates = generate_spherical_corner_finish_candidates(
        SimpleNamespace(diagonal=22.11),
        plan,
    )

    radii = {
        operation.size
        for candidate in candidates
        for operation in candidate.operations
        if isinstance(operation, EdgeFinishFeature)
    }
    assert radii == {0.132, 0.133, 0.134}
    for candidate in candidates:
        shape = build_plan(candidate).val()
        assert shape.isValid()
        assert "SPHERE" in {face.geomType() for face in shape.Faces()}


def test_spherical_corner_candidates_constrain_every_matching_arc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="rounded_power_supply",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-10.0,
            depth=20.0,
            outer=PathProfile(
                start=(5.0, 4.667),
                segments=[
                    ArcSegment(mid=(4.9, 4.906), end=(4.67, 5.0)),
                    LineSegment(end=(-4.667, 5.0)),
                    ArcSegment(mid=(-4.906, 4.9), end=(-5.0, 4.67)),
                    LineSegment(end=(-5.0, -4.677)),
                    ArcSegment(mid=(-4.9, -4.906), end=(-4.67, -5.0)),
                    LineSegment(end=(4.667, -5.0)),
                    ArcSegment(mid=(4.907, -4.898), end=(5.0, -4.667)),
                    LineSegment(end=(5.0, 4.667)),
                ],
            ),
        ),
    )
    finished = plan.model_copy(deep=True)
    finished.operations.extend(
        [
            EdgeFinishFeature(
                mode="fillet",
                axis=Axis.Z,
                end=end,
                size=0.3,
            )
            for end in ("start", "end")
        ]
    )
    monkeypatch.setattr(
        profiles_module,
        "generate_end_finish_candidates",
        lambda _data, _sources: [
            PlanCandidate(
                plan=finished,
                heuristic_score=0.0,
                section_consistency=1.0,
            )
        ],
    )
    monkeypatch.setattr(
        profiles_module,
        "_mesh_spherical_features",
        lambda _data: [
            SphereFeature(mode="add", center=(0.0, 0.0, 0.0), radius=0.333)
        ],
    )

    candidates = generate_spherical_corner_finish_candidates(
        SimpleNamespace(diagonal=24.5),
        plan,
    )

    assert len(candidates) == 3
    for candidate in candidates:
        finish_radius = next(
            operation.size
            for operation in candidate.operations
            if isinstance(operation, EdgeFinishFeature)
        )
        start = np.asarray(candidate.base.outer.start)
        arc_radii = []
        for segment in candidate.base.outer.segments:
            end = np.asarray(segment.end)
            if isinstance(segment, ArcSegment):
                fitted = profiles_module._circle_values(
                    np.asarray([start, segment.mid, end])
                )
                assert fitted is not None
                arc_radii.append(fitted[2])
            start = end
        assert arc_radii == pytest.approx(
            [finish_radius] * 4,
            abs=1e-6,
        )
        shape = build_plan(candidate).val()
        assert shape.isValid()
        assert "SPHERE" in {face.geomType() for face in shape.Faces()}


def test_torus_is_not_misclassified_as_many_spheres(
    tmp_path: Path,
) -> None:
    torus = cq.Workplane(obj=cq.Solid.makeTorus(4.0, 1.0))
    stl_path = step_to_stl(torus, tmp_path, "torus_not_spheres")

    features = _mesh_spherical_features(
        load_mesh(stl_path, stl_path.name)
    )

    assert features == []


def test_cross_axis_residuals_can_be_combined_in_one_plan(
    tmp_path: Path,
) -> None:
    stock = cq.Workplane("XY").box(
        20,
        20,
        10,
        centered=(True, True, False),
    )
    x_cutter = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            1.5,
            22,
            cq.Vector(-11, -4, 5),
            cq.Vector(1, 0, 0),
        )
    )
    y_cutter = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            1.25,
            22,
            cq.Vector(4, -11, 5),
            cq.Vector(0, 1, 0),
        )
    )
    stl_path = step_to_stl(
        stock.cut(x_cutter).cut(y_cutter),
        tmp_path,
        "multi_axis_residual",
    )
    data = load_mesh(stl_path, stl_path.name)
    source = ReconstructionPlan(
        name="stock",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=10,
            outer=PolygonProfile(
                points=[(-10, -10), (10, -10), (10, 10), (-10, 10)]
            ),
        ),
    )

    candidates = generate_cross_axis_residual_candidates(data, source)

    assert any(
        {operation.axis for operation in candidate.operations}
        >= {Axis.X, Axis.Y}
        for candidate in candidates
        if all(
            isinstance(operation, BooleanExtrudeFeature)
            for operation in candidate.operations
        )
    )


def test_adaptive_layers_keep_disconnected_regions_in_one_sketch(
    tmp_path: Path,
) -> None:
    model = cq.Workplane("XY").box(
        18,
        5,
        2,
        centered=(True, True, False),
    )
    for x_position in (-6, 0, 6):
        boss = cq.Workplane("XY", origin=(x_position, 0, 2)).box(
            3,
            3,
            6,
            centered=(True, True, False),
        )
        model = model.union(boss)
    stl_path = step_to_stl(model, tmp_path, "disconnected_layers")
    data = load_mesh(stl_path, stl_path.name)

    candidates = generate_adaptive_layer_candidates(
        data,
        "disconnected-layers",
        Axis.Z,
        slice_count=8,
    )

    assert candidates
    assert any(
        isinstance(operation, BooleanExtrudeFeature)
        and len(operation.additional_regions) == 2
        for operation in candidates[0].operations
    )


def test_profiled_cylinder_keeps_circular_end_lofts_analytic(
    tmp_path: Path,
) -> None:
    bottom = cq.Workplane(
        obj=cq.Solid.makeCone(
            3.75,
            5.0,
            1.25,
            cq.Vector(0, 0, -10),
            cq.Vector(0, 0, 1),
        )
    )
    middle = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            5.0,
            17.5,
            cq.Vector(0, 0, -8.75),
            cq.Vector(0, 0, 1),
        )
    )
    top = cq.Workplane(
        obj=cq.Solid.makeCone(
            5.0,
            3.75,
            1.25,
            cq.Vector(0, 0, 8.75),
            cq.Vector(0, 0, 1),
        )
    )
    cross_hole = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            3.125,
            10.0,
            cq.Vector(0.04, -5, 0),
            cq.Vector(0, 1, 0),
        )
    )
    model = bottom.union(middle).union(top).cut(cross_hole)
    stl_path = step_to_stl(model, tmp_path, "chamfered_cross_hole")
    data = load_mesh(stl_path, stl_path.name)

    candidates = generate_profiled_endcap_cylinder_candidates(
        data,
        "chamfered-cross-hole",
    )

    assert candidates
    plan = candidates[0]
    tapered = [
        operation
        for operation in plan.operations
        if isinstance(operation, TaperedAddFeature)
    ]
    assert len(plan.operations) == 3
    assert len(tapered) == 2
    assert all(
        isinstance(operation.start_outer, CircleProfile)
        and isinstance(operation.end_outer, CircleProfile)
        for operation in tapered
    )
    shape = build_plan(plan).val()
    surface_types = [face.geomType() for face in shape.Faces()]
    assert shape.isValid()
    assert surface_types.count("CONE") == 2
    assert surface_types.count("CYLINDER") >= 2


def test_symmetric_end_chamfers_use_the_unaffected_middle_section(
    tmp_path: Path,
) -> None:
    bottom = cq.Workplane(
        obj=cq.Solid.makeCone(
            4.4,
            5.0,
            0.6,
            cq.Vector(0, 0, 0),
            cq.Vector(0, 0, 1),
        )
    )
    middle = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            5.0,
            1.8,
            cq.Vector(0, 0, 0.6),
            cq.Vector(0, 0, 1),
        )
    )
    top = cq.Workplane(
        obj=cq.Solid.makeCone(
            5.0,
            4.4,
            0.6,
            cq.Vector(0, 0, 2.4),
            cq.Vector(0, 0, 1),
        )
    )
    bore = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            3.8,
            3.0,
            cq.Vector(0, 0, 0),
            cq.Vector(0, 0, 1),
        )
    )
    stl_path = step_to_stl(
        bottom.union(middle).union(top).cut(bore),
        tmp_path,
        "symmetric_end_chamfers",
    )
    data = load_mesh(stl_path, stl_path.name)
    source = PlanCandidate(
        plan=ReconstructionPlan(
            name="symmetric-end-chamfers",
            base=CylinderFeature(
                axis=Axis.Z,
                start=0,
                depth=3,
                center=(0, 0),
                radius=5,
                inner_radius=3.8,
            ),
        ),
        heuristic_score=0,
        section_consistency=1,
    )

    candidates = generate_circular_end_finish_candidates(data, [source])

    chamfered = [
        candidate.plan
        for candidate in candidates
        if len(candidate.plan.operations) == 2
        and all(
            isinstance(operation, EdgeFinishFeature)
            and operation.mode == "chamfer"
            for operation in candidate.plan.operations
        )
    ]
    assert chamfered
    assert {operation.end for operation in chamfered[0].operations} == {
        "start",
        "end",
    }
    assert all(
        operation.size == pytest.approx(0.6, abs=0.06)
        and operation.radius == pytest.approx(5.0, abs=0.04)
        for operation in chamfered[0].operations
        if isinstance(operation, EdgeFinishFeature)
    )
    shape = build_plan(chamfered[0]).val()
    assert shape.isValid()
    assert sum(face.geomType() == "CONE" for face in shape.Faces()) == 2


def run_case(
    model: cq.Workplane,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
):
    monkeypatch.setattr(reconstruction_module, "save_report", lambda report: None)
    source = step_to_stl(model, tmp_path, name)
    report = reconstruct("123456789abc", source, source.name)
    assert report.plan is not None
    assert report.score is not None and report.score.valid_solid
    assert (tmp_path / "reconstruction.step").is_file()
    return report


def test_recovers_stepped_shaft_as_multiple_extrusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lower = cq.Workplane("XY").circle(14).extrude(8)
    upper = cq.Workplane("XY", origin=(0, 0, 8)).circle(9).extrude(12)
    report = run_case(lower.union(upper), tmp_path, monkeypatch, "stepped_shaft")

    layered = [
        operation
        for operation in report.plan.operations
        if isinstance(operation, BooleanExtrudeFeature)
    ]
    assert report.status == "complete"
    assert len(layered) >= 1
    assert report.score.chamfer_p95_mm < 0.08
    assert report.score.volume_error_percent < 0.5


def test_recovers_blind_pocket_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = cq.Workplane("XY").box(50, 40, 12, centered=(True, True, False))
    model = model.faces(">Z").workplane().rect(30, 20).cutBlind(-7)
    report = run_case(model, tmp_path, monkeypatch, "blind_pocket")

    layers = [
        operation
        for operation in report.plan.operations
        if isinstance(operation, BooleanExtrudeFeature)
    ]
    assert report.status == "complete"
    assert layers
    boundaries = sorted({report.plan.base.start, *(operation.start for operation in layers)})
    assert any(boundary == pytest.approx(5, abs=0.05) for boundary in boundaries)
    assert report.score.chamfer_p95_mm < 0.08


def test_recovers_a_true_circular_arc_in_mixed_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = (
        cq.Workplane("XY")
        .moveTo(-12, -10)
        .lineTo(10, -10)
        .threePointArc((20, 0), (10, 10))
        .lineTo(-12, 10)
        .close()
        .extrude(9)
    )
    report = run_case(model, tmp_path, monkeypatch, "arc_profile")

    profile = report.plan.base.outer
    assert isinstance(profile, PathProfile)
    assert any(isinstance(segment, ArcSegment) for segment in profile.segments)
    assert len(profile.segments) <= 5
    assert report.status == "complete"
    assert report.score.chamfer_p95_mm < 0.08


def test_recovers_four_rounded_corners_as_arcs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = (
        cq.Workplane("XY")
        .moveTo(-15, -12)
        .lineTo(15, -12)
        .threePointArc((18.536, -10.536), (20, -7))
        .lineTo(20, 7)
        .threePointArc((18.536, 10.536), (15, 12))
        .lineTo(-15, 12)
        .threePointArc((-18.536, 10.536), (-20, 7))
        .lineTo(-20, -7)
        .threePointArc((-18.536, -10.536), (-15, -12))
        .close()
        .extrude(10)
    )
    report = run_case(model, tmp_path, monkeypatch, "rounded_plate")

    profile = report.plan.base.outer
    assert isinstance(profile, PathProfile)
    arcs = [segment for segment in profile.segments if isinstance(segment, ArcSegment)]
    assert len(arcs) == 4
    assert len(profile.segments) <= 9
    assert report.status == "complete"
    assert report.score.chamfer_p95_mm < 0.08


def test_combines_pocket_and_perpendicular_round_hole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = cq.Workplane("XY").box(40, 30, 20)
    model = model.faces(">Z").workplane().rect(20, 14).cutBlind(-5)
    model = model.cut(cq.Workplane("YZ").circle(4).extrude(50, both=True))
    report = run_case(model, tmp_path, monkeypatch, "pocket_side_hole")

    assert report.status == "complete"
    assert any(
        isinstance(operation, RoundHoleFeature)
        for operation in report.plan.operations
    )
    assert report.score.chamfer_p95_mm < 0.12
    assert report.score.volume_error_percent < 1.0


def test_smooth_dense_contour_uses_editable_curves_instead_of_polygon() -> None:
    import numpy as np

    angles = np.linspace(0, 2 * np.pi, 721)
    radii = 12 + 0.7 * np.sin(7 * angles)
    points = np.column_stack((radii * np.cos(angles), radii * np.sin(angles)))

    fitted = _profile_from_ring(points, tolerance=0.02)

    assert fitted is not None
    profile, _ = fitted
    assert isinstance(profile, PathProfile | SplineProfile)
    if isinstance(profile, PathProfile):
        assert any(isinstance(segment, ArcSegment) for segment in profile.segments)
    else:
        assert len(profile.points) <= 128


def test_dense_straight_chamfered_profile_is_not_misclassified_as_arcs() -> None:
    import numpy as np

    corners = np.array(
        [
            (-4.0, -10.0),
            (3.0, -10.0),
            (4.5, -6.0),
            (4.5, 9.0),
            (3.5, 10.0),
            (-4.0, 10.0),
        ]
    )
    samples = []
    for start, end in zip(corners, np.roll(corners, -1, axis=0), strict=True):
        samples.extend(np.linspace(start, end, 30, endpoint=False))
    samples.append(samples[0])

    fitted = _profile_from_ring(np.asarray(samples), tolerance=0.015)

    assert fitted is not None
    profile, _ = fitted
    assert not isinstance(profile, PathProfile) or not any(
        isinstance(segment, ArcSegment) for segment in profile.segments
    )


def test_recovers_multiple_disconnected_profile_islands_in_upper_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = cq.Workplane("XY").box(44, 20, 2, centered=(True, True, False))
    for x_position in (-14, 0, 14):
        boss = (
            cq.Workplane("XY", origin=(x_position, 0, 2))
            .circle(4)
            .extrude(6)
        )
        model = model.union(boss)

    report = run_case(model, tmp_path, monkeypatch, "three_bosses")

    assert report.status == "complete"
    assert len(report.plan.operations) == 1
    operation = report.plan.operations[0]
    assert isinstance(operation, BooleanExtrudeFeature)
    assert len(operation.additional_regions) == 2
    assert report.score.chamfer_p95_mm < 0.08


def test_recovers_end_fillet_as_editable_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = cq.Workplane("XY").circle(10).extrude(10)
    model = model.faces(">Z").edges().fillet(1)

    report = run_case(model, tmp_path, monkeypatch, "end_fillet")

    finishes = [
        operation
        for operation in report.plan.operations
        if isinstance(operation, EdgeFinishFeature)
    ]
    assert report.status == "complete"
    if finishes:
        assert finishes[0].mode == "fillet"
        assert finishes[0].end == "end"
        assert finishes[0].size == pytest.approx(1, abs=0.3)
    else:
        assert isinstance(report.plan.base, RevolveFeature)
        assert any(
            isinstance(segment, ArcSegment)
            for segment in report.plan.base.profile.segments
        )
    assert report.score.chamfer_p95_mm < 0.08


def test_recovers_end_chamfer_as_editable_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = cq.Workplane("XY").box(20, 14, 8, centered=(True, True, False))
    model = model.faces(">Z").edges().chamfer(1)

    report = run_case(model, tmp_path, monkeypatch, "end_chamfer")

    finishes = [
        operation
        for operation in report.plan.operations
        if isinstance(operation, EdgeFinishFeature)
    ]
    assert report.status == "complete"
    assert finishes
    assert finishes[0].mode == "chamfer"
    assert finishes[0].end == "end"
    assert finishes[0].size == pytest.approx(1, abs=0.3)
    assert report.score.chamfer_p95_mm < 0.08


def test_recovers_cylinder_then_countersink_cones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    centers = ((-6, -5), (6, -5), (-6, 5), (6, 5))
    model = cq.Workplane("XY", origin=(0, 0, -0.23)).box(
        20,
        16,
        0.46,
        centered=(True, True, False),
    )
    for x_position, y_position in centers:
        cylinder = cq.Solid.makeCylinder(
            0.145,
            0.17,
            cq.Vector(x_position, y_position, -0.23),
            cq.Vector(0, 0, 1),
        )
        countersink = cq.Solid.makeCone(
            0.145,
            0.435,
            0.29,
            cq.Vector(x_position, y_position, -0.06),
            cq.Vector(0, 0, 1),
        )
        model = model.cut(cq.Workplane(obj=cylinder)).cut(
            cq.Workplane(obj=countersink)
        )

    stl_path = step_to_stl(model, tmp_path, "cylinder_then_countersink")
    data = load_mesh(stl_path, stl_path.name, "mm")
    assert mesh_has_conical_patch(data)
    source = ReconstructionPlan(
        name="cylinder_then_countersink",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=-0.23,
            depth=0.46,
            outer=PolygonProfile(
                points=[(-10, -8), (10, -8), (10, 8), (-10, 8)]
            ),
        ),
        operations=[
            RoundHoleFeature(
                axis=Axis.Z,
                center=center,
                diameter=0.29,
                start=-0.23,
                depth=0.46,
                through=True,
            )
            for center in centers
        ],
    )

    candidates = generate_conical_hole_candidates(data, source)

    assert len(candidates) == 1
    cones = [
        operation
        for operation in candidates[0].operations
        if isinstance(operation, ConicalHoleFeature)
    ]
    assert len(cones) == 4
    assert all(cone.start == pytest.approx(-0.06, abs=0.02) for cone in cones)
    assert all(cone.start_diameter == pytest.approx(0.29, abs=0.03) for cone in cones)
    assert all(cone.end_diameter == pytest.approx(0.87, abs=0.03) for cone in cones)
    assert build_plan(candidates[0]).val().isValid()

    source_with_finish = source.model_copy(deep=True)
    source_with_finish.operations.append(
        EdgeFinishFeature(
            mode="fillet",
            axis=Axis.Z,
            size=0.01,
            selector="circle",
            center=centers[0],
            radius=0.145,
            position=0.23,
        )
    )
    finished_candidates = generate_conical_hole_candidates(
        data,
        source_with_finish,
    )
    assert finished_candidates
    assert not any(
        isinstance(operation, EdgeFinishFeature)
        for operation in finished_candidates[0].operations
    )
    assert build_plan(finished_candidates[0]).val().isValid()

    monkeypatch.setattr(reconstruction_module, "save_report", lambda report: None)
    report = reconstruct("123456789abc", stl_path, stl_path.name)
    assert report.plan is not None
    assert any(
        isinstance(operation, ConicalHoleFeature)
        for operation in report.plan.operations
    )


def test_recovers_arbitrary_axis_annular_cylinder_from_mesh(
    tmp_path: Path,
) -> None:
    direction = cq.Vector(1, 0, 1).normalized()
    outer = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            3,
            12,
            cq.Vector(-4, 0, 0),
            direction,
        )
    )
    inner = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            1,
            12,
            cq.Vector(-4, 0, 0),
            direction,
        )
    )
    tube = outer.cut(inner)
    base_model = cq.Workplane("XY").box(
        12,
        8,
        4,
        centered=(True, True, False),
    )
    model = base_model.union(tube)
    stl_path = step_to_stl(model, tmp_path, "arbitrary_axis_tube")
    data = load_mesh(stl_path, stl_path.name, "mm")
    source = ReconstructionPlan(
        name="arbitrary_axis_tube",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=4,
            outer=PolygonProfile(
                points=[(-6, -4), (6, -4), (6, 4), (-6, 4)]
            ),
        ),
    )

    candidates = generate_oriented_cylinder_candidates(data, source)

    assert candidates
    features = [
        operation
        for operation in candidates[0].operations
        if isinstance(operation, OrientedCylinderFeature)
    ]
    annulus = next(feature for feature in features if feature.mode == "add")
    assert annulus.radius == pytest.approx(3, abs=0.03)
    assert annulus.inner_radius == pytest.approx(1, abs=0.03)
    assert abs(annulus.direction[0]) == pytest.approx(2**-0.5, abs=0.003)
    assert abs(annulus.direction[2]) == pytest.approx(2**-0.5, abs=0.003)
    assert annulus.depth == pytest.approx(12, abs=0.05)
    assert build_plan(candidates[0]).val().isValid()


def test_angled_through_hole_is_attached_to_entry_and_exit_patches(
    tmp_path: Path,
) -> None:
    direction = cq.Vector(0.4, 0.25, 1).normalized()
    stock = cq.Workplane("XY").box(
        20,
        16,
        8,
        centered=(True, True, False),
    )
    bore = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            2,
            16,
            cq.Vector(-3 * direction.x, -3 * direction.y, -3),
            direction,
        )
    )
    model = stock.cut(bore)
    stl_path = step_to_stl(model, tmp_path, "angled_through_hole")
    data = load_mesh(stl_path, stl_path.name, "mm")
    source = ReconstructionPlan(
        name="angled_through_hole",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=8,
            outer=PolygonProfile(
                points=[(-10, -8), (10, -8), (10, 8), (-10, 8)]
            ),
        ),
    )

    candidates = generate_oriented_cylinder_candidates(
        data,
        source,
        cuts_only=True,
    )

    assert candidates
    holes = [
        operation
        for operation in candidates[0].operations
        if isinstance(operation, OrientedCylinderFeature)
        and operation.mode == "cut"
    ]
    assert holes
    hole = min(holes, key=lambda feature: abs(feature.radius - 2))
    assert hole.radius == pytest.approx(2, abs=0.03)
    assert abs(hole.direction[0]) > 0.1
    assert abs(hole.direction[1]) > 0.1
    assert hole.support_patch_id is not None
    assert hole.terminating_patch_id is not None
    assert hole.support_patch_id != hole.terminating_patch_id
    assert hole.through
    assert build_plan(candidates[0]).val().isValid()


def test_angled_hole_supports_rotate_with_the_part(tmp_path: Path) -> None:
    support_normal = np.asarray([0.25, 0.45, 0.857], dtype=float)
    support_normal /= np.linalg.norm(support_normal)
    x_direction = np.asarray([1.0, 0.0, 0.0])
    x_direction -= support_normal * float(x_direction @ support_normal)
    x_direction /= np.linalg.norm(x_direction)
    base_plan = ReconstructionPlan(
        name="rotated_angled_hole",
        base=OrientedExtrudeFeature(
            origin=(0, 0, 0),
            plane_normal=tuple(float(value) for value in support_normal),
            direction=tuple(float(value) for value in support_normal),
            x_direction=tuple(float(value) for value in x_direction),
            depth=8,
            outer=PolygonProfile(
                points=[(-10, -8), (10, -8), (10, 8), (-10, 8)]
            ),
        ),
    )
    stock = build_plan(base_plan)
    bore_direction = support_normal + 0.3 * x_direction
    bore_direction /= np.linalg.norm(bore_direction)
    bore_start = -3 * bore_direction
    bore = cq.Workplane(
        obj=cq.Solid.makeCylinder(
            1.5,
            18,
            cq.Vector(*bore_start),
            cq.Vector(*bore_direction),
        )
    )
    model = stock.cut(bore)
    stl_path = step_to_stl(model, tmp_path, "rotated_angled_hole")
    data = load_mesh(stl_path, stl_path.name, "mm")

    candidates = generate_oriented_cylinder_candidates(
        data,
        base_plan,
        cuts_only=True,
    )

    assert candidates
    hole = next(
        operation
        for operation in candidates[0].operations
        if isinstance(operation, OrientedCylinderFeature)
        and operation.mode == "cut"
    )
    assert hole.radius == pytest.approx(1.5, abs=0.03)
    assert hole.support_patch_id is not None
    assert hole.terminating_patch_id is not None
    assert hole.support_patch_id != hole.terminating_patch_id
    assert hole.through


def test_recovers_layered_arbitrary_axis_sketch_extrusions(
    tmp_path: Path,
) -> None:
    direction = (2**-0.5, 0, -(2**-0.5))
    source_plan = ReconstructionPlan(
        name="layered_diagonal_profile",
        base=OrientedExtrudeFeature(
            origin=tuple(component * -6 for component in direction),
            direction=direction,
            x_direction=(0, -1, 0),
            depth=8,
            outer=CircleProfile(center=(0, 0), radius=5),
            holes=[CircleProfile(center=(0, 0), radius=1.5)],
        ),
        operations=[
            OrientedBooleanExtrudeFeature(
                mode="add",
                origin=tuple(component * 2 for component in direction),
                direction=direction,
                x_direction=(0, -1, 0),
                depth=2,
                outer=PolygonProfile(
                    points=[(-4, -4), (4, -4), (4, 4), (-4, 4)]
                ),
            )
        ],
    )
    model = build_plan(source_plan)
    stl_path = step_to_stl(model, tmp_path, "layered_diagonal_profile")
    data = load_mesh(stl_path, stl_path.name, "mm")

    candidates = generate_arbitrary_axis_prismatic_candidates(
        data,
        "layered_diagonal_profile",
    )

    layered = [
        candidate.plan
        for candidate in candidates
        if isinstance(candidate.plan.base, OrientedExtrudeFeature)
        and any(
            isinstance(operation, OrientedBooleanExtrudeFeature)
            for operation in candidate.plan.operations
        )
    ]
    assert layered
    expected_volume = model.val().Volume()
    assert min(
        abs(build_plan(plan).val().Volume() - expected_volume)
        / expected_volume
        for plan in layered
    ) < 0.01


def test_recovers_all_planar_arbitrary_axis_extrusion(
    tmp_path: Path,
) -> None:
    direction = (0.0, math.cos(math.radians(20)), math.sin(math.radians(20)))
    source_plan = ReconstructionPlan(
        name="oblique_polygon",
        base=OrientedExtrudeFeature(
            origin=tuple(component * -1.5 for component in direction),
            direction=direction,
            x_direction=(1.0, 0.0, 0.0),
            depth=3.0,
            outer=PolygonProfile(
                points=[(-10, -3), (10, -3), (8, 2), (-4, 3), (-10, 1)]
            ),
        ),
    )
    model = build_plan(source_plan)
    stl_path = step_to_stl(model, tmp_path, "oblique_polygon")
    data = load_mesh(stl_path, stl_path.name, "mm")

    candidates = generate_arbitrary_axis_prismatic_candidates(
        data,
        "oblique_polygon",
    )

    assert candidates
    recovered = next(
        candidate.plan.base
        for candidate in candidates
        if isinstance(candidate.plan.base, OrientedExtrudeFeature)
        and len(candidate.plan.operations) == 0
        and abs(candidate.plan.base.direction[1])
        == pytest.approx(abs(direction[1]), abs=0.002)
    )
    assert recovered.support_patch_id is not None
    assert recovered.terminating_patch_id is not None
    assert recovered.support_patch_id != recovered.terminating_patch_id
    assert any(
        isinstance(candidate.plan.base, OrientedExtrudeFeature)
        and len(candidate.plan.operations) == 0
        and abs(candidate.plan.base.direction[1])
        == pytest.approx(abs(direction[1]), abs=0.002)
        for candidate in candidates
    )
    expected_volume = model.val().Volume()
    assert min(
        abs(build_plan(candidate.plan).val().Volume() - expected_volume)
        / expected_volume
        for candidate in candidates
    ) < 0.01


def test_separates_oblique_cylinder_from_prismatic_layers(
    tmp_path: Path,
) -> None:
    source_plan = ReconstructionPlan(
        name="layered_oblique_round",
        base=ExtrudeFeature(
            axis=Axis.Y,
            start=-5,
            depth=2,
            outer=PolygonProfile(
                points=[(-6, -3), (6, -3), (6, 3), (-6, 3)]
            ),
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.Y,
                start=-3,
                depth=6,
                outer=PolygonProfile(
                    points=[(-5, -2), (5, -2), (5, 2), (-5, 2)]
                ),
            ),
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.Y,
                start=3,
                depth=2,
                outer=PolygonProfile(
                    points=[(-6, -3), (6, -3), (6, 3), (-6, 3)]
                ),
            ),
            OrientedCylinderFeature(
                mode="add",
                origin=(-4, 0, -4),
                direction=(1, 0, 1),
                depth=8 * 2**0.5,
                radius=1.2,
            ),
        ],
    )
    model = build_plan(source_plan)
    stl_path = step_to_stl(model, tmp_path, "layered_oblique_round")
    data = load_mesh(stl_path, stl_path.name, "mm")

    candidates = generate_analytic_cylinder_separation_candidates(
        data,
        "layered_oblique_round",
    )

    assert candidates
    recovered = [
        operation
        for candidate in candidates
        for operation in candidate.plan.operations
        if isinstance(operation, OrientedCylinderFeature)
    ]
    assert recovered
    assert recovered[0].radius == pytest.approx(1.2, abs=0.03)
    assert abs(recovered[0].direction[0]) == pytest.approx(
        2**-0.5,
        abs=0.003,
    )
    assert all(build_plan(candidate.plan).val().isValid() for candidate in candidates)


def test_conical_patch_detector_rejects_planes_and_accepts_frustum(
    tmp_path: Path,
) -> None:
    box_path = step_to_stl(
        cq.Workplane("XY").box(10, 8, 2),
        tmp_path,
        "planar_box",
    )
    cone_path = step_to_stl(
        cq.Workplane("XY").add(cq.Solid.makeCone(5, 4, 2)),
        tmp_path,
        "conical_frustum",
    )
    cylinder_path = step_to_stl(
        cq.Workplane("XY").cylinder(2, 5),
        tmp_path,
        "cylinder",
    )
    torus_path = step_to_stl(
        cq.Workplane(obj=cq.Solid.makeTorus(5, 1)),
        tmp_path,
        "torus",
    )
    partial_cone = cq.Workplane(obj=cq.Solid.makeCone(5, 3.5, 2)).intersect(
        cq.Workplane("XY").box(5.5, 12, 4, centered=(False, True, True))
    )
    partial_cone_path = step_to_stl(
        partial_cone,
        tmp_path,
        "partial_conical_sector",
    )

    box = load_mesh(box_path, box_path.name, "mm")
    cone = load_mesh(cone_path, cone_path.name, "mm")
    cylinder = load_mesh(cylinder_path, cylinder_path.name, "mm")
    torus = load_mesh(torus_path, torus_path.name, "mm")
    partial = load_mesh(partial_cone_path, partial_cone_path.name, "mm")

    assert not mesh_has_conical_patch(box)
    assert not mesh_has_conical_patch(cylinder)
    assert not mesh_has_conical_patch(torus)
    assert mesh_has_conical_patch(cone)
    assert mesh_has_conical_patch(partial)

from __future__ import annotations

from pathlib import Path

import cadquery as cq
import pytest

from app.cad import build_plan, plan_to_source
from app.schemas import (
    Axis,
    BooleanExtrudeFeature,
    CircleProfile,
    CylinderFeature,
    EdgeFinishFeature,
    ExtrudeFeature,
    OrientedBooleanExtrudeFeature,
    OrientedCylinderFeature,
    OrientedExtrudeFeature,
    PolygonProfile,
    ReconstructionPlan,
    RevolveFeature,
    SphereFeature,
    TaperedAddFeature,
)


def rectangle(
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
) -> PolygonProfile:
    return PolygonProfile(
        points=[
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
    )


def execute_generated_source(
    plan: ReconstructionPlan,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> cq.Workplane:
    monkeypatch.chdir(tmp_path)
    namespace: dict[str, object] = {}
    exec(plan_to_source(plan), namespace)
    result = namespace["result"]
    assert isinstance(result, cq.Workplane)
    assert (tmp_path / "reconstruction.step").is_file()
    return result


def test_ruled_loft_is_editable_and_generated_source_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="tapered-frame",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=2,
            outer=rectangle(-10, -10, 10, 10),
            holes=[rectangle(-4, -4, 4, 4)],
        ),
        operations=[
            TaperedAddFeature(
                axis=Axis.Z,
                start=2,
                depth=4,
                start_outer=rectangle(-10, -10, 10, 10),
                end_outer=rectangle(-8, -8, 8, 8),
                holes=[rectangle(-4, -4, 4, 4)],
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )


def test_smooth_multisection_loft_generated_source_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="smooth-loft",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=0.5,
            outer=rectangle(-3, -2, 3, 2),
        ),
        operations=[
            TaperedAddFeature(
                axis=Axis.Z,
                start=0.5,
                depth=5.5,
                start_outer=rectangle(-3, -2, 3, 2),
                intermediate_offsets=[1.5, 3.5],
                intermediate_profiles=[
                    rectangle(-4, -2.5, 4, 2.5),
                    rectangle(-3.5, -3, 3.5, 3),
                ],
                end_outer=rectangle(-2, -2, 2, 2),
                smooth=True,
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )


def test_oriented_cylinder_local_chamfer_matches_generated_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="locally-chamfered-cylinder-cut",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=4,
            outer=rectangle(-6, -6, 6, 6),
        ),
        operations=[
            OrientedCylinderFeature(
                mode="cut",
                origin=(0, 0, 0),
                direction=(0, 0, 1),
                depth=4,
                radius=2,
            ),
            EdgeFinishFeature(
                mode="chamfer",
                axis=Axis.Z,
                end="start",
                size=0.1,
                selector="circle",
                center=(0, 0),
                radius=2,
                feature_index=0,
            ),
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)
    surface_types = {face.geomType() for face in runtime.val().Faces()}

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )
    assert "CONE" in surface_types


def test_revolve_profile_preserves_analytic_cylinders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="turned-tube",
        base=RevolveFeature(
            axis=Axis.Z,
            center=(0, 0),
            profile=rectangle(5, -4, 7, 4),
        ),
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)
    runtime_types = {face.geomType() for face in runtime.val().Faces()}

    assert runtime.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )
    assert "CYLINDER" in runtime_types


def test_feature_local_fillet_generated_source_matches_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="local-fillet",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=2,
            outer=rectangle(-8, -8, 8, 8),
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.Z,
                start=2,
                depth=4,
                outer=CircleProfile(center=(0, 0), radius=4),
            ),
            EdgeFinishFeature(
                mode="fillet",
                axis=Axis.Z,
                end="end",
                size=0.6,
                selector="circle",
                center=(0, 0),
                radius=4,
                feature_index=0,
            ),
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )
    assert "TORUS" in {face.geomType() for face in runtime.val().Faces()}


def test_one_extrusion_can_hold_multiple_sketch_regions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="multi-region-extrusion",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=1,
            outer=rectangle(-6, -3, 6, 3),
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.Z,
                start=1,
                depth=2,
                outer=CircleProfile(center=(-3, 0), radius=2),
                additional_regions=[
                    CircleProfile(center=(3, 0), radius=2),
                ],
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )


def test_base_extrusion_can_hold_multiple_sketch_regions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="multi-region-base",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=1,
            outer=CircleProfile(center=(-3, 0), radius=2),
            additional_regions=[CircleProfile(center=(3, 0), radius=2)],
        ),
        operations=[
            BooleanExtrudeFeature(
                mode="add",
                axis=Axis.Z,
                start=1,
                depth=1,
                outer=rectangle(-5, -2, 5, 2),
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )


def test_base_local_fillet_generated_source_matches_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="base-fillet",
        base=CylinderFeature(
            axis=Axis.Z,
            start=0,
            depth=3,
            center=(0, 0),
            radius=5,
        ),
        operations=[
            EdgeFinishFeature(
                mode="fillet",
                axis=Axis.Z,
                end="start",
                size=0.2,
                selector="nearest",
                center=(0, 0),
                feature_index=-1,
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )
    assert "TORUS" in {face.geomType() for face in runtime.val().Faces()}


def test_oriented_annular_cylinder_generated_source_matches_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="diagonal-tube",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=4,
            outer=rectangle(-6, -4, 6, 4),
        ),
        operations=[
            OrientedCylinderFeature(
                mode="add",
                origin=(-4, 0, 0),
                direction=(1, 0, 1),
                depth=12,
                radius=3,
                inner_radius=1,
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )
    assert len(
        [face for face in runtime.val().Faces() if face.geomType() == "CYLINDER"]
    ) >= 2


def test_layered_oriented_extrusion_generated_source_matches_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direction = (2**-0.5, 0, -(2**-0.5))
    x_direction = (0, -1, 0)
    plan = ReconstructionPlan(
        name="diagonal-layered-extrusion",
        base=OrientedExtrudeFeature(
            origin=tuple(component * -6 for component in direction),
            direction=direction,
            x_direction=x_direction,
            depth=8,
            outer=CircleProfile(center=(0, 0), radius=5),
            holes=[CircleProfile(center=(0, 0), radius=2)],
        ),
        operations=[
            OrientedBooleanExtrudeFeature(
                mode="add",
                origin=tuple(component * 2 for component in direction),
                direction=direction,
                x_direction=x_direction,
                depth=2,
                outer=rectangle(-4, -4, 4, 4),
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert len(runtime.val().Solids()) == 1
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )


def test_sphere_cut_is_editable_and_generated_source_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReconstructionPlan(
        name="spherical-pocket",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=5,
            outer=rectangle(-6, -6, 6, 6),
        ),
        operations=[
            SphereFeature(
                mode="cut",
                center=(4.5, 4.5, 4.0),
                radius=2.0,
            )
        ],
    )

    runtime = build_plan(plan)
    generated = execute_generated_source(plan, tmp_path, monkeypatch)

    assert runtime.val().isValid()
    assert generated.val().isValid()
    assert generated.val().Volume() == pytest.approx(
        runtime.val().Volume(),
        rel=1e-9,
    )
    assert "SPHERE" in {face.geomType() for face in runtime.val().Faces()}

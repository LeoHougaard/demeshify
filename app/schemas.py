from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

Point2D = tuple[float, float]
Point3D = tuple[float, float, float]


class FeatureNode(BaseModel):
    """Common identity and timeline state for every editable CAD feature.

    Geometry generators only need to provide their historical geometric
    fields. ``ReconstructionPlan`` assigns stable ids, readable names, and
    explicit linear dependencies when it validates the completed plan.
    """

    feature_id: str = ""
    name: str = ""
    tree_index: Annotated[int, Field(ge=0)] = 0
    depends_on: list[str] = Field(default_factory=list)
    suppressed: bool = False
    confidence: Annotated[float | None, Field(default=None, ge=0, le=1)] = None


class Axis(StrEnum):
    X = "X"
    Y = "Y"
    Z = "Z"


class CircleProfile(BaseModel):
    kind: Literal["circle"] = "circle"
    center: Point2D
    radius: Annotated[float, Field(gt=0)]


class PolygonProfile(BaseModel):
    kind: Literal["polygon"] = "polygon"
    points: Annotated[list[Point2D], Field(min_length=3)]


class LineSegment(BaseModel):
    kind: Literal["line"] = "line"
    end: Point2D


class ArcSegment(BaseModel):
    kind: Literal["arc"] = "arc"
    mid: Point2D
    end: Point2D


class SplineSegment(BaseModel):
    """An open, changing-curvature sketch segment.

    ``points`` are editable interpolation points between the current path point
    and ``end``.  Optional endpoint tangent directions preserve a smooth join
    to neighboring lines/arcs without pretending the curve has one radius.
    """

    kind: Literal["spline"] = "spline"
    points: Annotated[list[Point2D], Field(min_length=1, max_length=64)]
    end: Point2D
    start_tangent: Point2D | None = None
    end_tangent: Point2D | None = None


PathSegment = Annotated[
    LineSegment | ArcSegment | SplineSegment,
    Field(discriminator="kind"),
]


class PathProfile(BaseModel):
    kind: Literal["path"] = "path"
    start: Point2D
    segments: Annotated[list[PathSegment], Field(min_length=2)]


class SplineProfile(BaseModel):
    kind: Literal["spline"] = "spline"
    points: Annotated[list[Point2D], Field(min_length=4, max_length=256)]
    periodic: Literal[True] = True


Profile = Annotated[
    CircleProfile | PolygonProfile | PathProfile | SplineProfile,
    Field(discriminator="kind"),
]


class ExtrudeFeature(FeatureNode):
    kind: Literal["extrude"] = "extrude"
    axis: Axis
    start: float
    depth: Annotated[float, Field(gt=0)]
    outer: Profile
    holes: list[Profile] = Field(default_factory=list)
    additional_regions: list[Profile] = Field(default_factory=list)


class CylinderFeature(FeatureNode):
    kind: Literal["cylinder"] = "cylinder"
    axis: Axis
    start: float
    depth: Annotated[float, Field(gt=0)]
    center: Point2D
    radius: Annotated[float, Field(gt=0)]
    inner_radius: Annotated[float | None, Field(default=None, gt=0)] = None

    @model_validator(mode="after")
    def validate_inner_radius(self) -> CylinderFeature:
        if self.inner_radius is not None and self.inner_radius >= self.radius:
            raise ValueError("inner_radius must be smaller than radius")
        return self


class RevolveFeature(FeatureNode):
    kind: Literal["revolve"] = "revolve"
    axis: Axis
    center: Point2D
    profile: Profile


class OrientedExtrudeFeature(FeatureNode):
    """A sketch extrusion on an arbitrary 3D plane."""

    kind: Literal["oriented_extrude"] = "oriented_extrude"
    origin: Point3D
    direction: Point3D
    x_direction: Point3D
    depth: Annotated[float, Field(gt=0)]
    outer: Profile
    holes: list[Profile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_frame(self) -> OrientedExtrudeFeature:
        direction_length = sum(component * component for component in self.direction) ** 0.5
        x_length = sum(component * component for component in self.x_direction) ** 0.5
        dot = sum(
            direction * x_direction
            for direction, x_direction in zip(
                self.direction,
                self.x_direction,
                strict=True,
            )
        )
        if direction_length <= 1e-9 or x_length <= 1e-9:
            raise ValueError("direction and x_direction must be non-zero")
        if abs(dot) / (direction_length * x_length) > 1e-4:
            raise ValueError("direction and x_direction must be perpendicular")
        return self


BaseFeature = Annotated[
    ExtrudeFeature | CylinderFeature | RevolveFeature | OrientedExtrudeFeature,
    Field(discriminator="kind"),
]


class RoundHoleFeature(FeatureNode):
    kind: Literal["round_hole"] = "round_hole"
    axis: Axis
    center: Point2D
    diameter: Annotated[float, Field(gt=0)]
    start: float
    depth: Annotated[float, Field(gt=0)]
    through: bool = True


class ConicalHoleFeature(FeatureNode):
    kind: Literal["conical_hole"] = "conical_hole"
    axis: Axis
    center: Point2D
    start: float
    depth: Annotated[float, Field(gt=0)]
    start_diameter: Annotated[float, Field(gt=0)]
    end_diameter: Annotated[float, Field(gt=0)]


class ConicalAddFeature(FeatureNode):
    kind: Literal["conical_add"] = "conical_add"
    axis: Axis
    center: Point2D
    start: float
    depth: Annotated[float, Field(gt=0)]
    start_diameter: Annotated[float, Field(gt=0)]
    end_diameter: Annotated[float, Field(gt=0)]
    holes: list[Profile] = Field(default_factory=list)


class SphereFeature(FeatureNode):
    """An analytic spherical addition or cut recovered from a mesh patch."""

    kind: Literal["sphere"] = "sphere"
    mode: Literal["add", "cut"]
    center: Point3D
    radius: Annotated[float, Field(gt=0)]


class BooleanExtrudeFeature(FeatureNode):
    kind: Literal["boolean_extrude"] = "boolean_extrude"
    mode: Literal["add", "cut"]
    axis: Axis
    start: float
    depth: Annotated[float, Field(gt=0)]
    outer: Profile
    holes: list[Profile] = Field(default_factory=list)
    # A single CAD extrusion can select several disjoint sketch regions. Keep
    # them in one timeline feature instead of inflating each region into a
    # separate boolean operation. Regions containing their own holes remain
    # separate operations because those require per-region hole ownership.
    additional_regions: list[Profile] = Field(default_factory=list)


class OrientedCylinderFeature(FeatureNode):
    """An editable cylindrical add/cut whose axis is not limited to X/Y/Z."""

    kind: Literal["oriented_cylinder"] = "oriented_cylinder"
    mode: Literal["add", "cut"]
    origin: Point3D
    direction: Point3D
    depth: Annotated[float, Field(gt=0)]
    radius: Annotated[float, Field(gt=0)]
    inner_radius: Annotated[float | None, Field(default=None, gt=0)] = None

    @model_validator(mode="after")
    def validate_oriented_cylinder(self) -> OrientedCylinderFeature:
        direction_length = sum(component * component for component in self.direction) ** 0.5
        if direction_length <= 1e-9:
            raise ValueError("direction must be non-zero")
        if self.inner_radius is not None:
            if self.mode != "add":
                raise ValueError("inner_radius is only valid for additive cylinders")
            if self.inner_radius >= self.radius:
                raise ValueError("inner_radius must be smaller than radius")
        return self


class OrientedBooleanExtrudeFeature(FeatureNode):
    kind: Literal["oriented_boolean_extrude"] = "oriented_boolean_extrude"
    mode: Literal["add", "cut"]
    origin: Point3D
    direction: Point3D
    x_direction: Point3D
    depth: Annotated[float, Field(gt=0)]
    outer: Profile
    holes: list[Profile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_frame(self) -> OrientedBooleanExtrudeFeature:
        direction_length = sum(component * component for component in self.direction) ** 0.5
        x_length = sum(component * component for component in self.x_direction) ** 0.5
        dot = sum(
            direction * x_direction
            for direction, x_direction in zip(
                self.direction,
                self.x_direction,
                strict=True,
            )
        )
        if direction_length <= 1e-9 or x_length <= 1e-9:
            raise ValueError("direction and x_direction must be non-zero")
        if abs(dot) / (direction_length * x_length) > 1e-4:
            raise ValueError("direction and x_direction must be perpendicular")
        return self


class TaperedAddFeature(FeatureNode):
    kind: Literal["tapered_add"] = "tapered_add"
    axis: Axis
    start: float
    depth: Annotated[float, Field(gt=0)]
    start_outer: Profile
    end_outer: Profile
    intermediate_offsets: list[float] = Field(default_factory=list)
    intermediate_profiles: list[Profile] = Field(default_factory=list)
    smooth: bool = False
    holes: list[Profile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_intermediate_sections(self) -> TaperedAddFeature:
        if len(self.intermediate_offsets) != len(self.intermediate_profiles):
            raise ValueError(
                "loft intermediate offsets and profiles must have equal lengths"
            )
        if any(
            offset <= 0 or offset >= self.depth
            for offset in self.intermediate_offsets
        ):
            raise ValueError("loft intermediate offsets must lie inside its depth")
        if any(
            second <= first
            for first, second in zip(
                self.intermediate_offsets,
                self.intermediate_offsets[1:],
                strict=False,
            )
        ):
            raise ValueError("loft intermediate offsets must be strictly increasing")
        return self


class EdgeFinishFeature(FeatureNode):
    kind: Literal["edge_finish"] = "edge_finish"
    mode: Literal["fillet", "chamfer"]
    axis: Axis
    end: Literal["start", "end", "both"] | None = None
    size: Annotated[float, Field(gt=0)]
    size2: Annotated[float | None, Field(default=None, gt=0)] = None
    selector: Literal["all", "circle", "outer", "nearest"] = "all"
    center: Point2D | None = None
    radius: Annotated[float | None, Field(default=None, gt=0)] = None
    position: float | None = None
    # -1 addresses the base feature; non-negative values address operations.
    feature_index: Annotated[int | None, Field(default=None, ge=-1)] = None
    # Stable ids survive timeline reordering. ``feature_index`` remains for
    # backwards compatibility with reconstruction plans generated before v2.
    target_feature_id: str | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> EdgeFinishFeature:
        if self.selector == "circle" and (
            self.center is None or self.radius is None
        ):
            raise ValueError("circle edge finishes require center and radius")
        if self.selector == "nearest" and self.center is None:
            raise ValueError("nearest edge finishes require a projected center")
        if self.end is None and self.position is None:
            raise ValueError("edge finishes require an end or an axis position")
        if self.end is not None and self.position is not None:
            raise ValueError("edge finish end and position are mutually exclusive")
        if self.feature_index is not None and self.position is not None:
            raise ValueError("feature-local finishes cannot use an absolute position")
        if self.feature_index is not None and self.end == "both":
            raise ValueError("feature-local finishes must name one feature end")
        if self.size2 is not None and self.mode != "chamfer":
            raise ValueError("a second edge distance is only valid for chamfers")
        return self


OperationFeature = Annotated[
    RoundHoleFeature
    | ConicalHoleFeature
    | ConicalAddFeature
    | SphereFeature
    | BooleanExtrudeFeature
    | OrientedCylinderFeature
    | OrientedBooleanExtrudeFeature
    | TaperedAddFeature
    | EdgeFinishFeature,
    Field(discriminator="kind"),
]


class ReconstructionPlan(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    revision: Annotated[int, Field(ge=0)] = 0
    name: str
    units: Literal["mm"] = "mm"
    base: BaseFeature
    operations: list[OperationFeature] = Field(default_factory=list)
    # Original measured values stay separate from the currently selected CAD
    # parameters, so the editor can show deltas and restore measurements even
    # after the document has been saved and reopened.
    measured_values: dict[str, float] = Field(default_factory=dict)
    parameter_sources: dict[
        str,
        Literal["measured", "nominal", "user", "ai"],
    ] = Field(default_factory=dict)
    locked_parameters: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    source: Literal["deterministic", "ai", "hybrid"] = "deterministic"

    @model_validator(mode="after")
    def normalize_feature_tree(self) -> ReconstructionPlan:
        features: list[FeatureNode] = [self.base, *self.operations]
        used_ids: set[str] = set()
        kind_counts: dict[str, int] = {}
        names = {
            "extrude": "Base extrude",
            "cylinder": "Base cylinder",
            "revolve": "Base revolve",
            "oriented_extrude": "Base oriented extrude",
            "round_hole": "Hole",
            "conical_hole": "Countersink",
            "conical_add": "Conical addition",
            "sphere": "Sphere",
            "boolean_extrude": "Extrude",
            "oriented_cylinder": "Oriented cylinder",
            "oriented_boolean_extrude": "Oriented extrude",
            "tapered_add": "Loft",
            "edge_finish": "Edge finish",
        }

        for index, feature in enumerate(features):
            default_id = "base" if index == 0 else f"feature-{index:03d}"
            candidate_id = feature.feature_id.strip()
            if not candidate_id or candidate_id in used_ids:
                candidate_id = default_id
                suffix = 2
                while candidate_id in used_ids:
                    candidate_id = f"{default_id}-{suffix}"
                    suffix += 1
            feature.feature_id = candidate_id
            used_ids.add(candidate_id)

            kind_counts[feature.kind] = kind_counts.get(feature.kind, 0) + 1
            if not feature.name.strip():
                label = names.get(feature.kind, feature.kind.replace("_", " ").title())
                if index == 0 or kind_counts[feature.kind] == 1:
                    feature.name = label
                else:
                    feature.name = f"{label} {kind_counts[feature.kind]}"
            feature.tree_index = index

        # A Part Studio is a linear regeneration history. Record that ordering
        # explicitly, then add stable target dependencies for feature-local
        # fillets and chamfers.
        for index, feature in enumerate(features):
            dependencies = [] if index == 0 else [features[index - 1].feature_id]
            if isinstance(feature, EdgeFinishFeature):
                if feature.target_feature_id is None and feature.feature_index is not None:
                    target_index = 0 if feature.feature_index == -1 else feature.feature_index + 1
                    if 0 <= target_index < len(features):
                        feature.target_feature_id = features[target_index].feature_id
                if feature.target_feature_id is not None:
                    if feature.target_feature_id not in used_ids:
                        raise ValueError(
                            f"Unknown edge-finish target: {feature.target_feature_id}"
                        )
                    target_index = next(
                        item_index
                        for item_index, item in enumerate(features)
                        if item.feature_id == feature.target_feature_id
                    )
                    if target_index >= index:
                        raise ValueError(
                            "An edge finish must appear after its target feature"
                        )
                    feature.feature_index = -1 if target_index == 0 else target_index - 1
                    if feature.target_feature_id not in dependencies:
                        dependencies.append(feature.target_feature_id)
            feature.depends_on = dependencies

        current_values: dict[str, float] = {}

        def collect(value: Any, path: str) -> None:
            if isinstance(value, bool) or value is None:
                return
            if isinstance(value, (int, float)):
                current_values[path] = float(value)
                return
            if isinstance(value, BaseModel):
                for field_name in type(value).model_fields:
                    if field_name in {
                        "tree_index",
                        "confidence",
                        "feature_index",
                    }:
                        continue
                    collect(getattr(value, field_name), f"{path}.{field_name}")
                return
            if isinstance(value, (list, tuple)):
                for item_index, item in enumerate(value):
                    collect(item, f"{path}.{item_index}")

        for feature in features:
            collect(feature, feature.feature_id)

        self.measured_values = {
            path: float(self.measured_values.get(path, value))
            for path, value in current_values.items()
        }
        self.parameter_sources = {
            path: self.parameter_sources.get(path, "measured")
            for path in current_values
        }
        self.locked_parameters = sorted(
            path for path in set(self.locked_parameters) if path in current_values
        )
        return self


class PlanEditRequest(BaseModel):
    plan: ReconstructionPlan
    expected_revision: Annotated[int, Field(ge=0)]


class MeshReport(BaseModel):
    file_name: str
    triangle_count: int
    vertex_count: int
    watertight: bool
    body_count: int
    dimensions_mm: Point3D
    volume_mm3: float | None
    surface_area_mm2: float
    input_units: str
    unit_scale: float


class ScoreReport(BaseModel):
    score: float
    chamfer_rms_mm: float
    chamfer_p95_mm: float
    chamfer_max_mm: float
    volume_error_percent: float
    valid_solid: bool
    candidate_count: int


class ReconstructionReport(BaseModel):
    id: str
    status: Literal["complete", "best_effort", "failed"]
    mesh: MeshReport
    plan: ReconstructionPlan | None
    score: ScoreReport | None
    warnings: list[str] = Field(default_factory=list)
    elapsed_seconds: float
    prompt: str = ""

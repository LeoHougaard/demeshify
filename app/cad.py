from __future__ import annotations

from pathlib import Path

import cadquery as cq

from .schemas import (
    ArcSegment,
    Axis,
    BooleanExtrudeFeature,
    CircleProfile,
    ConicalAddFeature,
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
    Profile,
    ReconstructionPlan,
    RevolveFeature,
    RoundHoleFeature,
    SphereFeature,
    SplineProfile,
    SplineSegment,
    TaperedAddFeature,
)


def _plane(axis: Axis, start: float) -> cq.Plane:
    if axis == Axis.X:
        return cq.Plane(origin=(start, 0, 0), xDir=(0, 1, 0), normal=(1, 0, 0))
    if axis == Axis.Y:
        return cq.Plane(origin=(0, start, 0), xDir=(1, 0, 0), normal=(0, 1, 0))
    return cq.Plane(origin=(0, 0, start), xDir=(1, 0, 0), normal=(0, 0, 1))


def _oriented_plane(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    x_direction: tuple[float, float, float],
) -> cq.Plane:
    return cq.Plane(origin=origin, xDir=x_direction, normal=direction)


def _direction_vector(
    direction: tuple[float, float, float],
    depth: float,
) -> cq.Vector:
    vector = cq.Vector(*direction)
    return vector.normalized().multiply(depth)


def _profile_solid(profile: Profile, plane: cq.Plane, depth: float) -> cq.Workplane:
    workplane = cq.Workplane(plane)
    if isinstance(profile, CircleProfile):
        return workplane.center(*profile.center).circle(profile.radius).extrude(depth)
    if isinstance(profile, PolygonProfile):
        return workplane.polyline(profile.points).close().extrude(depth)
    if isinstance(profile, PathProfile):
        path = workplane.moveTo(*profile.start)
        for segment in profile.segments:
            if isinstance(segment, LineSegment):
                path = path.lineTo(*segment.end)
            elif isinstance(segment, ArcSegment):
                path = path.threePointArc(segment.mid, segment.end)
            elif isinstance(segment, SplineSegment):
                tangents = (
                    [segment.start_tangent, segment.end_tangent]
                    if segment.start_tangent is not None
                    and segment.end_tangent is not None
                    else None
                )
                path = path.spline(
                    [*segment.points, segment.end],
                    tangents=tangents,
                    includeCurrent=True,
                )
        return path.close().extrude(depth)
    if isinstance(profile, SplineProfile):
        return workplane.spline(profile.points, periodic=True).close().extrude(depth)
    raise TypeError(f"Unsupported profile: {type(profile).__name__}")


def _profile_wire(profile: Profile, plane: cq.Plane) -> cq.Wire:
    workplane = cq.Workplane(plane)
    if isinstance(profile, CircleProfile):
        return workplane.center(*profile.center).circle(profile.radius).val()
    if isinstance(profile, PolygonProfile):
        return workplane.polyline(profile.points).close().val()
    if isinstance(profile, PathProfile):
        path = workplane.moveTo(*profile.start)
        for segment in profile.segments:
            if isinstance(segment, LineSegment):
                path = path.lineTo(*segment.end)
            elif isinstance(segment, ArcSegment):
                path = path.threePointArc(segment.mid, segment.end)
            elif isinstance(segment, SplineSegment):
                tangents = (
                    [segment.start_tangent, segment.end_tangent]
                    if segment.start_tangent is not None
                    and segment.end_tangent is not None
                    else None
                )
                path = path.spline(
                    [*segment.points, segment.end],
                    tangents=tangents,
                    includeCurrent=True,
                )
        return path.close().val()
    if isinstance(profile, SplineProfile):
        return workplane.spline(profile.points, periodic=True).close().val()
    raise TypeError(f"Unsupported profile: {type(profile).__name__}")


def _profile_prism(
    outer: Profile,
    holes: list[Profile],
    plane: cq.Plane,
    direction: tuple[float, float, float],
    depth: float,
) -> cq.Workplane:
    """Extrude a sketch along an arbitrary vector, independent of its plane."""

    vector = _direction_vector(direction, depth)
    solid = cq.Solid.extrudeLinear(
        _profile_wire(outer, plane),
        [_profile_wire(hole, plane) for hole in holes],
        vector,
    )
    return cq.Workplane(obj=solid)


def _revolve_plane(base: RevolveFeature) -> tuple[cq.Plane, cq.Vector, cq.Vector]:
    if base.axis == Axis.X:
        origin = cq.Vector(0, base.center[0], base.center[1])
        plane = cq.Plane(
            origin=origin,
            xDir=(0, 1, 0),
            normal=(0, 0, -1),
        )
        return plane, origin, origin + cq.Vector(1, 0, 0)
    if base.axis == Axis.Y:
        origin = cq.Vector(base.center[0], 0, -base.center[1])
        plane = cq.Plane(
            origin=origin,
            xDir=(1, 0, 0),
            normal=(0, 0, 1),
        )
        return plane, origin, origin + cq.Vector(0, 1, 0)
    origin = cq.Vector(base.center[0], base.center[1], 0)
    plane = cq.Plane(
        origin=origin,
        xDir=(1, 0, 0),
        normal=(0, -1, 0),
    )
    return plane, origin, origin + cq.Vector(0, 0, 1)


def _build_base(
    base: ExtrudeFeature
    | CylinderFeature
    | RevolveFeature
    | OrientedExtrudeFeature,
) -> cq.Workplane:
    if isinstance(base, RevolveFeature):
        plane, axis_start, axis_end = _revolve_plane(base)
        wire = _profile_wire(base.profile, plane)
        return cq.Workplane(
            obj=cq.Solid.revolve(
                wire,
                [],
                360,
                axis_start,
                axis_end,
            )
        )
    if isinstance(base, OrientedExtrudeFeature):
        plane = _oriented_plane(
            base.origin,
            base.plane_normal or base.direction,
            base.x_direction,
        )
        result = _profile_prism(
            base.outer,
            base.holes,
            plane,
            base.direction,
            base.depth,
        )
        for region in base.additional_regions:
            result = result.union(
                _profile_prism(
                    region,
                    [],
                    plane,
                    base.direction,
                    base.depth,
                )
            )
        return result
    plane = _plane(base.axis, base.start)
    if isinstance(base, CylinderFeature):
        result = cq.Workplane(plane).center(*base.center).circle(base.radius).extrude(base.depth)
        if base.inner_radius is not None:
            cutter = (
                cq.Workplane(plane)
                .center(*base.center)
                .circle(base.inner_radius)
                .extrude(base.depth)
            )
            result = result.cut(cutter)
        return result

    result = _profile_solid(base.outer, plane, base.depth)
    for hole in base.holes:
        result = result.cut(_profile_solid(hole, plane, base.depth))
    for region in base.additional_regions:
        result = result.union(_profile_solid(region, plane, base.depth))
    return result


def _hole_cutter(operation: RoundHoleFeature) -> cq.Workplane:
    plane = _plane(operation.axis, operation.start)
    return (
        cq.Workplane(plane)
        .center(*operation.center)
        .circle(operation.diameter / 2)
        .extrude(operation.depth)
    )


def _conical_hole_cutter(operation: ConicalHoleFeature) -> cq.Workplane:
    if operation.axis == Axis.X:
        start = cq.Vector(operation.start, operation.center[0], operation.center[1])
        direction = cq.Vector(1, 0, 0)
    elif operation.axis == Axis.Y:
        start = cq.Vector(operation.center[0], operation.start, -operation.center[1])
        direction = cq.Vector(0, 1, 0)
    else:
        start = cq.Vector(operation.center[0], operation.center[1], operation.start)
        direction = cq.Vector(0, 0, 1)
    solid = cq.Solid.makeCone(
        operation.start_diameter / 2,
        operation.end_diameter / 2,
        operation.depth,
        start,
        direction,
    )
    return cq.Workplane(obj=solid)


def _conical_addition(operation: ConicalAddFeature) -> cq.Workplane:
    cutter_like = ConicalHoleFeature(
        axis=operation.axis,
        center=operation.center,
        start=operation.start,
        depth=operation.depth,
        start_diameter=operation.start_diameter,
        end_diameter=operation.end_diameter,
    )
    result = _conical_hole_cutter(cutter_like)
    plane = _plane(operation.axis, operation.start)
    for hole in operation.holes:
        result = result.cut(_profile_solid(hole, plane, operation.depth))
    return result


def _boolean_extrusion(operation: BooleanExtrudeFeature) -> cq.Workplane:
    plane = _plane(operation.axis, operation.start)
    result = _profile_solid(operation.outer, plane, operation.depth)
    for hole in operation.holes:
        result = result.cut(_profile_solid(hole, plane, operation.depth))
    for region in operation.additional_regions:
        result = result.union(_profile_solid(region, plane, operation.depth))
    return result


def _oriented_cylinder(operation: OrientedCylinderFeature) -> cq.Workplane:
    origin = cq.Vector(*operation.origin)
    direction = cq.Vector(*operation.direction).normalized()
    solid = cq.Solid.makeCylinder(
        operation.radius,
        operation.depth,
        origin,
        direction,
    )
    result = cq.Workplane(obj=solid)
    if operation.inner_radius is not None:
        inner = cq.Workplane(
            obj=cq.Solid.makeCylinder(
                operation.inner_radius,
                operation.depth,
                origin,
                direction,
            )
        )
        result = result.cut(inner)
    return result


def _sphere(operation: SphereFeature) -> cq.Workplane:
    return cq.Workplane(
        obj=cq.Solid.makeSphere(
            operation.radius,
            cq.Vector(*operation.center),
        )
    )


def _oriented_boolean_extrusion(
    operation: OrientedBooleanExtrudeFeature,
) -> cq.Workplane:
    plane = _oriented_plane(
        operation.origin,
        operation.plane_normal or operation.direction,
        operation.x_direction,
    )
    result = _profile_prism(
        operation.outer,
        operation.holes,
        plane,
        operation.direction,
        operation.depth,
    )
    for region in operation.additional_regions:
        result = result.union(
            _profile_prism(
                region,
                [],
                plane,
                operation.direction,
                operation.depth,
            )
        )
    return result


def _tapered_addition(operation: TaperedAddFeature) -> cq.Workplane:
    positions = [
        operation.start,
        *(
            operation.start + offset
            for offset in operation.intermediate_offsets
        ),
        operation.start + operation.depth,
    ]
    profiles = [
        operation.start_outer,
        *operation.intermediate_profiles,
        operation.end_outer,
    ]
    solid = cq.Solid.makeLoft(
        [
            _profile_wire(profile, _plane(operation.axis, position))
            for position, profile in zip(positions, profiles, strict=True)
        ],
        ruled=not operation.smooth,
    )
    result = cq.Workplane(obj=solid)
    start_plane = _plane(operation.axis, operation.start)
    for hole in operation.holes:
        result = result.cut(_profile_solid(hole, start_plane, operation.depth))
    return result


def _edge_axis_extent(edge: cq.Edge, axis: Axis) -> float:
    bounds = edge.BoundingBox()
    if axis == Axis.X:
        return float(bounds.xlen)
    if axis == Axis.Y:
        return float(bounds.ylen)
    return float(bounds.zlen)


def _edge_axis_center(edge: cq.Edge, axis: Axis) -> float:
    bounds = edge.BoundingBox()
    if axis == Axis.X:
        return float((bounds.xmin + bounds.xmax) / 2)
    if axis == Axis.Y:
        return float((bounds.ymin + bounds.ymax) / 2)
    return float((bounds.zmin + bounds.zmax) / 2)


def _edge_circle_parameters(
    edge: cq.Edge,
    axis: Axis,
) -> tuple[tuple[float, float], float] | None:
    if edge.geomType() != "CIRCLE":
        return None
    circle = edge._geomAdaptor().Circle()
    location = circle.Location()
    if axis == Axis.X:
        center = (location.Y(), location.Z())
    elif axis == Axis.Y:
        center = (location.X(), -location.Z())
    else:
        center = (location.X(), location.Y())
    return (float(center[0]), float(center[1])), float(circle.Radius())


def _edge_plane_center(edge: cq.Edge, axis: Axis) -> tuple[float, float]:
    center = edge.Center()
    if axis == Axis.X:
        return float(center.y), float(center.z)
    if axis == Axis.Y:
        return float(center.x), float(-center.z)
    return float(center.x), float(center.y)


def _apply_edge_finish(
    result: cq.Workplane,
    operation: EdgeFinishFeature,
) -> cq.Workplane:
    shape = result.val()
    bounds = shape.BoundingBox()
    axis_min, axis_max = {
        Axis.X: (bounds.xmin, bounds.xmax),
        Axis.Y: (bounds.ymin, bounds.ymax),
        Axis.Z: (bounds.zmin, bounds.zmax),
    }[operation.axis]
    diagonal = (bounds.xlen**2 + bounds.ylen**2 + bounds.zlen**2) ** 0.5
    tolerance = max(diagonal * 1e-6, 1e-6)
    selector_tolerance = max(diagonal * 1e-4, 1e-4)
    outer_edges: list[cq.Edge] = []
    if operation.selector == "outer":
        for face in shape.Faces():
            if face.geomType() != "PLANE":
                continue
            face_bounds = face.BoundingBox()
            face_extent = {
                Axis.X: face_bounds.xlen,
                Axis.Y: face_bounds.ylen,
                Axis.Z: face_bounds.zlen,
            }[operation.axis]
            if face_extent > tolerance:
                continue
            face_position = {
                Axis.X: (face_bounds.xmin + face_bounds.xmax) / 2,
                Axis.Y: (face_bounds.ymin + face_bounds.ymax) / 2,
                Axis.Z: (face_bounds.zmin + face_bounds.zmax) / 2,
            }[operation.axis]
            position_matches = (
                operation.position is not None
                and abs(face_position - operation.position) <= selector_tolerance
            ) or (
                operation.position is None
                and (
                    operation.end == "both"
                    or (
                        operation.end == "start"
                        and abs(face_position - axis_min) <= tolerance
                    )
                    or (
                        operation.end == "end"
                        and abs(face_position - axis_max) <= tolerance
                    )
                )
            )
            if position_matches:
                outer_edges.extend(face.outerWire().Edges())
    edges = [
        edge
        for edge in shape.Edges()
        if _edge_axis_extent(edge, operation.axis) <= tolerance
        and (
            (
                operation.position is not None
                and abs(
                    _edge_axis_center(edge, operation.axis) - operation.position
                )
                <= selector_tolerance
            )
            or (
                operation.position is None
                and (
                    operation.end == "both"
                    or (
                        operation.end == "start"
                        and abs(
                            _edge_axis_center(edge, operation.axis) - axis_min
                        )
                        <= tolerance
                    )
                    or (
                        operation.end == "end"
                        and abs(
                            _edge_axis_center(edge, operation.axis) - axis_max
                        )
                        <= tolerance
                    )
                )
            )
        )
        and (
            operation.selector == "all"
            or operation.selector == "nearest"
            or (
                operation.selector == "outer"
                and any(edge.isSame(outer_edge) for outer_edge in outer_edges)
            )
            or (
                (circle := _edge_circle_parameters(edge, operation.axis)) is not None
                and operation.center is not None
                and operation.radius is not None
                and (
                    (circle[0][0] - operation.center[0]) ** 2
                    + (circle[0][1] - operation.center[1]) ** 2
                )
                ** 0.5
                <= selector_tolerance
                and abs(circle[1] - operation.radius) <= selector_tolerance
            )
        )
    ]
    if operation.selector == "nearest" and edges and operation.center is not None:
        edges = [
            min(
                edges,
                key=lambda edge: (
                    (_edge_plane_center(edge, operation.axis)[0] - operation.center[0])
                    ** 2
                    + (
                        _edge_plane_center(edge, operation.axis)[1]
                        - operation.center[1]
                    )
                    ** 2
                ),
            )
        ]
    if not edges:
        raise ValueError("No transverse edges matched the requested edge finish")
    selection = result.newObject(edges)
    if operation.mode == "fillet":
        return selection.fillet(operation.size)
    if operation.size2 is not None:
        return selection.chamfer(operation.size, operation.size2)
    return selection.chamfer(operation.size)


def normalize_plan(plan: ReconstructionPlan) -> ReconstructionPlan:
    """Upgrade a mutated legacy plan and keep the caller's object in sync."""

    normalized = ReconstructionPlan.model_validate(plan.model_dump(mode="python"))
    for field_name in type(plan).model_fields:
        setattr(plan, field_name, getattr(normalized, field_name))
    return plan


def _active_plan(plan: ReconstructionPlan) -> ReconstructionPlan:
    """Return the evaluable timeline while retaining suppressed nodes in JSON."""

    normalized = normalize_plan(plan)
    if normalized.base.suppressed:
        raise ValueError("The base feature cannot be suppressed")

    active = normalized.model_copy(deep=True)
    original_operations = list(active.operations)
    enabled_ids = {
        operation.feature_id
        for operation in original_operations
        if not operation.suppressed
    }
    old_to_new = {
        old_index: new_index
        for new_index, old_index in enumerate(
            index
            for index, operation in enumerate(original_operations)
            if not operation.suppressed
        )
    }
    id_to_new = {
        operation.feature_id: old_to_new[old_index]
        for old_index, operation in enumerate(original_operations)
        if old_index in old_to_new
    }
    active_operations = []
    for operation in original_operations:
        if operation.suppressed:
            continue
        if isinstance(operation, EdgeFinishFeature):
            if operation.target_feature_id == active.base.feature_id:
                operation.feature_index = -1
            elif operation.target_feature_id:
                if operation.target_feature_id not in enabled_ids:
                    continue
                operation.feature_index = id_to_new[operation.target_feature_id]
            elif operation.feature_index is not None:
                if operation.feature_index == -1:
                    pass
                elif operation.feature_index not in old_to_new:
                    continue
                else:
                    operation.feature_index = old_to_new[operation.feature_index]
        active_operations.append(operation)
    active.operations = active_operations
    return active


def build_plan(plan: ReconstructionPlan) -> cq.Workplane:
    plan = _active_plan(plan)
    result = _build_base(plan.base)
    feature_finishes: dict[int, list[EdgeFinishFeature]] = {}
    for operation in plan.operations:
        if isinstance(operation, EdgeFinishFeature) and operation.feature_index is not None:
            feature_finishes.setdefault(operation.feature_index, []).append(operation)
    for finish in feature_finishes.get(-1, []):
        result = _apply_edge_finish(result, finish)
    for operation_index, operation in enumerate(plan.operations):
        if isinstance(operation, RoundHoleFeature):
            result = result.cut(_hole_cutter(operation))
        elif isinstance(operation, ConicalHoleFeature):
            result = result.cut(_conical_hole_cutter(operation))
        elif isinstance(operation, ConicalAddFeature):
            tool = _conical_addition(operation)
            for finish in feature_finishes.get(operation_index, []):
                tool = _apply_edge_finish(tool, finish)
            result = result.union(tool)
        elif isinstance(operation, BooleanExtrudeFeature):
            tool = _boolean_extrusion(operation)
            for finish in feature_finishes.get(operation_index, []):
                tool = _apply_edge_finish(tool, finish)
            result = result.union(tool) if operation.mode == "add" else result.cut(tool)
        elif isinstance(operation, OrientedCylinderFeature):
            tool = _oriented_cylinder(operation)
            for finish in feature_finishes.get(operation_index, []):
                tool = _apply_edge_finish(tool, finish)
            result = result.union(tool) if operation.mode == "add" else result.cut(tool)
        elif isinstance(operation, SphereFeature):
            tool = _sphere(operation)
            result = result.union(tool) if operation.mode == "add" else result.cut(tool)
        elif isinstance(operation, OrientedBooleanExtrudeFeature):
            tool = _oriented_boolean_extrusion(operation)
            result = result.union(tool) if operation.mode == "add" else result.cut(tool)
        elif isinstance(operation, TaperedAddFeature):
            result = result.union(_tapered_addition(operation))
        elif isinstance(operation, EdgeFinishFeature) and operation.feature_index is None:
            result = _apply_edge_finish(result, operation)
    return result


def export_plan(
    plan: ReconstructionPlan,
    directory: Path,
    *,
    high_quality_stl: bool = True,
) -> cq.Workplane:
    directory.mkdir(parents=True, exist_ok=True)
    normalize_plan(plan)
    result = build_plan(plan)
    cq.exporters.export(result, str(directory / "reconstruction.step"))
    if high_quality_stl:
        cq.exporters.export(
            result,
            str(directory / "reconstruction.stl"),
            tolerance=0.01,
            angularTolerance=0.04,
        )
    else:
        cq.exporters.export(
            result,
            str(directory / "reconstruction.stl"),
            tolerance=0.02,
            angularTolerance=0.1,
        )
    (directory / "reconstruction.py").write_text(plan_to_source(plan), encoding="utf-8")
    (directory / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return result


def _profile_source(profile: Profile, plane_name: str, variable: str) -> list[str]:
    if isinstance(profile, CircleProfile):
        return [
            f"{variable} = (",
            f"    cq.Workplane({plane_name})",
            f"    .center({profile.center[0]!r}, {profile.center[1]!r})",
            f"    .circle({profile.radius!r})",
            ")",
        ]
    if isinstance(profile, PolygonProfile):
        lines = [f"{variable} = (", f"    cq.Workplane({plane_name})", "    .polyline(["]
        lines.extend(f"        {point!r}," for point in profile.points)
        lines.extend(["    ])", "    .close()", ")"])
        return lines
    if isinstance(profile, PathProfile):
        lines = [
            f"{variable} = (",
            f"    cq.Workplane({plane_name})",
            f"    .moveTo({profile.start[0]!r}, {profile.start[1]!r})",
        ]
        for segment in profile.segments:
            if isinstance(segment, LineSegment):
                lines.append(f"    .lineTo({segment.end[0]!r}, {segment.end[1]!r})")
            elif isinstance(segment, ArcSegment):
                lines.append(f"    .threePointArc({segment.mid!r}, {segment.end!r})")
            elif isinstance(segment, SplineSegment):
                spline_points = [*segment.points, segment.end]
                tangents = (
                    [segment.start_tangent, segment.end_tangent]
                    if segment.start_tangent is not None
                    and segment.end_tangent is not None
                    else None
                )
                lines.append(
                    f"    .spline({spline_points!r}, tangents={tangents!r}, "
                    "includeCurrent=True)"
                )
        lines.extend(["    .close()", ")"])
        return lines
    if isinstance(profile, SplineProfile):
        lines = [f"{variable} = (", f"    cq.Workplane({plane_name})", "    .spline(["]
        lines.extend(f"        {point!r}," for point in profile.points)
        lines.extend(["    ], periodic=True)", "    .close()", ")"])
        return lines
    raise TypeError(f"Unsupported profile: {type(profile).__name__}")


def _edge_finish_source(
    operation: EdgeFinishFeature,
    index: int,
    target: str,
) -> list[str]:
    extent_name = {
        Axis.X: "xlen",
        Axis.Y: "ylen",
        Axis.Z: "zlen",
    }[operation.axis]
    minimum_name, maximum_name = {
        Axis.X: ("xmin", "xmax"),
        Axis.Y: ("ymin", "ymax"),
        Axis.Z: ("zmin", "zmax"),
    }[operation.axis]
    lines = [
        "",
        f"finish_bounds_{index} = {target}.val().BoundingBox()",
        f"finish_tolerance_{index} = max((",
        f"    finish_bounds_{index}.xlen ** 2",
        f"    + finish_bounds_{index}.ylen ** 2",
        f"    + finish_bounds_{index}.zlen ** 2",
        ") ** 0.5 * 1e-6, 1e-6)",
        f"finish_edges_{index} = [",
        f"    edge for edge in {target}.val().Edges()",
        f"    if edge.BoundingBox().{extent_name} <= finish_tolerance_{index}",
    ]
    if operation.selector == "outer":
        lines = lines[:-3] + [
            f"finish_outer_edges_{index} = []",
            f"for finish_face_{index} in {target}.val().Faces():",
            f"    finish_face_bounds_{index} = finish_face_{index}.BoundingBox()",
            f"    if finish_face_{index}.geomType() != 'PLANE':",
            "        continue",
            f"    if finish_face_bounds_{index}.{extent_name} "
            f"> finish_tolerance_{index}:",
            "        continue",
        ]
        if operation.position is not None:
            lines.extend(
                [
                    f"    if abs((finish_face_bounds_{index}.{minimum_name} + "
                    f"finish_face_bounds_{index}.{maximum_name}) / 2 - "
                    f"{operation.position!r}) > max(",
                    f"        finish_tolerance_{index},",
                    f"        (finish_bounds_{index}.xlen ** 2",
                    f"         + finish_bounds_{index}.ylen ** 2",
                    f"         + finish_bounds_{index}.zlen ** 2) ** 0.5 * 1e-4,",
                    "    ):",
                    "        continue",
                ]
            )
        elif operation.end != "both":
            bound = minimum_name if operation.end == "start" else maximum_name
            lines.extend(
                [
                    f"    if abs((finish_face_bounds_{index}.{minimum_name} + "
                    f"finish_face_bounds_{index}.{maximum_name}) / 2 - "
                    f"finish_bounds_{index}.{bound}) > finish_tolerance_{index}:",
                    "        continue",
                ]
            )
        lines.extend(
            [
                f"    finish_outer_edges_{index}.extend("
                f"finish_face_{index}.outerWire().Edges())",
                f"finish_edges_{index} = [",
                f"    edge for edge in {target}.val().Edges()",
                f"    if edge.BoundingBox().{extent_name} <= finish_tolerance_{index}",
            ]
        )
    if operation.position is not None:
        lines.extend(
            [
                "    and abs((",
                f"        edge.BoundingBox().{minimum_name}",
                f"        + edge.BoundingBox().{maximum_name}",
                "    ) / 2",
                f"    - {operation.position!r}) <= max(",
                f"        finish_tolerance_{index},",
                f"        (finish_bounds_{index}.xlen ** 2",
                f"         + finish_bounds_{index}.ylen ** 2",
                f"         + finish_bounds_{index}.zlen ** 2) ** 0.5 * 1e-4,",
                "    )",
            ]
        )
    elif operation.end != "both":
        bound = minimum_name if operation.end == "start" else maximum_name
        lines.extend(
            [
                "    and abs((",
                f"        edge.BoundingBox().{minimum_name}",
                f"        + edge.BoundingBox().{maximum_name}",
                "    ) / 2",
                f"    - finish_bounds_{index}.{bound}) <= finish_tolerance_{index}",
            ]
        )
    if operation.selector == "circle":
        if operation.center is None or operation.radius is None:
            raise ValueError("Circular edge selector is incomplete")
        if operation.axis == Axis.X:
            center_expressions = (
                "edge._geomAdaptor().Circle().Location().Y()",
                "edge._geomAdaptor().Circle().Location().Z()",
            )
        elif operation.axis == Axis.Y:
            center_expressions = (
                "edge._geomAdaptor().Circle().Location().X()",
                "-edge._geomAdaptor().Circle().Location().Z()",
            )
        else:
            center_expressions = (
                "edge._geomAdaptor().Circle().Location().X()",
                "edge._geomAdaptor().Circle().Location().Y()",
            )
        radius_expression = "edge._geomAdaptor().Circle().Radius()"
        selector_tolerance = (
            f"max((finish_bounds_{index}.xlen ** 2 + "
            f"finish_bounds_{index}.ylen ** 2 + "
            f"finish_bounds_{index}.zlen ** 2) ** 0.5 * 1e-4, 1e-4)"
        )
        lines.extend(
            [
                "    and edge.geomType() == 'CIRCLE'",
                f"    and abs({center_expressions[0]} - "
                f"{operation.center[0]!r}) <= {selector_tolerance}",
                f"    and abs({center_expressions[1]} - "
                f"{operation.center[1]!r}) <= {selector_tolerance}",
                f"    and abs({radius_expression} - "
                f"{operation.radius!r}) <= {selector_tolerance}",
            ]
        )
    elif operation.selector == "outer":
        lines.append(
            f"    and any(edge.isSame(outer_edge) "
            f"for outer_edge in finish_outer_edges_{index})"
        )
    lines.append("]")
    if operation.selector == "nearest":
        if operation.center is None:
            raise ValueError("Nearest edge selector is incomplete")
        if operation.axis == Axis.X:
            center_expressions = ("edge.Center().y", "edge.Center().z")
        elif operation.axis == Axis.Y:
            center_expressions = ("edge.Center().x", "-edge.Center().z")
        else:
            center_expressions = ("edge.Center().x", "edge.Center().y")
        lines.extend(
            [
                f"finish_edges_{index} = [min(",
                f"    finish_edges_{index},",
                "    key=lambda edge: (",
                f"        ({center_expressions[0]} - {operation.center[0]!r}) ** 2",
                f"        + ({center_expressions[1]} - {operation.center[1]!r}) ** 2",
                "    ),",
                ")]",
            ]
        )
    method = "fillet" if operation.mode == "fillet" else "chamfer"
    arguments = (
        f"{operation.size!r}, {operation.size2!r}"
        if operation.size2 is not None
        else f"{operation.size!r}"
    )
    lines.append(
        f"{target} = {target}.newObject(finish_edges_{index})."
        f"{method}({arguments})"
    )
    return lines


def plan_to_source(plan: ReconstructionPlan) -> str:
    plan = _active_plan(plan)
    base = plan.base
    lines = [
        '"""Parametric reconstruction generated by MeshMind CAD."""',
        "import cadquery as cq",
        "",
    ]
    if isinstance(base, RevolveFeature):
        if base.axis == Axis.X:
            origin = f"cq.Vector(0, {base.center[0]!r}, {base.center[1]!r})"
            plane_expr = (
                f"cq.Plane(origin={origin}, xDir=(0, 1, 0), normal=(0, 0, -1))"
            )
            axis_end = f"{origin} + cq.Vector(1, 0, 0)"
        elif base.axis == Axis.Y:
            origin = f"cq.Vector({base.center[0]!r}, 0, {-base.center[1]!r})"
            plane_expr = (
                f"cq.Plane(origin={origin}, xDir=(1, 0, 0), normal=(0, 0, 1))"
            )
            axis_end = f"{origin} + cq.Vector(0, 1, 0)"
        else:
            origin = f"cq.Vector({base.center[0]!r}, {base.center[1]!r}, 0)"
            plane_expr = (
                f"cq.Plane(origin={origin}, xDir=(1, 0, 0), normal=(0, -1, 0))"
            )
            axis_end = f"{origin} + cq.Vector(0, 0, 1)"
        lines.extend(
            [
                f"plane = {plane_expr}",
                f"axis_start = {origin}",
                f"axis_end = {axis_end}",
            ]
        )
        lines.extend(_profile_source(base.profile, "plane", "profile"))
        lines.append(
            "result = cq.Workplane(obj=cq.Solid.revolve("
            "profile.val(), [], 360, axis_start, axis_end))"
        )
    elif isinstance(base, OrientedExtrudeFeature):
        plane_normal = base.plane_normal or base.direction
        plane_expr = (
            f"cq.Plane(origin={base.origin!r}, "
            f"xDir={base.x_direction!r}, normal={plane_normal!r})"
        )
        lines.append(f"plane = {plane_expr}")
    else:
        if base.axis == Axis.X:
            plane_expr = (
                f"cq.Plane(origin=({base.start!r}, 0, 0), "
                "xDir=(0, 1, 0), normal=(1, 0, 0))"
            )
        elif base.axis == Axis.Y:
            plane_expr = (
                f"cq.Plane(origin=(0, {base.start!r}, 0), "
                "xDir=(1, 0, 0), normal=(0, 1, 0))"
            )
        else:
            plane_expr = (
                f"cq.Plane(origin=(0, 0, {base.start!r}), "
                "xDir=(1, 0, 0), normal=(0, 0, 1))"
            )
        lines.append(f"plane = {plane_expr}")
    if isinstance(base, CylinderFeature):
        lines.extend(
            [
                "result = (",
                "    cq.Workplane(plane)",
                f"    .center({base.center[0]!r}, {base.center[1]!r})",
                f"    .circle({base.radius!r})",
                f"    .extrude({base.depth!r})",
                ")",
            ]
        )
        if base.inner_radius is not None:
            lines.extend(
                [
                    "cutter = (",
                    "    cq.Workplane(plane)",
                    f"    .center({base.center[0]!r}, {base.center[1]!r})",
                    f"    .circle({base.inner_radius!r})",
                    f"    .extrude({base.depth!r})",
                    ")",
                    "result = result.cut(cutter)",
                ]
            )
    elif isinstance(base, ExtrudeFeature):
        lines.extend(_profile_source(base.outer, "plane", "profile"))
        lines.append(f"result = profile.extrude({base.depth!r})")
        for index, hole in enumerate(base.holes):
            variable = f"hole_{index}"
            lines.extend(_profile_source(hole, "plane", variable))
            lines.append(f"result = result.cut({variable}.extrude({base.depth!r}))")
        for index, region in enumerate(base.additional_regions):
            variable = f"base_region_{index}"
            lines.extend(_profile_source(region, "plane", variable))
            lines.append(f"result = result.union({variable}.extrude({base.depth!r}))")
    elif isinstance(base, OrientedExtrudeFeature):
        lines.extend(_profile_source(base.outer, "plane", "profile"))
        hole_names: list[str] = []
        for index, hole in enumerate(base.holes):
            variable = f"hole_{index}"
            lines.extend(_profile_source(hole, "plane", variable))
            hole_names.append(f"{variable}.val()")
        vector = tuple(
            float(component) / sum(value * value for value in base.direction) ** 0.5
            * base.depth
            for component in base.direction
        )
        lines.extend(
            [
                "result = cq.Workplane(obj=cq.Solid.extrudeLinear(",
                "    profile.val(),",
                f"    [{', '.join(hole_names)}],",
                f"    cq.Vector{vector!r},",
                "))",
            ]
        )
        for index, region in enumerate(base.additional_regions):
            variable = f"base_region_{index}"
            lines.extend(_profile_source(region, "plane", variable))
            lines.extend(
                [
                    f"{variable}_solid = cq.Workplane(obj=cq.Solid.extrudeLinear(",
                    f"    {variable}.val(), [], cq.Vector{vector!r},",
                    "))",
                    f"result = result.union({variable}_solid)",
                ]
            )
    feature_finishes: dict[int, list[tuple[int, EdgeFinishFeature]]] = {}
    for finish_index, operation in enumerate(plan.operations):
        if isinstance(operation, EdgeFinishFeature) and operation.feature_index is not None:
            feature_finishes.setdefault(operation.feature_index, []).append(
                (finish_index, operation)
            )
    for finish_index, finish in feature_finishes.get(-1, []):
        lines.extend(_edge_finish_source(finish, finish_index, "result"))
    for index, operation in enumerate(plan.operations):
        if isinstance(operation, EdgeFinishFeature):
            if operation.feature_index is None:
                lines.extend(_edge_finish_source(operation, index, "result"))
            continue
        if isinstance(operation, OrientedCylinderFeature):
            lines.extend(
                [
                    "",
                    f"feature_{index} = cq.Workplane(obj=cq.Solid.makeCylinder(",
                    f"    {operation.radius!r},",
                    f"    {operation.depth!r},",
                    f"    cq.Vector{operation.origin!r},",
                    f"    cq.Vector{operation.direction!r}.normalized(),",
                    "))",
                ]
            )
            if operation.inner_radius is not None:
                lines.extend(
                    [
                        f"feature_{index}_inner = cq.Workplane(",
                        "    obj=cq.Solid.makeCylinder(",
                        f"        {operation.inner_radius!r},",
                        f"        {operation.depth!r},",
                        f"        cq.Vector{operation.origin!r},",
                        f"        cq.Vector{operation.direction!r}.normalized(),",
                        "    )",
                        ")",
                        f"feature_{index} = feature_{index}.cut("
                        f"feature_{index}_inner)",
                    ]
                )
            for finish_index, finish in feature_finishes.get(index, []):
                lines.extend(
                    _edge_finish_source(finish, finish_index, f"feature_{index}")
                )
            method = "union" if operation.mode == "add" else "cut"
            lines.append(f"result = result.{method}(feature_{index})")
            continue
        if isinstance(operation, SphereFeature):
            method = "union" if operation.mode == "add" else "cut"
            lines.extend(
                [
                    "",
                    f"feature_{index} = cq.Workplane(obj=cq.Solid.makeSphere(",
                    f"    {operation.radius!r},",
                    f"    cq.Vector{operation.center!r},",
                    "))",
                    f"result = result.{method}(feature_{index})",
                ]
            )
            continue
        if isinstance(operation, OrientedBooleanExtrudeFeature):
            plane_name = f"feature_plane_{index}"
            profile_name = f"feature_profile_{index}"
            plane_normal = operation.plane_normal or operation.direction
            direction_length = sum(
                value * value for value in operation.direction
            ) ** 0.5
            vector = tuple(
                float(component) / direction_length * operation.depth
                for component in operation.direction
            )
            lines.extend(
                [
                    "",
                    f"{plane_name} = cq.Plane(",
                    f"    origin={operation.origin!r},",
                    f"    xDir={operation.x_direction!r},",
                    f"    normal={plane_normal!r},",
                    ")",
                ]
            )
            lines.extend(
                _profile_source(operation.outer, plane_name, profile_name)
            )
            hole_names = []
            for hole_index, hole in enumerate(operation.holes):
                hole_name = f"feature_{index}_hole_{hole_index}"
                lines.extend(_profile_source(hole, plane_name, hole_name))
                hole_names.append(f"{hole_name}.val()")
            lines.extend(
                [
                    f"feature_{index} = cq.Workplane(obj=cq.Solid.extrudeLinear(",
                    f"    {profile_name}.val(),",
                    f"    [{', '.join(hole_names)}],",
                    f"    cq.Vector{vector!r},",
                    "))",
                ]
            )
            for region_index, region in enumerate(operation.additional_regions):
                region_name = f"feature_{index}_region_{region_index}"
                lines.extend(_profile_source(region, plane_name, region_name))
                lines.extend(
                    [
                        f"{region_name}_solid = cq.Workplane("
                        "obj=cq.Solid.extrudeLinear(",
                        f"    {region_name}.val(), [], cq.Vector{vector!r},",
                        "))",
                        f"feature_{index} = feature_{index}.union("
                        f"{region_name}_solid)",
                    ]
                )
            method = "union" if operation.mode == "add" else "cut"
            lines.append(f"result = result.{method}(feature_{index})")
            continue
        if operation.axis == Axis.X:
            operation_plane = (
                f"cq.Plane(origin=({operation.start!r}, 0, 0), xDir=(0, 1, 0), normal=(1, 0, 0))"
            )
        elif operation.axis == Axis.Y:
            operation_plane = (
                f"cq.Plane(origin=(0, {operation.start!r}, 0), xDir=(1, 0, 0), normal=(0, 1, 0))"
            )
        else:
            operation_plane = (
                f"cq.Plane(origin=(0, 0, {operation.start!r}), xDir=(1, 0, 0), normal=(0, 0, 1))"
            )
        if isinstance(operation, TaperedAddFeature):
            end_position = operation.start + operation.depth
            if operation.axis == Axis.X:
                end_plane = (
                    f"cq.Plane(origin=({end_position!r}, 0, 0), "
                    "xDir=(0, 1, 0), normal=(1, 0, 0))"
                )
            elif operation.axis == Axis.Y:
                end_plane = (
                    f"cq.Plane(origin=(0, {end_position!r}, 0), "
                    "xDir=(1, 0, 0), normal=(0, 1, 0))"
                )
            else:
                end_plane = (
                    f"cq.Plane(origin=(0, 0, {end_position!r}), "
                    "xDir=(1, 0, 0), normal=(0, 0, 1))"
                )
            start_plane_name = f"feature_start_plane_{index}"
            end_plane_name = f"feature_end_plane_{index}"
            start_profile_name = f"feature_start_profile_{index}"
            end_profile_name = f"feature_end_profile_{index}"
            lines.extend(
                [
                    "",
                    f"{start_plane_name} = {operation_plane}",
                    f"{end_plane_name} = {end_plane}",
                ]
            )
            lines.extend(
                _profile_source(
                    operation.start_outer,
                    start_plane_name,
                    start_profile_name,
                )
            )
            lines.extend(
                _profile_source(
                    operation.end_outer,
                    end_plane_name,
                    end_profile_name,
                )
            )
            intermediate_profile_names: list[str] = []
            for section_index, (offset, profile) in enumerate(
                zip(
                    operation.intermediate_offsets,
                    operation.intermediate_profiles,
                    strict=True,
                )
            ):
                position = operation.start + offset
                plane_name = (
                    f"feature_intermediate_plane_{index}_{section_index}"
                )
                profile_name = (
                    f"feature_intermediate_profile_{index}_{section_index}"
                )
                if operation.axis == Axis.X:
                    plane_source = (
                        f"cq.Plane(origin=({position!r}, 0, 0), "
                        "xDir=(0, 1, 0), normal=(1, 0, 0))"
                    )
                elif operation.axis == Axis.Y:
                    plane_source = (
                        f"cq.Plane(origin=(0, {position!r}, 0), "
                        "xDir=(1, 0, 0), normal=(0, 1, 0))"
                    )
                else:
                    plane_source = (
                        f"cq.Plane(origin=(0, 0, {position!r}), "
                        "xDir=(1, 0, 0), normal=(0, 0, 1))"
                    )
                lines.append(f"{plane_name} = {plane_source}")
                lines.extend(
                    _profile_source(profile, plane_name, profile_name)
                )
                intermediate_profile_names.append(profile_name)
            lines.extend(
                [
                    f"feature_{index} = cq.Workplane(obj=cq.Solid.makeLoft([",
                    f"    {start_profile_name}.val(),",
                    *(
                        f"    {profile_name}.val(),"
                        for profile_name in intermediate_profile_names
                    ),
                    f"    {end_profile_name}.val(),",
                    f"], ruled={not operation.smooth!r}))",
                ]
            )
            for hole_index, hole in enumerate(operation.holes):
                hole_name = f"feature_{index}_hole_{hole_index}"
                lines.extend(
                    _profile_source(hole, start_plane_name, hole_name)
                )
                lines.append(
                    f"feature_{index} = feature_{index}.cut("
                    f"{hole_name}.extrude({operation.depth!r}))"
                )
            lines.append(f"result = result.union(feature_{index})")
        elif isinstance(operation, ConicalAddFeature):
            if operation.axis == Axis.X:
                start_vector = (
                    f"cq.Vector({operation.start!r}, {operation.center[0]!r}, "
                    f"{operation.center[1]!r})"
                )
                direction_vector = "cq.Vector(1, 0, 0)"
            elif operation.axis == Axis.Y:
                start_vector = (
                    f"cq.Vector({operation.center[0]!r}, {operation.start!r}, "
                    f"{-operation.center[1]!r})"
                )
                direction_vector = "cq.Vector(0, 1, 0)"
            else:
                start_vector = (
                    f"cq.Vector({operation.center[0]!r}, {operation.center[1]!r}, "
                    f"{operation.start!r})"
                )
                direction_vector = "cq.Vector(0, 0, 1)"
            lines.extend(
                [
                    "",
                    f"feature_{index} = cq.Workplane(obj=cq.Solid.makeCone(",
                    f"    {operation.start_diameter / 2!r},",
                    f"    {operation.end_diameter / 2!r},",
                    f"    {operation.depth!r},",
                    f"    {start_vector},",
                    f"    {direction_vector},",
                    "))",
                ]
            )
            for hole_index, hole in enumerate(operation.holes):
                hole_name = f"feature_{index}_hole_{hole_index}"
                plane_name = f"feature_plane_{index}"
                lines.append(f"{plane_name} = {operation_plane}")
                lines.extend(_profile_source(hole, plane_name, hole_name))
                lines.append(
                    f"feature_{index} = feature_{index}.cut("
                    f"{hole_name}.extrude({operation.depth!r}))"
                )
            for finish_index, finish in feature_finishes.get(index, []):
                lines.extend(
                    _edge_finish_source(finish, finish_index, f"feature_{index}")
                )
            lines.append(f"result = result.union(feature_{index})")
        elif isinstance(operation, ConicalHoleFeature):
            if operation.axis == Axis.X:
                start_vector = (
                    f"cq.Vector({operation.start!r}, {operation.center[0]!r}, "
                    f"{operation.center[1]!r})"
                )
                direction_vector = "cq.Vector(1, 0, 0)"
            elif operation.axis == Axis.Y:
                start_vector = (
                    f"cq.Vector({operation.center[0]!r}, {operation.start!r}, "
                    f"{-operation.center[1]!r})"
                )
                direction_vector = "cq.Vector(0, 1, 0)"
            else:
                start_vector = (
                    f"cq.Vector({operation.center[0]!r}, {operation.center[1]!r}, "
                    f"{operation.start!r})"
                )
                direction_vector = "cq.Vector(0, 0, 1)"
            lines.extend(
                [
                    "",
                    f"cutter_{index} = cq.Workplane(obj=cq.Solid.makeCone(",
                    f"    {operation.start_diameter / 2!r},",
                    f"    {operation.end_diameter / 2!r},",
                    f"    {operation.depth!r},",
                    f"    {start_vector},",
                    f"    {direction_vector},",
                    "))",
                    f"result = result.cut(cutter_{index})",
                ]
            )
        elif isinstance(operation, RoundHoleFeature):
            lines.extend(
                [
                    "",
                    f"hole_plane_{index} = {operation_plane}",
                    f"cutter_{index} = (",
                    f"    cq.Workplane(hole_plane_{index})",
                    f"    .center({operation.center[0]!r}, {operation.center[1]!r})",
                    f"    .circle({operation.diameter / 2!r})",
                    f"    .extrude({operation.depth!r})",
                    ")",
                    f"result = result.cut(cutter_{index})",
                ]
            )
        elif isinstance(operation, BooleanExtrudeFeature):
            plane_name = f"feature_plane_{index}"
            profile_name = f"feature_profile_{index}"
            lines.extend(["", f"{plane_name} = {operation_plane}"])
            lines.extend(_profile_source(operation.outer, plane_name, profile_name))
            lines.append(f"feature_{index} = {profile_name}.extrude({operation.depth!r})")
            for hole_index, hole in enumerate(operation.holes):
                hole_name = f"feature_{index}_hole_{hole_index}"
                lines.extend(_profile_source(hole, plane_name, hole_name))
                lines.append(
                    f"feature_{index} = feature_{index}.cut("
                    f"{hole_name}.extrude({operation.depth!r}))"
                )
            for region_index, region in enumerate(operation.additional_regions):
                region_name = f"feature_{index}_region_{region_index}"
                lines.extend(_profile_source(region, plane_name, region_name))
                lines.append(
                    f"feature_{index} = feature_{index}.union("
                    f"{region_name}.extrude({operation.depth!r}))"
                )
            for finish_index, finish in feature_finishes.get(index, []):
                lines.extend(
                    _edge_finish_source(finish, finish_index, f"feature_{index}")
                )
            method = "union" if operation.mode == "add" else "cut"
            lines.append(f"result = result.{method}(feature_{index})")
    lines.extend(
        [
            "",
            "cq.exporters.export(result, 'reconstruction.step')",
            "",
        ]
    )
    return "\n".join(lines)

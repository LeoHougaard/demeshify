from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

import cadquery as cq
import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.multipolygon import MultiPolygon
from shapely.ops import unary_union

from .mesh import MeshData
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
from .surface_graph import cylinder_support_patches, detect_surface_graph

AXES: dict[Axis, tuple[np.ndarray, np.ndarray, np.ndarray, int]] = {
    Axis.X: (
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, 0.0, 1.0]),
        0,
    ),
    Axis.Y: (
        np.array([0.0, 1.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 0.0, -1.0]),
        1,
    ),
    Axis.Z: (
        np.array([0.0, 0.0, 1.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        2,
    ),
}


@dataclass(slots=True)
class SectionShape:
    outer: Profile
    holes: list[Profile]
    additional_regions: list[SectionRegion]
    area: float
    polygon: Polygon


@dataclass(slots=True)
class SectionRegion:
    outer: Profile
    holes: list[Profile]
    polygon: Polygon


@dataclass(slots=True)
class PlanCandidate:
    plan: ReconstructionPlan
    heuristic_score: float
    section_consistency: float


def _clean(value: float) -> float:
    value = float(value)
    hundredth = round(value, 2)
    rounded = hundredth if abs(value - hundredth) <= 0.0015 else round(value, 3)
    return 0.0 if rounded == -0.0 else rounded


def _project(points: np.ndarray, axis: Axis) -> np.ndarray:
    _, u_axis, v_axis, _ = AXES[axis]
    return np.column_stack((points @ u_axis, points @ v_axis))


def _fit_circle(points: np.ndarray, tolerance: float) -> CircleProfile | None:
    if len(points) < 8:
        return None
    points = points[:-1] if np.linalg.norm(points[0] - points[-1]) < tolerance else points
    fitted = _circle_values(points)
    if fitted is None:
        return None
    directions = np.diff(np.vstack((points, points[0])), axis=0)
    directions /= np.maximum(
        np.linalg.norm(directions, axis=1, keepdims=True),
        1e-12,
    )
    local_turns = np.arccos(
        np.clip(
            np.sum(directions * np.roll(directions, 1, axis=0), axis=1),
            -1,
            1,
        )
    )
    # The four corners of a rectangle are co-circular.  Radial residual alone
    # therefore cannot distinguish a square from a tessellated circle.
    if len(local_turns) and float(np.max(local_turns)) > np.deg2rad(40):
        return None
    center_x, center_y, radius, radial_error = fitted
    if float(np.sqrt(np.mean(radial_error**2))) > tolerance:
        return None
    angles = np.unwrap(np.arctan2(points[:, 1] - center_y, points[:, 0] - center_x))
    if float(np.ptp(angles)) < np.pi * 1.75:
        return None
    return CircleProfile(
        center=(_clean(center_x), _clean(center_y)),
        radius=_clean(radius),
    )


def _circle_values(
    points: np.ndarray,
) -> tuple[float, float, float, np.ndarray] | None:
    x = points[:, 0]
    y = points[:, 1]
    matrix = np.column_stack((2 * x, 2 * y, np.ones_like(x)))
    rhs = x * x + y * y
    center_x, center_y, constant = np.linalg.lstsq(matrix, rhs, rcond=None)[0]
    radius_sq = constant + center_x * center_x + center_y * center_y
    if radius_sq <= 0:
        return None
    radius = float(np.sqrt(radius_sq))
    radial_error = np.abs(np.hypot(x - center_x, y - center_y) - radius)
    return float(center_x), float(center_y), radius, radial_error


def _line_error(points: np.ndarray) -> float:
    direction = points[-1] - points[0]
    length = float(np.linalg.norm(direction))
    if length <= 1e-9:
        return float("inf")
    relative = points - points[0]
    distances = np.abs(relative[:, 0] * direction[1] - relative[:, 1] * direction[0])
    return float(np.max(distances) / length)


def _arc_segment(points: np.ndarray, tolerance: float) -> ArcSegment | None:
    if len(points) < 4:
        return None
    chord_lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    four_point_chord_ratio: float | None = None
    if len(points) == 4:
        four_point_chord_ratio = float(
            np.max(chord_lengths) / max(np.min(chord_lengths), 1e-12)
        )
    if _line_error(points) <= tolerance * 1.5:
        return None
    edge_directions = np.diff(points, axis=0)
    edge_directions /= np.maximum(
        np.linalg.norm(edge_directions, axis=1, keepdims=True),
        1e-12,
    )
    local_turns = np.arccos(
        np.clip(np.sum(edge_directions[:-1] * edge_directions[1:], axis=1), -1, 1)
    )
    # Four corners of a rectangle are also co-circular. An arc must turn smoothly;
    # a sharp vertex inside the candidate span means this is a chain of lines.
    if len(local_turns) and float(np.max(local_turns)) > np.deg2rad(40):
        return None
    if len(local_turns) >= 4:
        turning_fraction = float(np.mean(local_turns > np.deg2rad(0.2)))
        if turning_fraction < 0.45:
            return None
    midpoint_sample = points[len(points) // 2]
    chord = points[-1] - points[0]
    chord_length = float(np.linalg.norm(chord))
    if chord_length <= tolerance:
        return None
    sagitta = abs(
        (midpoint_sample[0] - points[0, 0]) * chord[1]
        - (midpoint_sample[1] - points[0, 1]) * chord[0]
    ) / chord_length
    if (
        four_point_chord_ratio is not None
        and four_point_chord_ratio > 1.3
        and sagitta / chord_length < 0.18
    ):
        # Three straight sketch edges can form four co-circular corners. The
        # ambiguous line chain has both unequal chords and a shallow sagitta;
        # real compact rounds can have either property individually.
        return None
    shallow_arc = sagitta / chord_length < 0.02
    median_turn = float(np.median(local_turns)) if len(local_turns) else 0.0
    resolvable_shallow_arc = (
        shallow_arc
        and len(local_turns) >= 2
        and float(np.max(chord_lengths))
        / max(float(np.min(chord_lengths)), 1e-12)
        <= 1.2
        and median_turn >= math.radians(0.2)
        and float(np.max(np.abs(local_turns - median_turn)))
        <= max(math.radians(0.1), median_turn * 0.2)
        and sagitta >= tolerance * 1.5
    )
    if shallow_arc and not resolvable_shallow_arc:
        return None
    # CadQuery's threePointArc constructs the circle through exactly these
    # three points. Validate that circle, not a least-squares circle which can
    # differ wildly for an almost-collinear span.
    fitted = _circle_values(np.vstack((points[0], midpoint_sample, points[-1])))
    if fitted is None:
        return None
    center_x, center_y, radius, radial_error = fitted
    all_radial_error = np.abs(
        np.hypot(points[:, 0] - center_x, points[:, 1] - center_y) - radius
    )
    if (
        float(np.max(radial_error)) > tolerance
        or float(np.max(all_radial_error)) > tolerance
        or radius <= tolerance * 2
    ):
        return None
    angles = np.unwrap(np.arctan2(points[:, 1] - center_y, points[:, 0] - center_x))
    differences = np.diff(angles)
    significant = differences[np.abs(differences) > 1e-5]
    if len(significant) < 2:
        return None
    direction = np.sign(float(np.median(significant)))
    if float(np.mean(np.sign(significant) == direction)) < 0.9:
        return None
    span = abs(float(angles[-1] - angles[0]))
    minimum_span = math.radians(1.0 if resolvable_shallow_arc else 12.0)
    if span < minimum_span or span > np.pi * 1.95:
        return None
    midpoint = (_clean(midpoint_sample[0]), _clean(midpoint_sample[1]))
    return ArcSegment(
        mid=midpoint,
        end=(_clean(points[-1, 0]), _clean(points[-1, 1])),
    )


def _spline_segment(points: np.ndarray, tolerance: float) -> SplineSegment | None:
    """Fit one smooth, non-circular open curve through a contour span.

    This deliberately runs after line/arc fitting.  It preserves changing
    curvature (for example a tapering tangent transition) without allowing a
    spline to hide a real sketch corner or replace a dimensionable circle.
    """

    if len(points) < 5 or _line_error(points) <= tolerance * 1.5:
        return None
    edge_vectors = np.diff(points, axis=0)
    edge_lengths = np.linalg.norm(edge_vectors, axis=1)
    if float(np.min(edge_lengths)) <= 1e-12:
        return None
    median_edge = float(np.median(edge_lengths))
    if (
        edge_lengths[0] > median_edge * 4.0
        or edge_lengths[-1] > median_edge * 4.0
        or float(np.max(edge_lengths)) > median_edge * 7.0
    ):
        # Do not absorb a dimensionable circular round plus its long tangent
        # line into one generic spline.  STL curves normally contribute a
        # reasonably even chord chain; a single much longer endpoint chord is
        # the neighboring straight sketch edge.
        return None
    directions = edge_vectors / edge_lengths[:, None]
    turns = np.arccos(
        np.clip(np.sum(directions[:-1] * directions[1:], axis=1), -1, 1)
    )
    if not len(turns) or float(np.max(turns)) > math.radians(24):
        return None
    if float(np.sum(turns)) < math.radians(3):
        return None

    # A true circle/arc is more editable as a radius-driven ArcSegment.
    fitted_circle = _circle_values(points)
    if fitted_circle is not None:
        radius = fitted_circle[2]
        radial_error = fitted_circle[3]
        if float(np.max(radial_error)) <= tolerance * 1.25 and radius > tolerance * 2:
            return None

    reduced = np.asarray(
        LineString(points).simplify(tolerance * 0.55, preserve_topology=False).coords,
        dtype=float,
    )
    if len(reduced) < 4:
        middle = points[len(points) // 2]
        reduced = np.vstack((points[0], middle, points[-1]))
    if len(reduced) > 18:
        indices = np.linspace(0, len(reduced) - 1, 18, dtype=int)
        reduced = reduced[indices]
    if len(reduced) < 3:
        return None

    start_tangent = directions[0]
    end_tangent = directions[-1]
    interpolation = reduced[1:-1]
    if len(interpolation) == 0:
        return None
    return SplineSegment(
        points=[(_clean(point[0]), _clean(point[1])) for point in interpolation],
        end=(_clean(reduced[-1, 0]), _clean(reduced[-1, 1])),
        start_tangent=(
            _clean(start_tangent[0]),
            _clean(start_tangent[1]),
        ),
        end_tangent=(
            _clean(end_tangent[0]),
            _clean(end_tangent[1]),
        ),
    )


def _long_path_segments(
    ordered: np.ndarray,
    tolerance: float,
) -> list[LineSegment | ArcSegment | SplineSegment] | None:
    """Fit large tessellated contours without the cubic all-spans search."""
    count = len(ordered)
    segments: list[LineSegment | ArcSegment | SplineSegment] = []
    cursor = 0
    maximum_span = 400
    while cursor < count - 1:
        stop = min(count, cursor + maximum_span + 1)
        line_end = cursor + 1
        for end in range(cursor + 2, stop):
            if _line_error(ordered[cursor : end + 1]) > tolerance:
                break
            line_end = end

        best_curve: tuple[int, ArcSegment | SplineSegment] | None = None
        failures_after_fit = 0
        for end in range(cursor + 3, stop):
            arc = _arc_segment(ordered[cursor : end + 1], tolerance)
            spline = (
                None
                if arc is not None
                else _spline_segment(ordered[cursor : end + 1], tolerance)
            )
            curve = arc or spline
            if curve is not None:
                best_curve = (end, curve)
                failures_after_fit = 0
            elif best_curve is not None:
                failures_after_fit += 1
                if failures_after_fit >= 3:
                    break

        if best_curve is not None and best_curve[0] >= line_end + 2:
            cursor, segment = best_curve
        else:
            cursor = line_end
            endpoint = ordered[cursor]
            segment = LineSegment(end=(_clean(endpoint[0]), _clean(endpoint[1])))
        segments.append(segment)
        if len(segments) > 256:
            return None
    return segments


def _path_profile_is_valid(profile: PathProfile) -> bool:
    try:
        path = cq.Workplane("XY").moveTo(*profile.start)
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
        return bool(path.close().extrude(1).val().isValid())
    except Exception:
        return False


def _snap_arc_line_tangencies(
    profile: PathProfile,
    tolerance: float,
) -> PathProfile:
    """Restore tangent points removed by polygon simplification.

    A circle-to-line tangent is deliberately collinear with the following
    line, so GEOS may remove that exact endpoint while simplifying a dense
    tessellated contour.  The remaining arc then stops one mesh chord early.
    Recover only near-horizontal/vertical tangent lines and bound the move to
    a few fitting tolerances so unrelated sketch geometry is unchanged.
    """

    segments = list(profile.segments)
    current = np.asarray(profile.start, dtype=float)
    for index, segment in enumerate(segments[:-1]):
        if not isinstance(segment, ArcSegment):
            current = np.asarray(segment.end, dtype=float)
            continue
        following = segments[index + 1]
        if not isinstance(following, LineSegment):
            current = np.asarray(segment.end, dtype=float)
            continue
        arc_end = np.asarray(segment.end, dtype=float)
        line_end = np.asarray(following.end, dtype=float)
        line_delta = line_end - arc_end
        fitted = _circle_values(
            np.asarray((current, segment.mid, segment.end), dtype=float)
        )
        if fitted is None:
            current = arc_end
            continue
        center_x, center_y, radius, _ = fitted
        candidates: list[np.ndarray] = []
        if (
            abs(line_delta[0]) <= tolerance * 2
            and abs(line_delta[1]) >= tolerance * 8
        ):
            if abs(abs(line_end[0] - center_x) - radius) <= tolerance * 2:
                candidates.append(np.asarray((line_end[0], center_y)))
        elif (
            abs(line_delta[1]) <= tolerance * 2
            and abs(line_delta[0]) >= tolerance * 8
        ):
            if abs(abs(line_end[1] - center_y) - radius) <= tolerance * 2:
                candidates.append(np.asarray((center_x, line_end[1])))
        if candidates:
            tangent = min(candidates, key=lambda point: np.linalg.norm(point - arc_end))
            move = float(np.linalg.norm(tangent - arc_end))
            remaining = float(np.linalg.norm(line_end - tangent))
            if move <= tolerance * 6 and remaining > tolerance * 4:
                segments[index] = ArcSegment(
                    mid=segment.mid,
                    end=(_clean(tangent[0]), _clean(tangent[1])),
                )
        current = np.asarray(segments[index].end, dtype=float)
    return PathProfile(start=profile.start, segments=segments)


def _snap_spline_line_tangencies(profile: PathProfile) -> PathProfile:
    """Make an inferred spline exactly tangent to adjacent sketch lines."""

    segments = list(profile.segments)
    starts = [np.asarray(profile.start, dtype=float)]
    starts.extend(np.asarray(segment.end, dtype=float) for segment in segments[:-1])
    cosine_limit = math.cos(math.radians(12))
    for index, segment in enumerate(segments):
        if not isinstance(segment, SplineSegment):
            continue
        start_tangent = segment.start_tangent
        end_tangent = segment.end_tangent
        previous_index = (index - 1) % len(segments)
        previous = segments[previous_index]
        if isinstance(previous, LineSegment) and start_tangent is not None:
            line = starts[index] - starts[previous_index]
            length = float(np.linalg.norm(line))
            tangent = np.asarray(start_tangent, dtype=float)
            if length > 1e-12 and float(np.dot(line / length, tangent)) > cosine_limit:
                line /= length
                start_tangent = (_clean(line[0]), _clean(line[1]))
        following = segments[(index + 1) % len(segments)]
        if isinstance(following, LineSegment) and end_tangent is not None:
            line = np.asarray(following.end, dtype=float) - np.asarray(
                segment.end, dtype=float
            )
            length = float(np.linalg.norm(line))
            tangent = np.asarray(end_tangent, dtype=float)
            if length > 1e-12 and float(np.dot(line / length, tangent)) > cosine_limit:
                line /= length
                end_tangent = (_clean(line[0]), _clean(line[1]))
        segments[index] = segment.model_copy(
            update={
                "start_tangent": start_tangent,
                "end_tangent": end_tangent,
            }
        )
    return PathProfile(start=profile.start, segments=segments)


def _fit_path_profile(points: np.ndarray, tolerance: float) -> PathProfile | None:
    if len(points) < 6:
        return None
    if np.linalg.norm(points[0] - points[-1]) <= tolerance:
        points = points[:-1]
    polygon = Polygon(points)
    reduced = np.asarray(
        polygon.simplify(tolerance * 0.15, preserve_topology=True).exterior.coords[:-1]
    )
    if len(reduced) < 5:
        return None

    previous = np.roll(reduced, 1, axis=0)
    following = np.roll(reduced, -1, axis=0)
    incoming = reduced - previous
    outgoing = following - reduced
    incoming /= np.maximum(np.linalg.norm(incoming, axis=1, keepdims=True), 1e-12)
    outgoing /= np.maximum(np.linalg.norm(outgoing, axis=1, keepdims=True), 1e-12)
    turns = np.arccos(np.clip(np.sum(incoming * outgoing, axis=1), -1, 1))
    start_index = int(np.argmax(turns)) if float(np.max(turns)) > 0.35 else int(np.argmin(turns))
    ordered = np.vstack((reduced[start_index:], reduced[:start_index], reduced[start_index]))
    count = len(ordered)
    if count > 240:
        segments = _long_path_segments(ordered, tolerance)
        if segments is None or not any(
            isinstance(segment, (ArcSegment, SplineSegment)) for segment in segments
        ):
            return None
        profile = PathProfile(
            start=(_clean(ordered[0, 0]), _clean(ordered[0, 1])),
            segments=segments,
        )
        profile = _snap_arc_line_tangencies(profile, tolerance)
        profile = _snap_spline_line_tangencies(profile)
        return profile if _path_profile_is_valid(profile) else None

    costs = [float("inf")] * count
    choices: list[
        tuple[int, LineSegment | ArcSegment | SplineSegment] | None
    ] = [None] * count
    costs[-1] = 0.0

    for start in range(count - 2, -1, -1):
        for end in range(start + 1, count):
            sample = ordered[start : end + 1]
            if _line_error(sample) <= tolerance:
                segment = LineSegment(end=(_clean(sample[-1, 0]), _clean(sample[-1, 1])))
                cost = 1.0 + costs[end]
                if cost < costs[start]:
                    costs[start] = cost
                    choices[start] = (end, segment)
            arc = _arc_segment(sample, tolerance)
            if arc is not None:
                arc_cost = 1.05
                next_choice = choices[end]
                if next_choice is not None and isinstance(
                    next_choice[1], LineSegment
                ):
                    # A shallow tessellated fillet can fit inside the line
                    # tolerance right before a much longer tangent edge.  In
                    # that narrowly identified case, prefer the validated arc
                    # over one short chord plus the neighboring line.  Keeping
                    # the normal arc cost elsewhere prevents several unrelated
                    # rounds/chamfers from being merged into a false curve.
                    next_end = next_choice[0]
                    arc_chord = float(np.linalg.norm(sample[-1] - sample[0]))
                    next_vector = ordered[next_end] - ordered[end]
                    next_length = float(np.linalg.norm(next_vector))
                    final_chord = sample[-1] - sample[-2]
                    final_length = float(np.linalg.norm(final_chord))
                    tangent_alignment = (
                        float(np.dot(final_chord, next_vector))
                        / max(final_length * next_length, 1e-12)
                    )
                    if (
                        next_length > arc_chord * 3.0
                        and tangent_alignment > math.cos(math.radians(12))
                    ):
                        arc_cost = 0.95
                cost = arc_cost + costs[end]
                existing = choices[start]
                if cost < costs[start] or (
                    math.isclose(cost, costs[start], abs_tol=1e-12)
                    and existing is not None
                    and isinstance(existing[1], ArcSegment)
                    and end > existing[0]
                ):
                    # Equal-complexity fits should retain the longest validated
                    # circular span. Keeping the first (shortest) span can leave
                    # the tangent tail as a slightly sloped line, turning a true
                    # cylindrical face into a cone in the exported STEP model.
                    costs[start] = cost
                    choices[start] = (end, arc)
            if arc is None:
                spline = _spline_segment(sample, tolerance)
                if spline is not None:
                    # Slightly more expensive than a radius-driven arc, but far
                    # cheaper than replacing a genuine smooth transition with
                    # a chain of short polygon edges.
                    cost = 1.2 + costs[end]
                    if cost < costs[start]:
                        costs[start] = cost
                        choices[start] = (end, spline)

    segments: list[LineSegment | ArcSegment | SplineSegment] = []
    cursor = 0
    while cursor < count - 1 and choices[cursor] is not None:
        cursor, segment = choices[cursor]
        segments.append(segment)
    if cursor != count - 1 or len(segments) < 2:
        return None
    if not any(
        isinstance(segment, (ArcSegment, SplineSegment)) for segment in segments
    ):
        return None
    profile = PathProfile(
        start=(_clean(ordered[0, 0]), _clean(ordered[0, 1])),
        segments=segments,
    )
    profile = _snap_arc_line_tangencies(profile, tolerance)
    profile = _snap_spline_line_tangencies(profile)
    return profile if _path_profile_is_valid(profile) else None


def _fit_spline_profile(points: np.ndarray, tolerance: float) -> SplineProfile | None:
    if np.linalg.norm(points[0] - points[-1]) <= tolerance:
        points = points[:-1]
    if len(points) < 8:
        return None
    previous = np.roll(points, 1, axis=0)
    following = np.roll(points, -1, axis=0)
    incoming = points - previous
    outgoing = following - points
    incoming /= np.maximum(np.linalg.norm(incoming, axis=1, keepdims=True), 1e-12)
    outgoing /= np.maximum(np.linalg.norm(outgoing, axis=1, keepdims=True), 1e-12)
    turns = np.arccos(np.clip(np.sum(incoming * outgoing, axis=1), -1, 1))
    # Preserve real corners as line/arc paths or polygons. This fallback is for
    # contours whose mesh chords consistently describe a smooth unknown curve.
    if float(np.max(turns)) > np.deg2rad(28):
        return None

    polygon = Polygon(points)
    reduced = np.asarray(
        polygon.simplify(tolerance * 0.45, preserve_topology=True).exterior.coords[:-1]
    )
    if len(reduced) > 128:
        indices = np.linspace(0, len(reduced) - 1, 128, dtype=int)
        reduced = reduced[indices]
    if len(reduced) < 4:
        return None
    return SplineProfile(
        points=[(_clean(point[0]), _clean(point[1])) for point in reduced],
    )


def _profile_from_ring(points: np.ndarray, tolerance: float) -> tuple[Profile, Polygon] | None:
    if len(points) < 4:
        return None
    if np.linalg.norm(points[0] - points[-1]) > tolerance * 3:
        points = np.vstack((points, points[0]))
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if isinstance(polygon, MultiPolygon):
        polygon = max(polygon.geoms, key=lambda item: item.area)
    if polygon.is_empty or polygon.area <= tolerance * tolerance:
        return None
    # A circular stock edge with a small terminal fillet/chamfer is no longer
    # a mathematically perfect circle in a section through that finish. Allow
    # a modest circle-only residual margin so the stock stays an analytic
    # cylinder; subsequent edge-finish recovery encodes the measured deviation.
    circle = _fit_circle(np.asarray(polygon.exterior.coords), tolerance * 1.5)
    if circle is not None:
        return circle, polygon
    path = _fit_path_profile(np.asarray(polygon.exterior.coords), tolerance)
    if path is not None:
        return path, polygon
    spline = _fit_spline_profile(np.asarray(polygon.exterior.coords), tolerance)
    if spline is not None:
        return spline, polygon
    simplified = polygon.simplify(tolerance, preserve_topology=True)
    coords = list(simplified.exterior.coords)[:-1]
    if len(coords) < 3:
        return None
    profile = PolygonProfile(points=[(_clean(x), _clean(y)) for x, y in coords])
    return profile, polygon


def section_shape(data: MeshData, axis: Axis, location: float) -> SectionShape | None:
    cache_key = (axis.value, round(float(location), 9))
    if cache_key in data.section_cache:
        cached = data.section_cache[cache_key]
        return cached if isinstance(cached, SectionShape) else None

    normal, _, _, index = AXES[axis]
    origin = np.zeros(3)
    origin[index] = location
    section = data.mesh.section(plane_origin=origin, plane_normal=normal)
    if section is None:
        data.section_cache[cache_key] = None
        return None

    # Tie the fitting tolerance to the sketch plane as well as the model
    # diagonal.  A long, thin extrusion can be tens of millimetres long while
    # its entire sketch is sub-millimetre; a diagonal-only tolerance erases
    # small but real line/arc features and forces a many-slab fallback.
    transverse_extents = np.delete(np.asarray(data.mesh.extents), index)
    positive_extents = transverse_extents[transverse_extents > 1e-9]
    profile_scale = (
        float(np.min(positive_extents))
        if len(positive_extents)
        else data.diagonal
    )
    tolerance = max(
        min(data.diagonal * 0.0008, profile_scale * 0.002),
        0.001,
    )
    rings: list[tuple[Profile, Polygon]] = []
    for discrete in section.discrete:
        projected = _project(np.asarray(discrete), axis)
        fitted = _profile_from_ring(projected, tolerance)
        if fitted is not None:
            rings.append(fitted)
    if not rings:
        data.section_cache[cache_key] = None
        return None

    rings.sort(key=lambda item: item[1].area, reverse=True)
    parents: list[int | None] = [None] * len(rings)
    depths = [0] * len(rings)
    for index, (_, polygon) in enumerate(rings):
        marker = polygon.representative_point()
        containing = [
            parent_index
            for parent_index, (_, candidate) in enumerate(rings[:index])
            if candidate.contains(marker)
        ]
        if containing:
            parent = min(containing, key=lambda item: rings[item][1].area)
            parents[index] = parent
            depths[index] = depths[parent] + 1

    regions: list[SectionRegion] = []
    for index, (profile, polygon) in enumerate(rings):
        if depths[index] % 2:
            continue
        direct_holes = [
            (rings[child][0], rings[child][1])
            for child in range(len(rings))
            if parents[child] == index and depths[child] % 2
        ]
        net_region = polygon
        for _, hole_polygon in direct_holes:
            net_region = net_region.difference(hole_polygon)
        regions.append(
            SectionRegion(
                outer=profile,
                holes=[hole_profile for hole_profile, _ in direct_holes],
                polygon=net_region,
            )
        )
    if not regions:
        data.section_cache[cache_key] = None
        return None
    regions.sort(key=lambda region: region.polygon.area, reverse=True)
    primary = regions[0]
    net_polygon = primary.polygon
    for region in regions[1:]:
        net_polygon = net_polygon.union(region.polygon)
    result = SectionShape(
        outer=primary.outer,
        holes=primary.holes,
        additional_regions=regions[1:],
        area=float(net_polygon.area),
        polygon=net_polygon,
    )
    data.section_cache[cache_key] = result
    return result


def _polygon_similarity(first: Polygon, second: Polygon) -> float:
    union = first.union(second).area
    if union <= 0:
        return 0.0
    return float(first.intersection(second).area / union)


def generate_prismatic_candidates(data: MeshData, name: str) -> list[PlanCandidate]:
    bounds = data.mesh.bounds
    candidates: list[PlanCandidate] = []
    target_volume = data.report.volume_mm3

    for axis, (_, _, _, index) in AXES.items():
        start = _clean(bounds[0, index])
        depth = _clean(bounds[1, index] - bounds[0, index])
        if depth <= 0:
            continue
        positions = [start + depth * fraction for fraction in (0.2, 0.5, 0.8)]
        sections = [section_shape(data, axis, position) for position in positions]
        if any(section is None for section in sections):
            continue
        low, middle, high = sections
        assert low is not None and middle is not None and high is not None

        similarity = min(
            _polygon_similarity(low.polygon, middle.polygon),
            _polygon_similarity(middle.polygon, high.polygon),
        )
        estimated_volume = middle.area * depth
        volume_error = (
            abs(estimated_volume - target_volume) / max(target_volume, 1e-9)
            if target_volume is not None
            else 0.25
        )
        complexity = (
            len(middle.holes)
            + len(middle.additional_regions)
            + sum(len(region.holes) for region in middle.additional_regions)
        ) * 0.005
        heuristic = (1.0 - similarity) + volume_error * 0.45 + complexity

        if (
            isinstance(middle.outer, CircleProfile)
            and len(middle.holes) <= 1
            and (
                not middle.holes
                or (
                    isinstance(middle.holes[0], CircleProfile)
                    and np.linalg.norm(
                        np.asarray(middle.outer.center) - np.asarray(middle.holes[0].center)
                    )
                    <= max(data.diagonal * 0.001, 0.02)
                )
            )
        ):
            inner = middle.holes[0].radius if middle.holes else None
            base = CylinderFeature(
                axis=axis,
                start=start,
                depth=depth,
                center=middle.outer.center,
                radius=middle.outer.radius,
                inner_radius=inner,
            )
        else:
            base = ExtrudeFeature(
                axis=axis,
                start=start,
                depth=depth,
                outer=middle.outer,
                holes=middle.holes,
            )

        plan = ReconstructionPlan(
            name=name,
            base=base,
            operations=[
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=start,
                    depth=depth,
                    outer=region.outer,
                    holes=region.holes,
                )
                for region in middle.additional_regions
            ],
            assumptions=[
                f"Primary construction inferred as a constant {axis.value}-axis extrusion.",
                "Curved closed contours were converted to analytic circles when residuals allowed.",
            ],
        )
        candidates.append(
            PlanCandidate(
                plan=plan,
                heuristic_score=heuristic,
                section_consistency=similarity,
            )
        )

    return sorted(candidates, key=lambda candidate: candidate.heuristic_score)


def _axis_levels(data: MeshData, axis: Axis) -> list[float]:
    _, _, _, index = AXES[axis]
    bounds = data.mesh.bounds[:, index]
    tolerance = max(data.diagonal * 0.00015, 0.003)
    aligned = np.abs(data.mesh.face_normals[:, index]) >= 0.985
    coordinates = data.mesh.triangles_center[aligned, index]
    areas = data.mesh.area_faces[aligned]
    samples = sorted(zip(coordinates, areas, strict=True), key=lambda item: item[0])
    clusters: list[list[tuple[float, float]]] = []
    for coordinate, area in samples:
        if not clusters or abs(coordinate - clusters[-1][-1][0]) > tolerance:
            clusters.append([])
        clusters[-1].append((float(coordinate), float(area)))

    levels = [float(bounds[0]), float(bounds[1])]
    # A cylindrical tessellation contains small bands whose facet normals happen to
    # align with a cardinal axis. Treating every one as a sketch plane creates dozens
    # of false layers. Real extrusion shoulders contribute a coherent planar patch.
    minimum_area = max(float(data.mesh.area) * 0.005, tolerance * tolerance)
    for cluster in clusters:
        total_area = sum(area for _, area in cluster)
        if total_area < minimum_area:
            continue
        coordinate = sum(value * area for value, area in cluster) / total_area
        if bounds[0] + tolerance < coordinate < bounds[1] - tolerance:
            levels.append(coordinate)
    levels.sort()
    merged: list[float] = []
    for level in levels:
        if not merged or abs(level - merged[-1]) > tolerance:
            merged.append(_clean(level))
    return merged


def _base_from_section(
    section: SectionShape,
    axis: Axis,
    start: float,
    depth: float,
) -> ExtrudeFeature | CylinderFeature:
    if (
        isinstance(section.outer, CircleProfile)
        and len(section.holes) <= 1
        and (
            not section.holes
            or (
                isinstance(section.holes[0], CircleProfile)
                and np.linalg.norm(
                    np.asarray(section.outer.center) - np.asarray(section.holes[0].center)
                )
                <= 0.02
            )
        )
    ):
        return CylinderFeature(
            axis=axis,
            start=start,
            depth=depth,
            center=section.outer.center,
            radius=section.outer.radius,
            inner_radius=section.holes[0].radius if section.holes else None,
        )
    return ExtrudeFeature(
        axis=axis,
        start=start,
        depth=depth,
        outer=section.outer,
        holes=section.holes,
    )


def generate_layered_candidates(data: MeshData, name: str) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    target_volume = data.report.volume_mm3
    for axis in AXES:
        levels = _axis_levels(data, axis)
        if len(levels) < 3:
            continue
        layers: list[tuple[float, float, SectionShape]] = []
        for start, end in zip(levels, levels[1:], strict=False):
            depth = end - start
            if depth <= max(data.diagonal * 1e-5, 0.001):
                continue
            section = section_shape(data, axis, start + depth / 2)
            if section is not None:
                layers.append((_clean(start), _clean(end), section))
        if len(layers) < 2:
            continue

        groups: list[tuple[float, float, SectionShape]] = []
        for start, end, section in layers:
            if (
                groups
                and abs(start - groups[-1][1]) <= 0.005
                and _polygon_similarity(groups[-1][2].polygon, section.polygon) >= 0.998
            ):
                previous = groups[-1]
                groups[-1] = (previous[0], end, previous[2])
            else:
                groups.append((start, end, section))
        if len(groups) < 2:
            continue

        first_start, first_end, first_section = groups[0]
        base = _base_from_section(
            first_section,
            axis,
            first_start,
            _clean(first_end - first_start),
        )
        operations = [
            BooleanExtrudeFeature(
                mode="add",
                axis=axis,
                start=first_start,
                depth=_clean(first_end - first_start),
                outer=region.outer,
                holes=region.holes,
            )
            for region in first_section.additional_regions
        ]
        for start, end, section in groups[1:]:
            depth = _clean(end - start)
            operations.append(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=start,
                    depth=depth,
                    outer=section.outer,
                    holes=section.holes,
                )
            )
            operations.extend(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=start,
                    depth=depth,
                    outer=region.outer,
                    holes=region.holes,
                )
                for region in section.additional_regions
            )
        estimated_volume = sum(section.area * (end - start) for start, end, section in groups)
        volume_error = (
            abs(estimated_volume - target_volume) / max(target_volume, 1e-9)
            if target_volume is not None
            else 0.25
        )
        plan = ReconstructionPlan(
            name=name,
            base=base,
            operations=operations,
            assumptions=[
                f"Detected {len(groups)} constant-profile layers along the {axis.value} axis.",
                "Layer boundaries were inferred from planar mesh faces and verified by slicing.",
                "Lines, circles, and circular arcs were fitted analytically before "
                "polygon fallback.",
            ],
        )
        candidates.append(
            PlanCandidate(
                plan=plan,
                heuristic_score=volume_error + len(groups) * 0.002,
                section_consistency=1.0 - volume_error,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.heuristic_score)


def generate_axial_radius_candidates(
    data: MeshData,
    name: str,
) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    radial_tolerance = max(data.diagonal * 0.00025, 0.002)
    target_volume = data.report.volume_mm3
    for axis, (_, _, _, index) in AXES.items():
        coordinates = sorted(
            float(value)
            for value in np.unique(
                np.round(data.mesh.vertices[:, index], decimals=3)
            )
        )
        if len(coordinates) < 3 or len(coordinates) > 64:
            continue
        segments: list[tuple[float, float, float, float, np.ndarray]] = []
        valid = True
        for start, end in zip(coordinates, coordinates[1:], strict=False):
            if end - start <= radial_tolerance * 0.25:
                continue
            observations: list[tuple[float, CircleProfile]] = []
            for fraction in (0.2, 0.5, 0.8):
                location = start + (end - start) * fraction
                section = section_shape(data, axis, location)
                if (
                    section is None
                    or not isinstance(section.outer, CircleProfile)
                    or section.holes
                    or section.additional_regions
                ):
                    valid = False
                    break
                observations.append((location, section.outer))
            if not valid:
                break
            locations = np.asarray(
                [location for location, _ in observations]
            )
            radii = np.asarray(
                [circle.radius for _, circle in observations]
            )
            slope, intercept = np.polyfit(locations, radii, 1)
            residual = float(
                np.max(np.abs(slope * locations + intercept - radii))
            )
            if residual > radial_tolerance:
                valid = False
                break
            centers = np.asarray(
                [np.asarray(circle.center) for _, circle in observations]
            )
            center = np.mean(centers, axis=0)
            if float(
                np.max(np.linalg.norm(centers - center, axis=1))
            ) > radial_tolerance:
                valid = False
                break
            segments.append(
                (
                    start,
                    end,
                    float(slope * start + intercept),
                    float(slope * end + intercept),
                    center,
                )
            )
        if not valid or len(segments) < 2:
            continue

        merged: list[tuple[float, float, float, float, np.ndarray]] = []
        for segment in segments:
            if not merged:
                merged.append(segment)
                continue
            previous = merged[-1]
            previous_slope = (
                (previous[3] - previous[2])
                / max(previous[1] - previous[0], 1e-9)
            )
            current_slope = (
                (segment[3] - segment[2])
                / max(segment[1] - segment[0], 1e-9)
            )
            if (
                abs(previous[1] - segment[0]) <= radial_tolerance
                and abs(previous[3] - segment[2]) <= radial_tolerance * 2
                and abs(previous_slope - current_slope) <= 0.06
                and np.linalg.norm(previous[4] - segment[4])
                <= radial_tolerance
            ):
                merged[-1] = (
                    previous[0],
                    segment[1],
                    previous[2],
                    segment[3],
                    (previous[4] + segment[4]) / 2,
                )
            else:
                merged.append(segment)

        # Tessellated cones contain many nearly collinear rings and occasionally
        # a microscopic constant-radius ring at a tangent boundary.  Collapse
        # those mesh artifacts so one source cone becomes one editable feature.
        cleaned: list[tuple[float, float, float, float, np.ndarray]] = []
        segment_index = 0
        while segment_index < len(merged):
            segment = merged[segment_index]
            if (
                segment[1] - segment[0] <= radial_tolerance
                and segment_index + 1 < len(merged)
            ):
                following = merged[segment_index + 1]
                merged[segment_index + 1] = (
                    segment[0],
                    following[1],
                    segment[2],
                    following[3],
                    (segment[4] + following[4]) / 2,
                )
            else:
                cleaned.append(segment)
            segment_index += 1
        merged = cleaned

        consolidated: list[tuple[float, float, float, float, np.ndarray]] = []
        for segment in merged:
            if not consolidated:
                consolidated.append(segment)
                continue
            previous = consolidated[-1]
            previous_slope = (
                (previous[3] - previous[2])
                / max(previous[1] - previous[0], 1e-9)
            )
            current_slope = (
                (segment[3] - segment[2])
                / max(segment[1] - segment[0], 1e-9)
            )
            both_constant = (
                abs(previous[3] - previous[2]) <= radial_tolerance * 2
                and abs(segment[3] - segment[2]) <= radial_tolerance * 2
            )
            both_collinear = abs(previous_slope - current_slope) <= 0.06
            if (
                abs(previous[1] - segment[0]) <= radial_tolerance
                and abs(previous[3] - segment[2]) <= radial_tolerance * 2
                and (both_constant or both_collinear)
                and np.linalg.norm(previous[4] - segment[4])
                <= radial_tolerance
            ):
                consolidated[-1] = (
                    previous[0],
                    segment[1],
                    previous[2],
                    segment[3],
                    (previous[4] + segment[4]) / 2,
                )
            else:
                consolidated.append(segment)
        merged = consolidated
        # A true toroidal fillet appears in a tessellated mesh as a long chain
        # of short cones with continuously changing slope.  That is not a
        # clean turned-feature reconstruction; let the analytic fillet search
        # handle it instead.
        if len(merged) > 12:
            continue
        first = merged[0]
        if abs(first[3] - first[2]) > radial_tolerance * 2:
            continue
        base = CylinderFeature(
            axis=axis,
            start=_clean(first[0]),
            depth=_clean(first[1] - first[0]),
            center=(_clean(first[4][0]), _clean(first[4][1])),
            radius=_clean((first[2] + first[3]) / 2),
        )
        operations: list[
            BooleanExtrudeFeature | ConicalAddFeature
        ] = []
        for start, end, start_radius, end_radius, center in merged[1:]:
            if abs(end_radius - start_radius) <= radial_tolerance * 2:
                operations.append(
                    BooleanExtrudeFeature(
                        mode="add",
                        axis=axis,
                        start=_clean(start),
                        depth=_clean(end - start),
                        outer=CircleProfile(
                            center=(
                                _clean(center[0]),
                                _clean(center[1]),
                            ),
                            radius=_clean((start_radius + end_radius) / 2),
                        ),
                    )
                )
            else:
                operations.append(
                    ConicalAddFeature(
                        axis=axis,
                        center=(
                            _clean(center[0]),
                            _clean(center[1]),
                        ),
                        start=_clean(start),
                        depth=_clean(end - start),
                        start_diameter=_clean(start_radius * 2),
                        end_diameter=_clean(end_radius * 2),
                    )
                )
        estimated_volume = sum(
            math.pi
            * (end - start)
            * (
                start_radius * start_radius
                + start_radius * end_radius
                + end_radius * end_radius
            )
            / 3
            for start, end, start_radius, end_radius, _ in merged
        )
        volume_error = (
            abs(estimated_volume - target_volume) / max(target_volume, 1e-9)
            if target_volume is not None
            else 0.25
        )
        candidates.append(
            PlanCandidate(
                plan=ReconstructionPlan(
                    name=name,
                    base=base,
                    operations=operations,
                    assumptions=[
                        f"Recovered a {len(merged)}-segment analytic axial radius "
                        f"profile along {axis.value}.",
                        "Constant spans use cylinders and linearly varying spans "
                        "use conical additions.",
                    ],
                ),
                heuristic_score=volume_error + len(merged) * 0.001,
                section_consistency=1.0 - volume_error,
            )
        )
    return candidates


def generate_tapered_profile_candidates(
    data: MeshData,
    name: str,
) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    target_volume = data.report.volume_mm3
    for axis, (_, _, _, index) in AXES.items():
        coordinates = sorted(
            float(value)
            for value in np.unique(
                np.round(data.mesh.vertices[:, index], decimals=3)
            )
        )
        if len(coordinates) < 3 or len(coordinates) > 16:
            continue
        intervals: list[
            tuple[float, float, SectionShape, SectionShape, SectionShape, float]
        ] = []
        valid = True
        for start, end in zip(coordinates, coordinates[1:], strict=False):
            depth = end - start
            if depth <= max(data.diagonal * 1e-5, 0.0005):
                continue
            near_start = section_shape(data, axis, start + depth * 0.005)
            middle = section_shape(data, axis, (start + end) / 2)
            near_end = section_shape(data, axis, end - depth * 0.005)
            if (
                near_start is None
                or middle is None
                or near_end is None
                or near_start.additional_regions
                or middle.additional_regions
                or near_end.additional_regions
                or len(near_start.holes) != len(middle.holes)
                or len(near_end.holes) != len(middle.holes)
            ):
                valid = False
                break
            similarity = _polygon_similarity(
                near_start.polygon,
                near_end.polygon,
            )
            intervals.append(
                (start, end, near_start, middle, near_end, similarity)
            )
        if not valid or len(intervals) < 2:
            continue
        if all(interval[5] >= 0.9995 for interval in intervals):
            continue
        constant_intervals = [
            interval for interval in intervals if interval[5] >= 0.9995
        ]
        if not constant_intervals:
            continue
        base_interval = max(
            constant_intervals,
            key=lambda interval: interval[1] - interval[0],
        )
        base = _base_from_section(
            base_interval[3],
            axis,
            _clean(base_interval[0]),
            _clean(base_interval[1] - base_interval[0]),
        )
        operations: list[BooleanExtrudeFeature | TaperedAddFeature] = []
        for start, end, near_start, middle, near_end, similarity in intervals:
            if (start, end) == (base_interval[0], base_interval[1]):
                continue
            depth = _clean(end - start)
            if similarity >= 0.9995:
                operations.append(
                    BooleanExtrudeFeature(
                        mode="add",
                        axis=axis,
                        start=_clean(start),
                        depth=depth,
                        outer=middle.outer,
                        holes=middle.holes,
                    )
                )
            else:
                operations.append(
                    TaperedAddFeature(
                        axis=axis,
                        start=_clean(start),
                        depth=depth,
                        start_outer=near_start.outer,
                        end_outer=near_end.outer,
                        holes=middle.holes,
                    )
                )
        estimated_volume = sum(
            (near_start.area + near_end.area) * (end - start) / 2
            for start, end, near_start, _, near_end, _ in intervals
        )
        volume_error = (
            abs(estimated_volume - target_volume) / max(target_volume, 1e-9)
            if target_volume is not None
            else 0.25
        )
        plan = ReconstructionPlan(
            name=name,
            base=base,
            operations=operations,
            assumptions=[
                f"Detected {len(intervals)} profile intervals along the {axis.value} axis.",
                "Linearly changing silhouettes were reconstructed as editable ruled lofts.",
            ],
        )
        candidates.append(
            PlanCandidate(
                plan=plan,
                heuristic_score=volume_error + len(operations) * 0.002,
                section_consistency=1.0 - volume_error,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.heuristic_score)


def generate_compact_axial_cap_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Approximate a rounded axisymmetric cap with a bounded ruled loft.

    A tapered extrusion followed by a large axial fillet has one long constant
    prismatic stock interval and a circular radius function above it. Uniform
    slabs spend many features on the stock and represent the curved cap as
    cylinders. One exact stock extrusion plus ten radius-change lofts is both
    substantially cleaner and preserves analytic conical STEP faces.
    """

    candidates: list[ReconstructionPlan] = []
    for axis, (_, _, _, axis_index) in AXES.items():
        levels = _axis_levels(data, axis)
        if len(levels) < 3:
            continue
        stock_start, join = float(levels[0]), float(levels[1])
        axis_end = float(data.mesh.bounds[1, axis_index])
        if axis_end - join <= max(data.diagonal * 0.1, 0.1):
            continue
        stock = section_shape(data, axis, (stock_start + join) / 2)
        if stock is None or isinstance(stock.outer, CircleProfile):
            continue

        probe_levels = np.linspace(
            join + max((axis_end - join) * 1e-4, 1e-4),
            axis_end - max((axis_end - join) * 1e-4, 1e-4),
            257,
        )
        radii: list[float] = []
        centers: list[tuple[float, float]] = []
        valid = True
        for location in probe_levels:
            section = section_shape(data, axis, float(location))
            if (
                section is None
                or section.holes
                or section.additional_regions
                or section.polygon.length <= 1e-9
                or 4 * math.pi * section.area / section.polygon.length**2 < 0.88
            ):
                valid = False
                break
            centroid = section.polygon.centroid
            centers.append((float(centroid.x), float(centroid.y)))
            radii.append(math.sqrt(section.area / math.pi))
        if not valid:
            continue
        center = np.median(np.asarray(centers), axis=0)
        if np.max(np.linalg.norm(np.asarray(centers) - center, axis=1)) > max(
            data.diagonal * 0.002,
            0.02,
        ):
            continue
        radius_values = np.asarray(radii)
        derivatives = np.abs(np.diff(radius_values) / np.diff(probe_levels))
        baseline = float(np.median(derivatives[: max(6, len(derivatives) // 8)]))
        if baseline <= 1e-6:
            continue
        split_indices: set[int] = set()
        for ratio in (1.25, 1.5, 2.0):
            matches = np.flatnonzero(derivatives > baseline * ratio)
            if len(matches):
                split_indices.add(min(int(matches[0]) + 1, len(probe_levels) - 2))
        if split_indices:
            earliest = min(split_indices)
            split_indices.update(
                min(earliest + offset, len(probe_levels) - 2)
                for offset in (2, 4)
            )
        for split_index in sorted(split_indices):
            split = float(probe_levels[split_index])
            cap_levels = probe_levels[split_index:]
            cap_radii = radius_values[split_index:]
            changes = np.abs(np.diff(cap_radii))
            changes += max(float(np.mean(changes)) * 0.005, 1e-9)
            cumulative = np.r_[0.0, np.cumsum(changes)]
            indices = [
                int(np.argmin(np.abs(cumulative - target)))
                for target in np.linspace(0.0, float(cumulative[-1]), 10)
            ]
            indices[0] = 0
            indices[-1] = len(cap_levels) - 1
            cap_breaks = cap_levels[sorted(set(indices))]
            operation_levels = [join, split, *map(float, cap_breaks[1:])]
            if len(operation_levels) - 1 > 10:
                continue

            def circle_at(
                location: float,
                *,
                current_axis: Axis = axis,
                current_join: float = join,
                current_end: float = axis_end,
            ) -> CircleProfile | None:
                epsilon = max((current_end - current_join) * 1e-5, 1e-4)
                sample = section_shape(
                    data,
                    current_axis,
                    min(
                        max(location, current_join + epsilon),
                        current_end - epsilon,
                    ),
                )
                if sample is None:
                    return None
                sample_center = sample.polygon.centroid
                return CircleProfile(
                    center=(
                        _clean(sample_center.x),
                        _clean(sample_center.y),
                    ),
                    radius=float(math.sqrt(sample.area / math.pi)),
                )

            operations: list[TaperedAddFeature] = []
            for start, end in zip(
                operation_levels,
                operation_levels[1:],
                strict=False,
            ):
                # Adjacent ruled lofts must share the exact same boundary
                # circle. Sampling on opposite sides of a breakpoint creates
                # microscopic radius steps that OCCT may retain as internal
                # faces and that badly distort one-sided surface distance.
                start_profile = circle_at(start)
                end_profile = circle_at(end)
                if start_profile is None or end_profile is None:
                    operations = []
                    break
                operations.append(
                    TaperedAddFeature(
                        axis=axis,
                        start=_clean(start),
                        depth=_clean(end - start),
                        start_outer=start_profile,
                        end_outer=end_profile,
                    )
                )
            if not operations:
                continue
            candidates.append(
                ReconstructionPlan(
                    name=name,
                    base=_base_from_section(
                        stock,
                        axis,
                        _clean(stock_start),
                        _clean(join - stock_start),
                    ),
                    operations=operations,
                    assumptions=[
                        "Recovered constant prismatic stock followed by a "
                        "compact radius-change axial loft.",
                        "Preserved the tapered region as analytic conical STEP "
                        "faces within a bounded editable feature count.",
                    ],
                )
            )
    return candidates


def generate_revolved_profile_candidates(
    data: MeshData,
    name: str,
) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    bounds = data.mesh.bounds
    world_center = (bounds[0] + bounds[1]) / 2
    tolerance = max(data.diagonal * 0.0008, 0.015)
    for axis, (_, u_axis, v_axis, index) in AXES.items():
        coordinates = sorted(
            float(value)
            for value in np.unique(
                np.round(data.mesh.vertices[:, index], decimals=3)
            )
        )
        if len(coordinates) < 8 or len(coordinates) > 160:
            continue
        center = np.asarray(
            [world_center @ u_axis, world_center @ v_axis],
            dtype=float,
        )
        hole_radii: list[float] = []
        valid = True
        for fraction in np.linspace(0.03, 0.97, 9):
            location = coordinates[0] + (
                coordinates[-1] - coordinates[0]
            ) * float(fraction)
            section = section_shape(data, axis, location)
            if (
                section is None
                or not isinstance(section.outer, CircleProfile)
                or len(section.holes) > 1
                or section.additional_regions
                or np.linalg.norm(
                    np.asarray(section.outer.center) - center
                )
                > tolerance * 2
            ):
                valid = False
                break
            if section.holes:
                hole = section.holes[0]
                if (
                    not isinstance(hole, CircleProfile)
                    or np.linalg.norm(
                        np.asarray(hole.center)
                        - np.asarray(section.outer.center)
                    )
                    > tolerance * 2
                ):
                    valid = False
                    break
                hole_radii.append(hole.radius)
        if not valid:
            continue
        # The current revolve schema supports a constant bore. A strongly
        # varying inner radius is commonly an extrusion/cut history whose
        # outer envelope happens to be circular; treating it as a simple
        # revolve deletes real internal features.
        if hole_radii and float(np.ptp(hole_radii)) > tolerance * 2:
            continue
        inner_radius = (
            float(np.median(hole_radii)) if hole_radii else 0.0
        )
        outer_points: list[tuple[float, float]] = []
        coordinate_tolerance = max(data.diagonal * 1e-5, 0.0006)
        for coordinate in coordinates:
            level_vertices = data.mesh.vertices[
                np.abs(data.mesh.vertices[:, index] - coordinate)
                <= coordinate_tolerance
            ]
            if len(level_vertices) < 4:
                continue
            projected = np.column_stack(
                (level_vertices @ u_axis, level_vertices @ v_axis)
            )
            radii = np.linalg.norm(projected - center, axis=1)
            outer_points.append(
                (float(np.max(radii)), float(coordinate))
            )
        if len(outer_points) < 8:
            continue
        profile_points = [
            *outer_points,
            (inner_radius, outer_points[-1][1]),
            (inner_radius, outer_points[0][1]),
            outer_points[0],
        ]
        profile = _fit_path_profile(
            np.asarray(profile_points),
            tolerance,
        )
        if profile is None:
            continue
        plan = ReconstructionPlan(
            name=name,
            base=RevolveFeature(
                axis=axis,
                center=(_clean(center[0]), _clean(center[1])),
                profile=profile,
            ),
            assumptions=[
                f"Detected a fully axisymmetric profile around the {axis.value} axis.",
                "Cylinders, cones, and toroidal blends were retained as analytic "
                "lines and arcs in one editable revolve sketch.",
            ],
        )
        candidates.append(
            PlanCandidate(
                plan=plan,
                heuristic_score=0.001 + len(profile.segments) * 0.0001,
                section_consistency=1.0,
            )
        )
    return candidates


def generate_cross_axis_residual_candidates(
    data: MeshData,
    source: ReconstructionPlan,
    cut_extension_factor: float = 1.5,
    include_source_axis: bool = False,
) -> list[ReconstructionPlan]:
    from .cad import build_plan

    try:
        shape = build_plan(source).val()
        vertices, faces = shape.tessellate(0.02, 0.1)
    except Exception:
        return []
    if not vertices or not faces:
        # A valid OCCT wrapper can still contain no tessellatable solid after
        # an envelope Boolean collapses to the empty set.  Treat that source
        # as unusable instead of passing a shape-(0,) vertex array into
        # trimesh's plane intersection routine.
        return []
    candidate_mesh = trimesh.Trimesh(
        vertices=[vertex.toTuple() for vertex in vertices],
        faces=faces,
        process=True,
    )
    candidate_data = MeshData(
        mesh=candidate_mesh,
        report=data.report,
        source_path=data.source_path,
    )
    tolerance = max(data.diagonal * 0.0008, 0.015)
    plans: list[ReconstructionPlan] = []
    residual_groups: list[tuple[Axis, list[BooleanExtrudeFeature]]] = []
    source_axis = getattr(source.base, "axis", None)
    for axis in AXES:
        if axis == source_axis and not include_source_axis:
            continue
        levels = _axis_levels(data, axis)
        clusters: list[dict[str, object]] = []
        for start, end in zip(levels, levels[1:], strict=False):
            if end - start <= tolerance:
                continue
            location = (start + end) / 2
            target_section = section_shape(data, axis, location)
            candidate_section = section_shape(candidate_data, axis, location)
            if target_section is None or candidate_section is None:
                continue
            differences = (
                ("cut", candidate_section.polygon.difference(target_section.polygon)),
                ("add", target_section.polygon.difference(candidate_section.polygon)),
            )
            for mode, difference in differences:
                geometries = (
                    list(difference.geoms)
                    if isinstance(difference, MultiPolygon)
                    else [difference]
                )
                for geometry in geometries:
                    cleanup_distance = tolerance * 0.5
                    cleaned = geometry.buffer(
                        -cleanup_distance,
                        join_style="mitre",
                    ).buffer(
                        cleanup_distance,
                        join_style="mitre",
                    )
                    if cleaned.is_empty:
                        continue
                    if mode == "cut":
                        # A cutter inferred as candidate-minus-target often
                        # terminates exactly on the candidate skin. Boolean
                        # subtraction then leaves a zero-thickness strip whose
                        # broad face badly distorts one-sided distance metrics.
                        # Extend the cutter by less than the mesh-fit tolerance
                        # so it crosses that skin cleanly.
                        cleaned = cleaned.buffer(
                            cleanup_distance * cut_extension_factor,
                            join_style="mitre",
                        )
                    geometry = cleaned.simplify(
                        tolerance * 0.2,
                        preserve_topology=True,
                    )
                    if (
                        not isinstance(geometry, Polygon)
                        or geometry.area <= tolerance * tolerance * 4
                    ):
                        continue
                    match = next(
                        (
                            cluster
                            for cluster in clusters
                            if cluster["mode"] == mode
                            and (
                                geometry.intersection(
                                    cluster["representative"]
                                ).area
                                / max(
                                    min(
                                        geometry.area,
                                        cluster["representative"].area,
                                    ),
                                    1e-9,
                                )
                                > 0.2
                            )
                        ),
                        None,
                    )
                    if match is None:
                        clusters.append(
                            {
                                "mode": mode,
                                "representative": geometry,
                                "geometries": [(geometry, start, end)],
                                "start": start,
                                "end": end,
                            }
                        )
                    else:
                        match["start"] = min(float(match["start"]), start)
                        match["end"] = max(float(match["end"]), end)
                        match["geometries"].append((geometry, start, end))
        operations: list[BooleanExtrudeFeature] = []
        for cluster in clusters:
            # End slices near a sloped outer wall can make a constant-profile
            # residual look artificially larger and irregular. The widest
            # interval is the stable feature interior; narrow intervals are
            # normally just transitions at its terminating walls.
            geometry, _, _ = max(
                cluster["geometries"],
                key=lambda item: item[2] - item[1],
            )
            fitted = _profile_from_ring(
                np.asarray(geometry.exterior.coords),
                tolerance,
            )
            if fitted is None:
                continue
            profile, _ = fitted
            operations.append(
                BooleanExtrudeFeature(
                    mode=cluster["mode"],
                    axis=axis,
                    start=_clean(float(cluster["start"])),
                    depth=_clean(
                        float(cluster["end"]) - float(cluster["start"])
                    ),
                    outer=profile,
                )
            )
        if not operations or len(operations) > 12:
            continue
        plan = source.model_copy(deep=True)
        plan.operations.extend(operations)
        plan.assumptions.append(
            f"Recovered {len(operations)} localized constant-profile residual "
            f"features along the {axis.value} axis."
        )
        plans.append(plan)
        residual_groups.append((axis, operations))
    combined_operations = [
        operation
        for _, operations in residual_groups
        for operation in operations
    ]
    if len(residual_groups) >= 2 and len(combined_operations) <= 12:
        combined = source.model_copy(deep=True)
        combined.operations.extend(
            operation.model_copy(deep=True)
            for operation in combined_operations
        )
        combined.assumptions.append(
            "Recovered localized residual features across "
            + ", ".join(axis.value for axis, _ in residual_groups)
            + " axes in one editable construction."
        )
        plans.append(combined)
    return plans


def generate_constant_stock_cylinder_cut_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Recover an end-sampled cardinal extrusion plus orthogonal round cuts."""

    fitted_cuts = [
        feature
        for feature in _mesh_cylindrical_features(data)
        if feature.mode == "cut" and feature.inner_radius is None
    ]
    if not fitted_cuts:
        return []
    plans: list[ReconstructionPlan] = []
    for stock_axis, (stock_direction, _, _, axis_index) in AXES.items():
        cross_axis_cuts = [
            feature
            for feature in fitted_cuts
            if abs(float(np.dot(stock_direction, feature.direction))) < 0.01
        ]
        if not cross_axis_cuts:
            continue
        lower, upper = map(float, data.mesh.bounds[:, axis_index])
        span = upper - lower
        sections = [
            section
            for fraction in (0.04, 0.08, 0.12, 0.88, 0.92, 0.96)
            if (
                section := section_shape(
                    data,
                    stock_axis,
                    lower + span * fraction,
                )
            )
            is not None
            and not section.additional_regions
        ]
        if not sections:
            continue
        stock_section = max(sections, key=lambda section: section.area)
        smallest_end_area = min(section.area for section in sections)
        # Large end treatments require the measured-cap reconstruction rather
        # than extending the maximum section all the way to both end planes.
        if smallest_end_area < stock_section.area * 0.985:
            continue
        operations = [
            feature.model_copy(deep=True) for feature in cross_axis_cuts
        ]
        plans.append(
            ReconstructionPlan(
                name=name,
                base=ExtrudeFeature(
                    axis=stock_axis,
                    start=lower,
                    depth=span,
                    outer=stock_section.outer,
                    holes=stock_section.holes,
                ),
                operations=operations,
                assumptions=[
                    f"Recovered constant {stock_axis.value}-axis stock from "
                    "unaffected end sections and subtracted fitted orthogonal "
                    "analytic cylinder features."
                ],
            )
        )
    return plans


def generate_envelope_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Recover a pre-finish extrusion from the union of its axial sections.

    A large fillet or two-distance chamfer can make every individual section
    look unlike the original sketch. Their projected union still recovers the
    stock extrusion; subsequent residual analysis turns the removed material
    back into editable orthogonal cuts with analytic arcs where present.
    """

    tolerance = max(data.diagonal * 0.0008, 0.015)
    plans: list[ReconstructionPlan] = []
    for axis in AXES:
        levels = _axis_levels(data, axis)
        sections: list[Polygon] = []
        for start, end in zip(levels, levels[1:], strict=False):
            if end - start <= tolerance:
                continue
            for fraction in (
                0.001,
                0.01,
                0.05,
                0.15,
                0.3,
                0.5,
                0.7,
                0.85,
                0.95,
                0.99,
                0.999,
            ):
                section = section_shape(
                    data,
                    axis,
                    start + (end - start) * fraction,
                )
                if section is not None:
                    sections.append(section.polygon)
        if not sections:
            continue
        merged = unary_union(sections)
        regions = (
            list(merged.geoms)
            if isinstance(merged, MultiPolygon)
            else [merged]
        )
        regions = [
            region
            for region in regions
            if isinstance(region, Polygon)
            and region.area > tolerance * tolerance * 4
        ]
        if not regions:
            continue
        regions.sort(key=lambda region: region.area, reverse=True)
        fitted_regions: list[tuple[Profile, list[Profile]]] = []
        for region in regions[:8]:
            outer_fit = _profile_from_ring(
                np.asarray(region.exterior.coords),
                tolerance,
            )
            if outer_fit is None:
                continue
            holes: list[Profile] = []
            for interior in region.interiors:
                hole_fit = _profile_from_ring(
                    np.asarray(interior.coords),
                    tolerance,
                )
                if hole_fit is not None:
                    holes.append(hole_fit[0])
            fitted_regions.append((outer_fit[0], holes))
        if not fitted_regions:
            continue
        _, _, _, axis_index = AXES[axis]
        axis_start, axis_end = data.mesh.bounds[:, axis_index]
        base_outer, base_holes = fitted_regions[0]
        plan = ReconstructionPlan(
            name=name,
            base=ExtrudeFeature(
                axis=axis,
                start=float(axis_start),
                depth=float(axis_end - axis_start),
                outer=base_outer,
                holes=base_holes,
            ),
            operations=[
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=float(axis_start),
                    depth=float(axis_end - axis_start),
                    outer=outer,
                    holes=holes,
                )
                for outer, holes in fitted_regions[1:]
            ],
            assumptions=[
                f"Recovered the pre-finish {axis.value}-axis stock profile from "
                "the union of axial sections."
            ],
        )
        plans.append(plan)
    return plans


def generate_layer_envelope_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Recover separate stock profiles when each axial layer was later cut."""

    tolerance = max(data.diagonal * 0.0008, 0.015)
    fractions = (
        0.001,
        0.01,
        0.05,
        0.15,
        0.3,
        0.5,
        0.7,
        0.85,
        0.95,
        0.99,
        0.999,
    )

    def fit_regions(sections: list[Polygon]) -> list[tuple[Profile, list[Profile]]]:
        if not sections:
            return []
        merged = unary_union(sections)
        polygons = (
            list(merged.geoms)
            if isinstance(merged, MultiPolygon)
            else [merged]
        )
        polygons = [
            polygon
            for polygon in polygons
            if isinstance(polygon, Polygon)
            and polygon.area > tolerance * tolerance * 4
        ]
        polygons.sort(key=lambda polygon: polygon.area, reverse=True)
        fitted: list[tuple[Profile, list[Profile]]] = []
        for polygon in polygons[:8]:
            outer = _profile_from_ring(
                np.asarray(polygon.exterior.coords),
                tolerance,
            )
            if outer is None:
                continue
            holes = [
                hole[0]
                for interior in polygon.interiors
                if (
                    hole := _profile_from_ring(
                        np.asarray(interior.coords),
                        tolerance,
                    )
                )
                is not None
            ]
            fitted.append((outer[0], holes))
        return fitted

    plans: list[ReconstructionPlan] = []
    for axis in AXES:
        levels = _axis_levels(data, axis)
        if len(levels) < 3 or len(levels) > 9:
            continue
        layers: list[tuple[float, float, list[tuple[Profile, list[Profile]]]]] = []
        for start, end in zip(levels, levels[1:], strict=False):
            if end - start <= tolerance:
                continue
            sections = []
            for fraction in fractions:
                section = section_shape(
                    data,
                    axis,
                    start + (end - start) * fraction,
                )
                if section is not None:
                    sections.append(section.polygon)
            fitted = fit_regions(sections)
            if fitted:
                layers.append((start, end, fitted))
        if len(layers) < 2:
            continue
        start, end, first_regions = layers[0]
        first_outer, first_holes = first_regions[0]
        operations: list[BooleanExtrudeFeature] = [
            BooleanExtrudeFeature(
                mode="add",
                axis=axis,
                start=start,
                depth=end - start,
                outer=outer,
                holes=holes,
            )
            for outer, holes in first_regions[1:]
        ]
        for layer_start, layer_end, regions in layers[1:]:
            operations.extend(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=layer_start,
                    depth=layer_end - layer_start,
                    outer=outer,
                    holes=holes,
                )
                for outer, holes in regions
            )
        plans.append(
            ReconstructionPlan(
                name=name,
                base=ExtrudeFeature(
                    axis=axis,
                    start=start,
                    depth=end - start,
                    outer=first_outer,
                    holes=first_holes,
                ),
                operations=operations,
                assumptions=[
                    f"Recovered {len(layers)} pre-cut {axis.value}-axis stock "
                    "profiles from per-layer section envelopes."
                ],
            )
        )
    return plans


def generate_adaptive_layer_candidates(
    data: MeshData,
    name: str,
    axis: Axis,
    slice_count: int = 24,
) -> list[ReconstructionPlan]:
    """Approximate continuously varying mechanical detail with editable slabs.

    This is the bounded fallback for chained fillets and cross-axis cuts whose
    topology changes throughout an otherwise dominant extrusion direction.
    Every slab still uses line/arc sketch primitives; a score-improving analytic
    edge fillet is proposed where the first section contains a circular edge.
    """

    _, _, _, axis_index = AXES[axis]
    axis_start, axis_end = data.mesh.bounds[:, axis_index]
    levels = np.linspace(axis_start, axis_end, slice_count + 1)
    sections: list[SectionShape] = []
    for start, end in zip(levels, levels[1:], strict=False):
        section = section_shape(data, axis, float((start + end) / 2))
        if section is None:
            return []
        sections.append(section)

    first = sections[0]
    first_start = float(levels[0])
    first_depth = float(levels[1] - levels[0])
    first_plain_regions = [
        region.outer for region in first.additional_regions if not region.holes
    ]
    operations: list[BooleanExtrudeFeature | EdgeFinishFeature] = [
        BooleanExtrudeFeature(
            mode="add",
            axis=axis,
            start=first_start,
            depth=first_depth,
            outer=region.outer,
            holes=region.holes,
        )
        for region in first.additional_regions
        if region.holes
    ]
    for index, section in enumerate(sections[1:], start=1):
        start = float(levels[index])
        depth = float(levels[index + 1] - levels[index])
        operations.append(
            BooleanExtrudeFeature(
                mode="add",
                axis=axis,
                start=start,
                depth=depth,
                outer=section.outer,
                holes=section.holes,
                additional_regions=[
                    region.outer
                    for region in section.additional_regions
                    if not region.holes
                ],
            )
        )
        operations.extend(
            BooleanExtrudeFeature(
                mode="add",
                axis=axis,
                start=start,
                depth=depth,
                outer=region.outer,
                holes=region.holes,
            )
            for region in section.additional_regions
            if region.holes
        )
    plan = ReconstructionPlan(
        name=name,
        base=ExtrudeFeature(
            axis=axis,
            start=first_start,
            depth=first_depth,
            outer=first.outer,
            holes=first.holes,
            additional_regions=first_plain_regions,
        ),
        operations=operations,
        assumptions=[
            f"Recovered continuously varying cross-axis detail as {slice_count} "
            f"editable analytic-profile slabs along the {axis.value} axis."
        ],
        representation="sampled_approximation",
    )
    candidates = [plan]

    # A small end fillet can encode a real toroidal transition while also
    # reducing the slab error at a rounded axial boundary. It remains a normal
    # score-gated alternative, never an unconditional topology decoration.
    from .cad import build_plan

    try:
        shape = build_plan(
            ReconstructionPlan(
                name=name,
                base=plan.base,
            )
        ).val()
        axis_extent = {
            Axis.X: "xlen",
            Axis.Y: "ylen",
            Axis.Z: "zlen",
        }[axis]
        tolerance = max(data.diagonal * 1e-6, 1e-6)
        circular_edges = [
            edge
            for edge in shape.Edges()
            if edge.geomType() == "CIRCLE"
            and getattr(edge.BoundingBox(), axis_extent) <= tolerance
        ]
        if circular_edges:
            edge = max(circular_edges, key=lambda item: item.Length())
            center_3d = edge.Center()
            if axis == Axis.X:
                center = (float(center_3d.y), float(center_3d.z))
            elif axis == Axis.Y:
                center = (float(center_3d.x), float(-center_3d.z))
            else:
                center = (float(center_3d.x), float(center_3d.y))
            filleted = plan.model_copy(deep=True)
            filleted.operations.append(
                EdgeFinishFeature(
                    mode="fillet",
                    axis=axis,
                    end="start",
                    size=max(min(first_depth * 0.09, data.diagonal * 0.002), 0.01),
                    selector="nearest",
                    center=(_clean(center[0]), _clean(center[1])),
                    feature_index=-1,
                )
            )
            filleted.assumptions.append(
                "Encoded the rounded axial boundary as a true analytic edge "
                "fillet rather than a faceted curve."
            )
            candidates.append(filleted)
    except Exception:
        pass
    return candidates


def generate_change_weighted_layer_candidates(
    data: MeshData,
    name: str,
    axis: Axis,
    sample_count: int = 48,
    layer_budget: int = 18,
) -> list[ReconstructionPlan]:
    """Place editable slabs where the target section actually changes.

    Uniform slabs waste most features in constant regions and undersample
    localized fillets.  A fine section probe supplies a cumulative shape-change
    measure; quantiles of that measure concentrate a bounded number of layers
    around the curved transitions.
    """

    _, _, _, axis_index = AXES[axis]
    axis_start, axis_end = data.mesh.bounds[:, axis_index]
    probe_levels = np.linspace(axis_start, axis_end, sample_count + 1)
    probe_midpoints = (probe_levels[:-1] + probe_levels[1:]) / 2.0
    probe_sections: list[SectionShape] = []
    for midpoint in probe_midpoints:
        section = section_shape(data, axis, float(midpoint))
        if section is None:
            return []
        probe_sections.append(section)

    mean_area = float(np.mean([section.area for section in probe_sections]))
    baseline = mean_area * 0.003
    changes = np.asarray(
        [
            section.polygon.symmetric_difference(
                probe_sections[min(index + 1, len(probe_sections) - 1)].polygon
            ).area
            + baseline
            for index, section in enumerate(probe_sections)
        ],
        dtype=float,
    )
    cumulative = np.r_[0.0, np.cumsum(changes)]
    indices = [
        int(np.argmin(np.abs(cumulative - target)))
        for target in np.linspace(0.0, float(cumulative[-1]), layer_budget + 1)
    ]
    indices[0] = 0
    indices[-1] = sample_count
    indices = sorted(set(indices))
    levels = probe_levels[indices]
    if len(levels) < 4:
        return []

    base: ExtrudeFeature | None = None
    operations: list[BooleanExtrudeFeature | EdgeFinishFeature] = []
    for start, end in zip(levels, levels[1:], strict=False):
        target_midpoint = float((start + end) / 2)
        probe_index = int(np.argmin(np.abs(probe_midpoints - target_midpoint)))
        section = probe_sections[probe_index]
        depth = float(end - start)
        if base is None:
            grouped_regions = [
                region.outer
                for region in section.additional_regions
                if not region.holes
            ]
            base = ExtrudeFeature(
                axis=axis,
                start=float(start),
                depth=depth,
                outer=section.outer,
                holes=section.holes,
                additional_regions=grouped_regions,
            )
            operations.extend(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=float(start),
                    depth=depth,
                    outer=region.outer,
                    holes=region.holes,
                )
                for region in section.additional_regions
                if region.holes
            )
        else:
            grouped_regions = [
                region.outer
                for region in section.additional_regions
                if not region.holes
            ]
            operations.append(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=float(start),
                    depth=depth,
                    outer=section.outer,
                    holes=section.holes,
                    additional_regions=grouped_regions,
                )
            )
            operations.extend(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=float(start),
                    depth=depth,
                    outer=region.outer,
                    holes=region.holes,
                )
                for region in section.additional_regions
                if region.holes
            )
    if base is None:
        return []

    plan = ReconstructionPlan(
        name=name,
        base=base,
        operations=operations,
        assumptions=[
            f"Allocated {len(levels) - 1} editable {axis.value}-axis layers "
            "by measured cross-section change rather than uniform spacing."
        ],
        representation="sampled_approximation",
    )
    candidates = [plan]

    from .cad import _edge_circle_parameters, build_plan

    try:
        base_shape = build_plan(ReconstructionPlan(name=name, base=base)).val()
        circles = [
            (edge.Length(), circle)
            for edge in base_shape.Edges()
            if (circle := _edge_circle_parameters(edge, axis)) is not None
        ]
        if circles:
            _, (center, radius) = max(circles, key=lambda item: item[0])
            first_depth = float(levels[1] - levels[0])
            size = min(
                max(min(first_depth * 0.45, data.diagonal * 0.003), 0.01),
                radius * 0.25,
            )
            filleted = plan.model_copy(deep=True)
            filleted.operations.append(
                EdgeFinishFeature(
                    mode="fillet",
                    axis=axis,
                    end="start",
                    size=size,
                    selector="nearest",
                    center=(_clean(center[0]), _clean(center[1])),
                    feature_index=-1,
                )
            )
            filleted.assumptions.append(
                "Preserved a measured spatial round as a true analytic edge fillet."
            )
            candidates.append(filleted)
            chamfered = plan.model_copy(deep=True)
            chamfered.operations.append(
                EdgeFinishFeature(
                    mode="chamfer",
                    axis=axis,
                    end="start",
                    size=size,
                    selector="nearest",
                    center=(_clean(center[0]), _clean(center[1])),
                    feature_index=-1,
                )
            )
            chamfered.assumptions.append(
                "Preserved a measured bevel as a true analytic edge chamfer."
            )
            candidates.append(chamfered)
            for first_mode, second_mode in (
                ("chamfer", "fillet"),
                ("fillet", "chamfer"),
            ):
                combined = plan.model_copy(deep=True)
                combined.operations.extend(
                    [
                        EdgeFinishFeature(
                            mode=first_mode,
                            axis=axis,
                            end="start",
                            size=size,
                            selector="nearest",
                            center=(_clean(center[0]), _clean(center[1])),
                            feature_index=-1,
                        ),
                        EdgeFinishFeature(
                            mode=second_mode,
                            axis=axis,
                            end="end",
                            size=size,
                            selector="nearest",
                            center=(_clean(center[0]), _clean(center[1])),
                            feature_index=-1,
                        ),
                    ]
                )
                combined.assumptions.append(
                    "Preserved different measured treatments on the opposite "
                    "axial ends as one chamfer and one fillet."
                )
                candidates.append(combined)
    except Exception:
        pass
    return candidates


def rank_curve_aligned_axes(
    data: MeshData,
    excluded_axis: Axis | None = None,
) -> list[tuple[Axis, int]]:
    """Rank cardinal construction axes by repeated outer sketch-arc evidence."""

    ranked: list[tuple[Axis, int]] = []
    for axis, (_, _, _, axis_index) in AXES.items():
        if axis == excluded_axis:
            continue
        lower, upper = data.mesh.bounds[:, axis_index]
        support = 0
        for fraction in (0.1, 0.3, 0.5, 0.7, 0.9):
            section = section_shape(
                data,
                axis,
                float(lower + (upper - lower) * fraction),
            )
            if section is None or not isinstance(section.outer, PathProfile):
                continue
            support += sum(
                isinstance(segment, ArcSegment)
                for segment in section.outer.segments
            )
        if support:
            ranked.append((axis, support))
    return sorted(ranked, key=lambda item: (-item[1], item[0].value))


def generate_local_tangent_envelope_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    """Replace boundary stair steps with local plane-specific spline sketches.

    A layered construction can be accurate everywhere except a tangent outer
    curve that is normal to another cardinal axis.  Find fitted spline chains
    that touch a section envelope, measure their actual axial face extent from
    the mesh, then repair only a narrow band: add the measured interior and cut
    the measured exterior.  Interior features outside that band are untouched.
    """

    base_axis = getattr(source.base, "axis", None)
    if not isinstance(base_axis, Axis):
        return []
    profile_tolerance = max(data.diagonal * 0.00013, 0.003)
    boundary_tolerance = max(data.diagonal * 0.0015, 0.02)
    inner_margin = max(data.diagonal * 0.015, 0.1)
    outer_margin = max(data.diagonal * 0.002, 0.02)
    boolean_margin = max(data.diagonal * 0.000015, 0.0005)
    candidates: list[ReconstructionPlan] = []

    def geometry_polygons(geometry: object) -> list[Polygon]:
        if isinstance(geometry, Polygon):
            return [geometry]
        if isinstance(geometry, MultiPolygon):
            return list(geometry.geoms)
        return [
            item
            for item in getattr(geometry, "geoms", [])
            if isinstance(item, Polygon)
        ]

    def fitted_region(polygon: Polygon) -> tuple[Profile, list[Profile]] | None:
        outer = _profile_from_ring(
            np.asarray(polygon.exterior.coords),
            profile_tolerance,
        )
        if outer is None:
            return None
        holes: list[Profile] = []
        for interior in polygon.interiors:
            fitted = _profile_from_ring(
                np.asarray(interior.coords),
                profile_tolerance,
            )
            if fitted is None:
                return None
            holes.append(fitted[0])
        return outer[0], holes

    for axis, (_, _, _, axis_index) in AXES.items():
        if axis == base_axis:
            continue
        axial_bounds = data.mesh.bounds[:, axis_index]
        midpoint = float(np.mean(axial_bounds))
        section = section_shape(data, axis, midpoint)
        if section is None:
            continue
        transverse_bounds = section.polygon.bounds
        region_profiles = [section.outer]
        region_profiles.extend(
            region.outer for region in section.additional_regions
        )
        boundary_curves: list[np.ndarray] = []
        for profile in region_profiles:
            if not isinstance(profile, PathProfile):
                continue
            previous = np.asarray(profile.start, dtype=float)
            for segment in profile.segments:
                if isinstance(segment, SplineSegment):
                    curve = np.asarray(
                        [previous, *segment.points, segment.end],
                        dtype=float,
                    )
                    u_span = float(np.ptp(curve[:, 0]))
                    v_span = float(np.ptp(curve[:, 1]))
                    touches_u = min(
                        abs(float(np.min(curve[:, 0])) - transverse_bounds[0]),
                        abs(float(np.max(curve[:, 0])) - transverse_bounds[2]),
                    ) <= boundary_tolerance
                    touches_v = min(
                        abs(float(np.min(curve[:, 1])) - transverse_bounds[1]),
                        abs(float(np.max(curve[:, 1])) - transverse_bounds[3]),
                    ) <= boundary_tolerance
                    if (
                        len(curve) >= 6
                        and max(u_span, v_span) >= data.diagonal * 0.04
                        and ((u_span <= v_span and touches_u) or touches_v)
                    ):
                        boundary_curves.append(curve)
                previous = np.asarray(segment.end, dtype=float)
        if not boundary_curves:
            continue

        plan = source.model_copy(deep=True)
        repaired_count = 0
        projected_centers = _project(data.mesh.triangles_center, axis)
        for curve in boundary_curves:
            curve_line = LineString(curve)
            near_faces = np.asarray(
                [
                    curve_line.distance(Point(point)) <= boundary_tolerance
                    and abs(data.mesh.face_normals[index, axis_index]) <= 0.15
                    for index, point in enumerate(projected_centers)
                ],
                dtype=bool,
            )
            face_indices = np.flatnonzero(near_faces)
            if len(face_indices) < 8:
                continue
            vertex_indices = np.unique(data.mesh.faces[face_indices])
            axial = data.mesh.vertices[vertex_indices, axis_index]
            start = float(np.min(axial))
            end = float(np.max(axial))
            if end - start < data.diagonal * 0.05:
                continue
            # A shallow cap chamfer/fillet makes the main curved wall stop just
            # short of the mesh bound. The parent sketch still owns the curve
            # all the way to the stock end; the finish belongs later in the
            # feature tree. Snap only narrow end gaps so genuine shoulders
            # retain their measured extent.
            cap_tolerance = max(data.diagonal * 0.01, 0.05)
            if start - float(axial_bounds[0]) <= cap_tolerance:
                start = float(axial_bounds[0])
            if float(axial_bounds[1]) - end <= cap_tolerance:
                end = float(axial_bounds[1])

            min_u, min_v, max_u, max_v = transverse_bounds
            curve_min_u, curve_min_v = np.min(curve, axis=0)
            curve_max_u, curve_max_v = np.max(curve, axis=0)
            if float(np.ptp(curve[:, 0])) <= float(np.ptp(curve[:, 1])):
                if abs(curve_min_u - min_u) <= abs(curve_max_u - max_u):
                    strip = box(
                        min_u - outer_margin,
                        min_v - outer_margin,
                        curve_max_u + inner_margin,
                        max_v + outer_margin,
                    )
                else:
                    strip = box(
                        curve_min_u - inner_margin,
                        min_v - outer_margin,
                        max_u + outer_margin,
                        max_v + outer_margin,
                    )
            elif abs(curve_min_v - min_v) <= abs(curve_max_v - max_v):
                strip = box(
                    min_u - outer_margin,
                    min_v - outer_margin,
                    max_u + outer_margin,
                    curve_max_v + inner_margin,
                )
            else:
                strip = box(
                    min_u - outer_margin,
                    curve_min_v - inner_margin,
                    max_u + outer_margin,
                    max_v + outer_margin,
                )

            # Normalize the entire local band to simple oversized stock first,
            # then let one analytic removal define the final curved boundary.
            # Adding only the measured interior left the old layer chords and
            # the new spline coincident in the B-rep; depending on which side a
            # chord fell, both outlines could remain visible.  A full stock
            # band buries every prior approximation before the cut and makes
            # the removal the sole owner of the finished contour.
            for mode, geometry in (
                ("add", strip),
                ("cut", strip.difference(section.polygon)),
            ):
                for polygon in geometry_polygons(geometry):
                    if polygon.area <= profile_tolerance**2:
                        continue
                    fitted = fitted_region(polygon)
                    if fitted is None:
                        continue
                    outer, holes = fitted
                    plan.operations.append(
                        BooleanExtrudeFeature(
                            mode=mode,
                            axis=axis,
                            start=_clean(start - boolean_margin),
                            depth=_clean(
                                end - start + boolean_margin * 2
                            ),
                            outer=outer,
                            holes=holes,
                        )
                    )
            repaired_count += 1

        if repaired_count:
            plan.assumptions.append(
                f"Reconstructed {repaired_count} tangent boundary curves as "
                f"local editable spline sketches on the {axis.value} plane."
            )
            candidates.append(plan)
    return candidates


def generate_segmented_smooth_layer_candidates(
    data: MeshData,
    name: str,
    axis: Axis,
    sample_count: int = 48,
    layer_budget: int = 20,
) -> list[ReconstructionPlan]:
    """Replace changing extrusion slabs with continuous, topology-safe lofts.

    The ordinary weighted-layer fallback is deliberately conservative, but a
    stack of constant sections leaves stair steps on a tangent or tapered
    surface.  This variant samples the same measured sections, separates true
    topology changes from continuous shape changes, and lofts only within the
    continuous runs.  Circular openings are emitted as independent axial hole
    features so their walls remain continuous instead of inheriting a slightly
    different radius from every section.
    """

    _, _, _, axis_index = AXES[axis]
    axis_start, axis_end = data.mesh.bounds[:, axis_index]
    probe_levels = np.linspace(axis_start, axis_end, sample_count + 1)
    probe_midpoints = (probe_levels[:-1] + probe_levels[1:]) / 2.0
    probe_sections: list[SectionShape] = []
    for midpoint in probe_midpoints:
        section = section_shape(data, axis, float(midpoint))
        if section is None:
            return []
        probe_sections.append(section)

    mean_area = float(np.mean([section.area for section in probe_sections]))
    baseline = mean_area * 0.003
    changes = np.asarray(
        [
            section.polygon.symmetric_difference(
                probe_sections[min(index + 1, len(probe_sections) - 1)].polygon
            ).area
            + baseline
            for index, section in enumerate(probe_sections)
        ],
        dtype=float,
    )
    cumulative = np.r_[0.0, np.cumsum(changes)]
    indices = [
        int(np.argmin(np.abs(cumulative - target)))
        for target in np.linspace(0.0, float(cumulative[-1]), layer_budget + 1)
    ]
    indices[0] = 0
    indices[-1] = sample_count
    indices = sorted(set(indices))
    levels = probe_levels[indices]
    if len(levels) < 4:
        return []

    sections: list[SectionShape] = []
    midpoints: list[float] = []
    for start, end in zip(levels, levels[1:], strict=False):
        midpoint = float((start + end) / 2.0)
        probe_index = int(np.argmin(np.abs(probe_midpoints - midpoint)))
        sections.append(probe_sections[probe_index])
        midpoints.append(midpoint)

    # A discontinuity remains a sharp extrusion boundary.  Smooth lofts are
    # restricted to runs whose connected-region topology is stable and whose
    # adjacent section change is small enough to represent a tangent surface.
    split_before = {0}
    for index in range(1, len(sections)):
        previous = sections[index - 1]
        current = sections[index]
        shape_change = previous.polygon.symmetric_difference(current.polygon).area
        reference_area = max(previous.area, current.area, 1e-9)
        if (
            len(previous.additional_regions) != len(current.additional_regions)
            or shape_change / reference_area > 0.16
        ):
            split_before.add(index)
    group_starts = sorted(split_before)
    groups = [
        (start, group_starts[index + 1] - 1)
        if index + 1 < len(group_starts)
        else (start, len(sections) - 1)
        for index, start in enumerate(group_starts)
    ]

    first_midpoint = midpoints[0]
    base = ExtrudeFeature(
        axis=axis,
        start=float(levels[0]),
        depth=max(first_midpoint - float(levels[0]), 1e-6),
        outer=sections[0].outer,
    )
    operations: list[
        BooleanExtrudeFeature | RoundHoleFeature | TaperedAddFeature
    ] = []
    smooth_run_count = 0
    for group_start, group_end in groups:
        boundary_start = float(levels[group_start])
        boundary_end = float(levels[group_end + 1])
        first = midpoints[group_start]
        last = midpoints[group_end]
        if group_start > 0 and first > boundary_start + 1e-7:
            operations.append(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=boundary_start,
                    depth=first - boundary_start,
                    outer=sections[group_start].outer,
                )
            )
        if group_end > group_start:
            intermediate_indices = list(range(group_start + 1, group_end))
            # Equal-count measured boundary points give OCCT compatible loft
            # wires even when fitted source profiles contain different counts
            # of lines and arcs. Phase/orientation alignment prevents twist;
            # dense points retain tangent arcs without rounding sharp corners.
            loft_profiles: list[PolygonProfile] = []
            previous_points: np.ndarray | None = None
            point_count = 96
            for section_index in range(group_start, group_end + 1):
                polygon = sections[section_index].polygon
                if isinstance(polygon, MultiPolygon):
                    polygon = max(polygon.geoms, key=lambda item: item.area)
                points = np.asarray(
                    [
                        polygon.exterior.interpolate(
                            point_index / point_count,
                            normalized=True,
                        ).coords[0]
                        for point_index in range(point_count)
                    ],
                    dtype=float,
                )
                if previous_points is not None:
                    variants = (
                        np.roll(oriented, shift, axis=0)
                        for oriented in (points, points[::-1])
                        for shift in range(point_count)
                    )
                    points = min(
                        variants,
                        key=lambda item: float(
                            np.mean((item - previous_points) ** 2)
                        ),
                    )
                previous_points = points
                loft_profiles.append(
                    PolygonProfile(
                        points=[
                            (_clean(point[0]), _clean(point[1]))
                            for point in points
                        ]
                    )
                )
            operations.append(
                TaperedAddFeature(
                    axis=axis,
                    start=first - max(data.diagonal * 2e-6, 0.0001),
                    depth=(
                        last
                        - first
                        + 2 * max(data.diagonal * 2e-6, 0.0001)
                    ),
                    start_outer=loft_profiles[0],
                    end_outer=loft_profiles[-1],
                    intermediate_offsets=[
                        midpoints[index]
                        - first
                        + max(data.diagonal * 2e-6, 0.0001)
                        for index in intermediate_indices
                    ],
                    intermediate_profiles=loft_profiles[1:-1],
                    smooth=True,
                )
            )
            smooth_run_count += 1
        if last < boundary_end - 1e-7:
            operations.append(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=last,
                    depth=boundary_end - last,
                    outer=sections[group_end].outer,
                )
            )

    # Preserve disconnected islands without asking a loft to change topology.
    for index, section in enumerate(sections):
        start = float(levels[index])
        depth = float(levels[index + 1] - levels[index])
        for region in section.additional_regions:
            operations.append(
                BooleanExtrudeFeature(
                    mode="add",
                    axis=axis,
                    start=start,
                    depth=depth,
                    outer=region.outer,
                    holes=region.holes,
                )
            )

    # Track circular section openings by centre.  Consecutive observations
    # with a stable radius become one editable hole; a real counterbore or
    # taper naturally starts a new axial interval.
    center_tolerance = max(data.diagonal * 0.002, 0.025)
    radius_tolerance = max(data.diagonal * 0.001, 0.012)
    tracks: list[list[tuple[int, CircleProfile]]] = []
    for section_index, section in enumerate(sections):
        used_tracks: set[int] = set()
        for hole in section.holes:
            if not isinstance(hole, CircleProfile):
                operations.append(
                    BooleanExtrudeFeature(
                        mode="cut",
                        axis=axis,
                        start=float(levels[section_index]),
                        depth=float(
                            levels[section_index + 1] - levels[section_index]
                        ),
                        outer=hole,
                    )
                )
                continue
            circle = hole
            match_index = next(
                (
                    index
                    for index, track in enumerate(tracks)
                    if index not in used_tracks
                    and track[-1][0] == section_index - 1
                    and np.linalg.norm(
                        np.asarray(track[-1][1].center)
                        - np.asarray(circle.center)
                    )
                    <= center_tolerance
                ),
                None,
            )
            if match_index is None:
                tracks.append([(section_index, circle)])
                used_tracks.add(len(tracks) - 1)
            else:
                tracks[match_index].append((section_index, circle))
                used_tracks.add(match_index)

    for track in tracks:
        run: list[tuple[int, CircleProfile]] = []
        runs: list[list[tuple[int, CircleProfile]]] = []
        for observation in track:
            if run and abs(observation[1].radius - run[-1][1].radius) > radius_tolerance:
                runs.append(run)
                run = []
            run.append(observation)
        if run:
            runs.append(run)
        for radius_run in runs:
            first_index = radius_run[0][0]
            last_index = radius_run[-1][0]
            center = np.median(
                np.asarray([item[1].center for item in radius_run], dtype=float),
                axis=0,
            )
            radius = float(np.median([item[1].radius for item in radius_run]))
            operations.append(
                RoundHoleFeature(
                    axis=axis,
                    center=(_clean(center[0]), _clean(center[1])),
                    diameter=_clean(radius * 2.0),
                    start=float(levels[first_index]),
                    depth=float(levels[last_index + 1] - levels[first_index]),
                    through=(first_index == 0 and last_index == len(sections) - 1),
                )
            )

    if smooth_run_count == 0:
        return []
    return [
        ReconstructionPlan(
            name=name,
            base=base,
            operations=operations,
            assumptions=[
                f"Recovered {smooth_run_count} continuously changing "
                f"{axis.value}-axis profile runs as editable tangent lofts.",
                "Kept measured topology changes sharp and promoted section "
                "openings to continuous analytic hole features.",
            ],
        )
    ]


def generate_smooth_loft_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Approximate a smooth changing silhouette with one editable loft.

    Dense extrusion slabs are a useful fallback, but they are not a clean
    feature tree.  A multi-section OCCT loft preserves the measured curved
    envelope as one feature and is especially effective for large end rounds
    whose sections change continuously along a cardinal extrusion axis.
    """

    fractions = (
        0.0005,
        0.02,
        0.05,
        0.1,
        0.18,
        0.28,
        0.4,
        0.52,
        0.64,
        0.75,
        0.84,
        0.91,
        0.96,
        0.985,
        0.9995,
    )
    plans: list[ReconstructionPlan] = []
    for axis, (_, _, _, axis_index) in AXES.items():
        lower, upper = data.mesh.bounds[:, axis_index]
        span = float(upper - lower)
        if span <= max(data.diagonal * 1e-6, 0.0001):
            continue
        sampled: list[tuple[float, SectionShape]] = []
        valid = True
        hole_count: int | None = None
        for fraction in fractions:
            position = float(lower + span * fraction)
            section = section_shape(data, axis, position)
            if (
                section is None
                or section.additional_regions
            ):
                valid = False
                break
            if hole_count is None:
                hole_count = len(section.holes)
            elif len(section.holes) != hole_count:
                valid = False
                break
            sampled.append((position, section))
        if not valid:
            continue
        areas = np.asarray([section.area for _, section in sampled])
        if float(np.ptp(areas)) / max(float(np.max(areas)), 1e-9) < 0.02:
            continue

        first_position, first_section = sampled[0]
        loft_start = first_position
        loft_end = float(upper)
        intermediate = sampled[1:-1]
        operation = TaperedAddFeature(
            axis=axis,
            start=loft_start,
            depth=loft_end - loft_start,
            start_outer=first_section.outer,
            end_outer=sampled[-1][1].outer,
            intermediate_offsets=[
                position - loft_start for position, _ in intermediate
            ],
            intermediate_profiles=[
                section.outer for _, section in intermediate
            ],
            smooth=True,
            holes=first_section.holes,
        )
        plans.append(
            ReconstructionPlan(
                name=name,
                base=ExtrudeFeature(
                    axis=axis,
                    start=float(lower),
                    depth=first_position - float(lower),
                    outer=first_section.outer,
                    holes=first_section.holes,
                ),
                operations=[operation],
                assumptions=[
                    f"Recovered a continuously changing {axis.value}-axis "
                    "silhouette as one editable multi-section smooth loft.",
                    "Loft sections were sampled densely near both end caps and "
                    "verified against the source mesh.",
                ],
            )
        )
        if hole_count == 0:
            point_count = 48
            aligned_points: list[np.ndarray] = []
            previous_points: np.ndarray | None = None
            for _, section in sampled:
                points = np.asarray(
                    [
                        section.polygon.exterior.interpolate(
                            index / point_count,
                            normalized=True,
                        ).coords[0]
                        for index in range(point_count)
                    ],
                    dtype=float,
                )
                if previous_points is not None:
                    variants = [
                        np.roll(oriented, shift, axis=0)
                        for oriented in (points, points[::-1])
                        for shift in range(point_count)
                    ]
                    points = min(
                        variants,
                        key=lambda item: float(
                            np.mean((item - previous_points) ** 2)
                        ),
                    )
                aligned_points.append(points)
                previous_points = points
            spline_profiles = [
                SplineProfile(
                    points=[
                        (float(point[0]), float(point[1]))
                        for point in points
                    ]
                )
                for points in aligned_points
            ]
            plans.append(
                ReconstructionPlan(
                    name=name,
                    base=ExtrudeFeature(
                        axis=axis,
                        start=float(lower),
                        depth=first_position - float(lower) + span * 0.0001,
                        # Keep the measured line/arc end section analytic. The
                        # small overlap with the equal-parameter spline loft
                        # prevents a tolerance gap at their shared interface.
                        outer=first_section.outer,
                    ),
                    operations=[
                        TaperedAddFeature(
                            axis=axis,
                            start=loft_start,
                            depth=loft_end - loft_start,
                            start_outer=spline_profiles[0],
                            end_outer=spline_profiles[-1],
                            intermediate_offsets=[
                                position - loft_start
                                for position, _ in intermediate
                            ],
                            intermediate_profiles=spline_profiles[1:-1],
                            smooth=True,
                        )
                    ],
                    assumptions=[
                        f"Recovered a continuously changing {axis.value}-axis "
                        "silhouette as one editable multi-section smooth loft.",
                        "Aligned equal-parameter spline sections prevent profile "
                        "edge-count changes from twisting the compact loft.",
                    ],
                )
            )
    return plans


def generate_segmented_multi_region_loft_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Compact changing disconnected sections into topology-stable loft runs."""

    plans: list[ReconstructionPlan] = []
    point_count = 32
    for axis, (_, _, _, axis_index) in AXES.items():
        lower, upper = data.mesh.bounds[:, axis_index]
        span = float(upper - lower)
        positions = np.linspace(
            lower + span * 0.005,
            upper - span * 0.005,
            25,
        )
        sampled: list[tuple[float, list[Polygon]]] = []
        has_multiple = False
        valid = True
        for position in positions:
            section = section_shape(data, axis, float(position))
            if section is None or section.holes:
                valid = False
                break
            geometry = section.polygon
            components = (
                list(geometry.geoms)
                if isinstance(geometry, MultiPolygon)
                else [geometry]
            )
            components = sorted(
                components,
                key=lambda item: (item.centroid.y, item.centroid.x),
            )
            has_multiple |= len(components) > 1
            sampled.append((float(position), components))
        if not valid or not has_multiple:
            continue

        runs: list[list[tuple[float, list[Polygon]]]] = []
        for item in sampled:
            if not runs or len(runs[-1][-1][1]) != len(item[1]):
                runs.append([])
            runs[-1].append(item)
        if sum(len(run[0][1]) for run in runs) > 10:
            continue

        def aligned_splines(polygons: list[Polygon]) -> list[SplineProfile]:
            result: list[SplineProfile] = []
            previous: np.ndarray | None = None
            for polygon in polygons:
                points = np.asarray(
                    [
                        polygon.exterior.interpolate(
                            index / point_count,
                            normalized=True,
                        ).coords[0]
                        for index in range(point_count)
                    ],
                    dtype=float,
                )
                if previous is not None:
                    variants = [
                        np.roll(oriented, shift, axis=0)
                        for oriented in (points, points[::-1])
                        for shift in range(point_count)
                    ]
                    points = min(
                        variants,
                        key=lambda points_option: float(
                            np.mean((points_option - previous) ** 2)
                        ),
                    )
                result.append(
                    SplineProfile(
                        points=[
                            (float(point[0]), float(point[1]))
                            for point in points
                        ]
                    )
                )
                previous = points
            return result

        first_position, first_components = sampled[0]
        first_profiles = [
            aligned_splines([component])[0]
            for component in first_components
        ]
        operations: list[TaperedAddFeature] = []
        for run_index, run in enumerate(runs):
            if run_index == 0:
                start_position = run[0][0]
            else:
                start_position = (
                    runs[run_index - 1][-1][0] + run[0][0]
                ) / 2.0
            if run_index + 1 < len(runs):
                end_position = (run[-1][0] + runs[run_index + 1][0][0]) / 2.0
            else:
                end_position = float(upper)
            component_count = len(run[0][1])
            for component_index in range(component_count):
                profiles = aligned_splines(
                    [components[component_index] for _, components in run]
                )
                intermediate_items = run[1:] if run_index == 0 else run
                intermediate_profile_items = (
                    profiles[1:] if run_index == 0 else profiles
                )
                intermediate_offsets = [
                    position - start_position
                    for position, _ in intermediate_items
                ]
                operations.append(
                    TaperedAddFeature(
                        axis=axis,
                        start=start_position,
                        depth=end_position - start_position,
                        start_outer=profiles[0],
                        end_outer=profiles[-1],
                        intermediate_offsets=intermediate_offsets,
                        intermediate_profiles=intermediate_profile_items,
                        smooth=True,
                    )
                )
        plans.append(
            ReconstructionPlan(
                name=name,
                base=ExtrudeFeature(
                    axis=axis,
                    start=float(lower),
                    depth=first_position - float(lower),
                    outer=first_profiles[0],
                    additional_regions=first_profiles[1:],
                ),
                operations=operations,
                assumptions=[
                    f"Compacted changing disconnected {axis.value}-axis sections "
                    "into topology-stable multi-region smooth loft runs."
                ],
            )
        )
    return plans


def generate_profiled_endcap_cylinder_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Recover a constant extrusion with shaped ends and a cross-axis cut.

    A through-notch can split only the middle cardinal sections into two
    regions.  Treating those regions as independent lofts creates excessive
    material where the notch opens.  Instead, recover the uncut single-region
    sketch, reconstruct the two measured end transitions, and subtract the
    fitted analytic cylinder that caused the topology change.
    """

    fitted_cuts = [
        feature
        for feature in _mesh_cylindrical_features(data)
        if feature.mode == "cut" and feature.inner_radius is None
    ]
    if not fitted_cuts:
        return []

    plans: list[ReconstructionPlan] = []
    fractions = np.linspace(0.005, 0.995, 33)
    point_count = 64
    for axis, (normal, _, _, axis_index) in AXES.items():
        cross_axis_cuts = [
            feature
            for feature in fitted_cuts
            if abs(float(np.dot(normal, feature.direction))) < 0.01
        ]
        if not cross_axis_cuts:
            continue
        lower, upper = map(float, data.mesh.bounds[:, axis_index])
        span = upper - lower
        if span <= max(data.diagonal * 1e-6, 0.0001):
            continue

        sampled: list[tuple[float, SectionShape]] = []
        for fraction in fractions:
            position = lower + span * float(fraction)
            section = section_shape(data, axis, position)
            if section is None or section.holes:
                sampled = []
                break
            sampled.append((position, section))
        if not sampled:
            continue
        component_counts = [1 + len(section.additional_regions) for _, section in sampled]
        multi_indices = [
            index for index, count in enumerate(component_counts) if count > 1
        ]
        if (
            not multi_indices
            or min(multi_indices) == 0
            or max(multi_indices) == len(sampled) - 1
            or any(
                component_counts[index] != 1
                for index in range(min(multi_indices))
            )
            or any(
                component_counts[index] != 1
                for index in range(max(multi_indices) + 1, len(sampled))
            )
        ):
            continue

        single_samples = [
            (position, section)
            for position, section in sampled
            if not section.additional_regions
        ]
        full_position, full_section = max(
            single_samples,
            key=lambda item: item[1].area,
        )
        full_area = full_section.area
        end_area = min(sampled[0][1].area, sampled[-1][1].area)
        if end_area >= full_area * 0.98:
            continue
        plateau_tolerance = max(full_area * 0.0002, data.diagonal**2 * 1e-7)
        left_plateau = next(
            (
                position
                for position, section in sampled[: min(multi_indices)]
                if full_area - section.area <= plateau_tolerance
            ),
            None,
        )
        right_plateau = next(
            (
                position
                for position, section in reversed(sampled[max(multi_indices) + 1 :])
                if full_area - section.area <= plateau_tolerance
            ),
            None,
        )
        if left_plateau is None or right_plateau is None:
            continue

        def section_area(position: float, section_axis: Axis = axis) -> float:
            section = section_shape(data, section_axis, position)
            return section.area if section is not None else 0.0

        left_low = lower + span * 0.005
        left_high = float(left_plateau)
        for _ in range(12):
            midpoint = (left_low + left_high) / 2.0
            if full_area - section_area(midpoint) <= plateau_tolerance:
                left_high = midpoint
            else:
                left_low = midpoint
        left_transition = left_high

        right_low = float(right_plateau)
        right_high = upper - span * 0.005
        for _ in range(12):
            midpoint = (right_low + right_high) / 2.0
            if full_area - section_area(midpoint) <= plateau_tolerance:
                right_low = midpoint
            else:
                right_high = midpoint
        right_transition = right_low
        if (
            left_transition - lower <= span * 0.01
            or upper - right_transition <= span * 0.01
            or right_transition <= left_transition
        ):
            continue

        epsilon = max(span * 0.00125, 0.001)
        overlap = max(span * 0.0005, 0.0005)
        left_end = section_shape(data, axis, lower + epsilon)
        right_end = section_shape(data, axis, upper - epsilon)
        if (
            left_end is None
            or right_end is None
            or left_end.additional_regions
            or right_end.additional_regions
        ):
            continue

        def ring_points(section: SectionShape) -> np.ndarray:
            return np.asarray(
                [
                    section.polygon.exterior.interpolate(
                        index / point_count,
                        normalized=True,
                    ).coords[0]
                    for index in range(point_count)
                ],
                dtype=float,
            )

        full_points = ring_points(full_section)

        def aligned_profile(
            section: SectionShape,
            reference_points: np.ndarray = full_points,
        ) -> Profile:
            if isinstance(section.outer, CircleProfile):
                return section.outer.model_copy(deep=True)
            points = ring_points(section)
            variants = [
                np.roll(oriented, shift, axis=0)
                for oriented in (points, points[::-1])
                for shift in range(point_count)
            ]
            aligned = min(
                variants,
                key=lambda item: float(np.mean((item - reference_points) ** 2)),
            )
            return PolygonProfile(
                points=[tuple(map(float, point)) for point in aligned]
            )

        full_profile: Profile = (
            full_section.outer.model_copy(deep=True)
            if isinstance(full_section.outer, CircleProfile)
            else PolygonProfile(
                points=[tuple(map(float, point)) for point in full_points]
            )
        )
        operations: list[
            TaperedAddFeature | OrientedCylinderFeature | EdgeFinishFeature
        ] = [
            TaperedAddFeature(
                axis=axis,
                start=lower + epsilon,
                depth=left_transition - lower,
                start_outer=aligned_profile(left_end),
                end_outer=full_profile,
                smooth=False,
            ),
            TaperedAddFeature(
                axis=axis,
                start=right_transition - overlap,
                depth=upper - right_transition,
                start_outer=full_profile,
                end_outer=aligned_profile(right_end),
                smooth=False,
            ),
        ]
        operations.extend(
            feature.model_copy(deep=True) for feature in cross_axis_cuts
        )

        # The measured end transition contains circular chamfer strips, but a
        # portable equal-point loft is exported as B-spline faces. Preserve a
        # minute analytic conical strip on the already-detected circular cut;
        # this keeps the STEP topology honest without affecting fit tolerance.
        if isinstance(full_section.outer, PathProfile) and any(
            isinstance(segment, ArcSegment)
            for segment in full_section.outer.segments
        ):
            cutter_index = 2
            cutter = cross_axis_cuts[0]
            direction = np.asarray(cutter.direction, dtype=float)
            cardinal_index = int(np.argmax(np.abs(direction)))
            if abs(direction[cardinal_index]) >= 0.999:
                cutter_axis = (Axis.X, Axis.Y, Axis.Z)[cardinal_index]
                cutter_center = _project(
                    np.asarray(cutter.origin, dtype=float).reshape(1, 3),
                    cutter_axis,
                )[0]
                operations.append(
                    EdgeFinishFeature(
                        mode="chamfer",
                        axis=cutter_axis,
                        end="start",
                        size=min(
                            cutter.radius * 0.002,
                            max(data.diagonal * 0.00004, 0.001),
                        ),
                        selector="circle",
                        center=(
                            float(cutter_center[0]),
                            float(cutter_center[1]),
                        ),
                        radius=float(cutter.radius),
                        feature_index=cutter_index,
                    )
                )

        plans.append(
            ReconstructionPlan(
                name=name,
                base=ExtrudeFeature(
                    axis=axis,
                    start=left_transition - overlap,
                    depth=right_transition - left_transition + 2 * overlap,
                    outer=full_section.outer,
                    holes=full_section.holes,
                ),
                operations=operations,
                assumptions=[
                    f"Recovered the uncut constant {axis.value}-axis sketch, "
                    "two measured end transitions, and the fitted cross-axis "
                    "cylindrical cut as separate editable features."
                ],
            )
        )
    return plans


def generate_end_finish_candidates(
    data: MeshData,
    sources: list[PlanCandidate],
) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    fractions = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2)
    for source in sources:
        # A finish can change a surprisingly large fraction of a thin part's
        # end silhouette. Requiring near-perfect end/middle agreement excluded
        # exactly the chamfered plates and fully rounded extrusion edges this
        # search is meant to recover.
        if source.section_consistency < 0.65:
            continue
        base = source.plan.base
        additive_extents = [
            (operation.start, operation.start + operation.depth)
            for operation in source.plan.operations
            if isinstance(operation, BooleanExtrudeFeature)
            and operation.mode == "add"
            and operation.axis == base.axis
        ]
        plan_start = min([base.start, *(extent[0] for extent in additive_extents)])
        plan_end = max(
            [base.start + base.depth, *(extent[1] for extent in additive_extents)]
        )
        plan_depth = plan_end - plan_start
        internal_boundaries = sorted(
            {
                value
                for extent in [(base.start, base.start + base.depth), *additive_extents]
                for value in extent
                if plan_start + 0.001 < value < plan_end - 0.001
            }
        )

        observations: dict[str, list[tuple[float, Polygon]]] = {}
        references: dict[str, Polygon] = {}
        transitions: dict[str, float] = {}
        for end in ("start", "end"):
            layer_depth = plan_depth
            if internal_boundaries:
                layer_depth = (
                    internal_boundaries[0] - plan_start
                    if end == "start"
                    else plan_end - internal_boundaries[-1]
                )
            reference_distance = max(layer_depth * 0.5, plan_depth * 0.001)
            reference_location = (
                plan_start + reference_distance
                if end == "start"
                else plan_end - reference_distance
            )
            reference = section_shape(data, base.axis, reference_location)
            if reference is None:
                continue
            transition = 0.0
            samples: list[tuple[float, Polygon]] = []
            distances = {
                min(plan_depth * fraction, reference_distance)
                for fraction in fractions
                if plan_depth * fraction <= reference_distance
            }
            distances.add(reference_distance)
            for distance in sorted(distances):
                location = (
                    plan_start + distance
                    if end == "start"
                    else plan_end - distance
                )
                section = section_shape(data, base.axis, location)
                if section is None:
                    continue
                samples.append((distance, section.polygon))
                if _polygon_similarity(section.polygon, reference.polygon) < 0.9995:
                    transition = distance
            if transition > 0:
                observations[end] = samples
                references[end] = reference.polygon
                transitions[end] = transition
        if not observations:
            continue

        proposals: dict[str, list[tuple[float, str, float]]] = {}
        # A fully rounded end legitimately reaches half the stock thickness.
        # Keeping a 1% margin forced near-correct fillets to miss the geometry
        # gate on thin parts. Invalid limiting-radius candidates are already
        # rejected safely by the normal CAD build-and-score path.
        maximum = min(plan_depth * 0.5, min(data.mesh.extents) * 0.5)
        for end, samples in observations.items():
            reference_polygon = references[end]
            estimated = transitions[end]
            sizes = {
                _clean(min(estimated * factor, maximum))
                for factor in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5)
                if min(estimated * factor, maximum) > max(data.diagonal * 0.001, 0.01)
            }
            ranked: list[tuple[float, str, float]] = []
            for mode in ("fillet", "chamfer"):
                for size in sorted(sizes):
                    errors: list[float] = []
                    for distance, observed in samples:
                        if distance >= size:
                            inset = 0.0
                        elif mode == "chamfer":
                            inset = size - distance
                        else:
                            inset = size - math.sqrt(
                                max(size * size - (distance - size) ** 2, 0.0)
                            )
                        predicted = (
                            reference_polygon.buffer(-inset)
                            if inset > data.diagonal * 1e-7
                            else reference_polygon
                        )
                        errors.append(1.0 - _polygon_similarity(predicted, observed))
                    ranked.append(
                        (
                            float(np.mean(errors)) if errors else float("inf"),
                            mode,
                            size,
                        )
                    )
            end_proposals = [
                min(mode_proposals, key=lambda proposal: proposal[0])
                for mode in ("fillet", "chamfer")
                if (
                    mode_proposals := [
                        proposal for proposal in ranked if proposal[1] == mode
                    ]
                )
            ]
            if end_proposals:
                proposals[end] = end_proposals

        # Always retain one-ended alternatives. A tiny tessellation ripple on
        # an otherwise flat end can create a second observation; requiring
        # both inferred finishes then turns a true one-fillet part into either
        # the wrong solid or a many-slab fallback.
        for end, end_proposals in proposals.items():
            for fit_error, mode, size in end_proposals:
                plan = source.plan.model_copy(deep=True)
                plan.operations.append(
                    EdgeFinishFeature(
                        mode=mode,
                        axis=base.axis,
                        end=end,
                        size=size,
                    )
                )
                plan.assumptions.append(
                    f"Fitted a {size:g} mm {mode} on {end} transverse edges "
                    f"(section residual {fit_error:.4g})."
                )
                candidates.append(
                    PlanCandidate(
                        plan=plan,
                        heuristic_score=(
                            source.heuristic_score + 0.001 + fit_error * 0.1
                        ),
                        section_consistency=source.section_consistency,
                    )
                )

        if "start" in proposals and "end" in proposals:
            for start_proposal in proposals["start"]:
                for end_proposal in proposals["end"]:
                    plan = source.plan.model_copy(deep=True)
                    combined_error = start_proposal[0] + end_proposal[0]
                    for end, proposal in (
                        ("start", start_proposal),
                        ("end", end_proposal),
                    ):
                        _, mode, size = proposal
                        plan.operations.append(
                            EdgeFinishFeature(
                                mode=mode,
                                axis=base.axis,
                                end=end,
                                size=size,
                            )
                        )
                    plan.assumptions.append(
                        "Fitted independent transverse edge finishes at both ends."
                    )
                    candidates.append(
                        PlanCandidate(
                            plan=plan,
                            heuristic_score=(
                                source.heuristic_score + 0.002 + combined_error * 0.1
                            ),
                            section_consistency=source.section_consistency,
                        )
                    )
    return candidates


def _arc_midpoint_for_radius(
    start: np.ndarray,
    original_midpoint: np.ndarray,
    end: np.ndarray,
    radius: float,
) -> tuple[float, float] | None:
    """Move an arc midpoint while preserving its endpoints and sweep side."""

    fitted = _circle_values(
        np.asarray([start, original_midpoint, end], dtype=float)
    )
    chord = end - start
    chord_length = float(np.linalg.norm(chord))
    if (
        fitted is None
        or chord_length <= 1e-9
        or chord_length >= radius * 2.0
    ):
        return None
    chord_midpoint = (start + end) / 2.0
    perpendicular = np.asarray([-chord[1], chord[0]]) / chord_length
    center_offset = math.sqrt(
        max(radius**2 - (chord_length / 2.0) ** 2, 0.0)
    )
    possible_centers = (
        chord_midpoint + perpendicular * center_offset,
        chord_midpoint - perpendicular * center_offset,
    )
    original_center = np.asarray(fitted[:2])
    center = min(
        possible_centers,
        key=lambda item: float(np.linalg.norm(item - original_center)),
    )
    original_angles = np.unwrap(
        [
            math.atan2(
                start[1] - original_center[1],
                start[0] - original_center[0],
            ),
            math.atan2(
                original_midpoint[1] - original_center[1],
                original_midpoint[0] - original_center[0],
            ),
            math.atan2(
                end[1] - original_center[1],
                end[0] - original_center[0],
            ),
        ]
    )
    start_angle = math.atan2(
        start[1] - center[1],
        start[0] - center[0],
    )
    end_angle = math.atan2(
        end[1] - center[1],
        end[0] - center[0],
    )
    counterclockwise = (end_angle - start_angle) % (2 * math.pi)
    sweep = (
        counterclockwise
        if original_angles[-1] > original_angles[0]
        else counterclockwise - 2 * math.pi
    )
    midpoint = center + radius * np.asarray(
        [
            math.cos(start_angle + sweep / 2.0),
            math.sin(start_angle + sweep / 2.0),
        ]
    )
    return float(midpoint[0]), float(midpoint[1])


def generate_spherical_corner_finish_candidates(
    data: MeshData,
    plan: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    """Build equal-radius profile/end fillets that retain analytic spheres.

    A rolling-ball fillet around every edge of an extruded sketch produces a
    spherical corner where a sketch arc and an end fillet have the same
    radius.  Mesh fitting perturbs the two independently by a few hundredths
    of a millimetre, which makes OCCT export a generic surface of revolution
    instead.  This candidate restores the measured equal-radius constraint
    while keeping the endpoints of the fitted sketch arc fixed.
    """

    base = plan.base
    if not isinstance(base, ExtrudeFeature) or not isinstance(
        base.outer, PathProfile
    ):
        return []

    source_plan = plan.model_copy(deep=True)
    source_plan.operations = [
        operation
        for operation in source_plan.operations
        if not isinstance(operation, EdgeFinishFeature)
    ]
    baseline_count = len(source_plan.operations)
    finish_plans = generate_end_finish_candidates(
        data,
        [
            PlanCandidate(
                plan=source_plan,
                heuristic_score=0.0,
                section_consistency=1.0,
            )
        ],
    )
    candidates: list[ReconstructionPlan] = []
    for finish_candidate in finish_plans:
        additions = finish_candidate.plan.operations[baseline_count:]
        if (
            len(additions) != 2
            or not all(isinstance(item, EdgeFinishFeature) for item in additions)
            or not all(item.mode == "fillet" for item in additions)
            or {item.end for item in additions} != {"start", "end"}
        ):
            continue
        finishes = [item for item in additions if isinstance(item, EdgeFinishFeature)]
        shared_radius = float(np.mean([item.size for item in finishes]))
        if max(abs(item.size - shared_radius) for item in finishes) > max(
            shared_radius * 0.05,
            data.diagonal * 0.0005,
        ):
            continue
        # End-section sampling can underestimate a small rolling-ball radius
        # when the first slice already lies inside the curved transition. A
        # fitted spherical patch measures that radius directly. Prefer the
        # nearest detected sphere when it agrees with the section estimate;
        # this also restores the equal-radius constraint across every rounded
        # sketch corner instead of matching only one of them.
        spherical_radii = [
            feature.radius for feature in _mesh_spherical_features(data)
        ]
        if spherical_radii:
            spherical_radius = min(
                spherical_radii,
                key=lambda radius: abs(radius - shared_radius),
            )
            if abs(spherical_radius - shared_radius) <= max(
                shared_radius * 0.15,
                data.diagonal * 0.0005,
            ):
                shared_radius = spherical_radius
        if shared_radius <= max(data.diagonal * 1e-5, 0.001):
            continue

        profile = finish_candidate.plan.base.outer
        assert isinstance(profile, PathProfile)
        start = np.asarray(profile.start, dtype=float)
        matches: list[tuple[float, int, tuple[float, float]]] = []
        for segment_index, segment in enumerate(profile.segments):
            end = np.asarray(segment.end, dtype=float)
            if isinstance(segment, ArcSegment):
                fitted = _circle_values(
                    np.asarray([start, segment.mid, end], dtype=float)
                )
                chord = end - start
                chord_length = float(np.linalg.norm(chord))
                midpoint = _arc_midpoint_for_radius(
                    start,
                    np.asarray(segment.mid, dtype=float),
                    end,
                    shared_radius,
                )
                if (
                    fitted is not None
                    and chord_length > 1e-9
                    and chord_length < shared_radius * 2.0
                    and abs(fitted[2] - shared_radius) / shared_radius <= 0.12
                    and midpoint is not None
                ):
                    matches.append(
                        (
                            abs(fitted[2] - shared_radius) / shared_radius,
                            segment_index,
                            midpoint,
                        )
                    )
            start = end

        numerical_step = min(
            max(
                data.diagonal * 0.00005,
                shared_radius * 0.005,
                0.0005,
            ),
            shared_radius * 0.015,
        )
        radius_variants = list(
            dict.fromkeys(
                _clean(radius)
                for radius in (
                    shared_radius,
                    shared_radius + numerical_step,
                    shared_radius - numerical_step,
                )
                if radius > 0
            )
        )
        selected_matches = sorted(matches)[:12]
        for radius in radius_variants:
            candidate = finish_candidate.plan.model_copy(deep=True)
            candidate_base = candidate.base
            assert isinstance(candidate_base, ExtrudeFeature)
            candidate_profile = candidate_base.outer
            assert isinstance(candidate_profile, PathProfile)
            adjusted_count = 0
            for _, segment_index, midpoint in selected_matches:
                candidate_segment = candidate_profile.segments[segment_index]
                assert isinstance(candidate_segment, ArcSegment)
                segment_start = np.asarray(
                    candidate_profile.start
                    if segment_index == 0
                    else candidate_profile.segments[segment_index - 1].end,
                    dtype=float,
                )
                adjusted_midpoint = (
                    midpoint
                    if radius == _clean(shared_radius)
                    else _arc_midpoint_for_radius(
                        segment_start,
                        np.asarray(candidate_segment.mid, dtype=float),
                        np.asarray(candidate_segment.end, dtype=float),
                        radius,
                    )
                )
                if adjusted_midpoint is None:
                    continue
                candidate_segment.mid = adjusted_midpoint
                adjusted_count += 1
            if adjusted_count == 0:
                continue
            for operation in candidate.operations[baseline_count:]:
                assert isinstance(operation, EdgeFinishFeature)
                operation.size = radius
                operation.feature_index = -1
            candidate.assumptions.append(
                f"Matched {adjusted_count} sketch-corner arcs to the measured "
                "end-fillet radius so the rolling-ball corners remain analytic "
                "spheres."
            )
            if radius != _clean(shared_radius):
                candidate.assumptions.append(
                    "Applied a sub-tolerance equal-radius perturbation to "
                    "avoid an invalid tangent B-rep at the measured value."
                )
            candidates.append(candidate)
    return candidates


def _section_circles(
    section: SectionShape,
) -> list[tuple[CircleProfile, bool]]:
    circles: list[tuple[CircleProfile, bool]] = []
    if isinstance(section.outer, CircleProfile):
        circles.append((section.outer, False))
    circles.extend(
        (profile, True)
        for profile in section.holes
        if isinstance(profile, CircleProfile)
    )
    for region in section.additional_regions:
        if isinstance(region.outer, CircleProfile):
            circles.append((region.outer, False))
        circles.extend(
            (profile, True)
            for profile in region.holes
            if isinstance(profile, CircleProfile)
        )
    return circles


def _profile_round_boundaries(profile: Profile) -> list[CircleProfile]:
    if isinstance(profile, CircleProfile):
        return [profile]
    if not isinstance(profile, PathProfile):
        return []
    found: list[CircleProfile] = []
    start = np.asarray(profile.start)
    for segment in profile.segments:
        end = np.asarray(segment.end)
        if isinstance(segment, ArcSegment):
            fitted = _circle_values(
                np.vstack((start, np.asarray(segment.mid), end))
            )
            if fitted is not None:
                center_x, center_y, radius, _ = fitted
                circle = CircleProfile(
                    center=(_clean(center_x), _clean(center_y)),
                    radius=_clean(radius),
                )
                if not any(
                    np.linalg.norm(
                        np.asarray(existing.center) - np.asarray(circle.center)
                    )
                    <= 0.002
                    and abs(existing.radius - circle.radius) <= 0.002
                    for existing in found
                ):
                    found.append(circle)
        start = end
    return found


def _section_round_boundaries(
    section: SectionShape,
) -> list[tuple[CircleProfile, bool]]:
    boundaries: list[tuple[CircleProfile, bool]] = []
    boundaries.extend(
        (circle, False)
        for circle in _profile_round_boundaries(section.outer)
    )
    for profile in section.holes:
        boundaries.extend(
            (circle, True)
            for circle in _profile_round_boundaries(profile)
        )
    for region in section.additional_regions:
        boundaries.extend(
            (circle, False)
            for circle in _profile_round_boundaries(region.outer)
        )
        for profile in region.holes:
            boundaries.extend(
                (circle, True)
                for circle in _profile_round_boundaries(profile)
            )
    return boundaries


def generate_circular_end_finish_candidates(
    data: MeshData,
    sources: list[PlanCandidate],
) -> list[PlanCandidate]:
    candidates: list[PlanCandidate] = []
    fractions = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15)
    center_tolerance = max(data.diagonal * 0.002, 0.02)
    radial_tolerance = max(data.diagonal * 0.0002, 0.002)

    for source in sources:
        if source.section_consistency < 0.7:
            continue
        base = source.plan.base
        additive_extents = [
            (operation.start, operation.start + operation.depth)
            for operation in source.plan.operations
            if isinstance(operation, BooleanExtrudeFeature)
            and operation.mode == "add"
            and operation.axis == base.axis
        ]
        plan_start = min([base.start, *(extent[0] for extent in additive_extents)])
        plan_end = max(
            [base.start + base.depth, *(extent[1] for extent in additive_extents)]
        )
        plan_depth = plan_end - plan_start
        feature_extents = [(base.start, base.start + base.depth), *additive_extents]
        internal_boundaries = sorted(
            {
                value
                for extent in feature_extents
                for value in extent
                if plan_start + 0.001 < value < plan_end - 0.001
            }
        )
        inferred: list[tuple[float, EdgeFinishFeature]] = []
        alternates: list[tuple[int, float, EdgeFinishFeature]] = []
        nominal_section: SectionShape | None = None

        for end in ("start", "end"):
            layer_depth = plan_depth
            if internal_boundaries:
                layer_depth = (
                    internal_boundaries[0] - plan_start
                    if end == "start"
                    else plan_end - internal_boundaries[-1]
                )
            reference_options: list[tuple[float, SectionShape]] = []
            for reference_fraction in (0.5, 0.9):
                distance = max(
                    layer_depth * reference_fraction,
                    plan_depth * 0.001,
                )
                location = (
                    plan_start + distance
                    if end == "start"
                    else plan_end - distance
                )
                if (section := section_shape(data, base.axis, location)) is not None:
                    reference_options.append((distance, section))
            if not reference_options:
                continue
            reference_distance, reference_section = max(
                reference_options,
                key=lambda item: item[1].area,
            )
            if (
                nominal_section is None
                or reference_section.area > nominal_section.area
            ):
                nominal_section = reference_section
            reference_circles = _section_round_boundaries(reference_section)
            if not reference_circles:
                continue
            distances = {
                min(plan_depth * fraction, reference_distance)
                for fraction in fractions
                if plan_depth * fraction <= reference_distance
            }
            distances.add(reference_distance)
            observed_sections: list[tuple[float, SectionShape]] = []
            for distance in sorted(distances):
                location = (
                    plan_start + distance
                    if end == "start"
                    else plan_end - distance
                )
                section = section_shape(data, base.axis, location)
                if section is not None:
                    observed_sections.append((distance, section))

            for reference_circle, is_hole in reference_circles:
                radius_increases = is_hole or any(
                    np.linalg.norm(
                        np.asarray(other.center)
                        - np.asarray(reference_circle.center)
                    )
                    <= center_tolerance
                    and other.radius > reference_circle.radius + radial_tolerance
                    for other, _ in reference_circles
                )
                observations: list[tuple[float, float]] = []
                for distance, section in observed_sections:
                    matches = [
                        circle
                        for circle, candidate_is_hole in _section_round_boundaries(
                            section
                        )
                        if candidate_is_hole == is_hole
                        and np.linalg.norm(
                            np.asarray(circle.center)
                            - np.asarray(reference_circle.center)
                        )
                        <= center_tolerance
                    ]
                    if not matches:
                        continue
                    observed = min(
                        matches,
                        key=lambda circle: abs(circle.radius - reference_circle.radius),
                    )
                    inset = (
                        observed.radius - reference_circle.radius
                        if radius_increases
                        else reference_circle.radius - observed.radius
                    )
                    observations.append((distance, max(0.0, inset)))
                maximum_inset = max(
                    (inset for _, inset in observations),
                    default=0.0,
                )
                if maximum_inset <= radial_tolerance:
                    continue
                transition = max(
                    (
                        distance
                        for distance, inset in observations
                        if inset > radial_tolerance
                    ),
                    default=maximum_inset,
                )
                maximum_size = min(
                    plan_depth * 2.5,
                    reference_circle.radius * (
                        0.95 if radius_increases else 0.49
                    ),
                )
                size_seeds = (maximum_inset, transition)
                sizes = {
                    _clean(min(seed * factor, maximum_size))
                    for seed in size_seeds
                    for factor in (0.75, 1.0, 1.25, 1.5, 2.0)
                    if radial_tolerance < min(seed * factor, maximum_size)
                }
                fits: list[tuple[float, str, float]] = []
                for mode in ("fillet", "chamfer"):
                    for size in sizes:
                        errors: list[float] = []
                        for distance, observed_inset in observations:
                            if distance >= size:
                                predicted_inset = 0.0
                            elif mode == "chamfer":
                                predicted_inset = size - distance
                            else:
                                predicted_inset = size - math.sqrt(
                                    max(
                                        size * size - (distance - size) ** 2,
                                        0.0,
                                    )
                                )
                            errors.append(abs(predicted_inset - observed_inset))
                        fits.append(
                            (
                                float(np.mean(errors)) / max(size, radial_tolerance),
                                mode,
                                size,
                            )
                        )
                if not fits:
                    continue
                best_by_mode = {
                    mode: min(
                        (fit for fit in fits if fit[1] == mode),
                        key=lambda fit: fit[0],
                    )
                    for mode in ("fillet", "chamfer")
                }
                fit_error, mode, size = min(
                    best_by_mode.values(),
                    key=lambda fit: fit[0],
                )
                if any(
                    isinstance(operation, EdgeFinishFeature)
                    and operation.selector == "circle"
                    and operation.axis == base.axis
                    and operation.end == end
                    and operation.center is not None
                    and operation.radius is not None
                    and np.linalg.norm(
                        np.asarray(operation.center)
                        - np.asarray(reference_circle.center)
                    )
                    <= center_tolerance
                    and abs(operation.radius - reference_circle.radius)
                    <= radial_tolerance
                    for operation in source.plan.operations
                ):
                    continue
                inferred_index = len(inferred)
                inferred.append(
                    (
                        fit_error,
                        EdgeFinishFeature(
                            mode=mode,
                            axis=base.axis,
                            end=end,
                            size=size,
                            selector="circle",
                            center=reference_circle.center,
                            radius=reference_circle.radius,
                        ),
                    )
                )
                alternate_mode = "chamfer" if mode == "fillet" else "fillet"
                alternate_error, _, alternate_size = best_by_mode[alternate_mode]
                if alternate_error <= fit_error + 0.25:
                    alternates.append(
                        (
                            inferred_index,
                            alternate_error,
                            EdgeFinishFeature(
                                mode=alternate_mode,
                                axis=base.axis,
                                end=end,
                                size=alternate_size,
                                selector="circle",
                                center=reference_circle.center,
                                radius=reference_circle.radius,
                            ),
                        )
                    )

        if inferred:
            variants: list[list[tuple[float, EdgeFinishFeature]]] = [inferred]
            for inferred_index, alternate_error, alternate in alternates:
                variant = list(inferred)
                variant[inferred_index] = (alternate_error, alternate)
                variants.append(variant)
            for inferred_index, (fit_error, operation) in enumerate(inferred):
                for factor in (0.925, 1.075):
                    variant = list(inferred)
                    variant[inferred_index] = (
                        fit_error + 0.01,
                        operation.model_copy(
                            update={"size": _clean(operation.size * factor)}
                        ),
                    )
                    variants.append(variant)
            for variant in variants:
                plan = source.plan.model_copy(deep=True)
                if (
                    nominal_section is not None
                    and isinstance(plan.base, ExtrudeFeature)
                    and not nominal_section.additional_regions
                ):
                    plan.base.outer = nominal_section.outer
                    plan.base.holes = nominal_section.holes
                covered_ends = {
                    operation.end
                    for _, operation in variant
                }
                plan.operations = [
                    operation
                    for operation in plan.operations
                    if not (
                        isinstance(operation, EdgeFinishFeature)
                        and operation.selector == "all"
                        and (
                            operation.end == "both"
                            or operation.end in covered_ends
                        )
                    )
                ]
                ordered_variant = sorted(
                    variant,
                    key=lambda item: item[1].radius or 0.0,
                    reverse=True,
                )
                plan.operations.extend(
                    operation for _, operation in ordered_variant
                )
                plan.assumptions.append(
                    f"Fitted {len(variant)} circular edges independently as "
                    "analytic fillet/chamfer features."
                )
                candidates.append(
                    PlanCandidate(
                        plan=plan,
                        heuristic_score=(
                            source.heuristic_score
                            + 0.001 * len(variant)
                            + 0.05 * sum(error for error, _ in variant)
                        ),
                        section_consistency=source.section_consistency,
                    )
                )
    return candidates


def generate_internal_round_finish_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    from .cad import (
        _edge_axis_center,
        _edge_axis_extent,
        _edge_circle_parameters,
        build_plan,
    )

    axis = getattr(source.base, "axis", None)
    if not isinstance(axis, Axis):
        return []
    additive_extents = [
        (operation.start, operation.start + operation.depth)
        for operation in source.operations
        if isinstance(operation, (BooleanExtrudeFeature, ConicalAddFeature))
        and operation.axis == axis
        and getattr(operation, "mode", "add") == "add"
    ]
    if not additive_extents:
        return []
    plan_start = min(
        [source.base.start, *(extent[0] for extent in additive_extents)]
    )
    plan_end = max(
        [
            source.base.start + source.base.depth,
            *(extent[1] for extent in additive_extents),
        ]
    )
    boundaries = sorted(
        {
            value
            for extent in additive_extents
            for value in extent
            if plan_start + 0.001 < value < plan_end - 0.001
        }
    )
    if not boundaries:
        return []
    try:
        shape = build_plan(source).val()
    except Exception:
        return []
    bounds = shape.BoundingBox()
    diagonal = (bounds.xlen**2 + bounds.ylen**2 + bounds.zlen**2) ** 0.5
    axial_tolerance = max(diagonal * 1e-6, 1e-6)
    selector_tolerance = max(diagonal * 1e-4, 1e-4)
    selectors: list[tuple[float, tuple[float, float], float]] = []
    for edge in shape.Edges():
        if _edge_axis_extent(edge, axis) > axial_tolerance:
            continue
        position = _edge_axis_center(edge, axis)
        boundary = min(boundaries, key=lambda value: abs(value - position))
        if abs(boundary - position) > selector_tolerance:
            continue
        circle = _edge_circle_parameters(edge, axis)
        if circle is None:
            continue
        center, radius = circle
        selector = (boundary, center, radius)
        if not any(
            abs(existing[0] - boundary) <= selector_tolerance
            and np.linalg.norm(
                np.asarray(existing[1]) - np.asarray(center)
            )
            <= selector_tolerance
            and abs(existing[2] - radius) <= selector_tolerance
            for existing in selectors
        ):
            selectors.append(selector)
    if not selectors:
        return []

    curve_tolerance = max(data.diagonal * 0.002, 0.02)
    groups: list[list[tuple[float, tuple[float, float], float]]] = []
    for selector in selectors:
        match = next(
            (
                group
                for group in groups
                if np.linalg.norm(
                    np.asarray(group[0][1]) - np.asarray(selector[1])
                )
                <= curve_tolerance
                and abs(group[0][2] - selector[2]) <= curve_tolerance
            ),
            None,
        )
        if match is None:
            groups.append([selector])
        else:
            match.append(selector)
    groups = [
        group
        for group in groups
        if len({round(selector[0], 4) for selector in group}) >= 2
    ]
    if not groups:
        return []

    all_levels = [plan_start, *boundaries, plan_end]
    minimum_size = max(data.diagonal * 0.001, 0.01)
    candidates: list[ReconstructionPlan] = []
    for group in groups:
        minimum_span = min(
            min(
                position - max(
                    level for level in all_levels if level < position
                ),
                min(
                    level for level in all_levels if level > position
                )
                - position,
            )
            for position, _, _ in group
        )
        sizes = {
            _clean(minimum_span * fraction)
            for fraction in (0.04, 0.0667, 0.1, 0.15, 0.2)
            if minimum_span * fraction > minimum_size
        }
        for mode in ("fillet", "chamfer"):
            for size in sorted(sizes):
                plan = source.model_copy(deep=True)
                plan.operations.extend(
                    EdgeFinishFeature(
                        mode=mode,
                        axis=axis,
                        position=_clean(position),
                        size=size,
                        selector="circle",
                        center=(_clean(center[0]), _clean(center[1])),
                        radius=_clean(radius),
                    )
                    for position, center, radius in group
                )
                plan.assumptions.append(
                    f"Fitted {len(group)} repeated internal circular-edge "
                    f"{mode}s with a shared {size:g} mm size."
                )
                candidates.append(plan)
    return candidates


def generate_axial_circle_transition_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    """Find circular end edges whose target radius changes just inside the end."""

    from .cad import (
        _edge_axis_center,
        _edge_axis_extent,
        _edge_circle_parameters,
        build_plan,
    )

    axis = getattr(source.base, "axis", None)
    if not isinstance(axis, Axis):
        return []
    try:
        shape = build_plan(source).val()
    except Exception:
        return []
    bounds = shape.BoundingBox()
    axis_min, axis_max = {
        Axis.X: (bounds.xmin, bounds.xmax),
        Axis.Y: (bounds.ymin, bounds.ymax),
        Axis.Z: (bounds.zmin, bounds.zmax),
    }[axis]
    diagonal = (bounds.xlen**2 + bounds.ylen**2 + bounds.zlen**2) ** 0.5
    axial_tolerance = max(diagonal * 1e-6, 1e-6)
    radial_tolerance = max(data.diagonal * 0.0003, 0.003)
    detected: list[tuple[float, EdgeFinishFeature]] = []
    for end, end_position, direction in (
        ("start", axis_min, 1.0),
        ("end", axis_max, -1.0),
    ):
        for edge in shape.Edges():
            if (
                _edge_axis_extent(edge, axis) > axial_tolerance
                or abs(_edge_axis_center(edge, axis) - end_position)
                > max(axial_tolerance, 0.002)
            ):
                continue
            circle = _edge_circle_parameters(edge, axis)
            if circle is None:
                continue
            center, radius = circle
            observations: list[tuple[float, float]] = []
            for fraction in (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.6, 1.0):
                distance = max(radius * fraction, radial_tolerance)
                section = section_shape(
                    data,
                    axis,
                    end_position + direction * distance,
                )
                if section is None:
                    continue
                matches = [
                    candidate
                    for candidate, _ in _section_round_boundaries(section)
                    if np.linalg.norm(
                        np.asarray(candidate.center) - np.asarray(center)
                    )
                    <= max(radius * 1.5, data.diagonal * 0.02)
                ]
                if not matches:
                    continue
                match = min(
                    matches,
                    key=lambda candidate: np.linalg.norm(
                        np.asarray(candidate.center) - np.asarray(center)
                    ),
                )
                observations.append((distance, abs(match.radius - radius)))
            changed = [
                distance
                for distance, radius_change in observations
                if radius_change > radial_tolerance
            ]
            if not changed:
                continue
            size = _clean(max(changed))
            if size <= radial_tolerance or size >= radius * 0.8:
                continue
            detected.append(
                (
                    max(change for _, change in observations),
                    EdgeFinishFeature(
                        mode="fillet",
                        axis=axis,
                        end=end,
                        size=size,
                        selector="circle",
                        center=(_clean(center[0]), _clean(center[1])),
                        radius=_clean(radius),
                    ),
                )
            )
    candidates: list[ReconstructionPlan] = []
    for _, finish in sorted(detected, key=lambda item: item[0], reverse=True)[:6]:
        plan = source.model_copy(deep=True)
        plan.operations.append(finish)
        plan.assumptions.append(
            "Recovered a localized circular end transition as a true analytic "
            "edge fillet."
        )
        candidates.append(plan)
    return candidates


def generate_feature_round_finish_candidates(
    data: MeshData,
    source: ReconstructionPlan,
    *,
    include_all_groups: bool = False,
) -> list[ReconstructionPlan]:
    axis = getattr(source.base, "axis", None)
    if not isinstance(axis, Axis):
        return []
    curve_tolerance = max(data.diagonal * 0.002, 0.02)
    already_finished = {
        operation.feature_index
        for operation in source.operations
        if isinstance(operation, EdgeFinishFeature)
        and operation.feature_index is not None
    }
    groups: list[
        tuple[
            Axis,
            CircleProfile,
            list[
                tuple[
                    int,
                    BooleanExtrudeFeature | OrientedCylinderFeature,
                ]
            ],
        ]
    ] = []
    for operation_index, operation in enumerate(source.operations):
        if (
            not isinstance(operation, BooleanExtrudeFeature)
            or operation_index in already_finished
            or (
                not include_all_groups
                and (
                    operation.mode != "add"
                    or operation.axis != axis
                )
            )
        ):
            continue
        profiles = [operation.outer, *operation.holes]
        profiles.extend(operation.additional_regions)
        for profile in profiles:
            for circle in _profile_round_boundaries(profile):
                match = next(
                    (
                        group
                        for group in groups
                        if group[0] == operation.axis
                        if np.linalg.norm(
                            np.asarray(group[1].center)
                            - np.asarray(circle.center)
                        )
                        <= curve_tolerance
                        and abs(group[1].radius - circle.radius)
                        <= curve_tolerance
                    ),
                    None,
                )
                if match is None:
                    groups.append(
                        (
                            operation.axis,
                            circle,
                            [(operation_index, operation)],
                        )
                    )
                elif all(
                    existing_index != operation_index
                    for existing_index, _ in match[2]
                ):
                    match[2].append((operation_index, operation))

    for operation_index, operation in enumerate(source.operations):
        if (
            not include_all_groups
            or not isinstance(operation, OrientedCylinderFeature)
            or operation_index in already_finished
        ):
            continue
        direction = np.asarray(operation.direction, dtype=float)
        direction /= max(float(np.linalg.norm(direction)), 1e-12)
        axis_index = int(np.argmax(np.abs(direction)))
        if abs(direction[axis_index]) < 0.999:
            continue
        feature_axis = (Axis.X, Axis.Y, Axis.Z)[axis_index]
        center = _project(
            np.asarray(operation.origin, dtype=float).reshape(1, 3),
            feature_axis,
        )[0]
        groups.append(
            (
                feature_axis,
                CircleProfile(
                    center=(_clean(center[0]), _clean(center[1])),
                    radius=_clean(operation.radius),
                ),
                [(operation_index, operation)],
            )
        )

    minimum_repetitions = (
        1 if include_all_groups or already_finished else 2
    )
    repeated = [
        group for group in groups if len(group[2]) >= minimum_repetitions
    ]
    if repeated and not include_all_groups:
        repeated = [
            max(
                repeated,
                key=lambda group: group[1].radius * len(group[2]),
            )
        ]
    candidates: list[ReconstructionPlan] = []
    minimum_size = max(data.diagonal * 0.001, 0.01)
    for feature_axis, circle, features in repeated:
        minimum_depth = min(feature.depth for _, feature in features)
        sizes = {
            _clean(minimum_depth * fraction)
            for fraction in (
                0.005,
                0.01,
                0.02,
                0.04,
                0.0667,
                0.1,
                0.15,
                0.2,
            )
            if minimum_depth * fraction > minimum_size
            and minimum_depth * fraction < circle.radius * 0.8
        }
        for mode in ("fillet", "chamfer"):
            for size in sorted(sizes):
                end_groups = (
                    (("start",), ("end",), ("start", "end"))
                    if include_all_groups
                    else (("start", "end"),)
                )
                for ends in end_groups:
                    plan = source.model_copy(deep=True)
                    for operation_index, _ in features:
                        for end in ends:
                            plan.operations.append(
                                EdgeFinishFeature(
                                    mode=mode,
                                    axis=feature_axis,
                                    end=end,
                                    size=size,
                                    selector="circle",
                                    center=circle.center,
                                    radius=circle.radius,
                                    feature_index=operation_index,
                                )
                            )
                    plan.assumptions.append(
                        f"Fitted repeated feature-local circular-edge {mode}s "
                        f"with a shared {size:g} mm size."
                    )
                    candidates.append(plan)

    # Repeated drilled or added cylinders commonly share one fillet/chamfer
    # feature even when their centers differ. Propose that compact repeated
    # operation as well as the individual alternatives above.
    if include_all_groups:
        oriented_groups = [
            group
            for group in repeated
            if all(
                isinstance(feature, OrientedCylinderFeature)
                for _, feature in group[2]
            )
        ]
        compatible_sets: list[
            list[
                tuple[
                    Axis,
                    CircleProfile,
                    list[
                        tuple[
                            int,
                            BooleanExtrudeFeature | OrientedCylinderFeature,
                        ]
                    ],
                ]
            ]
        ] = []
        for group in oriented_groups:
            match = next(
                (
                    cluster
                    for cluster in compatible_sets
                    if cluster[0][0] == group[0]
                    and abs(cluster[0][1].radius - group[1].radius)
                    <= curve_tolerance
                    and abs(
                        cluster[0][2][0][1].depth
                        - group[2][0][1].depth
                    )
                    <= curve_tolerance
                ),
                None,
            )
            if match is None:
                compatible_sets.append([group])
            else:
                match.append(group)
        for compatible in compatible_sets:
            if len(compatible) < 2:
                continue
            minimum_depth = min(
                feature.depth
                for _, _, features in compatible
                for _, feature in features
            )
            smallest_radius = min(circle.radius for _, circle, _ in compatible)
            sizes = {
                _clean(minimum_depth * fraction)
                for fraction in (
                    0.005,
                    0.01,
                    0.02,
                    0.04,
                    0.0667,
                    0.1,
                    0.15,
                    0.2,
                )
                if minimum_depth * fraction > minimum_size
                and minimum_depth * fraction < smallest_radius * 0.8
            }
            for mode in ("fillet", "chamfer"):
                for size in sorted(sizes):
                    for ends in (("start",), ("end",), ("start", "end")):
                        plan = source.model_copy(deep=True)
                        for feature_axis, circle, features in compatible:
                            for operation_index, _ in features:
                                for end in ends:
                                    plan.operations.append(
                                        EdgeFinishFeature(
                                            mode=mode,
                                            axis=feature_axis,
                                            end=end,
                                            size=size,
                                            selector="circle",
                                            center=circle.center,
                                            radius=circle.radius,
                                            feature_index=operation_index,
                                        )
                                    )
                        plan.assumptions.append(
                            f"Fitted {len(compatible)} repeated cylinder-edge "
                            f"{mode}s with one shared {size:g} mm size."
                        )
                        candidates.append(plan)
    return candidates


def generate_internal_circular_finish_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    """Fillet persistent circular BRep edges at internal layer boundaries.

    Section-based end-finish searches only see the global ends of a model.
    Mechanical timelines also commonly fillet the shoulder where two coaxial
    extrusions meet.  Those shoulders survive reconstruction as nearly full
    circular BRep edges, so they are safer and cheaper to target directly than
    attempting another dense slab approximation.
    """

    from .cad import (
        _edge_axis_center,
        _edge_axis_extent,
        _edge_circle_parameters,
        build_plan,
    )

    try:
        shape = build_plan(source).val()
    except Exception:
        return []
    diagonal = max(data.diagonal, 1e-6)
    extent_tolerance = max(diagonal * 1e-6, 1e-6)
    position_tolerance = max(diagonal * 2e-4, 0.002)
    circle_tolerance = max(diagonal * 2e-4, 0.002)
    clusters: list[dict[str, object]] = []
    for axis in Axis:
        for edge in shape.Edges():
            if (
                edge.geomType() != "CIRCLE"
                or _edge_axis_extent(edge, axis) > extent_tolerance
                or (circle := _edge_circle_parameters(edge, axis)) is None
            ):
                continue
            center, radius = circle
            position = _edge_axis_center(edge, axis)
            match = next(
                (
                    cluster
                    for cluster in clusters
                    if cluster["axis"] == axis
                    and abs(float(cluster["position"]) - position)
                    <= position_tolerance
                    and np.linalg.norm(
                        np.asarray(cluster["center"]) - np.asarray(center)
                    )
                    <= circle_tolerance
                    and abs(float(cluster["radius"]) - radius)
                    <= circle_tolerance
                ),
                None,
            )
            if match is None:
                clusters.append(
                    {
                        "axis": axis,
                        "position": position,
                        "center": center,
                        "radius": radius,
                        "length": float(edge.Length()),
                    }
                )
            else:
                match["length"] = float(match["length"]) + float(edge.Length())

    base_axis = getattr(source.base, "axis", None)
    full_circles = [
        cluster
        for cluster in clusters
        if float(cluster["length"])
        / max(2 * math.pi * float(cluster["radius"]), 1e-9)
        >= 0.7
    ]
    full_circles.sort(
        key=lambda cluster: (
            cluster["axis"] != base_axis,
            -min(
                float(cluster["length"])
                / max(2 * math.pi * float(cluster["radius"]), 1e-9),
                1.0,
            ),
            -float(cluster["radius"]),
        )
    )
    candidates: list[ReconstructionPlan] = []
    for cluster in full_circles[:4]:
        radius = float(cluster["radius"])
        sizes = {
            _clean(min(max(diagonal * fraction, 0.01), radius * 0.2))
            for fraction in (0.001, 0.004)
            if min(max(diagonal * fraction, 0.01), radius * 0.2) > 0.001
        }
        for size in sorted(sizes):
            plan = source.model_copy(deep=True)
            plan.operations.append(
                EdgeFinishFeature(
                    mode="fillet",
                    axis=cluster["axis"],
                    position=_clean(float(cluster["position"])),
                    size=size,
                    selector="circle",
                    center=tuple(_clean(value) for value in cluster["center"]),
                    radius=_clean(radius),
                )
            )
            plan.assumptions.append(
                "Recovered a persistent circular shoulder as a true analytic "
                "toroidal fillet."
            )
            candidates.append(plan)
    if isinstance(source.base, ExtrudeFeature):
        base_circles = _profile_round_boundaries(source.base.outer)
        emitted_base_circles: set[tuple[float, float, float]] = set()
        for cluster in full_circles[:4]:
            matched = next(
                (
                    circle
                    for circle in base_circles
                    if np.linalg.norm(
                        np.asarray(circle.center)
                        - np.asarray(cluster["center"])
                    )
                    <= circle_tolerance
                    and abs(circle.radius - float(cluster["radius"]))
                    <= circle_tolerance
                ),
                None,
            )
            if matched is None:
                continue
            signature = (
                _clean(matched.center[0]),
                _clean(matched.center[1]),
                _clean(matched.radius),
            )
            if signature in emitted_base_circles:
                continue
            emitted_base_circles.add(signature)
            size = _clean(matched.radius * 0.12)
            if size <= 0.001 or size >= matched.radius * 0.2:
                continue
            for end in ("start", "end"):
                plan = source.model_copy(deep=True)
                plan.operations.append(
                    EdgeFinishFeature(
                        mode="fillet",
                        axis=source.base.axis,
                        end=end,
                        size=size,
                        selector="circle",
                        center=matched.center,
                        radius=matched.radius,
                        feature_index=-1,
                    )
                )
                plan.assumptions.append(
                    "Applied a persistent circular shoulder blend to its base "
                    "feature before later Boolean unions, preserving a torus."
                )
                candidates.append(plan)
    for operation_index, operation in enumerate(source.operations):
        if (
            not isinstance(operation, EdgeFinishFeature)
            or operation.mode != "fillet"
            or operation.selector != "circle"
            or operation.feature_index is not None
            or operation.end not in {"start", "end"}
        ):
            continue
        for factor in (0.2, 0.5):
            size = _clean(
                min(
                    operation.size * factor,
                    diagonal * 0.003,
                    (operation.radius or operation.size) * 0.2,
                )
            )
            if size <= 0.001:
                continue
            plan = source.model_copy(deep=True)
            replacement = operation.model_copy(
                update={"size": size, "feature_index": -1}
            )
            plan.operations[operation_index] = replacement
            plan.assumptions.append(
                "Applied the measured circular end blend to its base feature "
                "before Boolean unions so STEP retains an exact torus."
            )
            candidates.append(plan)
    return candidates


def generate_round_boundary_feature_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    tolerance = max(data.diagonal * 0.002, 0.02)
    clusters: list[
        tuple[Axis, np.ndarray, float, list[tuple[float, float]], bool]
    ] = []
    base_axis = getattr(source.base, "axis", None)
    for axis in AXES:
        if axis == base_axis:
            continue
        levels = _axis_levels(data, axis)
        for start, end in zip(levels, levels[1:], strict=False):
            section = section_shape(data, axis, (start + end) / 2)
            if section is None:
                continue
            for circle, is_hole in _section_round_boundaries(section):
                if is_hole:
                    continue
                center = np.asarray(circle.center)
                inside = bool(section.polygon.covers(Point(circle.center)))
                match = next(
                    (
                        cluster
                        for cluster in clusters
                        if cluster[0] == axis
                        and cluster[4] == inside
                        and np.linalg.norm(cluster[1] - center) <= tolerance
                        and abs(cluster[2] - circle.radius) <= tolerance
                    ),
                    None,
                )
                if match is None:
                    clusters.append(
                        (
                            axis,
                            center,
                            circle.radius,
                            [(start, end)],
                            inside,
                        )
                    )
                else:
                    match[3].append((start, end))

    candidates: list[ReconstructionPlan] = []
    for axis, center, radius, spans, inside in clusters:
        start = min(span[0] for span in spans)
        end = max(span[1] for span in spans)
        if end - start <= tolerance:
            continue
        preferred_mode = "add" if inside else "cut"
        for mode in (preferred_mode, "cut" if inside else "add"):
            plan = source.model_copy(deep=True)
            plan.operations.append(
                BooleanExtrudeFeature(
                    mode=mode,
                    axis=axis,
                    start=_clean(start),
                    depth=_clean(end - start),
                    outer=CircleProfile(
                        center=(_clean(center[0]), _clean(center[1])),
                        radius=_clean(radius),
                    ),
                )
            )
            plan.assumptions.append(
                "Recovered a persistent circular boundary arc as an analytic "
                f"{axis.value}-axis cylindrical {mode} feature."
            )
            candidates.append(plan)
    return candidates


def _canonical_direction(direction: np.ndarray) -> np.ndarray:
    result = direction / np.linalg.norm(direction)
    # SVD axes have arbitrary sign.  Choosing the sign from the first merely
    # non-zero component is unstable when an almost-cardinal axis contains a
    # tiny numerical X/Y component; two halves of one drilled hole can then
    # be reported as opposite directions and never merged.  Anchor the sign
    # to the dominant component instead.
    dominant = int(np.argmax(np.abs(result)))
    if result[dominant] < 0:
        result = -result
    return result


def _normal_plane_basis(direction: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    reference = (
        np.array([1.0, 0.0, 0.0])
        if abs(direction[0]) < 0.8
        else np.array([0.0, 1.0, 0.0])
    )
    first = np.cross(direction, reference)
    first /= np.linalg.norm(first)
    return first, np.cross(direction, first)


def mesh_has_spatially_curved_patch(data: MeshData) -> bool:
    """Return whether a sizeable smooth patch curves in all three dimensions."""

    mesh = data.mesh
    adjacency = mesh.face_adjacency
    if len(adjacency) == 0:
        return False
    smooth = np.isfinite(mesh.face_adjacency_angles) & (
        mesh.face_adjacency_angles < math.radians(8)
    )
    edges = adjacency[smooth]
    if len(edges) == 0:
        return False
    rows = np.concatenate((edges[:, 0], edges[:, 1]))
    columns = np.concatenate((edges[:, 1], edges[:, 0]))
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(mesh.faces), len(mesh.faces)),
    )
    component_count, labels = connected_components(graph, directed=False)
    minimum_area = max(float(mesh.area) * 0.01, data.diagonal * data.diagonal * 1e-6)
    for label in range(component_count):
        face_indices = np.flatnonzero(labels == label)
        if len(face_indices) < 24:
            continue
        if float(np.sum(mesh.area_faces[face_indices])) < minimum_area:
            continue
        singular_values = np.linalg.svd(
            mesh.face_normals[face_indices],
            compute_uv=False,
        )
        if (
            singular_values[1] > 1e-9
            and singular_values[2] / singular_values[1] > 0.1
        ):
            return True
    return False


def _smooth_mesh_components(
    data: MeshData,
    *,
    minimum_area_ratio: float = 0.0005,
) -> list[np.ndarray]:
    mesh = data.mesh
    adjacency = mesh.face_adjacency
    if len(adjacency) == 0:
        return []
    smooth = np.isfinite(mesh.face_adjacency_angles) & (
        mesh.face_adjacency_angles < math.radians(8)
    )
    edges = adjacency[smooth]
    if len(edges) == 0:
        return []
    rows = np.concatenate((edges[:, 0], edges[:, 1]))
    columns = np.concatenate((edges[:, 1], edges[:, 0]))
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(mesh.faces), len(mesh.faces)),
    )
    component_count, labels = connected_components(graph, directed=False)
    # Countersinks on thin sheet parts can each be well below 0.2% of the
    # total surface area even though they are intentional CAD features. Keep
    # those connected patches while the face-count and normal-space tests
    # below continue to reject tessellation noise.
    minimum_area = max(
        float(mesh.area) * minimum_area_ratio,
        data.diagonal**2 * 1e-6,
    )
    return [
        face_indices
        for label in range(component_count)
        if len(face_indices := np.flatnonzero(labels == label)) >= 24
        and float(np.sum(mesh.area_faces[face_indices])) >= minimum_area
    ]


def _mesh_spherical_features(data: MeshData) -> list[SphereFeature]:
    """Fit analytic spheres from clustered normal-curvature centers.

    A spherical fillet can be smoothly joined to adjacent cylinders and tori,
    so connected-component classification alone merges the surfaces. For two
    triangles on a sphere, ``center = point + signed_radius * normal`` is
    constant. Cluster those pairwise center estimates, then refit and validate
    each patch against all triangle centers and normals.
    """

    mesh = data.mesh
    adjacency = mesh.face_adjacency
    angles = mesh.face_adjacency_angles
    if len(adjacency) == 0:
        return []
    smooth = (
        np.isfinite(angles)
        & (angles > math.radians(0.15))
        & (angles < math.radians(8))
    )
    pairs = adjacency[smooth]
    if len(pairs) < 48:
        return []
    centers = mesh.triangles_center
    normals = mesh.face_normals
    point_delta = centers[pairs[:, 0]] - centers[pairs[:, 1]]
    normal_delta = normals[pairs[:, 0]] - normals[pairs[:, 1]]
    denominator = np.einsum("ij,ij->i", normal_delta, normal_delta)
    signed_radii = -np.einsum(
        "ij,ij->i",
        point_delta,
        normal_delta,
    ) / np.maximum(denominator, 1e-15)
    first_centers = (
        centers[pairs[:, 0]]
        + signed_radii[:, None] * normals[pairs[:, 0]]
    )
    second_centers = (
        centers[pairs[:, 1]]
        + signed_radii[:, None] * normals[pairs[:, 1]]
    )
    estimates = (first_centers + second_centers) / 2
    pair_error = np.linalg.norm(first_centers - second_centers, axis=1)
    minimum_radius = max(data.diagonal * 0.0004, 0.004)
    maximum_radius = data.diagonal * 0.3
    # Adjacent triangle normals are accurate, but their pairwise curvature
    # centers amplify tessellation-angle error on a clipped sphere.  Cluster
    # at the same order as the mesh chord tolerance, then use the much tighter
    # all-face least-squares validation below to reject false positives.
    center_bin = max(data.diagonal * 0.002, 0.008)
    radius_bin = max(center_bin * 0.5, 0.004)
    valid = (
        np.isfinite(signed_radii)
        & np.all(np.isfinite(estimates), axis=1)
        & (np.abs(signed_radii) >= minimum_radius)
        & (np.abs(signed_radii) <= maximum_radius)
        & (pair_error <= center_bin * 2)
    )
    estimates = estimates[valid]
    signed_radii = signed_radii[valid]
    if len(estimates) < 48:
        return []
    keys = np.column_stack(
        (
            np.round(estimates / center_bin).astype(np.int64),
            np.round(np.abs(signed_radii) / radius_bin).astype(np.int64),
            np.sign(signed_radii).astype(np.int64),
        )
    )
    counts = Counter(map(tuple, keys))
    minimum_votes = max(32, int(len(pairs) * 0.0001))
    fitted: list[SphereFeature] = []
    for key, votes in counts.most_common(96):
        if votes < minimum_votes:
            break
        selected = np.all(keys == key, axis=1)
        center = np.median(estimates[selected], axis=0)
        signed_radius = float(np.median(signed_radii[selected]))
        radius = abs(signed_radius)
        broad_error = max(radius * 0.15, center_bin * 4)
        radial_vectors = centers - center
        radial_distances = np.linalg.norm(radial_vectors, axis=1)
        alignment = np.abs(
            np.einsum("ij,ij->i", radial_vectors, normals)
            / np.maximum(radial_distances, 1e-12)
        )
        face_indices = np.flatnonzero(
            (np.abs(radial_distances - radius) <= broad_error)
            & (alignment >= 0.97)
        )
        if len(face_indices) < 48:
            continue
        # Solve center - signed_radius * normal = surface point. Triangle
        # centers lie infinitesimally inside their planar chords, but the
        # normal formulation remains well-conditioned even for an octant.
        matrix = np.zeros((len(face_indices) * 3, 4), dtype=np.float64)
        matrix[:, :3] = np.tile(np.eye(3), (len(face_indices), 1))
        matrix[:, 3] = -normals[face_indices].reshape(-1)
        rhs = centers[face_indices].reshape(-1)
        solution = np.linalg.lstsq(matrix, rhs, rcond=None)[0]
        center = solution[:3]
        signed_radius = float(solution[3])
        radius = abs(signed_radius)
        if radius < minimum_radius or radius > maximum_radius:
            continue
        radial_vectors = centers - center
        radial_distances = np.linalg.norm(radial_vectors, axis=1)
        expected_normals = (
            radial_vectors / np.maximum(radial_distances[:, None], 1e-12)
            if signed_radius < 0
            else -radial_vectors
            / np.maximum(radial_distances[:, None], 1e-12)
        )
        signed_alignment = np.einsum("ij,ij->i", normals, expected_normals)
        fit_tolerance = max(data.diagonal * 0.00015, 0.0025, radius * 0.01)
        face_indices = np.flatnonzero(
            (np.abs(radial_distances - radius) <= fit_tolerance)
            & (signed_alignment >= 0.99)
        )
        if len(face_indices) < 48:
            continue
        # A short toroidal elbow can locally resemble a sphere, but its fitted
        # radial residual fills almost the entire acceptance band.  Genuine
        # clipped spheres retain a materially tighter all-face residual even
        # when only an octant is visible.
        if float(
            np.percentile(
                np.abs(radial_distances[face_indices] - radius),
                95,
            )
        ) > fit_tolerance * 0.85:
            continue
        patch_normals = normals[face_indices]
        singular_values = np.linalg.svd(
            patch_normals - np.mean(patch_normals, axis=0),
            compute_uv=False,
        )
        if (
            singular_values[1] <= 1e-9
            # A sphere must curve substantially in two independent tangent
            # directions. A short torus strip has a locally plausible center
            # and radius, but its second normal-space span is much narrower.
            or singular_values[1] / singular_values[0] < 0.33
            or singular_values[2] / singular_values[1] < 0.12
        ):
            continue
        patch_area = float(np.sum(mesh.area_faces[face_indices]))
        if patch_area < max(float(mesh.area) * 0.00002, data.diagonal**2 * 1e-7):
            continue
        candidate = SphereFeature(
            mode="cut" if signed_radius > 0 else "add",
            center=tuple(_clean(value) for value in center),
            radius=_clean(radius),
        )
        if any(
            np.linalg.norm(
                np.asarray(existing.center) - np.asarray(candidate.center)
            )
            <= max(center_bin * 3, radius * 0.12)
            and abs(existing.radius - candidate.radius)
            <= max(radius_bin * 3, radius * 0.12)
            for existing in fitted
        ):
            continue
        fitted.append(candidate)
        if len(fitted) >= 12:
            break
    return fitted


def generate_spherical_patch_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    features = _mesh_spherical_features(data)
    if not features:
        return []
    groups = [features, *([feature] for feature in features)]
    candidates: list[ReconstructionPlan] = []
    for group in groups[:13]:
        plan = source.model_copy(deep=True)
        plan.operations.extend(
            feature.model_copy(deep=True) for feature in group
        )
        plan.assumptions.append(
            f"Recovered {len(group)} analytic spherical mesh patches as "
            "editable sphere boolean features."
        )
        candidates.append(plan)
    return candidates


def mesh_has_conical_patch(data: MeshData) -> bool:
    """Detect a smooth normal-space ring lying off the origin plane."""

    # Small countersinks and edge chamfers can occupy only 0.01% of a long
    # plate while still containing hundreds of clean tessellation triangles.
    # The cone-specific normal checks below are strong enough to evaluate
    # those patches without lowering the torus detector's area threshold.
    for face_indices in _smooth_mesh_components(
        data,
        minimum_area_ratio=0.0001,
    ):
        normals = data.mesh.face_normals[face_indices]
        centered = normals - np.mean(normals, axis=0)
        _, singular_values, directions = np.linalg.svd(
            centered,
            full_matrices=False,
        )
        if singular_values[0] <= 1e-9 or singular_values[1] <= 1e-9:
            continue
        axis = directions[-1]
        axial = normals @ axis
        if (
            # A complete bevel ring is nearly isotropic in its two changing
            # normal-space directions, while a cone clipped by the stock can
            # expose only a short sector.  The latter is intentionally
            # anisotropic, but still has substantial variation in both
            # directions.  The absolute variation check rejects planar
            # tessellation noise; the non-zero axial mean separates these
            # sectors from cylinders.
            singular_values[1] / singular_values[0] > 0.03
            and singular_values[1] / math.sqrt(len(face_indices)) > 0.003
            # A tangent fillet or effectively-planar spline can remain in the
            # same tessellated smooth component as a narrow chamfer.  That
            # broadens the fitted normal plane slightly without changing the
            # cone's non-zero, nearly constant axial normal component.
            and singular_values[2] / singular_values[1] < 0.2
            # Very shallow chamfers have an axial normal component near 0.02,
            # and short partial chamfer sectors span only a few degrees in
            # normal space. Their non-zero constant axial component still
            # separates them cleanly from cylindrical walls.
            and abs(float(np.mean(axial))) > 0.012
            and float(np.std(axial)) < 0.06
        ):
            return True
    return False


def mesh_has_toroidal_patch(data: MeshData) -> bool:
    """Detect a smooth patch whose changing normals span three dimensions."""

    for face_indices in _smooth_mesh_components(data):
        normals = data.mesh.face_normals[face_indices]
        centered = normals - np.mean(normals, axis=0)
        singular_values = np.linalg.svd(centered, compute_uv=False)
        if (
            singular_values[1] > 1e-9
            and singular_values[2] / singular_values[1] > 0.1
        ):
            return True
    return False


def _mesh_cylindrical_features(
    data: MeshData,
) -> list[OrientedCylinderFeature]:
    """Fit analytic cylinders to connected smooth triangle patches.

    A tessellated cylindrical face has changing normals, but every normal is
    perpendicular to one common axis.  Sharp cap/intersection edges separate
    that face from neighboring planes, so connected low-dihedral patches let
    us recover the source cylinder without access to the original STEP file.
    """

    mesh = data.mesh
    adjacency = mesh.face_adjacency
    if len(adjacency) == 0:
        return []
    smooth = np.isfinite(mesh.face_adjacency_angles) & (
        mesh.face_adjacency_angles < math.radians(8)
    )
    edges = adjacency[smooth]
    if len(edges) == 0:
        return []
    rows = np.concatenate((edges[:, 0], edges[:, 1]))
    columns = np.concatenate((edges[:, 1], edges[:, 0]))
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(mesh.faces), len(mesh.faces)),
    )
    component_count, labels = connected_components(graph, directed=False)
    radial_tolerance = max(data.diagonal * 0.0005, 0.004)
    line_tolerance = max(data.diagonal * 0.001, 0.01)
    fitted: list[dict[str, object]] = []

    for label in range(component_count):
        face_indices = np.flatnonzero(labels == label)
        if len(face_indices) < 24:
            continue
        normals = mesh.face_normals[face_indices]
        _, singular_values, vectors = np.linalg.svd(normals, full_matrices=False)
        if (
            singular_values[1] / max(singular_values[0], 1e-12) < 0.15
            or singular_values[2] / max(singular_values[1], 1e-12) > 0.01
        ):
            continue
        direction = _canonical_direction(vectors[-1])
        first, second = _normal_plane_basis(direction)
        vertex_indices = np.unique(mesh.faces[face_indices])
        points = mesh.vertices[vertex_indices]
        first_coordinates = points @ first
        second_coordinates = points @ second
        circle_matrix = np.column_stack(
            (
                2 * first_coordinates,
                2 * second_coordinates,
                np.ones(len(points)),
            )
        )
        circle_target = first_coordinates**2 + second_coordinates**2
        center_first, center_second, constant = np.linalg.lstsq(
            circle_matrix,
            circle_target,
            rcond=None,
        )[0]
        radius = math.sqrt(
            max(
                constant + center_first**2 + center_second**2,
                0.0,
            )
        )
        if radius <= radial_tolerance:
            continue
        radial_distances = np.sqrt(
            (first_coordinates - center_first) ** 2
            + (second_coordinates - center_second) ** 2
        )
        radial_error = np.abs(radial_distances - radius)
        if float(np.percentile(radial_error, 95)) > max(
            radial_tolerance,
            radius * 0.002,
        ):
            continue
        axial_coordinates = points @ direction
        start = float(np.min(axial_coordinates))
        end = float(np.max(axial_coordinates))
        if end - start <= radial_tolerance:
            continue

        angles = np.mod(
            np.arctan2(
                second_coordinates - center_second,
                first_coordinates - center_first,
            ),
            2 * math.pi,
        )
        sorted_angles = np.sort(np.unique(np.round(angles, 8)))
        if len(sorted_angles) < 8:
            continue
        gaps = np.diff(np.r_[sorted_angles, sorted_angles[0] + 2 * math.pi])
        angular_coverage = 2 * math.pi - float(np.max(gaps))
        if angular_coverage < math.radians(45):
            continue

        center_perpendicular = center_first * first + center_second * second
        triangle_centers = mesh.triangles_center[face_indices]
        radial_vectors = (
            triangle_centers
            - np.outer(triangle_centers @ direction, direction)
            - center_perpendicular
        )
        orientation = float(
            np.median(np.einsum("ij,ij->i", normals, radial_vectors))
        )
        if abs(orientation) < radius * 0.1:
            continue
        fitted.append(
            {
                "mode": "add" if orientation > 0 else "cut",
                "direction": direction,
                "center": center_perpendicular,
                "start": start,
                "end": end,
                "radius": float(radius),
            }
        )

    # A single source cylinder may be split into multiple faces by unions.
    # Merge those fragments before producing a feature.
    merged: list[dict[str, object]] = []
    for cylinder in fitted:
        direction = cylinder["direction"]
        center = cylinder["center"]
        match = next(
            (
                existing
                for existing in merged
                if existing["mode"] == cylinder["mode"]
                and float(np.dot(existing["direction"], direction)) >= 0.999
                and np.linalg.norm(existing["center"] - center) <= line_tolerance
                and abs(existing["radius"] - cylinder["radius"])
                <= max(radial_tolerance, cylinder["radius"] * 0.003)
            ),
            None,
        )
        if match is None:
            merged.append(cylinder.copy())
        else:
            match["start"] = min(match["start"], cylinder["start"])
            match["end"] = max(match["end"], cylinder["end"])

    additions = [item for item in merged if item["mode"] == "add"]
    cuts = [item for item in merged if item["mode"] == "cut"]
    paired_cut_ids: set[int] = set()
    features: list[OrientedCylinderFeature] = []
    for addition in sorted(additions, key=lambda item: -item["radius"]):
        coaxial_cuts = [
            cut
            for cut in cuts
            if cut["radius"] < addition["radius"]
            and float(np.dot(cut["direction"], addition["direction"])) >= 0.999
            and np.linalg.norm(cut["center"] - addition["center"]) <= line_tolerance
            and abs(cut["start"] - addition["start"]) <= line_tolerance
            and abs(cut["end"] - addition["end"]) <= line_tolerance
        ]
        inner = max(coaxial_cuts, key=lambda item: item["radius"], default=None)
        if inner is not None:
            paired_cut_ids.add(id(inner))
        direction = addition["direction"]
        origin = addition["center"] + direction * addition["start"]
        features.append(
            OrientedCylinderFeature(
                mode="add",
                origin=tuple(_clean(value) for value in origin),
                direction=tuple(_clean(value) for value in direction),
                depth=_clean(addition["end"] - addition["start"]),
                radius=_clean(addition["radius"]),
                inner_radius=(
                    _clean(inner["radius"]) if inner is not None else None
                ),
            )
        )
    for cut in cuts:
        if id(cut) in paired_cut_ids:
            continue
        direction = cut["direction"]
        origin = cut["center"] + direction * cut["start"]
        features.append(
            OrientedCylinderFeature(
                mode="cut",
                origin=tuple(_clean(value) for value in origin),
                direction=tuple(_clean(value) for value in direction),
                depth=_clean(cut["end"] - cut["start"]),
                radius=_clean(cut["radius"]),
            )
        )
    return features


def generate_cylindrical_stock_candidates(
    data: MeshData,
    name: str,
) -> list[ReconstructionPlan]:
    """Promote a dominant fitted cardinal cylinder to the stock feature."""

    candidates: list[ReconstructionPlan] = []
    extents = np.asarray(data.mesh.extents, dtype=float)
    tolerance = max(data.diagonal * 0.002, 0.03)
    for fitted in _mesh_cylindrical_features(data):
        if fitted.mode != "add" or fitted.inner_radius is not None:
            continue
        direction = np.asarray(fitted.direction, dtype=float)
        axis_index = int(np.argmax(np.abs(direction)))
        if abs(direction[axis_index]) < 0.999:
            continue
        axis = (Axis.X, Axis.Y, Axis.Z)[axis_index]
        transverse = np.delete(extents, axis_index)
        if (
            abs(fitted.depth - extents[axis_index]) > tolerance
            or max(abs(transverse - fitted.radius * 2.0))
            > max(tolerance, fitted.radius * 0.03)
        ):
            continue
        origin = np.asarray(fitted.origin, dtype=float)
        endpoint = origin + direction * fitted.depth
        start = min(origin[axis_index], endpoint[axis_index])
        center = _project(origin.reshape(1, 3), axis)[0]
        candidates.append(
            ReconstructionPlan(
                name=name,
                base=CylinderFeature(
                    axis=axis,
                    start=float(start),
                    # Keep the stock infinitesimally inside the measured end
                    # plane. A rounded-up fitted depth can create a sliver in
                    # the boolean residual and collapse a four-point cut
                    # profile into the wrong triangle.
                    depth=float(
                        fitted.depth
                        - max(data.diagonal * 1e-5, 0.001)
                    ),
                    center=(float(center[0]), float(center[1])),
                    radius=float(fitted.radius),
                ),
                assumptions=[
                    "Recovered the dominant measured cylindrical wall as the "
                    "primary stock before subtracting cross-axis residuals."
                ],
            )
        )
    return candidates


def generate_oriented_cylinder_candidates(
    data: MeshData,
    source: ReconstructionPlan,
    *,
    cuts_only: bool = False,
) -> list[ReconstructionPlan]:
    tolerance = max(data.diagonal * 0.002, 0.025)
    surface_graph = detect_surface_graph(data)
    existing_holes = _existing_round_holes(source)
    features: list[OrientedCylinderFeature | RoundHoleFeature] = []
    for fitted in _mesh_cylindrical_features(data):
        direction = np.asarray(fitted.direction, dtype=float)
        fitted_origin = np.asarray(fitted.origin, dtype=float)
        support_patch_id, terminating_patch_id = cylinder_support_patches(
            surface_graph,
            fitted_origin,
            direction,
            fitted.depth,
            tolerance,
            fitted.radius,
        )
        fitted.support_patch_id = support_patch_id
        fitted.terminating_patch_id = terminating_patch_id
        fitted.through = bool(
            support_patch_id is not None
            and terminating_patch_id is not None
            and support_patch_id != terminating_patch_id
        )
        cardinal_index = int(np.argmax(np.abs(direction)))
        if fitted.mode == "add" and abs(direction[cardinal_index]) >= 0.999:
            axis = (Axis.X, Axis.Y, Axis.Z)[cardinal_index]
            origin = np.asarray(fitted.origin, dtype=float)
            endpoint = origin + direction * fitted.depth
            center = _project(origin.reshape(1, 3), axis)[0]
            fitted_start = min(
                float(origin[cardinal_index]),
                float(endpoint[cardinal_index]),
            )
            fitted_end = max(
                float(origin[cardinal_index]),
                float(endpoint[cardinal_index]),
            )
            already_profiled = False
            for operation in [source.base, *source.operations]:
                if (
                    getattr(operation, "axis", None) != axis
                    or getattr(operation, "mode", "add") != "add"
                    or not hasattr(operation, "outer")
                ):
                    continue
                operation_start = float(getattr(operation, "start", 0.0))
                operation_end = operation_start + float(
                    getattr(operation, "depth", 0.0)
                )
                if (
                    abs(operation_start - fitted_start) > tolerance
                    or abs(operation_end - fitted_end) > tolerance
                ):
                    continue
                profiles = [operation.outer]
                profiles.extend(getattr(operation, "additional_regions", []))
                if any(
                    isinstance(profile, CircleProfile)
                    and np.linalg.norm(
                        np.asarray(profile.center) - center
                    )
                    <= tolerance
                    and abs(profile.radius - fitted.radius) <= tolerance
                    for profile in profiles
                ):
                    already_profiled = True
                    break
            if already_profiled:
                continue
        if fitted.mode != "cut" or abs(direction[cardinal_index]) < 0.999:
            features.append(fitted)
            continue

        axis = (Axis.X, Axis.Y, Axis.Z)[cardinal_index]
        origin = np.asarray(fitted.origin, dtype=float)
        endpoint = origin + direction * fitted.depth
        center = _project(origin.reshape(1, 3), axis)[0]
        start = min(float(origin[cardinal_index]), float(endpoint[cardinal_index]))
        end = max(float(origin[cardinal_index]), float(endpoint[cardinal_index]))
        if any(
            _round_cut_covers(
                existing,
                axis=axis,
                center=center,
                radius=fitted.radius,
                start=start,
                end=end,
                tolerance=tolerance,
            )
            or _round_cut_substantially_covers(
                existing,
                axis=axis,
                center=center,
                radius=fitted.radius,
                start=start,
                end=end,
                tolerance=tolerance,
                minimum_fraction=0.97,
            )
            for existing in existing_holes
        ):
            continue
        bounds = data.mesh.bounds[:, cardinal_index]
        matching_hole = next(
            (
                feature
                for feature in features
                if isinstance(feature, RoundHoleFeature)
                and feature.axis == axis
                and np.linalg.norm(np.asarray(feature.center) - center) <= tolerance
                and abs(feature.diameter / 2.0 - fitted.radius) <= tolerance
            ),
            None,
        )
        if matching_hole is not None:
            merged_start = min(matching_hole.start, start)
            merged_end = max(matching_hole.start + matching_hole.depth, end)
            matching_hole.start = _clean(merged_start)
            matching_hole.depth = _clean(merged_end - merged_start)
            matching_hole.through = (
                merged_start <= float(bounds[0]) + tolerance
                and merged_end >= float(bounds[1]) - tolerance
            )
        else:
            features.append(
                RoundHoleFeature(
                    axis=axis,
                    center=(_clean(center[0]), _clean(center[1])),
                    diameter=_clean(fitted.radius * 2.0),
                    start=_clean(start),
                    depth=_clean(end - start),
                    through=(
                        start <= float(bounds[0]) + tolerance
                        and end >= float(bounds[1]) - tolerance
                    ),
                    support_patch_id=support_patch_id,
                    terminating_patch_id=terminating_patch_id,
                )
            )
    if cuts_only:
        features = [
            feature
            for feature in features
            if isinstance(feature, RoundHoleFeature)
            or feature.mode == "cut"
        ]
    if not features:
        return []

    feature_groups = [features]
    cardinal_groups: dict[Axis, list[OrientedCylinderFeature | RoundHoleFeature]] = {}
    for feature in features:
        if isinstance(feature, RoundHoleFeature):
            cardinal_groups.setdefault(feature.axis, []).append(feature)
    feature_groups.extend(
        group
        for group in cardinal_groups.values()
        if 1 < len(group) < len(features)
    )
    feature_groups.extend([feature] for feature in features)
    candidates: list[ReconstructionPlan] = []
    for group in feature_groups[:12]:
        round_holes = [
            feature for feature in group if isinstance(feature, RoundHoleFeature)
        ]
        plan = _replace_profile_circles_with_round_holes(
            source,
            round_holes,
            tolerance,
        )
        plan.operations.extend(
            feature.model_copy(deep=True)
            for feature in group
            if not isinstance(feature, RoundHoleFeature)
        )
        plan.assumptions.append(
            "Recovered smooth mesh patches as editable analytic cylinders with "
            "their measured 3D axes, radii, and add/cut orientation."
        )
        candidates.append(plan)
    return candidates


def generate_arbitrary_axis_prismatic_candidates(
    data: MeshData,
    name: str,
) -> list[PlanCandidate]:
    surface_graph = detect_surface_graph(data)
    directions: list[np.ndarray] = []
    for feature in _mesh_cylindrical_features(data):
        direction = np.asarray(feature.direction, dtype=np.float64)
        direction /= np.linalg.norm(direction)
        if max(abs(direction)) >= 0.999:
            continue
        if not any(abs(float(np.dot(direction, found))) >= 0.999 for found in directions):
            directions.append(direction)

    # An all-planar oblique extrusion has no cylindrical wall from which to
    # infer its axis. Its two end caps do, however, contribute sizeable groups
    # of triangles with equal and opposite normals. Rank those paired normal
    # groups by their supported area so the true caps are tried before pairs
    # of narrower side walls.
    paired_normals: dict[
        tuple[float, float, float],
        dict[str, object],
    ] = {}
    for normal, area in zip(
        data.mesh.face_normals,
        data.mesh.area_faces,
        strict=True,
    ):
        direction = _canonical_direction(normal)
        key = tuple(float(value) for value in np.round(direction, 4))
        group = paired_normals.setdefault(
            key,
            {
                "direction": direction,
                "positive_area": 0.0,
                "negative_area": 0.0,
            },
        )
        side = (
            "positive_area"
            if float(np.dot(normal, direction)) >= 0
            else "negative_area"
        )
        group[side] = float(group[side]) + float(area)
    minimum_cap_area = max(float(data.mesh.area) * 0.002, data.diagonal**2 * 1e-6)
    planar_directions = sorted(
        (
            group
            for group in paired_normals.values()
            if min(
                float(group["positive_area"]),
                float(group["negative_area"]),
            )
            >= minimum_cap_area
        ),
        key=lambda group: -min(
            float(group["positive_area"]),
            float(group["negative_area"]),
        ),
    )
    for group in planar_directions:
        direction = np.asarray(group["direction"], dtype=np.float64)
        if max(abs(direction)) >= 0.999:
            continue
        if not any(
            abs(float(np.dot(direction, found))) >= 0.999
            for found in directions
        ):
            directions.append(direction)

    candidates: list[PlanCandidate] = []
    for direction in directions[:6]:
        first, second = _normal_plane_basis(direction)
        local_vertices = np.column_stack(
            (
                data.mesh.vertices @ first,
                data.mesh.vertices @ second,
                data.mesh.vertices @ direction,
            )
        )
        local_mesh = trimesh.Trimesh(
            vertices=local_vertices,
            faces=data.mesh.faces.copy(),
            process=False,
        )
        local_mesh.fix_normals(multibody=True)
        local_report = data.report.model_copy(
            update={
                "dimensions_mm": tuple(float(value) for value in local_mesh.extents),
            }
        )
        local_data = MeshData(
            mesh=local_mesh,
            report=local_report,
            source_path=data.source_path,
        )
        local_candidates = generate_prismatic_candidates(local_data, name)
        local_candidates.extend(generate_layered_candidates(local_data, name))
        for local_candidate in local_candidates:
            base = local_candidate.plan.base
            if base.axis != Axis.Z:
                continue
            if isinstance(base, CylinderFeature):
                outer: Profile = CircleProfile(
                    center=base.center,
                    radius=base.radius,
                )
                holes: list[Profile] = (
                    [
                        CircleProfile(
                            center=base.center,
                            radius=base.inner_radius,
                        )
                    ]
                    if base.inner_radius is not None
                    else []
                )
                start = base.start
                depth = base.depth
            elif isinstance(base, ExtrudeFeature):
                outer = base.outer
                holes = list(base.holes)
                start = base.start
                depth = base.depth
            else:
                continue
            origin = direction * start
            cap_tolerance = max(data.diagonal * 0.001, 0.01)
            parallel_patches = [
                patch
                for patch in surface_graph.planar_patches
                if abs(float(patch.normal @ direction)) >= 0.999
            ]
            support_patch = min(
                parallel_patches,
                key=lambda patch: abs(float(patch.origin @ direction) - start),
                default=None,
            )
            termination_patch = min(
                parallel_patches,
                key=lambda patch: abs(
                    float(patch.origin @ direction) - (start + depth)
                ),
                default=None,
            )
            support_patch_id = (
                support_patch.patch_id
                if support_patch is not None
                and abs(float(support_patch.origin @ direction) - start)
                <= cap_tolerance
                else None
            )
            terminating_patch_id = (
                termination_patch.patch_id
                if termination_patch is not None
                and abs(
                    float(termination_patch.origin @ direction)
                    - (start + depth)
                )
                <= cap_tolerance
                else None
            )
            oriented = ReconstructionPlan(
                name=local_candidate.plan.name,
                base=OrientedExtrudeFeature(
                    origin=tuple(float(value) for value in origin),
                    plane_normal=tuple(float(value) for value in direction),
                    direction=tuple(float(value) for value in direction),
                    x_direction=tuple(float(value) for value in first),
                    depth=_clean(depth),
                    outer=outer,
                    holes=holes,
                    support_patch_id=support_patch_id,
                    terminating_patch_id=terminating_patch_id,
                    extent_kind=(
                        "up_to_patch"
                        if terminating_patch_id is not None
                        else "distance"
                    ),
                ),
                assumptions=[
                    *local_candidate.plan.assumptions,
                    "Recovered the extrusion sketch on its measured non-cardinal "
                    "3D plane, preserving analytic arcs and holes.",
                ],
            )
            for operation in local_candidate.plan.operations:
                if (
                    isinstance(operation, BooleanExtrudeFeature)
                    and operation.axis == Axis.Z
                ):
                    operation_origin = direction * operation.start
                    operation_termination = operation.start + operation.depth
                    operation_support = min(
                        parallel_patches,
                        key=lambda patch: abs(
                            float(patch.origin @ direction) - operation.start
                        ),
                        default=None,
                    )
                    operation_end_patch = min(
                        parallel_patches,
                        key=lambda patch: abs(
                            float(patch.origin @ direction)
                            - operation_termination
                        ),
                        default=None,
                    )
                    operation_support_id = (
                        operation_support.patch_id
                        if operation_support is not None
                        and abs(
                            float(operation_support.origin @ direction)
                            - operation.start
                        )
                        <= cap_tolerance
                        else None
                    )
                    operation_end_id = (
                        operation_end_patch.patch_id
                        if operation_end_patch is not None
                        and abs(
                            float(operation_end_patch.origin @ direction)
                            - operation_termination
                        )
                        <= cap_tolerance
                        else None
                    )
                    oriented.operations.append(
                        OrientedBooleanExtrudeFeature(
                            mode=operation.mode,
                            origin=tuple(
                                float(value) for value in operation_origin
                            ),
                            plane_normal=tuple(
                                float(value) for value in direction
                            ),
                            direction=tuple(
                                float(value) for value in direction
                            ),
                            x_direction=tuple(
                                float(value) for value in first
                            ),
                            depth=_clean(operation.depth),
                            outer=operation.outer,
                            holes=list(operation.holes),
                            additional_regions=list(
                                operation.additional_regions
                            ),
                            support_patch_id=operation_support_id,
                            terminating_patch_id=operation_end_id,
                            extent_kind=(
                                "up_to_patch"
                                if operation_end_id is not None
                                else "distance"
                            ),
                        )
                    )
            candidates.append(
                PlanCandidate(
                    plan=oriented,
                    heuristic_score=local_candidate.heuristic_score - 0.25,
                    section_consistency=local_candidate.section_consistency,
                )
            )
    return candidates


def generate_analytic_cylinder_separation_candidates(
    data: MeshData,
    name: str,
) -> list[PlanCandidate]:
    """Separate an oblique additive cylinder from cardinal slice layers.

    Layered silhouette reconstruction can approximate an oblique round feature
    with several polygonal slabs.  Removing that cylinder's cross-section from
    each residual layer and adding one analytic cylinder back yields a much
    cleaner feature tree and restores the true curved surface.
    """

    tolerance = max(data.diagonal * 0.0008, 0.015)
    candidates: list[PlanCandidate] = []
    for cylinder in _mesh_cylindrical_features(data):
        if (
            cylinder.mode != "add"
            or cylinder.inner_radius is not None
            or max(abs(value) for value in cylinder.direction) >= 0.999
        ):
            continue
        direction = np.asarray(cylinder.direction, dtype=np.float64)
        direction /= np.linalg.norm(direction)
        origin = np.asarray(cylinder.origin, dtype=np.float64)
        axis_end = origin + direction * cylinder.depth
        for slice_axis, (
            slice_normal,
            first,
            second,
            slice_index,
        ) in AXES.items():
            if abs(float(np.dot(direction, slice_normal))) > 0.01:
                continue
            radial_in_plane = np.cross(slice_normal, direction)
            radial_length = np.linalg.norm(radial_in_plane)
            if radial_length <= 1e-9:
                continue
            radial_in_plane /= radial_length
            levels = _axis_levels(data, slice_axis)
            if len(levels) < 2 or len(levels) > 12:
                continue
            for separation_buffer in (
                tolerance * 0.15,
                cylinder.radius * 0.35,
            ):
                layers: list[
                    tuple[float, float, list[tuple[Profile, list[Profile]]]]
                ] = []
                for start, end in zip(levels, levels[1:], strict=False):
                    if end - start <= tolerance:
                        continue
                    location = (start + end) / 2
                    section = section_shape(
                        data,
                        slice_axis,
                        location,
                    )
                    if section is None:
                        continue
                    geometry = section.polygon
                    offset = location - float(origin[slice_index])
                    if abs(offset) < cylinder.radius:
                        half_width = math.sqrt(
                            max(cylinder.radius**2 - offset**2, 0.0)
                        )
                        rectangle_points = (
                            origin + radial_in_plane * half_width,
                            axis_end + radial_in_plane * half_width,
                            axis_end - radial_in_plane * half_width,
                            origin - radial_in_plane * half_width,
                        )
                        rectangle = Polygon(
                            [
                                (
                                    float(np.dot(point, first)),
                                    float(np.dot(point, second)),
                                )
                                for point in rectangle_points
                            ]
                        )
                        geometry = geometry.difference(
                            rectangle.buffer(separation_buffer)
                        )
                    regions = (
                        list(geometry.geoms)
                        if isinstance(geometry, MultiPolygon)
                        else [geometry]
                    )
                    fitted_regions: list[tuple[Profile, list[Profile]]] = []
                    for region in regions:
                        if (
                            not isinstance(region, Polygon)
                            or region.area <= tolerance * tolerance * 4
                        ):
                            continue
                        outer = _profile_from_ring(
                            np.asarray(region.exterior.coords),
                            tolerance,
                        )
                        if outer is None:
                            continue
                        holes = [
                            fitted[0]
                            for interior in region.interiors
                            if (
                                fitted := _profile_from_ring(
                                    np.asarray(interior.coords),
                                    tolerance,
                                )
                            )
                            is not None
                        ]
                        fitted_regions.append((outer[0], holes))
                    if fitted_regions:
                        layers.append((start, end, fitted_regions))
                if not layers:
                    continue
                base_layer, *remaining_layers = layers
                base_start, base_end, base_regions = base_layer
                base_outer, base_holes = base_regions[0]
                operations: list[
                    BooleanExtrudeFeature | OrientedCylinderFeature
                ] = [
                    BooleanExtrudeFeature(
                        mode="add",
                        axis=slice_axis,
                        start=base_start,
                        depth=base_end - base_start,
                        outer=outer,
                        holes=holes,
                    )
                    for outer, holes in base_regions[1:]
                ]
                for layer_start, layer_end, regions in remaining_layers:
                    operations.extend(
                        BooleanExtrudeFeature(
                            mode="add",
                            axis=slice_axis,
                            start=layer_start,
                            depth=layer_end - layer_start,
                            outer=outer,
                            holes=holes,
                        )
                        for outer, holes in regions
                    )
                operations.append(cylinder.model_copy(deep=True))
                plan = ReconstructionPlan(
                    name=name,
                    base=ExtrudeFeature(
                        axis=slice_axis,
                        start=base_start,
                        depth=base_end - base_start,
                        outer=base_outer,
                        holes=base_holes,
                    ),
                    operations=operations,
                    assumptions=[
                        f"Separated a polygonally layered {slice_axis.value}-axis "
                        "approximation into one measured analytic oblique cylinder "
                        "and its surrounding prismatic features."
                    ],
                )
                candidates.append(
                    PlanCandidate(
                        plan=plan,
                        heuristic_score=0.002 + len(operations) * 0.0001,
                        section_consistency=1.0,
                    )
                )
    return candidates


def _profile_arc_observations(
    profile: Profile,
) -> list[tuple[np.ndarray, float, float, float]]:
    if not isinstance(profile, PathProfile):
        return []
    observations: list[tuple[np.ndarray, float, float, float]] = []
    start = np.asarray(profile.start, dtype=np.float64)
    for segment in profile.segments:
        end = np.asarray(segment.end, dtype=np.float64)
        if isinstance(segment, ArcSegment):
            fitted = _circle_values(
                np.vstack((start, np.asarray(segment.mid), end))
            )
            if fitted is not None:
                center_x, center_y, radius, _ = fitted
                center = np.asarray((center_x, center_y))
                start_angle = math.atan2(
                    start[1] - center_y,
                    start[0] - center_x,
                )
                end_angle = math.atan2(
                    end[1] - center_y,
                    end[0] - center_x,
                )
                mid_angle = math.atan2(
                    segment.mid[1] - center_y,
                    segment.mid[0] - center_x,
                )
                while mid_angle - start_angle > math.pi:
                    mid_angle -= 2 * math.pi
                while mid_angle - start_angle < -math.pi:
                    mid_angle += 2 * math.pi
                while end_angle - mid_angle > math.pi:
                    end_angle -= 2 * math.pi
                while end_angle - mid_angle < -math.pi:
                    end_angle += 2 * math.pi
                observations.append(
                    (center, radius, start_angle, end_angle)
                )
        start = end
    return observations


def generate_partial_cone_adaptive_candidates(
    data: MeshData,
    name: str,
) -> list[PlanCandidate]:
    """Recover partial conical chamfers whose section arcs change linearly."""

    bounds = data.mesh.bounds
    center_tolerance = max(data.diagonal * 0.002, 0.025)
    candidates: list[PlanCandidate] = []
    for cone_axis, (_, _, _, axis_index) in AXES.items():
        axis_min = float(bounds[0, axis_index])
        axis_max = float(bounds[1, axis_index])
        axis_mid = (axis_min + axis_max) / 2
        center_section = section_shape(data, cone_axis, axis_mid)
        if center_section is None:
            continue
        center_profiles = [
            center_section.outer,
            *center_section.holes,
            *[
                profile
                for region in center_section.additional_regions
                for profile in (region.outer, *region.holes)
            ],
        ]
        seeds = [
            observation
            for profile in center_profiles
            for observation in _profile_arc_observations(profile)
            if observation[1] < data.diagonal * 0.2
        ]
        recovered: list[
            tuple[
                np.ndarray,
                float,
                float,
                float,
                float,
                float,
            ]
        ] = []
        for center, base_radius, start_angle, end_angle in seeds:
            end_fits: list[tuple[float, float]] = []
            observed_centers = [center]
            valid = True
            for axis_end in (axis_min, axis_max):
                positions = np.linspace(
                    axis_mid,
                    axis_end,
                    15,
                    endpoint=False,
                )[1:]
                positions = np.r_[positions, axis_end * 0.999 + axis_mid * 0.001]
                observations: list[tuple[float, float]] = []
                for position in positions:
                    section = section_shape(
                        data,
                        cone_axis,
                        float(position),
                    )
                    if section is None:
                        continue
                    profiles = [
                        section.outer,
                        *section.holes,
                        *[
                            profile
                            for region in section.additional_regions
                            for profile in (region.outer, *region.holes)
                        ],
                    ]
                    matches = [
                        item
                        for profile in profiles
                        for item in _profile_arc_observations(profile)
                        if np.linalg.norm(item[0] - center)
                        <= center_tolerance
                        and abs(item[1] - base_radius)
                        <= max(base_radius * 0.35, center_tolerance)
                    ]
                    if matches:
                        match = min(
                            matches,
                            key=lambda item: abs(item[1] - base_radius),
                        )
                        observed_centers.append(match[0])
                        observations.append((float(position), match[1]))
                change_tolerance = max(center_tolerance, base_radius * 0.015)
                changed = [
                    item
                    for item in observations
                    if item[1] > base_radius + change_tolerance
                ]
                if len(changed) < 3:
                    valid = False
                    break
                positions_array = np.asarray(
                    [position for position, _ in changed]
                )
                radii_array = np.asarray(
                    [radius for _, radius in changed]
                )
                slope, intercept = np.polyfit(
                    positions_array,
                    radii_array,
                    1,
                )
                if abs(slope) < 0.2:
                    valid = False
                    break
                transition = float((base_radius - intercept) / slope)
                end_radius = float(slope * axis_end + intercept)
                if (
                    transition < min(axis_mid, axis_end)
                    or transition > max(axis_mid, axis_end)
                    or end_radius <= base_radius + change_tolerance
                ):
                    valid = False
                    break
                end_fits.append((transition, end_radius))
            if valid and len(end_fits) == 2:
                recovered.append(
                    (
                        np.mean(observed_centers, axis=0),
                        base_radius,
                        start_angle,
                        end_angle,
                        end_fits[0][0],
                        end_fits[1][0],
                    )
                )
        if len(recovered) < 2:
            continue
        transverse_axes = [
            axis for axis in AXES if axis != cone_axis
        ]
        transverse_axis = min(
            transverse_axes,
            key=lambda axis: data.mesh.extents[AXES[axis][3]],
        )
        adaptive = generate_adaptive_layer_candidates(
            data,
            name,
            transverse_axis,
            24,
        )
        if not adaptive:
            continue
        plan = adaptive[0].model_copy(deep=True)
        recovered_radii = [item[1] for item in recovered]
        shared_radius = (
            round(float(np.mean(recovered_radii)), 2)
            if np.ptp(recovered_radii)
            <= max(center_tolerance, float(np.mean(recovered_radii)) * 0.02)
            else None
        )
        negative_transitions = [item[4] for item in recovered]
        positive_transitions = [item[5] for item in recovered]
        shared_negative_transition = (
            max(negative_transitions)
            if np.ptp(negative_transitions) <= center_tolerance
            else None
        )
        shared_positive_transition = (
            min(positive_transitions)
            if np.ptp(positive_transitions) <= center_tolerance
            else None
        )
        recovered_centers = np.asarray([item[0] for item in recovered])
        shared_center_coordinates: list[float | None] = [
            (
                _clean(float(np.mean(recovered_centers[:, coordinate])))
                if np.ptp(recovered_centers[:, coordinate])
                <= center_tolerance
                else None
            )
            for coordinate in range(2)
        ]

        def sector(
            center: np.ndarray,
            radius: float,
            angle_start: float,
            angle_end: float,
        ) -> PathProfile:
            def point(angle: float) -> tuple[float, float]:
                return (
                    float(center[0] + radius * math.cos(angle)),
                    float(center[1] + radius * math.sin(angle)),
                )

            start_point = point(angle_start)
            return PathProfile(
                start=(float(center[0]), float(center[1])),
                segments=[
                    LineSegment(end=start_point),
                    ArcSegment(
                        mid=point((angle_start + angle_end) / 2),
                        end=point(angle_end),
                    ),
                    LineSegment(
                        end=(float(center[0]), float(center[1]))
                    ),
                ],
            )

        for (
            center,
            base_radius,
            start_angle,
            end_angle,
            negative_transition,
            positive_transition,
        ) in sorted(recovered, key=lambda item: item[0][1])[:4]:
            center = center.copy()
            for coordinate, shared_coordinate in enumerate(
                shared_center_coordinates
            ):
                center[coordinate] = (
                    shared_coordinate
                    if shared_coordinate is not None
                    else round(float(center[coordinate]) / 0.005) * 0.005
                )
            if shared_radius is not None:
                base_radius = shared_radius
            if shared_negative_transition is not None:
                negative_transition = shared_negative_transition
            if shared_positive_transition is not None:
                positive_transition = shared_positive_transition
            angle_start = round(start_angle / (math.pi / 2)) * (math.pi / 2)
            angle_end = round(end_angle / (math.pi / 2)) * (math.pi / 2)
            if angle_end < angle_start:
                angle_start, angle_end = angle_end, angle_start
            if abs(angle_end - angle_start) < math.pi / 4:
                continue
            for start, end in (
                (positive_transition, axis_max),
                (axis_min, negative_transition),
            ):
                feature_start = _clean(start)
                feature_depth = _clean(end - start)
                if start > axis_mid:
                    start_radius = base_radius
                    end_radius = base_radius + feature_depth
                else:
                    start_radius = base_radius + feature_depth
                    end_radius = base_radius
                plan.operations.append(
                    TaperedAddFeature(
                        axis=cone_axis,
                        start=feature_start,
                        depth=feature_depth,
                        start_outer=sector(
                            center,
                            start_radius,
                            angle_start,
                            angle_end,
                        ),
                        end_outer=sector(
                            center,
                            end_radius,
                            angle_start,
                            angle_end,
                        ),
                    )
                )
        if not any(
            isinstance(operation, TaperedAddFeature)
            for operation in plan.operations
        ):
            continue
        plan.assumptions.append(
            "Recovered linearly changing partial circular arcs as analytic "
            "conical chamfer sectors."
        )
        candidates.append(
            PlanCandidate(
                plan=plan,
                heuristic_score=0.001,
                section_consistency=1.0,
            )
        )
    return candidates


def generate_conical_boundary_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    radial_tolerance = max(data.diagonal * 0.00025, 0.002)
    center_tolerance = max(data.diagonal * 0.002, 0.02)
    candidates: list[ReconstructionPlan] = []
    base_axis = getattr(source.base, "axis", None)
    for axis in AXES:
        if axis == base_axis:
            continue
        levels = _axis_levels(data, axis)
        for start, end in zip(levels, levels[1:], strict=False):
            depth = end - start
            if depth <= radial_tolerance:
                continue
            sampled: list[
                tuple[float, list[tuple[CircleProfile, bool]]]
            ] = []
            for fraction in (0.1, 0.3, 0.5, 0.7, 0.9):
                location = start + depth * fraction
                section = section_shape(data, axis, location)
                if section is not None:
                    sampled.append(
                        (location, _section_round_boundaries(section))
                    )
            if len(sampled) < 4:
                continue
            for seed, seed_is_hole in sampled[0][1]:
                observations: list[tuple[float, CircleProfile]] = []
                for location, boundaries in sampled:
                    matches = [
                        circle
                        for circle, is_hole in boundaries
                        if is_hole == seed_is_hole
                        and np.linalg.norm(
                            np.asarray(circle.center)
                            - np.asarray(seed.center)
                        )
                        <= center_tolerance
                    ]
                    if matches:
                        observations.append(
                            (
                                location,
                                min(
                                    matches,
                                    key=lambda circle: abs(
                                        circle.radius - seed.radius
                                    ),
                                ),
                            )
                        )
                if len(observations) < 4:
                    continue
                locations = np.asarray(
                    [location for location, _ in observations]
                )
                radii = np.asarray(
                    [circle.radius for _, circle in observations]
                )
                slope, intercept = np.polyfit(locations, radii, 1)
                predicted = slope * locations + intercept
                start_radius = float(slope * start + intercept)
                end_radius = float(slope * end + intercept)
                radius_change = abs(end_radius - start_radius)
                residual = float(np.max(np.abs(predicted - radii)))
                if (
                    min(start_radius, end_radius) <= radial_tolerance
                    or radius_change <= max(
                        radial_tolerance * 2,
                        float(np.mean(radii)) * 0.01,
                    )
                    or residual > max(radial_tolerance, radius_change * 0.05)
                ):
                    continue
                center = np.mean(
                    [np.asarray(circle.center) for _, circle in observations],
                    axis=0,
                )
                for mode in ("add", "cut"):
                    plan = source.model_copy(deep=True)
                    feature_values = {
                        "axis": axis,
                        "center": (
                            _clean(center[0]),
                            _clean(center[1]),
                        ),
                        "start": _clean(start),
                        "depth": _clean(depth),
                        "start_diameter": _clean(start_radius * 2),
                        "end_diameter": _clean(end_radius * 2),
                    }
                    if mode == "add":
                        plan.operations.append(
                            ConicalAddFeature(**feature_values)
                        )
                    else:
                        plan.operations.append(
                            ConicalHoleFeature(**feature_values)
                        )
                    plan.assumptions.append(
                        "Recovered a linearly varying circular boundary as an "
                        f"analytic conical {mode} feature."
                    )
                    candidates.append(plan)
    return candidates


def _circular_holes(section: SectionShape) -> list[CircleProfile]:
    holes = [
        profile for profile in section.holes if isinstance(profile, CircleProfile)
    ]
    for region in section.additional_regions:
        holes.extend(
            profile for profile in region.holes if isinstance(profile, CircleProfile)
        )
    return holes


@dataclass(slots=True)
class _RoundCutExtent:
    """One analytic circular cut, including its extent normal to the sketch.

    Center and radius alone are not enough to decide that a reconstructed
    hole already exists.  A counterbore or one adaptive-layer fragment can
    share both values with a longer bore.  Keeping the axial interval in the
    identity prevents that fragment from suppressing the complete feature.
    """

    axis: Axis
    center: np.ndarray
    radius: float
    start: float
    end: float


def _existing_round_holes(plan: ReconstructionPlan) -> list[_RoundCutExtent]:
    found: list[_RoundCutExtent] = []
    base = plan.base
    if isinstance(base, CylinderFeature) and base.inner_radius is not None:
        found.append(
            _RoundCutExtent(
                axis=base.axis,
                center=np.asarray(base.center),
                radius=base.inner_radius,
                start=base.start,
                end=base.start + base.depth,
            )
        )
    for operation in plan.operations:
        if isinstance(operation, RoundHoleFeature):
            found.append(
                _RoundCutExtent(
                    axis=operation.axis,
                    center=np.asarray(operation.center),
                    radius=operation.diameter / 2,
                    start=operation.start,
                    end=operation.start + operation.depth,
                )
            )
        elif (
            isinstance(operation, OrientedCylinderFeature)
            and operation.mode == "cut"
        ):
            direction = np.asarray(operation.direction, dtype=float)
            direction_length = float(np.linalg.norm(direction))
            if direction_length <= 1e-12:
                continue
            direction /= direction_length
            axis_index = int(np.argmax(np.abs(direction)))
            if abs(direction[axis_index]) < 0.999:
                continue
            axis = (Axis.X, Axis.Y, Axis.Z)[axis_index]
            origin = np.asarray(operation.origin, dtype=float)
            endpoint = origin + direction * operation.depth
            found.append(
                _RoundCutExtent(
                    axis=axis,
                    center=_project(origin.reshape(1, 3), axis)[0],
                    radius=operation.radius,
                    start=min(origin[axis_index], endpoint[axis_index]),
                    end=max(origin[axis_index], endpoint[axis_index]),
                )
            )
    return found


def _round_cut_covers(
    existing: _RoundCutExtent,
    *,
    axis: Axis,
    center: np.ndarray,
    radius: float,
    start: float,
    end: float,
    tolerance: float,
) -> bool:
    return (
        existing.axis == axis
        and np.linalg.norm(existing.center - center) <= tolerance
        and abs(existing.radius - radius) <= tolerance
        and existing.start <= start + tolerance
        and existing.end >= end - tolerance
    )


def _round_cut_substantially_covers(
    existing: _RoundCutExtent,
    *,
    axis: Axis,
    center: np.ndarray,
    radius: float,
    start: float,
    end: float,
    tolerance: float,
    minimum_fraction: float = 0.9,
) -> bool:
    """Accept small section-sampling overrun around an existing bore.

    Sections through a countersink or terminating cap can extend a circle
    cluster slightly beyond its true cylindrical wall.  A nearly complete
    analytic hole should not be replaced by that overrun.  Materially short
    fragments still fail this test and are promoted to the full measured
    interval by ``_round_cut_covers`` callers.
    """

    if (
        existing.axis != axis
        or np.linalg.norm(existing.center - center) > tolerance
        or abs(existing.radius - radius) > tolerance
    ):
        return False
    overlap = max(0.0, min(existing.end, end) - max(existing.start, start))
    return overlap / max(end - start, tolerance) >= minimum_fraction


def _profile_circle_samples(profile: Profile) -> np.ndarray | None:
    """Sample a closed profile without assuming its stored primitive type."""

    if isinstance(profile, PolygonProfile):
        points = np.asarray(profile.points, dtype=float)
        return np.vstack((points, points[0]))
    if isinstance(profile, SplineProfile):
        points = np.asarray(profile.points, dtype=float)
        return np.vstack((points, points[0]))
    if not isinstance(profile, PathProfile):
        return None

    current = np.asarray(profile.start, dtype=float)
    sampled = [current]
    for segment in profile.segments:
        endpoint = np.asarray(segment.end, dtype=float)
        if isinstance(segment, LineSegment):
            sampled.append(endpoint)
        elif isinstance(segment, SplineSegment):
            sampled.extend(np.asarray(segment.points, dtype=float))
            sampled.append(endpoint)
        else:
            fitted = _circle_values(
                np.vstack((current, np.asarray(segment.mid), endpoint))
            )
            if fitted is None:
                return None
            center_x, center_y, radius, _ = fitted
            start_angle = math.atan2(current[1] - center_y, current[0] - center_x)
            mid_angle = math.atan2(
                segment.mid[1] - center_y,
                segment.mid[0] - center_x,
            )
            end_angle = math.atan2(
                endpoint[1] - center_y,
                endpoint[0] - center_x,
            )
            while mid_angle - start_angle > math.pi:
                mid_angle -= 2 * math.pi
            while mid_angle - start_angle < -math.pi:
                mid_angle += 2 * math.pi
            while end_angle - mid_angle > math.pi:
                end_angle -= 2 * math.pi
            while end_angle - mid_angle < -math.pi:
                end_angle += 2 * math.pi
            count = max(4, int(math.ceil(abs(end_angle - start_angle) / (math.pi / 12))))
            angles = np.linspace(start_angle, end_angle, count + 1)[1:]
            sampled.extend(
                np.column_stack(
                    (
                        center_x + radius * np.cos(angles),
                        center_y + radius * np.sin(angles),
                    )
                )
            )
        current = endpoint
    return np.asarray(sampled, dtype=float).reshape(-1, 2)


def _profile_is_full_circle_approximation(
    profile: Profile,
    center: np.ndarray,
    radius: float,
    tolerance: float,
) -> bool:
    """Prove that a closed polygon/path represents one complete circle.

    Layer slicing may store a cylindrical opening as chords, mixed arcs, or a
    periodic spline.  Once the analytic cylinder is known, retaining that
    sampled boundary creates two competing walls.  The radial, closure, area,
    and angular-coverage gates below deliberately reject partial arcs, slots,
    tangent blends, and unrelated polygonal regions.
    """

    if isinstance(profile, CircleProfile):
        return bool(
            np.linalg.norm(np.asarray(profile.center) - center) <= tolerance
            and abs(profile.radius - radius) <= tolerance
        )
    points = _profile_circle_samples(profile)
    if points is None or len(points) < 8:
        return False
    radial_tolerance = max(tolerance, radius * 0.012)
    if np.linalg.norm(points[0] - points[-1]) > radial_tolerance * 2:
        return False
    ring = points[:-1]
    radii = np.linalg.norm(ring - center, axis=1)
    if (
        float(np.max(np.abs(radii - radius))) > radial_tolerance
        or float(np.percentile(np.abs(radii - radius), 90))
        > radial_tolerance * 0.65
    ):
        return False
    angles = np.sort(
        np.mod(
            np.arctan2(
                ring[:, 1] - center[1],
                ring[:, 0] - center[0],
            ),
            2 * math.pi,
        )
    )
    gaps = np.diff(np.r_[angles, angles[0] + 2 * math.pi])
    if float(np.max(gaps)) > math.pi / 2:
        return False
    polygon = Polygon(ring)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    area_ratio = float(polygon.area) / max(math.pi * radius * radius, 1e-12)
    return 0.90 <= area_ratio <= 1.12


def _replace_profile_circles_with_round_holes(
    plan: ReconstructionPlan,
    holes: list[RoundHoleFeature],
    tolerance: float,
) -> ReconstructionPlan:
    """Promote measured circles without leaving duplicate slab geometry.

    Adaptive layers may encode one bore as many circle regions.  Once a
    continuous analytic hole has been recovered, those same-radius regions
    must be removed or their tiny per-layer fit differences produce stepped
    walls.  Different radii and all path profiles are retained so counterbores
    and genuine partial-arc outlines are not flattened into a simple hole.
    """

    cleaned = plan.model_copy(deep=True)
    existing_analytic_holes = [
        operation
        for operation in cleaned.operations
        if isinstance(operation, RoundHoleFeature)
    ]
    analytic_holes = [*existing_analytic_holes, *holes]

    def covering_holes(feature: object) -> list[RoundHoleFeature]:
        axis = getattr(feature, "axis", None)
        feature_start = getattr(feature, "start", None)
        feature_depth = getattr(feature, "depth", None)
        if axis is None or feature_start is None or feature_depth is None:
            return []
        feature_end = float(feature_start) + float(feature_depth)
        return [
            hole
            for hole in analytic_holes
            if hole.axis == axis
            and hole.start <= float(feature_start) + tolerance
            and hole.start + hole.depth >= feature_end - tolerance
        ]

    def matches(profile: Profile, covering: list[RoundHoleFeature]) -> bool:
        return any(
            _profile_is_full_circle_approximation(
                profile,
                np.asarray(hole.center, dtype=float),
                hole.diameter / 2,
                tolerance,
            )
            for hole in covering
        )

    retained_operations = []
    for feature in [cleaned.base, *cleaned.operations]:
        if isinstance(feature, RoundHoleFeature) and any(
            replacement.axis == feature.axis
            and np.linalg.norm(
                np.asarray(replacement.center) - np.asarray(feature.center)
            )
            <= tolerance
            and abs(replacement.diameter - feature.diameter) <= tolerance * 2
            and replacement.start <= feature.start + tolerance
            and replacement.start + replacement.depth
            >= feature.start + feature.depth - tolerance
            for replacement in holes
        ):
            # The measured complete bore replaces a shorter feature with the
            # same sketch circle. Leaving both in the tree is geometrically
            # redundant and makes dimension editing ambiguous.
            continue
        covering = covering_holes(feature)
        if not covering:
            if feature is not cleaned.base:
                retained_operations.append(feature)
            continue

        feature_holes = getattr(feature, "holes", None)
        if feature_holes is not None and not (
            isinstance(feature, BooleanExtrudeFeature)
            and feature.mode == "cut"
        ):
            feature.holes = [
                profile
                for profile in feature_holes
                if not matches(profile, covering)
            ]

        if (
            isinstance(feature, BooleanExtrudeFeature)
            and feature.mode == "cut"
        ):
            regions = [feature.outer, *feature.additional_regions]
            regions = [
                profile for profile in regions if not matches(profile, covering)
            ]
            if not regions:
                continue
            feature.outer = regions[0]
            feature.additional_regions = regions[1:]

        if feature is not cleaned.base:
            retained_operations.append(feature)

    cleaned.operations = retained_operations
    cleaned.operations.extend(hole.model_copy(deep=True) for hole in holes)
    return cleaned


def _replace_fragmented_profile_holes(
    plan: ReconstructionPlan,
    holes: list[RoundHoleFeature],
    tolerance: float,
) -> ReconstructionPlan:
    return _replace_profile_circles_with_round_holes(
        plan,
        holes,
        tolerance,
    )


def generate_embedded_circle_promotion_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    """Lift repeated layer-profile circles into ordered hole features.

    A layered reconstruction can reproduce a bore by putting essentially the
    same circle in every additive slab.  That is geometrically close, but it
    leaves a stepped wall and makes the diameter impossible to edit as one
    feature.  Cluster those observations independently for every sketch plane
    and radius, remove only the covered circle profiles, and append continuous
    analytic cuts at the point in the ordered tree where all stock exists.

    Path profiles are deliberately excluded: a path containing an arc may be
    a partial circle, tangent blend, slot, or arbitrary outer boundary.
    """

    # Keep diameter steps distinct.  The broader cylinder-detection tolerance
    # is intentionally inappropriate here: on a small counterbore it can make
    # neighboring measured radii look interchangeable and delete the wrong
    # layer circle.
    tolerance = max(data.diagonal * 0.001, 0.02)
    radius_tolerance = max(data.diagonal * 0.00015, 0.003)
    boolean_margin = max(data.diagonal * 0.000015, 0.0005)
    observations: list[dict[str, object]] = []

    for feature in [source.base, *source.operations]:
        axis = getattr(feature, "axis", None)
        start = getattr(feature, "start", None)
        depth = getattr(feature, "depth", None)
        if (
            isinstance(feature, BooleanExtrudeFeature)
            and feature.mode == "cut"
        ):
            circle_regions = [feature.outer, *feature.additional_regions]
        else:
            circle_regions = list(getattr(feature, "holes", None) or [])
        if (
            not isinstance(axis, Axis)
            or start is None
            or depth is None
            or not circle_regions
        ):
            continue
        interval_start = float(start)
        interval_end = interval_start + float(depth)
        for profile in circle_regions:
            if not isinstance(profile, CircleProfile):
                continue
            center = np.asarray(profile.center, dtype=float)
            cluster = next(
                (
                    item
                    for item in observations
                    if item["axis"] == axis
                    and np.linalg.norm(
                        np.asarray(item["center"], dtype=float) - center
                    )
                    <= tolerance
                    and abs(float(item["radius"]) - profile.radius)
                    <= radius_tolerance
                ),
                None,
            )
            if cluster is None:
                observations.append(
                    {
                        "axis": axis,
                        "centers": [center],
                        "center": center,
                        "radii": [profile.radius],
                        "radius": profile.radius,
                        "intervals": [(interval_start, interval_end)],
                    }
                )
            else:
                cluster["centers"].append(center)
                cluster["radii"].append(profile.radius)
                cluster["intervals"].append((interval_start, interval_end))
                cluster["center"] = np.median(
                    np.asarray(cluster["centers"], dtype=float), axis=0
                )
                cluster["radius"] = float(np.median(cluster["radii"]))

    existing = _existing_round_holes(source)
    promoted: list[RoundHoleFeature] = []
    for cluster in observations:
        intervals = sorted(cluster["intervals"])
        axis = cluster["axis"]
        center = np.asarray(cluster["center"], dtype=float)
        radius = float(cluster["radius"])
        contiguous_runs: list[list[tuple[float, float]]] = []
        for interval in intervals:
            if (
                not contiguous_runs
                or interval[0] > contiguous_runs[-1][-1][1] + tolerance
            ):
                contiguous_runs.append([interval])
            else:
                contiguous_runs[-1].append(interval)
        for run in contiguous_runs:
            start = min(interval[0] for interval in run)
            end = max(interval[1] for interval in run)
            if any(
                _round_cut_covers(
                    hole,
                    axis=axis,
                    center=center,
                    radius=radius,
                    start=start,
                    end=end,
                    tolerance=tolerance,
                )
                for hole in existing
            ):
                continue
            axis_index = (Axis.X, Axis.Y, Axis.Z).index(axis)
            bounds = data.mesh.bounds[:, axis_index]
            promoted.append(
                RoundHoleFeature(
                    axis=axis,
                    center=(_clean(center[0]), _clean(center[1])),
                    diameter=_clean(radius * 2),
                    # Coincident cut/layer faces can leave a zero-thickness
                    # cap in OCCT.  A micron-scale axial overlap is far below
                    # the mesh fitting tolerance and makes the Boolean result
                    # both topologically clean and geometrically equivalent.
                    start=_clean(start - boolean_margin),
                    depth=_clean(end - start + boolean_margin * 2),
                    through=(
                        start <= float(bounds[0]) + tolerance
                        and end >= float(bounds[1]) - tolerance
                    ),
                )
            )

    if not promoted:
        return []
    candidate = _replace_profile_circles_with_round_holes(
        source,
        promoted,
        tolerance,
    )
    candidate.assumptions.append(
        f"Promoted {len(promoted)} repeated layer-profile circles on "
        "their measured sketch planes into editable analytic hole features."
    )
    return [candidate]


def generate_round_hole_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    bounds = data.mesh.bounds
    tolerance = max(data.diagonal * 0.002, 0.025)
    existing = _existing_round_holes(source)
    detected_by_axis: dict[Axis, list[RoundHoleFeature]] = {}

    for axis, (_, _, _, index) in AXES.items():
        axis_start = float(bounds[0, index])
        axis_end = float(bounds[1, index])
        levels = _axis_levels(data, axis)
        clusters: list[dict[str, object]] = []
        for interval_start, interval_end in zip(levels, levels[1:], strict=False):
            if interval_end - interval_start <= tolerance * 0.1:
                continue
            section = section_shape(
                data,
                axis,
                (interval_start + interval_end) / 2,
            )
            if section is None:
                continue
            for circle in _circular_holes(section):
                center = np.asarray(circle.center)
                matched = None
                for cluster in clusters:
                    if (
                        np.linalg.norm(center - cluster["center"]) <= tolerance
                        and abs(circle.radius - cluster["radius"]) <= tolerance
                    ):
                        matched = cluster
                        break
                if matched is None:
                    clusters.append(
                        {
                            "center": center,
                            "radius": circle.radius,
                            "spans": [(interval_start, interval_end)],
                        }
                    )
                else:
                    matched["spans"].append((interval_start, interval_end))

        holes: list[RoundHoleFeature] = []
        for cluster in clusters:
            center = np.asarray(cluster["center"])
            radius = float(cluster["radius"])
            spans = cluster["spans"]
            start = min(span[0] for span in spans)
            end = max(span[1] for span in spans)
            if any(
                _round_cut_covers(
                    found,
                    axis=axis,
                    center=center,
                    radius=radius,
                    start=start,
                    end=end,
                    tolerance=tolerance,
                )
                or _round_cut_substantially_covers(
                    found,
                    axis=axis,
                    center=center,
                    radius=radius,
                    start=start,
                    end=end,
                    tolerance=tolerance,
                )
                for found in existing
            ):
                continue
            margin = max(data.diagonal * 0.001, 0.01)
            holes.append(
                RoundHoleFeature(
                    axis=axis,
                    center=(_clean(center[0]), _clean(center[1])),
                    diameter=_clean(radius * 2),
                    start=_clean(start - margin),
                    depth=_clean(end - start + margin * 2),
                    through=(
                        start <= axis_start + tolerance
                        and end >= axis_end - tolerance
                    ),
                )
            )
        if holes:
            detected_by_axis[axis] = holes

    candidates: list[ReconstructionPlan] = []
    all_holes = [hole for holes in detected_by_axis.values() for hole in holes]
    if all_holes:
        combined = _replace_fragmented_profile_holes(
            source,
            all_holes,
            tolerance,
        )
        combined.assumptions.append(
            f"Consolidated {len(all_holes)} persistent circular voids into editable "
            "through-hole features."
        )
        candidates.append(combined)
    repeated_diameter_groups: list[list[RoundHoleFeature]] = []
    for hole in all_holes:
        matching_group = next(
            (
                group
                for group in repeated_diameter_groups
                if group[0].axis == hole.axis
                and abs(group[0].diameter - hole.diameter) <= tolerance * 2
                and abs(group[0].start - hole.start) <= tolerance * 2
                and abs(
                    group[0].start
                    + group[0].depth
                    - hole.start
                    - hole.depth
                )
                <= tolerance * 2
            ),
            None,
        )
        if matching_group is None:
            repeated_diameter_groups.append([hole])
        else:
            matching_group.append(hole)
    repeated_diameter_groups.sort(key=lambda group: (-len(group), group[0].diameter))
    for group in repeated_diameter_groups[:12]:
        if len(group) == len(all_holes):
            continue
        plan = _replace_fragmented_profile_holes(source, group, tolerance)
        plan.assumptions.append(
            f"Consolidated {len(group)} repeated same-diameter circular voids "
            f"on the {group[0].axis.value} sketch plane into continuous "
            "editable hole features."
        )
        candidates.append(plan)
    if len(detected_by_axis) <= 1:
        return candidates
    for holes in detected_by_axis.values():
        plan = _replace_fragmented_profile_holes(source, holes, tolerance)
        plan.assumptions.append(
            f"Consolidated {len(holes)} aligned circular voids into editable "
            "through-hole features."
        )
        candidates.append(plan)
    return candidates


def _oriented_cylinder_matches_conical_group(
    operation: OrientedCylinderFeature,
    axis: Axis,
    center: np.ndarray,
    segments: list[tuple[float, float, float, float, bool]],
    radial_tolerance: float,
    center_tolerance: float,
) -> bool:
    """Match an older generic cut that a recovered hole group supersedes."""

    direction = np.asarray(operation.direction, dtype=float)
    direction_length = float(np.linalg.norm(direction))
    if direction_length <= 1e-12:
        return False
    direction /= direction_length
    _, _, _, axis_index = AXES[axis]
    # AXES stores the sketch basis after the stock direction. Compare against
    # the actual cardinal direction explicitly; the sign is immaterial.
    cardinal_direction = np.zeros(3, dtype=float)
    cardinal_direction[axis_index] = 1.0
    if abs(float(direction @ cardinal_direction)) < 0.999:
        return False

    origin = np.asarray(operation.origin, dtype=float)
    projected_center = _project(origin.reshape(1, 3), axis)[0]
    if np.linalg.norm(projected_center - center) > center_tolerance:
        return False

    endpoint = origin + direction * operation.depth
    operation_start = min(origin[axis_index], endpoint[axis_index])
    operation_end = max(origin[axis_index], endpoint[axis_index])
    radius_tolerance = max(
        radial_tolerance * 3.0,
        operation.radius * 0.05,
    )
    return any(
        min(operation_end, end) - max(operation_start, start)
        >= -radial_tolerance
        and min(
            abs(operation.radius - start_radius),
            abs(operation.radius - end_radius),
        )
        <= radius_tolerance
        for start, end, start_radius, end_radius, _is_conical in segments
    )


def generate_conical_hole_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    radial_tolerance = max(data.diagonal * 0.00025, 0.002)
    center_tolerance = max(data.diagonal * 0.002, 0.02)
    reconstructed: list[
        tuple[Axis, np.ndarray, list[tuple[float, float, float, float, bool]]]
    ] = []
    expected_centers: dict[Axis, list[np.ndarray]] = {}
    for operation in source.operations:
        if isinstance(operation, RoundHoleFeature):
            expected_centers.setdefault(operation.axis, []).append(
                np.asarray(operation.center)
            )

    for axis in AXES:
        segments: list[tuple[np.ndarray, float, float, float, float, bool]] = []
        levels = _axis_levels(data, axis)
        for interval_start, interval_end in zip(levels, levels[1:], strict=False):
            depth = interval_end - interval_start
            if depth <= radial_tolerance:
                continue
            samples: list[tuple[float, list[CircleProfile]]] = []
            for fraction in (
                0.005,
                0.015,
                0.03,
                0.05,
                0.075,
                0.1,
                0.2,
                0.4,
                0.6,
                0.8,
                0.95,
            ):
                location = interval_start + depth * fraction
                section = section_shape(data, axis, location)
                if section is not None:
                    circles = _circular_holes(section)
                    if expected_centers.get(axis):
                        circles = [
                            circle
                            for circle, _ in _section_round_boundaries(section)
                            if any(
                                np.linalg.norm(
                                    np.asarray(circle.center) - center
                                )
                                <= center_tolerance
                                for center in expected_centers[axis]
                            )
                        ]
                    samples.append((location, circles))
            populated_samples = [
                sample for sample in samples if sample[1]
            ]
            if len(populated_samples) < 3:
                continue
            for seed in populated_samples[0][1]:
                observations: list[tuple[float, float]] = []
                centers: list[np.ndarray] = []
                for location, circles in populated_samples:
                    matches = [
                        circle
                        for circle in circles
                        if np.linalg.norm(
                            np.asarray(circle.center) - np.asarray(seed.center)
                        )
                        <= center_tolerance
                    ]
                    if not matches:
                        continue
                    match = min(
                        matches,
                        key=lambda circle: np.linalg.norm(
                            np.asarray(circle.center) - np.asarray(seed.center)
                        ),
                    )
                    observations.append((location, match.radius))
                    centers.append(np.asarray(match.center))
                if len(observations) < 4:
                    continue
                locations = np.asarray([item[0] for item in observations])
                radii = np.asarray([item[1] for item in observations])
                stable_radius = float(np.median(radii[:3]))
                changed = np.flatnonzero(
                    np.abs(radii - stable_radius) > radial_tolerance
                )
                if (
                    float(np.ptp(radii[:3])) <= radial_tolerance * 1.5
                    and len(changed)
                    and 2 <= int(changed[0]) <= len(radii) - 3
                ):
                    first_varying = int(changed[0])
                    slope, intercept = np.polyfit(
                        locations[first_varying:],
                        radii[first_varying:],
                        1,
                    )
                    if abs(slope) > 1e-6:
                        transition = float(
                            np.clip(
                                (stable_radius - intercept) / slope,
                                interval_start,
                                interval_end,
                            )
                        )
                        end_radius = float(slope * interval_end + intercept)
                        predicted = (
                            slope * locations[first_varying:] + intercept
                        )
                        residual = float(
                            np.max(
                                np.abs(
                                    predicted - radii[first_varying:]
                                )
                            )
                        )
                        if (
                            transition - interval_start > radial_tolerance
                            and interval_end - transition > radial_tolerance
                            and min(stable_radius, end_radius) > radial_tolerance
                            and abs(end_radius - stable_radius)
                            > radial_tolerance * 2
                            and residual <= radial_tolerance * 1.5
                        ):
                            segments.extend(
                                [
                                    (
                                        np.mean(centers, axis=0),
                                        interval_start,
                                        transition,
                                        stable_radius,
                                        stable_radius,
                                        False,
                                    ),
                                    (
                                        np.mean(centers, axis=0),
                                        transition,
                                        interval_end,
                                        stable_radius,
                                        end_radius,
                                        True,
                                    ),
                                ]
                            )
                            continue
                stable_radius = float(np.median(radii[-3:]))
                changed = np.flatnonzero(
                    np.abs(radii - stable_radius) > radial_tolerance
                )
                if len(changed) and int(changed[-1]) < len(radii) - 2:
                    first_stable = int(changed[-1]) + 1
                    # Fit only the varying-radius samples. Including the first
                    # cylindrical sample biases the cone slope and can move the
                    # inferred transition far enough to reject a clean
                    # countersink whose STEP transition was not retained as a
                    # distinct tessellation vertex level.
                    fit_end = first_stable
                    slope, intercept = np.polyfit(
                        locations[:fit_end],
                        radii[:fit_end],
                        1,
                    )
                    if abs(slope) > 1e-6:
                        transition = float(
                            np.clip(
                                (stable_radius - intercept) / slope,
                                interval_start,
                                interval_end,
                            )
                        )
                        start_radius = float(
                            slope * interval_start + intercept
                        )
                        predicted = (
                            slope * locations[:fit_end] + intercept
                        )
                        residual = float(
                            np.max(
                                np.abs(
                                    predicted - radii[:fit_end]
                                )
                            )
                        )
                        if (
                            transition - interval_start > radial_tolerance
                            and min(start_radius, stable_radius)
                            > radial_tolerance
                            and abs(start_radius - stable_radius)
                            > radial_tolerance * 2
                            and residual <= radial_tolerance * 1.5
                        ):
                            segments.extend(
                                [
                                    (
                                        np.mean(centers, axis=0),
                                        interval_start,
                                        transition,
                                        start_radius,
                                        stable_radius,
                                        True,
                                    ),
                                    (
                                        np.mean(centers, axis=0),
                                        transition,
                                        interval_end,
                                        stable_radius,
                                        stable_radius,
                                        False,
                                    ),
                                ]
                            )
                            continue
                slope, intercept = np.polyfit(locations, radii, 1)
                predicted = slope * locations + intercept
                residual = float(np.max(np.abs(predicted - radii)))
                start_radius = float(slope * interval_start + intercept)
                end_radius = float(slope * interval_end + intercept)
                radius_change = abs(end_radius - start_radius)
                is_conical = (
                    min(start_radius, end_radius) > radial_tolerance
                    and radius_change
                    > max(radial_tolerance * 2, float(np.mean(radii)) * 0.01)
                    and residual <= max(radial_tolerance, radius_change * 0.05)
                )
                if not is_conical:
                    stable_radius = float(np.mean(radii))
                    start_radius = stable_radius
                    end_radius = stable_radius
                segments.append(
                    (
                        np.mean(centers, axis=0),
                        interval_start,
                        interval_end,
                        start_radius,
                        end_radius,
                        is_conical,
                    )
                )

        groups: list[
            tuple[np.ndarray, list[tuple[float, float, float, float, bool]]]
        ] = []
        for center, start, end, start_radius, end_radius, is_conical in segments:
            match = next(
                (
                    group
                    for group in groups
                    if np.linalg.norm(center - group[0]) <= center_tolerance
                ),
                None,
            )
            segment = (start, end, start_radius, end_radius, is_conical)
            if match is None:
                groups.append((center, [segment]))
            else:
                match[1].append(segment)
        reconstructed.extend(
            (axis, center, sorted(group_segments))
            for center, group_segments in groups
            if any(segment[4] for segment in group_segments)
        )

    if not reconstructed:
        return []

    plan = source.model_copy(deep=True)
    reconstructed_centers = [
        (axis, center)
        for axis, center, _segments in reconstructed
    ]
    # A global circular edge finish is evaluated at its timeline position.
    # Replacing the underlying cylindrical hole with a cone changes that edge,
    # so carrying the old selector into the candidate can make an otherwise
    # valid conical reconstruction unbuildable. Drop only finishes attached to
    # reconstructed cuts; the topology-focused torus pass will re-fit any
    # fillet that is present in the mesh after the cone is established.
    plan.operations = [
        operation
        for operation in plan.operations
        if not (
            isinstance(operation, EdgeFinishFeature)
            and operation.center is not None
            and any(
                operation.axis == axis
                and np.linalg.norm(
                    np.asarray(operation.center) - center
                )
                <= center_tolerance
                for axis, center in reconstructed_centers
            )
        )
    ]
    for axis, center, segments in reconstructed:
        for feature in [plan.base, *plan.operations]:
            feature_holes = getattr(feature, "holes", None)
            if feature_holes is None or feature.axis != axis:
                continue
            feature.holes = [
                profile
                for profile in feature_holes
                if not (
                    isinstance(profile, CircleProfile)
                    and np.linalg.norm(
                        np.asarray(profile.center) - center
                    )
                    <= center_tolerance
                )
            ]
        plan.operations = [
            operation
            for operation in plan.operations
            if not (
                (
                    isinstance(operation, (RoundHoleFeature, ConicalHoleFeature))
                    and operation.axis == axis
                    and np.linalg.norm(np.asarray(operation.center) - center)
                    <= center_tolerance
                )
                or (
                    isinstance(operation, OrientedCylinderFeature)
                    and operation.mode == "cut"
                    and operation.inner_radius is None
                    and _oriented_cylinder_matches_conical_group(
                        operation,
                        axis,
                        center,
                        segments,
                        radial_tolerance,
                        center_tolerance,
                    )
                )
            )
        ]
        for start, end, start_radius, end_radius, is_conical in segments:
            feature_depth = _clean(end - start)
            if feature_depth <= 0:
                continue
            if is_conical:
                plan.operations.append(
                    ConicalHoleFeature(
                        axis=axis,
                        center=(_clean(center[0]), _clean(center[1])),
                        start=_clean(start),
                        depth=feature_depth,
                        start_diameter=_clean(start_radius * 2),
                        end_diameter=_clean(end_radius * 2),
                    )
                )
            else:
                plan.operations.append(
                    RoundHoleFeature(
                        axis=axis,
                        center=(_clean(center[0]), _clean(center[1])),
                        diameter=_clean(start_radius + end_radius),
                        start=_clean(start),
                        depth=feature_depth,
                        through=False,
                    )
                )
    cone_count = sum(
        1
        for _, _, segments in reconstructed
        for segment in segments
        if segment[4]
    )
    plan.assumptions.append(
        f"Recovered {cone_count} linearly varying circular cuts as editable "
        "conical-hole features."
    )
    return [plan]


def generate_conical_add_candidates(
    data: MeshData,
    source: ReconstructionPlan,
) -> list[ReconstructionPlan]:
    radial_tolerance = max(data.diagonal * 0.00025, 0.002)
    center_tolerance = max(data.diagonal * 0.002, 0.02)
    replacements: list[tuple[int, ConicalAddFeature]] = []

    for axis in AXES:
        levels = _axis_levels(data, axis)
        for interval_start, interval_end in zip(levels, levels[1:], strict=False):
            depth = interval_end - interval_start
            if depth <= radial_tolerance:
                continue
            observations: list[tuple[float, CircleProfile]] = []
            for fraction in (0.1, 0.3, 0.5, 0.7, 0.9):
                location = interval_start + depth * fraction
                section = section_shape(data, axis, location)
                if section is not None and isinstance(
                    section.outer,
                    CircleProfile,
                ):
                    observations.append((location, section.outer))
            if len(observations) < 4:
                continue
            centers = np.asarray(
                [np.asarray(circle.center) for _, circle in observations]
            )
            center_spread = float(
                np.max(
                    np.linalg.norm(
                        centers - np.mean(centers, axis=0),
                        axis=1,
                    )
                )
            )
            if center_spread > center_tolerance:
                continue
            locations = np.asarray([location for location, _ in observations])
            radii = np.asarray([circle.radius for _, circle in observations])
            slope, intercept = np.polyfit(locations, radii, 1)
            predicted = slope * locations + intercept
            start_radius = float(slope * interval_start + intercept)
            end_radius = float(slope * interval_end + intercept)
            radius_change = abs(end_radius - start_radius)
            residual = float(np.max(np.abs(predicted - radii)))
            if (
                min(start_radius, end_radius) <= radial_tolerance
                or radius_change
                <= max(radial_tolerance * 2, float(np.mean(radii)) * 0.01)
                or residual > max(radial_tolerance, radius_change * 0.05)
            ):
                continue
            center = np.mean(centers, axis=0)
            for index, operation in enumerate(source.operations):
                if (
                    isinstance(operation, BooleanExtrudeFeature)
                    and operation.mode == "add"
                    and operation.axis == axis
                    and abs(operation.start - interval_start) <= center_tolerance
                    and abs(
                        operation.start + operation.depth - interval_end
                    )
                    <= center_tolerance
                    and isinstance(operation.outer, CircleProfile)
                    and np.linalg.norm(
                        np.asarray(operation.outer.center) - center
                    )
                    <= center_tolerance
                ):
                    replacements.append(
                        (
                            index,
                            ConicalAddFeature(
                                axis=axis,
                                center=(_clean(center[0]), _clean(center[1])),
                                start=_clean(interval_start),
                                depth=_clean(depth),
                                start_diameter=_clean(start_radius * 2),
                                end_diameter=_clean(end_radius * 2),
                                holes=operation.holes,
                            ),
                        )
                    )
                    break

    if not replacements:
        return []
    plan = source.model_copy(deep=True)
    for index, replacement in replacements:
        plan.operations[index] = replacement
    plan.assumptions.append(
        f"Replaced {len(replacements)} stepped circular layers with editable "
        "analytic conical additions."
    )
    return [plan]

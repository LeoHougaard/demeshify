from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import cadquery as cq
import numpy as np
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Copy,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_NurbsConvert,
    BRepBuilderAPI_Sewing,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFeat import BRepFeat_SplitShape
from OCP.BRepFill import BRepFill, BRepFill_Filling
from OCP.BRepLib import BRepLib
from OCP.BRepTools import BRepTools, BRepTools_ReShape, BRepTools_WireExplorer
from OCP.GCE2d import GCE2d_MakeSegment
from OCP.Geom import (
    Geom_ConicalSurface,
    Geom_CylindricalSurface,
    Geom_Plane,
    Geom_SphericalSurface,
    Geom_Surface,
    Geom_SurfaceOfLinearExtrusion,
    Geom_SurfaceOfRevolution,
    Geom_ToroidalSurface,
)
from OCP.Geom2dAPI import Geom2dAPI_Interpolate
from OCP.GeomAbs import GeomAbs_C0, GeomAbs_C2
from OCP.GeomAPI import (
    GeomAPI_Interpolate,
    GeomAPI_IntSS,
    GeomAPI_PointsToBSpline,
    GeomAPI_PointsToBSplineSurface,
    GeomAPI_ProjectPointOnCurve,
    GeomAPI_ProjectPointOnSurf,
)
from OCP.GeomProjLib import GeomProjLib
from OCP.gp import (
    gp_Ax1,
    gp_Ax2,
    gp_Ax3,
    gp_Circ,
    gp_Cone,
    gp_Cylinder,
    gp_Dir,
    gp_Pln,
    gp_Pnt,
    gp_Pnt2d,
    gp_Sphere,
    gp_Torus,
)
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds, ShapeAnalysis_Shell
from OCP.ShapeFix import (
    ShapeFix_Edge,
    ShapeFix_Face,
    ShapeFix_Shape,
    ShapeFix_Shell,
    ShapeFix_Solid,
    ShapeFix_Wire,
)
from OCP.TColgp import (
    TColgp_Array1OfPnt,
    TColgp_Array2OfPnt,
    TColgp_HArray1OfPnt,
    TColgp_HArray1OfPnt2d,
)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_WIRE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import (
    TopoDS,
    TopoDS_Edge,
    TopoDS_Face,
    TopoDS_Shell,
    TopoDS_Solid,
    TopoDS_Vertex,
    TopoDS_Wire,
)
from OCP.TopTools import TopTools_HSequenceOfShape
from scipy.interpolate import RBFInterpolator
from scipy.optimize import least_squares
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from .mesh import MeshData
from .surface_graph import (
    ConicalPatch,
    CylindricalPatch,
    FreeformPatch,
    LinearExtrusionPatch,
    PlanarPatch,
    SphericalPatch,
    SurfaceGraph,
    SurfaceOfRevolutionPatch,
    SurfacePatch,
    ToroidalPatch,
    _ordered_cloud_path,
    _plane_basis,
    detect_surface_graph,
)


@dataclass(slots=True)
class _SurfaceModel:
    patch: SurfacePatch
    surface: Geom_Surface
    elementary: gp_Pln | gp_Cylinder | gp_Cone | gp_Sphere | gp_Torus


@dataclass(slots=True)
class _TrimEdge:
    edge: TopoDS_Edge
    points: np.ndarray
    source_points: np.ndarray | None = None

    @property
    def closed(self) -> bool:
        return bool(
            len(self.points) >= 3 and np.linalg.norm(self.points[0] - self.points[-1]) <= 1e-7
        )

    @property
    def start(self) -> np.ndarray:
        return self.points[0]

    @property
    def end(self) -> np.ndarray:
        return self.points[-1]


@dataclass(slots=True)
class _CanonicalVertex:
    """One corner shared by every reconstructed edge incident to it."""

    point: np.ndarray
    vertex: TopoDS_Vertex
    tolerance: float


@dataclass(slots=True)
class SurfaceBRepResult:
    shape: cq.Shape
    graph: SurfaceGraph
    surface_counts: dict[str, int]
    face_count: int
    solid_count: int
    free_edge_count: int
    sewing_tolerance: float
    valid: bool
    closed: bool
    faceted_fallback: bool = False
    point_fitted_face_count: int = 0
    topology_vertex_count: int = 0
    topology_edge_count: int = 0
    joined_shape: cq.Shape | None = None
    warnings: list[str] = field(default_factory=list)
    faceted_patch_count: int = 0
    faceted_face_count: int = 0
    faceted_patch_ids: list[str] = field(default_factory=list)
    unfitted_patch_ids: list[str] = field(default_factory=list)
    source_mesh_fallback: bool = False


def _point(value: np.ndarray) -> gp_Pnt:
    return gp_Pnt(*(float(component) for component in value))


def _direction(value: np.ndarray) -> gp_Dir:
    normalized = np.asarray(value, dtype=float)
    normalized /= max(float(np.linalg.norm(normalized)), 1e-15)
    return gp_Dir(*(float(component) for component in normalized))


def _valid_face_with_minimum_area(face: TopoDS_Face, minimum_area: float) -> bool:
    try:
        wrapped = cq.Face(face)
        area = float(wrapped.Area())
        return bool(
            BRepCheck_Analyzer(face).IsValid()
            and wrapped.isValid()
            and math.isfinite(area)
            and area > minimum_area
        )
    except Exception:
        return False


def _usable_face(face: TopoDS_Face, tolerance: float = 0.0) -> bool:
    """Apply both kernel and CadQuery validity checks before a face is sewn."""

    return _valid_face_with_minimum_area(face, tolerance**2)


def _surface_model(patch: SurfacePatch, mesh: object) -> _SurfaceModel | None:
    if isinstance(patch, PlanarPatch):
        plane = gp_Pln(_point(patch.origin), _direction(patch.normal))
        return _SurfaceModel(patch, Geom_Plane(plane), plane)
    if isinstance(patch, CylindricalPatch):
        x_direction, _ = _plane_basis(patch.axis)
        position = gp_Ax3(
            _point(patch.origin),
            _direction(patch.axis),
            _direction(x_direction),
        )
        cylinder = gp_Cylinder(position, patch.radius)
        return _SurfaceModel(patch, Geom_CylindricalSurface(cylinder), cylinder)
    if isinstance(patch, ConicalPatch):
        vertex_indices = np.unique(mesh.faces[patch.face_indices])
        points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
        axis = np.asarray(patch.axis, dtype=float)
        axial = (points - patch.apex) @ axis
        if float(np.median(axial)) < 0:
            axis = -axis
        x_direction, _ = _plane_basis(axis)
        position = gp_Ax3(
            _point(patch.apex),
            _direction(axis),
            _direction(x_direction),
        )
        cone = gp_Cone(position, patch.semi_angle, 0.0)
        return _SurfaceModel(patch, Geom_ConicalSurface(cone), cone)
    if isinstance(patch, SphericalPatch):
        sphere = gp_Sphere(
            gp_Ax3(_point(patch.center), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)),
            patch.radius,
        )
        return _SurfaceModel(patch, Geom_SphericalSurface(sphere), sphere)
    if isinstance(patch, ToroidalPatch):
        x_direction, _ = _plane_basis(patch.axis)
        torus = gp_Torus(
            gp_Ax3(
                _point(patch.center),
                _direction(patch.axis),
                _direction(x_direction),
            ),
            patch.major_radius,
            patch.minor_radius,
        )
        return _SurfaceModel(patch, Geom_ToroidalSurface(torus), torus)
    return None


def _project_parameters(points: np.ndarray, curve: object) -> tuple[float, np.ndarray]:
    parameters: list[float] = []
    distances: list[float] = []
    sample_indices = np.linspace(
        0,
        len(points) - 1,
        min(len(points), 32),
        dtype=int,
    )
    for point in points[sample_indices]:
        projection = GeomAPI_ProjectPointOnCurve(_point(point), curve)
        if projection.NbPoints() == 0:
            return math.inf, np.empty(0)
        distances.append(float(projection.LowerDistance()))
        parameters.append(float(projection.LowerDistanceParameter()))
    return float(np.sqrt(np.mean(np.square(distances)))), np.asarray(parameters)


def _set_edge_tolerance(edge: TopoDS_Edge, tolerance: float) -> TopoDS_Edge:
    builder = BRep_Builder()
    builder.UpdateVertex(TopExp.FirstVertex_s(edge), tolerance)
    builder.UpdateVertex(TopExp.LastVertex_s(edge), tolerance)
    return edge


def _surface_residual(model: _SurfaceModel, point: np.ndarray) -> float:
    """Signed distance-like residual used to reconcile corner positions."""

    patch = model.patch
    if isinstance(patch, PlanarPatch):
        return float((point - patch.origin) @ patch.normal)
    if isinstance(patch, CylindricalPatch):
        relative = point - patch.origin
        radial = relative - float(relative @ patch.axis) * patch.axis
        return float(np.linalg.norm(radial) - patch.radius)
    if isinstance(patch, ConicalPatch):
        relative = point - patch.apex
        axial = float(relative @ patch.axis)
        radial = relative - axial * patch.axis
        return float(np.linalg.norm(radial) - abs(axial) * math.tan(patch.semi_angle))
    if isinstance(patch, SphericalPatch):
        return float(np.linalg.norm(point - patch.center) - patch.radius)
    if isinstance(patch, ToroidalPatch):
        relative = point - patch.center
        axial = float(relative @ patch.axis)
        radial = relative - axial * patch.axis
        ring_distance = math.hypot(np.linalg.norm(radial) - patch.major_radius, axial)
        return float(ring_distance - patch.minor_radius)
    return 0.0


def _refine_corner(
    source_points: np.ndarray,
    incident_patch_ids: set[str],
    models: dict[str, _SurfaceModel],
    tolerance: float,
) -> tuple[np.ndarray, float]:
    """Find the least-movement common point of the incident analytic surfaces.

    Independent best fits rarely intersect at exactly one point. This is the
    local equivalent of geometric-constraint perfecting: surface residuals are
    minimized together while a weak anchor prevents a corner jumping to a
    different branch of a sphere, torus, or cone.
    """

    origin = np.mean(source_points, axis=0)
    incident = [models[patch_id] for patch_id in sorted(incident_patch_ids) if patch_id in models]
    point = origin
    if len(incident) >= 2:
        anchor_weight = 0.05

        def objective(candidate: np.ndarray) -> np.ndarray:
            surface_terms = [_surface_residual(model, candidate) for model in incident]
            anchor_terms = anchor_weight * (candidate - origin)
            return np.r_[surface_terms, anchor_terms]

        try:
            fit = least_squares(
                objective,
                origin,
                method="trf",
                x_scale=max(tolerance, 1e-9),
                max_nfev=80,
            )
            if fit.success and np.all(np.isfinite(fit.x)):
                displacement = float(np.linalg.norm(fit.x - origin))
                if displacement <= tolerance * 5:
                    point = np.asarray(fit.x, dtype=float)
        except Exception:
            pass

    deviations = [float(np.linalg.norm(point - candidate)) for candidate in source_points]
    deviations.extend(abs(_surface_residual(model, point)) for model in incident)
    vertex_tolerance = max(tolerance * 5, max(deviations, default=0.0) * 1.25)
    vertex_tolerance = min(vertex_tolerance, tolerance * 10)
    return point, vertex_tolerance


def _canonical_vertices(
    records: list[tuple[str, str, np.ndarray]],
    models: dict[str, _SurfaceModel],
    tolerance: float,
) -> tuple[dict[tuple[int, int], _CanonicalVertex], list[_CanonicalVertex]]:
    """Cluster mesh-chain endpoints and create shared OpenCascade vertices."""

    candidates: list[np.ndarray] = []
    references: list[tuple[int, int]] = []
    incident_ids: list[set[str]] = []
    for record_index, (first_id, second_id, points) in enumerate(records):
        if len(points) < 2 or np.linalg.norm(points[0] - points[-1]) <= tolerance * 3:
            continue
        for endpoint in (0, 1):
            candidates.append(np.asarray(points[0 if endpoint == 0 else -1], dtype=float))
            references.append((record_index, endpoint))
            incident_ids.append({first_id, second_id})
    if not candidates:
        return {}, []

    candidate_array = np.asarray(candidates, dtype=float)
    parents = list(range(len(candidate_array)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    tree = cKDTree(candidate_array)
    # These endpoints originate at shared STL nodes, so only absorb numerical
    # noise. A broad geometric merge could collapse two legitimately distinct
    # nearby corners on a small feature.
    for first, second in tree.query_pairs(max(tolerance * 0.5, 1e-9)):
        union(int(first), int(second))
    clusters: dict[int, list[int]] = {}
    for index in range(len(candidate_array)):
        clusters.setdefault(find(index), []).append(index)

    mapping: dict[tuple[int, int], _CanonicalVertex] = {}
    vertices: list[_CanonicalVertex] = []
    builder = BRep_Builder()
    for indices in clusters.values():
        patches: set[str] = set()
        for index in indices:
            patches.update(incident_ids[index])
        point, vertex_tolerance = _refine_corner(
            candidate_array[indices],
            patches,
            models,
            tolerance,
        )
        vertex = BRepBuilderAPI_MakeVertex(_point(point)).Vertex()
        builder.UpdateVertex(vertex, vertex_tolerance)
        canonical = _CanonicalVertex(point, vertex, vertex_tolerance)
        vertices.append(canonical)
        for index in indices:
            mapping[references[index]] = canonical
    return mapping, vertices


def _intersection_edge(
    first: _SurfaceModel,
    second: _SurfaceModel,
    points: np.ndarray,
    tolerance: float,
    start: _CanonicalVertex | None = None,
    end: _CanonicalVertex | None = None,
    maximum_error_factor: float = 8.0,
) -> _TrimEdge | None:
    try:
        intersection = GeomAPI_IntSS(first.surface, second.surface, tolerance)
    except Exception:
        return None
    if not intersection.IsDone() or intersection.NbLines() == 0:
        return None
    best: tuple[float, object, np.ndarray] | None = None
    for index in range(1, intersection.NbLines() + 1):
        curve = intersection.Line(index)
        error, parameters = _project_parameters(points, curve)
        if len(parameters) == 0:
            continue
        if best is None or error < best[0]:
            best = (error, curve, parameters)
    if best is None or best[0] > tolerance * maximum_error_factor:
        return None
    _, curve, parameters = best
    mesh_closed = bool(len(points) >= 3 and np.linalg.norm(points[0] - points[-1]) <= tolerance * 3)
    curve_closed = bool(
        math.isfinite(float(curve.FirstParameter()))
        and math.isfinite(float(curve.LastParameter()))
        and abs(float(curve.LastParameter()) - float(curve.FirstParameter())) < 1e6
        and curve.Value(curve.FirstParameter()).Distance(curve.Value(curve.LastParameter()))
        <= tolerance * 3
    )
    edge_curve = curve
    if curve_closed and hasattr(curve, "BasisCurve"):
        basis = curve.BasisCurve()
        if basis is not None and basis.IsPeriodic():
            edge_curve = basis

    reverse = False
    if mesh_closed and curve_closed:
        if start is not None and end is not None and start.vertex.IsSame(end.vertex):
            _, seam_parameters = _project_parameters(
                np.asarray([start.point], dtype=float),
                edge_curve,
            )
            seam_parameter = (
                float(seam_parameters[0])
                if len(seam_parameters)
                else float(parameters[0])
            )
            period = float(curve.LastParameter() - curve.FirstParameter())
            maker = BRepBuilderAPI_MakeEdge(
                edge_curve,
                start.vertex,
                start.vertex,
                seam_parameter,
                seam_parameter + period,
            )
        else:
            maker = BRepBuilderAPI_MakeEdge(edge_curve)
    else:
        unwrapped = parameters.copy()
        if curve_closed:
            period = float(curve.LastParameter() - curve.FirstParameter())
            if period > 0:
                for index in range(1, len(unwrapped)):
                    while unwrapped[index] - unwrapped[index - 1] > period / 2:
                        unwrapped[index] -= period
                    while unwrapped[index] - unwrapped[index - 1] < -period / 2:
                        unwrapped[index] += period
        first_parameter = float(unwrapped[0])
        last_parameter = float(unwrapped[-1])
        reverse = bool(last_parameter < first_parameter)
        low_parameter, high_parameter = sorted((first_parameter, last_parameter))
        if start is not None and end is not None:
            low_vertex, high_vertex = (
                (end.vertex, start.vertex) if reverse else (start.vertex, end.vertex)
            )
            maker = BRepBuilderAPI_MakeEdge(
                edge_curve,
                low_vertex,
                high_vertex,
                low_parameter,
                high_parameter,
            )
        else:
            maker = BRepBuilderAPI_MakeEdge(edge_curve, low_parameter, high_parameter)
    if not maker.IsDone():
        return None
    edge = maker.Edge()
    if reverse:
        edge = _reverse_edge(edge)
    mesh_length = float(np.sum(np.linalg.norm(np.diff(points, axis=0), axis=1)))
    edge_length = float(cq.Edge(edge).Length())
    length_tolerance = max(tolerance * 10, mesh_length * 0.05)
    if abs(edge_length - mesh_length) > length_tolerance:
        return None
    canonical_points = np.asarray(points, dtype=float).copy()
    if start is not None and end is not None and not mesh_closed:
        canonical_points[0] = start.point
        canonical_points[-1] = end.point
    edge_tolerance = max(
        tolerance * 5,
        start.tolerance if start is not None else 0.0,
        end.tolerance if end is not None else 0.0,
    )
    return _TrimEdge(_set_edge_tolerance(edge, edge_tolerance), canonical_points)


def _wrapped_intersection_trim_chain(
    first: _SurfaceModel,
    second: _SurfaceModel,
    points: np.ndarray,
    tolerance: float,
    start: _CanonicalVertex | None,
    end: _CanonicalVertex | None,
) -> list[_TrimEdge]:
    """Split a closed intersection curve when the requested arc crosses its seam."""

    if start is None or end is None or len(points) < 3:
        return []
    try:
        intersection = GeomAPI_IntSS(first.surface, second.surface, tolerance)
    except Exception:
        return []
    if not intersection.IsDone() or intersection.NbLines() == 0:
        return []
    best: tuple[float, object, np.ndarray] | None = None
    for index in range(1, intersection.NbLines() + 1):
        curve = intersection.Line(index)
        error, parameters = _project_parameters(points, curve)
        if len(parameters) == 0 or not math.isfinite(error):
            continue
        if best is None or error < best[0]:
            best = (error, curve, parameters)
    if best is None or best[0] > tolerance:
        return []
    _, curve, parameters = best
    first_parameter = float(curve.FirstParameter())
    last_parameter = float(curve.LastParameter())
    period = last_parameter - first_parameter
    if (
        not math.isfinite(first_parameter)
        or not math.isfinite(last_parameter)
        or period <= 0
        or curve.Value(first_parameter).Distance(curve.Value(last_parameter))
        > tolerance * 3
    ):
        return []
    jumps = np.flatnonzero(np.abs(np.diff(parameters)) > period * 0.5)
    if len(jumps) != 1:
        return []
    split = int(jumps[0])
    jump = float(parameters[split + 1] - parameters[split])

    low_point = curve.Value(first_parameter)
    high_point = curve.Value(last_parameter)
    seam_point = np.asarray(
        [
            (low_point.X() + high_point.X()) * 0.5,
            (low_point.Y() + high_point.Y()) * 0.5,
            (low_point.Z() + high_point.Z()) * 0.5,
        ],
        dtype=float,
    )
    seam_vertex = BRepBuilderAPI_MakeVertex(_point(seam_point)).Vertex()
    builder = BRep_Builder()
    builder.UpdateVertex(seam_vertex, tolerance * 5)
    edge_tolerance = max(tolerance * 5, start.tolerance, end.tolerance)

    def make_segment(
        low: float,
        high: float,
        low_vertex: TopoDS_Vertex,
        high_vertex: TopoDS_Vertex,
        reverse: bool,
        samples: np.ndarray,
    ) -> _TrimEdge | None:
        maker = BRepBuilderAPI_MakeEdge(curve, low_vertex, high_vertex, low, high)
        if not maker.IsDone():
            return None
        edge = maker.Edge()
        if reverse:
            edge = _reverse_edge(edge)
        return _TrimEdge(_set_edge_tolerance(edge, edge_tolerance), samples)

    source = np.asarray(points, dtype=float).copy()
    source[0] = start.point
    source[-1] = end.point
    if jump > 0:
        first_samples = np.vstack((source[: split + 1], seam_point))
        second_samples = np.vstack((seam_point, source[split + 1 :]))
        segments = [
            make_segment(
                first_parameter,
                float(parameters[0]),
                seam_vertex,
                start.vertex,
                True,
                first_samples,
            ),
            make_segment(
                float(parameters[-1]),
                last_parameter,
                end.vertex,
                seam_vertex,
                True,
                second_samples,
            ),
        ]
    else:
        first_samples = np.vstack((source[: split + 1], seam_point))
        second_samples = np.vstack((seam_point, source[split + 1 :]))
        segments = [
            make_segment(
                float(parameters[0]),
                last_parameter,
                start.vertex,
                seam_vertex,
                False,
                first_samples,
            ),
            make_segment(
                first_parameter,
                float(parameters[-1]),
                seam_vertex,
                end.vertex,
                False,
                second_samples,
            ),
        ]
    if any(segment is None for segment in segments):
        return []
    result = [segment for segment in segments if segment is not None]
    mesh_length = float(np.sum(np.linalg.norm(np.diff(source, axis=0), axis=1)))
    edge_length = sum(float(cq.Edge(segment.edge).Length()) for segment in result)
    if abs(edge_length - mesh_length) > max(tolerance * 10, mesh_length * 0.05):
        return []
    return result


def _fallback_curve_edge(
    points: np.ndarray,
    tolerance: float,
    start: _CanonicalVertex | None = None,
    end: _CanonicalVertex | None = None,
) -> _TrimEdge | None:
    points = np.asarray(points, dtype=float)
    if len(points) < 2:
        return None
    closed = bool(np.linalg.norm(points[0] - points[-1]) <= tolerance * 3)
    if not closed and start is not None and end is not None:
        points = points.copy()
        points[0] = start.point
        points[-1] = end.point
    fit_points = points[:-1] if closed else points
    if len(fit_points) >= 2:
        keep = np.r_[
            True,
            np.linalg.norm(np.diff(fit_points, axis=0), axis=1) > max(tolerance, 1e-12),
        ]
        fit_points = fit_points[keep]
    if len(fit_points) < 2:
        return None

    centered = fit_points - np.mean(fit_points, axis=0)
    _, singular_values, directions = np.linalg.svd(centered, full_matrices=False)
    line_error = (
        float(singular_values[1] / math.sqrt(len(fit_points))) if len(singular_values) > 1 else 0.0
    )
    if not closed and line_error <= tolerance:
        maker = (
            BRepBuilderAPI_MakeEdge(start.vertex, end.vertex)
            if start is not None and end is not None
            else BRepBuilderAPI_MakeEdge(_point(fit_points[0]), _point(fit_points[-1]))
        )
        edge_tolerance = max(
            tolerance * 5,
            start.tolerance if start is not None else 0.0,
            end.tolerance if end is not None else 0.0,
        )
        return (
            _TrimEdge(_set_edge_tolerance(maker.Edge(), edge_tolerance), points)
            if maker.IsDone()
            else None
        )

    if closed and len(fit_points) >= 6:
        normal = directions[-1]
        first, second = _plane_basis(normal)
        u = fit_points @ first
        v = fit_points @ second
        matrix = np.column_stack((2 * u, 2 * v, np.ones(len(u))))
        target = u**2 + v**2
        center_u, center_v, constant = np.linalg.lstsq(matrix, target, rcond=None)[0]
        radius_squared = constant + center_u**2 + center_v**2
        if radius_squared > tolerance**2:
            radius = math.sqrt(float(radius_squared))
            radial_error = np.abs(np.hypot(u - center_u, v - center_v) - radius)
            planar_error = np.abs(centered @ normal)
            if (
                float(np.max(radial_error)) <= tolerance * 4
                and float(np.max(planar_error)) <= tolerance * 4
            ):
                center = center_u * first + center_v * second
                center += normal * float(np.mean(fit_points @ normal))
                circle = gp_Circ(gp_Ax2(_point(center), _direction(normal)), radius)
                maker = BRepBuilderAPI_MakeEdge(circle)
                return (
                    _TrimEdge(
                        _set_edge_tolerance(maker.Edge(), tolerance * 5),
                        points,
                    )
                    if maker.IsDone()
                    else None
                )

    maximum_points = 128
    if len(fit_points) > maximum_points:
        fit_points = fit_points[np.linspace(0, len(fit_points) - 1, maximum_points, dtype=int)]
    approximation_points = np.vstack((fit_points, fit_points[0])) if closed else fit_points
    curve_scale = float(np.linalg.norm(np.ptp(approximation_points, axis=0)))
    approximation_tolerance = max(tolerance * 0.5, curve_scale * 1e-5, 1e-8)
    point_array = TColgp_Array1OfPnt(1, len(approximation_points))
    for index, point in enumerate(approximation_points, start=1):
        point_array.SetValue(index, _point(point))
    try:
        approximation = GeomAPI_PointsToBSpline(
            point_array,
            3,
            8,
            GeomAbs_C2,
            approximation_tolerance,
        )
    except Exception:
        approximation = None
    if approximation is not None and approximation.IsDone():
        curve = approximation.Curve()
        maker = (
            BRepBuilderAPI_MakeEdge(
                curve,
                start.vertex,
                end.vertex,
                float(curve.FirstParameter()),
                float(curve.LastParameter()),
            )
            if not closed and start is not None and end is not None
            else BRepBuilderAPI_MakeEdge(curve)
        )
        edge_tolerance = max(
            tolerance * 5,
            approximation_tolerance,
            start.tolerance if start is not None else 0.0,
            end.tolerance if end is not None else 0.0,
        )
        if maker.IsDone():
            return _TrimEdge(
                _set_edge_tolerance(maker.Edge(), edge_tolerance),
                points,
            )

    # Interpolation remains a conservative fallback for unusual point sets the
    # approximation API cannot parameterize.
    array = TColgp_HArray1OfPnt(1, len(fit_points))
    for index, point in enumerate(fit_points, start=1):
        array.SetValue(index, _point(point))
    try:
        interpolate = GeomAPI_Interpolate(array, closed, tolerance)
        interpolate.Perform()
    except Exception:
        return None
    if not interpolate.IsDone():
        return None
    curve = interpolate.Curve()
    if not closed and start is not None and end is not None:
        maker = BRepBuilderAPI_MakeEdge(
            curve,
            start.vertex,
            end.vertex,
            float(curve.FirstParameter()),
            float(curve.LastParameter()),
        )
    else:
        maker = BRepBuilderAPI_MakeEdge(curve)
    edge_tolerance = max(
        tolerance * 5,
        start.tolerance if start is not None else 0.0,
        end.tolerance if end is not None else 0.0,
    )
    return (
        _TrimEdge(_set_edge_tolerance(maker.Edge(), edge_tolerance), points)
        if maker.IsDone()
        else None
    )


def _polyline_trim_edges(
    points: np.ndarray,
    tolerance: float,
    start: _CanonicalVertex | None = None,
    end: _CanonicalVertex | None = None,
) -> list[_TrimEdge]:
    """Create a shared, explicitly connected trim chain through mesh nodes."""

    raw = np.asarray(points, dtype=float).copy()
    if len(raw) < 2:
        return []
    keep = np.r_[
        True,
        np.linalg.norm(np.diff(raw, axis=0), axis=1) > max(tolerance * 0.1, 1e-12),
    ]
    raw = raw[keep]
    if len(raw) < 2:
        return []
    closed = np.linalg.norm(raw[0] - raw[-1]) <= tolerance * 3
    unique_points = raw[:-1] if closed else raw
    if len(unique_points) < 2:
        return []
    maximum_vertices = 384
    if len(unique_points) > maximum_vertices:
        unique_points = unique_points[
            np.linspace(0, len(unique_points) - 1, maximum_vertices, dtype=int)
        ]
    builder = BRep_Builder()
    vertices: list[TopoDS_Vertex] = []
    for index, point in enumerate(unique_points):
        if not closed and index == 0 and start is not None:
            vertex = start.vertex
            unique_points[index] = start.point
        elif not closed and index == len(unique_points) - 1 and end is not None:
            vertex = end.vertex
            unique_points[index] = end.point
        else:
            vertex = BRepBuilderAPI_MakeVertex(_point(point)).Vertex()
            builder.UpdateVertex(vertex, tolerance * 5)
        vertices.append(vertex)
    result: list[_TrimEdge] = []
    segment_count = len(unique_points) if closed else len(unique_points) - 1
    for index in range(segment_count):
        following = (index + 1) % len(unique_points)
        maker = BRepBuilderAPI_MakeEdge(vertices[index], vertices[following])
        if not maker.IsDone():
            return []
        result.append(
            _TrimEdge(
                _set_edge_tolerance(maker.Edge(), tolerance * 5),
                np.vstack((unique_points[index], unique_points[following])),
            )
        )
    return result


def _global_polyline_trim_edges(
    records: list[tuple[str, str, np.ndarray]],
    patch_ids: list[str],
    tolerance: float,
) -> tuple[dict[str, list[_TrimEdge]], int, int]:
    """Build dense trim chains with one vertex registry for the whole shell."""

    by_patch: dict[str, list[_TrimEdge]] = {patch_id: [] for patch_id in patch_ids}
    registry: dict[tuple[int, int, int], tuple[np.ndarray, TopoDS_Vertex]] = {}
    builder = BRep_Builder()
    key_scale = max(tolerance * 0.01, 1e-10)

    def shared_vertex(point: np.ndarray) -> tuple[np.ndarray, TopoDS_Vertex]:
        key = tuple(int(value) for value in np.round(point / key_scale))
        existing = registry.get(key)
        if existing is not None:
            return existing
        canonical = np.asarray(point, dtype=float)
        vertex = BRepBuilderAPI_MakeVertex(_point(canonical)).Vertex()
        builder.UpdateVertex(vertex, tolerance * 5)
        registry[key] = (canonical, vertex)
        return canonical, vertex

    edge_count = 0
    for first_id, second_id, raw_points in records:
        points = np.asarray(raw_points, dtype=float)
        if len(points) < 2:
            continue
        keep = np.r_[
            True,
            np.linalg.norm(np.diff(points, axis=0), axis=1) > max(tolerance * 0.01, 1e-12),
        ]
        points = points[keep]
        if len(points) < 2:
            continue
        closed = np.linalg.norm(points[0] - points[-1]) <= tolerance * 3
        if closed:
            points = points[:-1]
        if len(points) < 2:
            continue
        maximum_vertices = 384
        if len(points) > maximum_vertices:
            points = points[np.linspace(0, len(points) - 1, maximum_vertices, dtype=int)]
        vertices = [shared_vertex(point) for point in points]
        segment_count = len(points) if closed else len(points) - 1
        for index in range(segment_count):
            following = (index + 1) % len(points)
            start_point, start_vertex = vertices[index]
            end_point, end_vertex = vertices[following]
            maker = BRepBuilderAPI_MakeEdge(start_vertex, end_vertex)
            if not maker.IsDone():
                continue
            trim = _TrimEdge(
                _set_edge_tolerance(maker.Edge(), tolerance * 5),
                np.vstack((start_point, end_point)),
            )
            by_patch[first_id].append(trim)
            by_patch[second_id].append(trim)
            edge_count += 1
    return by_patch, len(registry), edge_count


def _surface_conforming_trim_chain(
    model: _SurfaceModel,
    raw_points: np.ndarray,
    tolerance: float,
    start: _CanonicalVertex | None = None,
    end: _CanonicalVertex | None = None,
) -> list[_TrimEdge]:
    """Split a residual/analytic border into exact on-surface edge segments.

    Each source border node is projected independently, but consecutive UV
    coordinates are unwrapped before the edges are built.  This is important
    on cylinders, cones, spheres, and tori: a straight 3-D chord cannot bound
    the analytic face, while a straight segment in parameter space always
    creates a curve that lies exactly on that support.
    """

    points = np.asarray(raw_points, dtype=float)
    if len(points) < 2:
        return []
    keep = np.r_[
        True,
        np.linalg.norm(np.diff(points, axis=0), axis=1) > max(tolerance * 0.01, 1e-12),
    ]
    points = points[keep]
    closed = bool(len(points) >= 3 and np.linalg.norm(points[0] - points[-1]) <= tolerance * 3)
    source = points[:-1] if closed else points
    if len(source) < 2 or len(source) > 4096:
        return []

    uv: list[list[float]] = []
    projected: list[np.ndarray] = []
    distances: list[float] = []
    for point in source:
        try:
            projection = GeomAPI_ProjectPointOnSurf(
                _point(point),
                model.surface,
                max(tolerance * 0.05, 1e-10),
            )
            if not projection.IsDone() or projection.NbPoints() == 0:
                return []
            u, v = projection.LowerDistanceParameters()
            nearest = projection.NearestPoint()
            uv.append([float(u), float(v)])
            projected.append(np.asarray((nearest.X(), nearest.Y(), nearest.Z()), dtype=float))
            distances.append(float(projection.LowerDistance()))
        except Exception:
            return []
    # Recognition already tolerance-gates the support.  Refuse a transition
    # that would substantially deform the source boundary rather than hiding
    # a bad fit behind a permissive vertex tolerance.
    if max(distances, default=0.0) > tolerance * 10:
        return []

    parameters = np.asarray(uv, dtype=float)
    for dimension, periodic, period in (
        (
            0,
            bool(model.surface.IsUPeriodic()),
            float(model.surface.UPeriod()) if model.surface.IsUPeriodic() else 0.0,
        ),
        (
            1,
            bool(model.surface.IsVPeriodic()),
            float(model.surface.VPeriod()) if model.surface.IsVPeriodic() else 0.0,
        ),
    ):
        if not periodic or period <= 0:
            continue
        for index in range(1, len(parameters)):
            while parameters[index, dimension] - parameters[index - 1, dimension] > period / 2:
                parameters[index, dimension] -= period
            while parameters[index, dimension] - parameters[index - 1, dimension] < -period / 2:
                parameters[index, dimension] += period

    builder = BRep_Builder()
    vertices: list[TopoDS_Vertex] = []
    projected_array = np.asarray(projected, dtype=float)
    for index, point in enumerate(projected_array):
        canonical = None
        if not closed and index == 0:
            canonical = start
        elif not closed and index == len(projected_array) - 1:
            canonical = end
        if canonical is not None and np.linalg.norm(canonical.point - point) <= tolerance * 2:
            vertex = canonical.vertex
            builder.UpdateVertex(
                vertex,
                max(canonical.tolerance, float(np.linalg.norm(canonical.point - point))),
            )
        else:
            vertex = BRepBuilderAPI_MakeVertex(_point(point)).Vertex()
            builder.UpdateVertex(vertex, tolerance * 5)
        vertices.append(vertex)

    segment_count = len(source) if closed else len(source) - 1
    result: list[_TrimEdge] = []
    for index in range(segment_count):
        following = (index + 1) % len(source)
        uv_end = parameters[following].copy()
        if closed and following == 0:
            for dimension, periodic, period in (
                (
                    0,
                    model.surface.IsUPeriodic(),
                    model.surface.UPeriod() if model.surface.IsUPeriodic() else 0.0,
                ),
                (
                    1,
                    model.surface.IsVPeriodic(),
                    model.surface.VPeriod() if model.surface.IsVPeriodic() else 0.0,
                ),
            ):
                if periodic and period > 0:
                    uv_end[dimension] += (
                        round((parameters[index, dimension] - uv_end[dimension]) / period) * period
                    )
        segment = GCE2d_MakeSegment(
            gp_Pnt2d(float(parameters[index, 0]), float(parameters[index, 1])),
            gp_Pnt2d(float(uv_end[0]), float(uv_end[1])),
        )
        if not segment.IsDone():
            return []
        maker = BRepBuilderAPI_MakeEdge(
            segment.Value(),
            model.surface,
            vertices[index],
            vertices[following],
        )
        if not maker.IsDone():
            return []
        edge = maker.Edge()
        if not BRepLib.BuildCurves3d_s(edge, max(tolerance * 0.1, 1e-10)):
            return []
        result.append(
            _TrimEdge(
                _set_edge_tolerance(edge, tolerance * 5),
                np.vstack((projected_array[index], projected_array[following])),
                np.vstack((source[index], source[following])),
            )
        )
    return result


def _shared_trim_edges(
    graph: SurfaceGraph,
    models: dict[str, _SurfaceModel],
    tolerance: float,
) -> tuple[dict[str, list[_TrimEdge]], int, int]:
    by_patch: dict[str, list[_TrimEdge]] = {patch.patch_id: [] for patch in graph.patches}
    records = [
        (adjacency.first_patch_id, adjacency.second_patch_id, np.asarray(points, dtype=float))
        for adjacency in graph.adjacency
        for points in adjacency.boundary_curves
    ]
    endpoints, canonical_vertices = _canonical_vertices(records, models, tolerance)
    vertex_occurrences = Counter(id(vertex) for vertex in endpoints.values())
    canonical_edge_count = 0
    for record_index, (first_id, second_id, points) in enumerate(records):
        first = models.get(first_id)
        second = models.get(second_id)
        start = endpoints.get((record_index, 0))
        end = endpoints.get((record_index, 1))
        if len(points) >= 3 and np.linalg.norm(points[0] - points[-1]) <= tolerance * 3:
            segments_start = points[:-1]
            segments_end = points[1:]
            candidates: list[tuple[int, float, _CanonicalVertex]] = []
            for vertex in canonical_vertices:
                if vertex is start or vertex_occurrences[id(vertex)] < 2:
                    continue
                directions = segments_end - segments_start
                lengths_squared = np.einsum("ij,ij->i", directions, directions)
                fractions = np.clip(
                    np.einsum("ij,ij->i", vertex.point - segments_start, directions)
                    / np.maximum(lengths_squared, 1e-30),
                    0.0,
                    1.0,
                )
                projections = segments_start + fractions[:, None] * directions
                distance = float(
                    np.min(np.linalg.norm(projections - vertex.point, axis=1))
                )
                if distance <= tolerance * 0.5:
                    candidates.append(
                        (vertex_occurrences[id(vertex)], distance, vertex)
                    )
            if candidates:
                _, _, junction = min(
                    candidates,
                    key=lambda item: (-item[0], item[1]),
                )
                start = end = junction
        try:
            # Mesh-repair tools make mismatched boundaries conform by splitting
            # the opposite border at every incoming vertex before welding it.
            # Do the B-rep equivalent when an analytic region meets a residual:
            # project every source boundary node to the analytic support and
            # create one exact on-surface edge per source mesh edge.  The same
            # edge chain is then consumed by both regions.
            one_analytic_support = (first is None) != (second is None)
            coincident_cylinders = bool(
                first is not None
                and second is not None
                and isinstance(first.patch, CylindricalPatch)
                and isinstance(second.patch, CylindricalPatch)
                and abs(first.patch.radius - second.patch.radius) <= tolerance
                and math.acos(
                    float(
                        np.clip(
                            abs(float(first.patch.axis @ second.patch.axis)),
                            -1.0,
                            1.0,
                        )
                    )
                )
                <= math.radians(0.25)
                and np.linalg.norm(
                    (second.patch.origin - first.patch.origin)
                    - first.patch.axis
                    * float(
                        (second.patch.origin - first.patch.origin)
                        @ first.patch.axis
                    )
                )
                <= tolerance
            )
            conforming = (
                _surface_conforming_trim_chain(
                    first or second,
                    points,
                    tolerance,
                    start,
                    end,
                )
                if one_analytic_support or coincident_cylinders
                else []
            )
            edge = (
                _intersection_edge(
                    first,
                    second,
                    points,
                    tolerance,
                    start,
                    end,
                    maximum_error_factor=1.0,
                )
                if first is not None and second is not None and not coincident_cylinders
                else None
            )
            wrapped = (
                _wrapped_intersection_trim_chain(
                    first,
                    second,
                    points,
                    tolerance,
                    start,
                    end,
                )
                if edge is None and first is not None and second is not None
                else []
            )
            if (
                edge is None
                and not wrapped
                and not conforming
                and first is None
                and second is None
            ):
                # Two residual/swept patches must retain their exact source
                # interface. A single unconstrained B-spline approximation can
                # shortcut a long boundary and change the enclosed area even
                # when its total length looks plausible.
                record_edges = _polyline_trim_edges(
                    points,
                    tolerance,
                    start,
                    end,
                )
            else:
                fallback = (
                    _fallback_curve_edge(points, tolerance, start, end)
                    if edge is None and not wrapped and not conforming
                    else None
                )
                record_edges = conforming or wrapped or (
                    [edge or fallback] if edge is not None or fallback is not None else []
                )
            if not record_edges and first is not None and second is not None:
                fallback = _intersection_edge(
                    first,
                    second,
                    points,
                    tolerance,
                    start,
                    end,
                )
                record_edges = [fallback] if fallback is not None else []
        except Exception:
            record_edges = []
        if not record_edges:
            continue
        # Both faces receive the same TopoDS_Edge TShape. Orientation may be
        # reversed later when each face wire is ordered, but identity is kept.
        by_patch[first_id].extend(record_edges)
        by_patch[second_id].extend(record_edges)
        canonical_edge_count += len(record_edges)
    return by_patch, len(canonical_vertices), canonical_edge_count


def _reverse_edge(edge: TopoDS_Edge) -> TopoDS_Edge:
    return TopoDS.Edge_s(edge.Reversed())


def _split_edge_at_middle(edge: TopoDS_Edge) -> tuple[TopoDS_Edge, TopoDS_Edge] | None:
    """Split one edge without changing its curve or endpoint vertices."""

    try:
        adaptor = BRepAdaptor_Curve(edge)
        first_parameter = float(adaptor.FirstParameter())
        last_parameter = float(adaptor.LastParameter())
        if not math.isfinite(first_parameter) or not math.isfinite(last_parameter):
            return None
        middle_parameter = (first_parameter + last_parameter) / 2
        curve = adaptor.Curve().Curve()
        start = TopExp.FirstVertex_s(edge, True)
        end = TopExp.LastVertex_s(edge, True)
        start_point = np.asarray(cq.Vertex(start).toTuple(), dtype=float)
        low_point = curve.Value(first_parameter)
        high_point = curve.Value(last_parameter)
        low_distance = np.linalg.norm(
            start_point - np.asarray((low_point.X(), low_point.Y(), low_point.Z()))
        )
        high_distance = np.linalg.norm(
            start_point - np.asarray((high_point.X(), high_point.Y(), high_point.Z()))
        )
        middle = BRepBuilderAPI_MakeVertex(curve.Value(middle_parameter)).Vertex()
        if low_distance <= high_distance:
            first_maker = BRepBuilderAPI_MakeEdge(
                curve,
                start,
                middle,
                first_parameter,
                middle_parameter,
            )
            second_maker = BRepBuilderAPI_MakeEdge(
                curve,
                middle,
                end,
                middle_parameter,
                last_parameter,
            )
            reverse = False
        else:
            first_maker = BRepBuilderAPI_MakeEdge(
                curve,
                middle,
                start,
                middle_parameter,
                last_parameter,
            )
            second_maker = BRepBuilderAPI_MakeEdge(
                curve,
                end,
                middle,
                first_parameter,
                middle_parameter,
            )
            reverse = True
        if not first_maker.IsDone() or not second_maker.IsDone():
            return None
        first_edge = first_maker.Edge()
        second_edge = second_maker.Edge()
        if reverse:
            first_edge = _reverse_edge(first_edge)
            second_edge = _reverse_edge(second_edge)
        return first_edge, second_edge
    except Exception:
        return None


def _wire_groups(edges: list[_TrimEdge], tolerance: float) -> list[tuple[TopoDS_Wire, np.ndarray]]:
    # Let OCCT assemble the complete edge graph before falling back to the
    # historical greedy walk. The greedy walk can take the wrong branch at a
    # multi-surface corner, leaving a long conforming polyline as dozens of
    # one-edge open wires. FreeBounds maximizes shared-TShape connectivity and
    # correctly recovers each closed boundary component.
    if edges:
        try:
            sequence = TopTools_HSequenceOfShape()
            for trim in edges:
                sequence.Append(trim.edge)
            trims_by_hash: dict[int, list[_TrimEdge]] = {}
            for trim in edges:
                trims_by_hash.setdefault(hash(trim.edge), []).append(trim)
            connected = TopTools_HSequenceOfShape()
            ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(
                sequence,
                tolerance * 10,
                True,
                connected,
            )
            freebound_groups: list[tuple[TopoDS_Wire, np.ndarray]] = []
            consumed_edges = 0
            for wire_index in range(1, connected.Length() + 1):
                wire = TopoDS.Wire_s(connected.Value(wire_index))
                samples: list[np.ndarray] = []
                explorer = BRepTools_WireExplorer(wire)
                while explorer.More():
                    oriented = TopoDS.Edge_s(explorer.Current())
                    match = next(
                        (
                            trim
                            for trim in trims_by_hash.get(hash(oriented), [])
                            if oriented.IsSame(trim.edge)
                        ),
                        None,
                    )
                    if match is not None:
                        points = (
                            match.points
                            if oriented.Orientation() == match.edge.Orientation()
                            else match.points[::-1]
                        )
                        samples.append(points[:-1] if len(points) > 1 else points)
                    consumed_edges += 1
                    explorer.Next()
                if samples:
                    freebound_groups.append((wire, np.vstack(samples)))
            if consumed_edges == len(edges) and freebound_groups:
                return freebound_groups
        except Exception:
            pass

    groups: list[tuple[TopoDS_Wire, np.ndarray]] = []
    remaining = list(edges)
    while remaining:
        first = remaining.pop(0)
        if first.closed:
            maker = BRepBuilderAPI_MakeWire(first.edge)
            if maker.IsDone():
                groups.append((maker.Wire(), first.points))
            continue
        ordered: list[tuple[TopoDS_Edge, np.ndarray]] = [(first.edge, first.points)]
        start = first.start
        current = first.end
        start_vertex = TopExp.FirstVertex_s(first.edge, True)
        current_vertex = TopExp.LastVertex_s(first.edge, True)
        while remaining and not (
            (not start_vertex.IsNull() and current_vertex.IsSame(start_vertex))
            or np.linalg.norm(current - start) <= tolerance * 5
        ):
            match_index = -1
            reverse = False
            best_distance = math.inf
            for index, candidate in enumerate(remaining):
                candidate_start = TopExp.FirstVertex_s(candidate.edge, True)
                candidate_end = TopExp.LastVertex_s(candidate.edge, True)
                if not current_vertex.IsNull() and current_vertex.IsSame(candidate_start):
                    match_index, reverse, best_distance = index, False, -1.0
                    break
                if not current_vertex.IsNull() and current_vertex.IsSame(candidate_end):
                    match_index, reverse, best_distance = index, True, -1.0
                    break
                forward_distance = float(np.linalg.norm(current - candidate.start))
                reverse_distance = float(np.linalg.norm(current - candidate.end))
                if forward_distance < best_distance:
                    match_index, reverse, best_distance = index, False, forward_distance
                if reverse_distance < best_distance:
                    match_index, reverse, best_distance = index, True, reverse_distance
            if match_index < 0 or best_distance > tolerance * 10:
                break
            candidate = remaining.pop(match_index)
            if reverse:
                candidate_points = candidate.points[::-1]
                candidate_edge = _reverse_edge(candidate.edge)
            else:
                candidate_points = candidate.points
                candidate_edge = candidate.edge
            ordered.append((candidate_edge, candidate_points))
            current = candidate_points[-1]
            current_vertex = TopExp.LastVertex_s(candidate_edge, True)
        maker = BRepBuilderAPI_MakeWire()
        samples: list[np.ndarray] = []
        for edge, edge_points in ordered:
            maker.Add(edge)
            samples.append(edge_points[:-1])
        if maker.IsDone():
            groups.append((maker.Wire(), np.vstack(samples)))
    return groups


def _projected_area(points: np.ndarray, patch: SurfacePatch) -> float:
    if len(points) < 3:
        return 0.0
    if isinstance(patch, PlanarPatch):
        first, second = patch.x_direction, patch.y_direction
        origin = patch.origin
    elif isinstance(patch, CylindricalPatch):
        first, second = _plane_basis(patch.axis)
        origin = patch.origin
    elif isinstance(patch, ConicalPatch):
        first, second = _plane_basis(patch.axis)
        origin = patch.apex
    elif isinstance(patch, SphericalPatch):
        first, second = np.eye(3)[0], np.eye(3)[1]
        origin = patch.center
    elif isinstance(patch, ToroidalPatch):
        first, second = _plane_basis(patch.axis)
        origin = patch.center
    else:
        centered = points - np.mean(points, axis=0)
        _, _, directions = np.linalg.svd(centered, full_matrices=False)
        first, second = directions[0], directions[1]
        origin = np.mean(points, axis=0)
    projected = np.column_stack(((points - origin) @ first, (points - origin) @ second))
    x, y = projected[:, 0], projected[:, 1]
    return abs(float(np.sum(x * np.roll(y, -1) - y * np.roll(x, -1)) / 2))


def _angular_coverage(points: np.ndarray, origin: np.ndarray, axis: np.ndarray) -> float:
    first, second = _plane_basis(axis)
    relative = points - origin
    angles = np.mod(np.arctan2(relative @ second, relative @ first), 2 * math.pi)
    angles = np.sort(np.unique(np.round(angles, 8)))
    if len(angles) < 3:
        return 0.0
    gaps = np.diff(np.r_[angles, angles[0] + 2 * math.pi])
    return 2 * math.pi - float(np.max(gaps))


def _orient_face(face: TopoDS_Face, patch: SurfacePatch, mesh: object) -> TopoDS_Face:
    centers = np.asarray(mesh.triangles_center[patch.face_indices], dtype=float)
    normals = np.asarray(mesh.face_normals[patch.face_indices], dtype=float)
    expected: np.ndarray | None = None
    if isinstance(patch, CylindricalPatch):
        relative = centers - patch.origin
        radial = relative - np.outer(relative @ patch.axis, patch.axis)
        expected = radial / np.maximum(np.linalg.norm(radial, axis=1)[:, None], 1e-15)
    elif isinstance(patch, ConicalPatch):
        relative = centers - patch.apex
        axial = relative @ patch.axis
        radial = relative - np.outer(axial, patch.axis)
        radial /= np.maximum(np.linalg.norm(radial, axis=1)[:, None], 1e-15)
        side = np.sign(axial)
        tangent = math.tan(patch.semi_angle)
        expected = radial - side[:, None] * tangent * patch.axis
        expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    elif isinstance(patch, SphericalPatch):
        expected = centers - patch.center
        expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    elif isinstance(patch, ToroidalPatch):
        relative = centers - patch.center
        axial = relative @ patch.axis
        planar = relative - np.outer(axial, patch.axis)
        planar /= np.maximum(np.linalg.norm(planar, axis=1)[:, None], 1e-15)
        ring = patch.center + patch.major_radius * planar
        expected = centers - ring
        expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    elif isinstance(
        patch,
        (FreeformPatch, LinearExtrusionPatch, SurfaceOfRevolutionPatch),
    ):
        try:
            face_normal = np.asarray(cq.Face(face).normalAt().toTuple(), dtype=float)
            if float(np.mean(normals, axis=0) @ face_normal) < 0:
                face.Reverse()
        except Exception:
            pass
        return face
    if expected is not None and float(np.median(np.einsum("ij,ij->i", normals, expected))) < 0:
        face.Reverse()
    return face


def _bounded_periodic_face(
    model: _SurfaceModel,
    mesh: object,
) -> TopoDS_Face | None:
    patch = model.patch
    vertex_indices = np.unique(mesh.faces[patch.face_indices])
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    if isinstance(patch, CylindricalPatch):
        if _angular_coverage(points, patch.origin, patch.axis) < math.radians(330):
            return None
        axial = (points - patch.origin) @ patch.axis
        maker = BRepBuilderAPI_MakeFace(
            model.elementary,
            0.0,
            2 * math.pi,
            float(np.min(axial)),
            float(np.max(axial)),
        )
        return maker.Face() if maker.IsDone() else None
    if isinstance(patch, ConicalPatch):
        axis = model.surface.Axis().Direction()
        axis_array = np.asarray([axis.X(), axis.Y(), axis.Z()])
        axial = (points - patch.apex) @ axis_array
        if _angular_coverage(points, patch.apex, axis_array) < math.radians(330):
            return None
        cosine = math.cos(patch.semi_angle)
        maker = BRepBuilderAPI_MakeFace(
            model.elementary,
            0.0,
            2 * math.pi,
            float(np.min(axial) / cosine),
            float(np.max(axial) / cosine),
        )
        return maker.Face() if maker.IsDone() else None
    if isinstance(patch, SphericalPatch) and not patch.boundary_loops:
        maker = BRepBuilderAPI_MakeFace(model.elementary)
        return maker.Face() if maker.IsDone() else None
    if isinstance(patch, ToroidalPatch) and not patch.boundary_loops:
        maker = BRepBuilderAPI_MakeFace(model.elementary)
        return maker.Face() if maker.IsDone() else None
    return None


def _split_periodic_face(
    model: _SurfaceModel,
    periodic_face: TopoDS_Face,
    edges: list[_TrimEdge],
    tolerance: float,
) -> TopoDS_Face:
    """Replace false axial end caps with actual surface intersections.

    A full-angle cylinder/cone can still terminate on another curved surface.
    Its mesh extrema create circular parameter bounds, but those are not the
    physical B-rep boundary. Split the periodic support face by the canonical
    non-isoparametric intersection curves and select the region whose area
    agrees with the node-supported patch.
    """

    splitting_edges = [
        edge for edge in edges if cq.Edge(edge.edge).geomType() not in {"CIRCLE", "LINE"}
    ]
    if not splitting_edges:
        return periodic_face
    try:
        edge_fixer = ShapeFix_Edge()
        for edge in splitting_edges:
            if not edge_fixer.FixAddPCurve(
                edge.edge,
                periodic_face,
                False,
                tolerance,
            ):
                return periodic_face
        splitter = BRepFeat_SplitShape(periodic_face)
        for edge in splitting_edges:
            splitter.Add(edge.edge, periodic_face)
        splitter.Build()
        if not splitter.IsDone():
            return periodic_face
        candidates: list[TopoDS_Face] = []
        explorer = TopExp_Explorer(splitter.Shape(), TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            if _usable_face(face, tolerance):
                candidates.append(face)
            explorer.Next()
        if not candidates:
            return periodic_face
        target_area = max(model.patch.area, tolerance**2)
        return min(
            candidates,
            key=lambda face: abs(
                math.log(max(float(cq.Face(face).Area()), tolerance**2) / target_area)
            ),
        )
    except Exception:
        return periodic_face


def _replace_periodic_boundary_edges(
    face: TopoDS_Face,
    edges: list[_TrimEdge],
    tolerance: float,
) -> TopoDS_Face:
    """Replace generated periodic end edges with canonical shared TShapes."""

    closed = [edge for edge in edges if edge.closed]
    if not closed:
        return face
    try:
        support_edges = [
            edge
            for edge in cq.Face(face).Edges()
            if len(edge.Vertices()) <= 1 or edge.geomType() == "CIRCLE"
        ]
        if not support_edges:
            return face
        reshaper = BRepTools_ReShape()
        unused = list(closed)
        edge_fixer = ShapeFix_Edge()
        replaced_count = 0
        for support_edge in support_edges:
            support_length = float(support_edge.Length())
            match = min(
                unused,
                key=lambda item: abs(float(cq.Edge(item.edge).Length()) - support_length),
                default=None,
            )
            if match is None:
                continue
            length_error = abs(float(cq.Edge(match.edge).Length()) - support_length)
            if length_error > max(tolerance * 20, support_length * 0.01):
                continue
            try:
                edge_fixer.FixAddPCurve(match.edge, face, False, tolerance)
            except Exception:
                continue
            reshaper.Replace(support_edge.wrapped, match.edge)
            unused.remove(match)
            replaced_count += 1
        if replaced_count == 0:
            return face
        replaced = reshaper.Apply(face)
        if replaced.ShapeType() != TopAbs_FACE:
            return face
        candidate = TopoDS.Face_s(replaced)
        return candidate if _usable_face(candidate, tolerance) else face
    except Exception:
        return face


def _rectangular_cylinder_face(
    model: _SurfaceModel,
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    patch = model.patch
    if not isinstance(patch, CylindricalPatch) or not patch.boundary_loops:
        return None

    position = model.elementary.Position()
    location = np.asarray(
        [position.Location().X(), position.Location().Y(), position.Location().Z()]
    )
    axis = np.asarray(
        [position.Direction().X(), position.Direction().Y(), position.Direction().Z()]
    )
    x_direction = np.asarray(
        [position.XDirection().X(), position.XDirection().Y(), position.XDirection().Z()]
    )
    y_direction = np.asarray(
        [position.YDirection().X(), position.YDirection().Y(), position.YDirection().Z()]
    )
    boundary = np.vstack(patch.boundary_loops)
    relative = boundary - location
    angles = np.mod(
        np.arctan2(relative @ y_direction, relative @ x_direction),
        2 * math.pi,
    )
    ordered = np.sort(angles)
    gaps = np.diff(np.r_[ordered, ordered[0] + 2 * math.pi])
    gap_index = int(np.argmax(gaps))
    start = float(ordered[(gap_index + 1) % len(ordered)])
    unwrapped = np.mod(angles - start, 2 * math.pi) + start
    end = float(np.max(unwrapped))
    if end - start >= math.radians(330):
        return None

    axial = relative @ axis
    v_min, v_max = float(np.min(axial)), float(np.max(axial))
    distance_to_u = np.minimum(unwrapped - start, end - unwrapped) * patch.radius
    distance_to_v = np.minimum(axial - v_min, v_max - axial)
    if float(np.max(np.minimum(distance_to_u, distance_to_v))) > tolerance * 10:
        return None

    maker = BRepBuilderAPI_MakeFace(
        model.elementary,
        start,
        end,
        v_min,
        v_max,
    )
    if not maker.IsDone() or not BRepCheck_Analyzer(maker.Face()).IsValid():
        return None
    return _orient_face(maker.Face(), patch, mesh)


def _rectangular_torus_face(
    model: _SurfaceModel,
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Build a torus patch directly when its node boundary is parametric."""

    patch = model.patch
    if not isinstance(patch, ToroidalPatch) or not patch.boundary_loops:
        return None
    position = model.elementary.Position()
    location = np.asarray(
        [position.Location().X(), position.Location().Y(), position.Location().Z()]
    )
    axis = np.asarray(
        [position.Direction().X(), position.Direction().Y(), position.Direction().Z()]
    )
    x_direction = np.asarray(
        [position.XDirection().X(), position.XDirection().Y(), position.XDirection().Z()]
    )
    y_direction = np.asarray(
        [position.YDirection().X(), position.YDirection().Y(), position.YDirection().Z()]
    )
    points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
    relative = points - location
    axial = relative @ axis
    planar_x = relative @ x_direction
    planar_y = relative @ y_direction
    planar_radius = np.hypot(planar_x, planar_y)
    u = np.mod(np.arctan2(planar_y, planar_x), 2 * math.pi)
    v = np.mod(
        np.arctan2(axial, planar_radius - patch.major_radius),
        2 * math.pi,
    )

    def unwrap(values: np.ndarray) -> tuple[float, float, np.ndarray]:
        ordered = np.sort(values)
        gaps = np.diff(np.r_[ordered, ordered[0] + 2 * math.pi])
        gap_index = int(np.argmax(gaps))
        start = float(ordered[(gap_index + 1) % len(ordered)])
        unwrapped = np.mod(values - start, 2 * math.pi) + start
        return start, float(np.max(unwrapped)), unwrapped

    full_u = _angular_coverage(points, patch.center, patch.axis) >= math.radians(330)
    u_min, u_max = (0.0, 2 * math.pi) if full_u else unwrap(u)[:2]
    v_min, v_max, _ = unwrap(v)
    boundary = np.vstack(patch.boundary_loops)
    boundary_relative = boundary - location
    boundary_axial = boundary_relative @ axis
    boundary_x = boundary_relative @ x_direction
    boundary_y = boundary_relative @ y_direction
    boundary_radius = np.hypot(boundary_x, boundary_y)
    boundary_u = (
        np.mod(
            np.arctan2(boundary_y, boundary_x) - u_min,
            2 * math.pi,
        )
        + u_min
    )
    boundary_v = (
        np.mod(
            np.arctan2(
                boundary_axial,
                boundary_radius - patch.major_radius,
            )
            - v_min,
            2 * math.pi,
        )
        + v_min
    )
    distance_to_u = np.minimum(
        np.abs(boundary_u - u_min),
        np.abs(u_max - boundary_u),
    ) * np.maximum(boundary_radius, patch.minor_radius)
    distance_to_v = (
        np.minimum(
            np.abs(boundary_v - v_min),
            np.abs(v_max - boundary_v),
        )
        * patch.minor_radius
    )
    boundary_error = float(np.max(np.minimum(distance_to_u, distance_to_v)))
    maker = BRepBuilderAPI_MakeFace(
        model.elementary,
        u_min,
        u_max,
        v_min,
        v_max,
    )
    if not maker.IsDone() or not BRepCheck_Analyzer(maker.Face()).IsValid():
        return None
    face = _orient_face(maker.Face(), patch, mesh)
    area_ratio = float(cq.Face(face).Area()) / max(patch.area, tolerance**2)
    if boundary_error <= tolerance * 10:
        return face if 0.5 <= area_ratio <= 1.5 else None
    return face if 0.8 <= area_ratio <= 1.2 else None


def _toroidal_revolution_face(
    patch: ToroidalPatch,
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Build a full-angle torus band from its collapsed meridian nodes."""

    points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
    if _angular_coverage(points, patch.center, patch.axis) < math.radians(330):
        return None
    relative = points - patch.center
    axial = relative @ patch.axis
    radial = np.linalg.norm(
        relative - np.outer(axial, patch.axis),
        axis=1,
    )
    ordered = _ordered_cloud_path(
        np.column_stack((axial, radial)),
        tolerance,
    )
    if ordered is None:
        return None
    radial_direction, _ = _plane_basis(patch.axis)
    profile_points = (
        patch.center + ordered[:, 0, None] * patch.axis + ordered[:, 1, None] * radial_direction
    )
    revolution = SurfaceOfRevolutionPatch(
        patch_id=patch.patch_id,
        face_indices=patch.face_indices,
        vertex_indices=patch.vertex_indices,
        origin=patch.center,
        axis=patch.axis,
        radial_direction=radial_direction,
        profile_points=profile_points,
        area=patch.area,
        boundary_loops=patch.boundary_loops,
        rms_error=patch.rms_error,
        max_error=patch.max_error,
        normal_error_degrees=patch.normal_error_degrees,
    )
    return _surface_of_revolution_face(revolution, [], mesh, tolerance)


def _periodic_surface_uv(
    model: _SurfaceModel,
    points: np.ndarray,
) -> np.ndarray | None:
    """Map ordered boundary nodes to a continuous periodic UV path."""

    patch = model.patch
    position = model.elementary.Position()
    location = np.asarray(
        [position.Location().X(), position.Location().Y(), position.Location().Z()]
    )
    axis = np.asarray(
        [position.Direction().X(), position.Direction().Y(), position.Direction().Z()]
    )
    x_direction = np.asarray(
        [position.XDirection().X(), position.XDirection().Y(), position.XDirection().Z()]
    )
    y_direction = np.asarray(
        [position.YDirection().X(), position.YDirection().Y(), position.YDirection().Z()]
    )
    relative = np.asarray(points, dtype=float) - location
    planar_x = relative @ x_direction
    planar_y = relative @ y_direction
    u = np.unwrap(np.arctan2(planar_y, planar_x))
    if isinstance(patch, CylindricalPatch):
        v = relative @ axis
    elif isinstance(patch, ConicalPatch):
        v = (relative @ axis) / max(math.cos(patch.semi_angle), 1e-12)
    elif isinstance(patch, ToroidalPatch):
        axial = relative @ axis
        planar_radius = np.hypot(planar_x, planar_y)
        v = np.unwrap(np.arctan2(axial, planar_radius - patch.major_radius))
    else:
        return None
    uv = np.column_stack((u, v))
    return uv if np.all(np.isfinite(uv)) else None


def _uv_boundary_face(
    model: _SurfaceModel,
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Trim a periodic analytic support with exact curves in parameter space."""

    patch = model.patch
    if not isinstance(patch, (CylindricalPatch, ConicalPatch, ToroidalPatch)):
        return None
    if not patch.boundary_loops:
        return None
    candidates: list[tuple[float, TopoDS_Wire]] = []
    for raw_loop in patch.boundary_loops:
        loop = np.asarray(raw_loop, dtype=float)
        if len(loop) < 4:
            continue
        closed = np.linalg.norm(loop[0] - loop[-1]) <= tolerance * 5
        if not closed:
            continue
        maximum_points = 192
        if len(loop) > maximum_points:
            selected = np.linspace(0, len(loop) - 1, maximum_points, dtype=int)
            loop = loop[selected]
            loop[-1] = loop[0]
        uv = _periodic_surface_uv(model, loop)
        if uv is None or len(uv) < 4:
            continue
        # The first and last 3-D points are identical, but a non-contractible
        # loop may legitimately end one period away in U or V.
        parameters = TColgp_HArray1OfPnt2d(1, len(uv))
        for index, value in enumerate(uv, start=1):
            parameters.SetValue(index, gp_Pnt2d(float(value[0]), float(value[1])))
        try:
            interpolator = Geom2dAPI_Interpolate(parameters, False, 1e-9)
            interpolator.Perform()
            if not interpolator.IsDone():
                continue
            edge_maker = BRepBuilderAPI_MakeEdge(
                interpolator.Curve(),
                model.surface,
            )
            if not edge_maker.IsDone():
                continue
            wire_maker = BRepBuilderAPI_MakeWire(edge_maker.Edge())
            if not wire_maker.IsDone():
                continue
            wire = wire_maker.Wire()
            if not wire.Closed():
                continue
            signed_area = float(np.sum(uv[:-1, 0] * uv[1:, 1] - uv[:-1, 1] * uv[1:, 0]) / 2)
            candidates.append((abs(signed_area), wire))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    try:
        maker = BRepBuilderAPI_MakeFace(model.surface, candidates[0][1], True)
        if not maker.IsDone():
            return None
        for _, wire in candidates[1:]:
            maker.Add(wire)
        if not maker.IsDone():
            return None
        fixer = ShapeFix_Face(maker.Face())
        fixer.SetPrecision(tolerance)
        fixer.SetMaxTolerance(tolerance * 5)
        fixer.FixOrientation()
        fixer.Perform()
        face = _orient_face(fixer.Face(), patch, mesh)
    except Exception:
        return None
    if not _usable_face(face, tolerance):
        return None
    area_ratio = float(cq.Face(face).Area()) / max(patch.area, tolerance**2)
    return face if 0.5 <= area_ratio <= 1.5 else None


def _analytic_face(
    model: _SurfaceModel,
    edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    # A UV-bounded cone band carries a generated periodic p-curve which can
    # become self-intersecting when sewing substitutes its circular borders.
    # Trim cones directly with their canonical shared intersection edges.
    periodic = (
        _bounded_periodic_face(model, mesh)
        if len(edges) <= 20 and not isinstance(model.patch, ConicalPatch)
        else None
    )
    if periodic is not None:
        periodic = _split_periodic_face(model, periodic, edges, tolerance)
        periodic = _replace_periodic_boundary_edges(periodic, edges, tolerance)
        periodic = _orient_face(periodic, model.patch, mesh)
        area_ratio = float(cq.Face(periodic).Area()) / max(
            model.patch.area,
            tolerance**2,
        )
        if 0.8 <= area_ratio <= 1.2:
            return periodic
    try:
        support = BRepBuilderAPI_MakeFace(model.surface, tolerance).Face()
        edge_fixer = ShapeFix_Edge()
        for trim_edge in edges:
            edge_fixer.FixAddPCurve(
                trim_edge.edge,
                support,
                False,
                tolerance,
            )
    except Exception:
        pass
    groups = _wire_groups(edges, tolerance)
    if not groups:
        return _rectangular_cylinder_face(model, mesh, tolerance)
    groups.sort(key=lambda item: -_projected_area(item[1], model.patch))
    repaired_groups: list[tuple[TopoDS_Wire, np.ndarray]] = []
    for wire, points in groups:
        wire_fixer = ShapeFix_Wire(wire, support, tolerance)
        wire_fixer.SetPrecision(tolerance)
        wire_fixer.SetMaxTolerance(tolerance * 10)
        wire_fixer.FixReorder()
        wire_fixer.FixConnected()
        wire_fixer.FixEdgeCurves()
        wire_fixer.FixClosed()
        wire = wire_fixer.Wire()
        if not wire.Closed() and len(cq.Wire(wire).Edges()) == 1:
            edge = TopoDS.Edge_s(cq.Wire(wire).Edges()[0].wrapped)
            first = TopExp.FirstVertex_s(edge, True)
            last = TopExp.LastVertex_s(edge, True)
            if not first.IsNull() and not last.IsNull():
                gap = np.linalg.norm(
                    np.asarray(cq.Vertex(first).toTuple(), dtype=float)
                    - np.asarray(cq.Vertex(last).toTuple(), dtype=float)
                )
                if gap <= tolerance * 10:
                    try:
                        reshaper = BRepTools_ReShape()
                        reshaper.Replace(last, first)
                        closed_edge = TopoDS.Edge_s(reshaper.Apply(edge))
                        closed_maker = BRepBuilderAPI_MakeWire(closed_edge)
                        if closed_maker.IsDone():
                            wire = closed_maker.Wire()
                    except Exception:
                        pass
                    wire.Closed(True)
        repaired_groups.append((wire, points))
    groups = repaired_groups
    if isinstance(model.patch, CylindricalPatch):
        patch_points = np.asarray(
            mesh.vertices[model.patch.vertex_indices],
            dtype=float,
        )
        if _angular_coverage(
            patch_points,
            model.patch.origin,
            model.patch.axis,
        ) >= math.radians(330):
            interior_wires = [
                wire
                for wire, points in groups
                if float(
                    np.ptp((points - model.patch.origin) @ model.patch.axis)
                )
                > tolerance * 10
                and _angular_coverage(
                    points,
                    model.patch.origin,
                    model.patch.axis,
                )
                < math.radians(330)
            ]
            if interior_wires and len(interior_wires) <= 4:
                axial = (patch_points - model.patch.origin) @ model.patch.axis
                periodic_candidates: list[TopoDS_Face] = []
                for orientation_mask in range(1 << len(interior_wires)):
                    try:
                        trial = BRepBuilderAPI_MakeFace(
                            model.elementary,
                            0.0,
                            2 * math.pi,
                            float(np.min(axial)),
                            float(np.max(axial)),
                        )
                        for index, wire in enumerate(interior_wires):
                            trial.Add(
                                TopoDS.Wire_s(wire.Reversed())
                                if orientation_mask & (1 << index)
                                else wire
                            )
                        if not trial.IsDone():
                            continue
                        candidate = trial.Face()
                        if _usable_face(candidate, tolerance):
                            periodic_candidates.append(candidate)
                    except Exception:
                        continue
                if periodic_candidates:
                    face = min(
                        periodic_candidates,
                        key=lambda item: abs(
                            float(cq.Face(item).Area()) - model.patch.area
                        ),
                    )
                    area_ratio = float(cq.Face(face).Area()) / max(
                        model.patch.area,
                        tolerance**2,
                    )
                    if 0.5 <= area_ratio <= 1.5:
                        return _orient_face(face, model.patch, mesh)
    if isinstance(model.patch, PlanarPatch) and len(groups) > 1:

        def planar_wire_area(item: tuple[TopoDS_Wire, np.ndarray]) -> float:
            try:
                candidate = BRepBuilderAPI_MakeFace(model.surface, item[0], True)
                return abs(float(cq.Face(candidate.Face()).Area())) if candidate.IsDone() else 0.0
            except Exception:
                return 0.0

        # Determine containment from the actual planar wire area. A one-edge
        # circle can be either the outside of a thin annulus or an ordinary
        # hole; edge count alone cannot distinguish those cases.
        groups.sort(key=planar_wire_area, reverse=True)
        signed_areas = []
        for _, points in groups:
            projected = np.column_stack(
                (
                    (points - model.patch.origin) @ model.patch.x_direction,
                    (points - model.patch.origin) @ model.patch.y_direction,
                )
            )
            signed_areas.append(
                float(
                    np.sum(
                        projected[:, 0] * np.roll(projected[:, 1], -1)
                        - projected[:, 1] * np.roll(projected[:, 0], -1)
                    )
                    / 2
                )
            )
        outer_sign = 1.0 if signed_areas[0] >= 0 else -1.0
        groups = [
            (
                TopoDS.Wire_s(wire.Reversed()) if index > 0 and area * outer_sign > 0 else wire,
                points,
            )
            for index, ((wire, points), area) in enumerate(zip(groups, signed_areas, strict=True))
        ]
        if len(groups) == 2:
            planar_candidates: list[TopoDS_Face] = []
            for outer_reversed in (False, True):
                outer = TopoDS.Wire_s(groups[0][0].Reversed()) if outer_reversed else groups[0][0]
                for inner_reversed in (False, True):
                    inner = (
                        TopoDS.Wire_s(groups[1][0].Reversed()) if inner_reversed else groups[1][0]
                    )
                    try:
                        trial = BRepBuilderAPI_MakeFace(model.surface, outer, True)
                        trial.Add(inner)
                        if trial.IsDone() and _usable_face(trial.Face(), tolerance):
                            planar_candidates.append(trial.Face())
                    except Exception:
                        continue
            if planar_candidates:
                face = min(
                    planar_candidates,
                    key=lambda item: abs(float(cq.Face(item).Area()) - model.patch.area),
                )
                area_ratio = float(cq.Face(face).Area()) / max(
                    model.patch.area,
                    tolerance**2,
                )
                if 0.5 <= area_ratio <= 1.5:
                    return _orient_face(face, model.patch, mesh)
    outer_wire = groups[0][0]
    maker = BRepBuilderAPI_MakeFace(model.surface, outer_wire, True)
    if not maker.IsDone():
        return None
    for wire, _ in groups[1:]:
        maker.Add(wire)
    if not maker.IsDone():
        return None
    raw_face = maker.Face()
    if isinstance(model.patch, ConicalPatch):
        if BRepCheck_Analyzer(raw_face).IsValid():
            raw_area_ratio = float(cq.Face(raw_face).Area()) / max(
                model.patch.area,
                tolerance**2,
            )
            if 0.5 <= raw_area_ratio <= 1.5:
                return _orient_face(raw_face, model.patch, mesh)
        bounded = _bounded_periodic_face(model, mesh)
        if bounded is not None and BRepCheck_Analyzer(bounded).IsValid():
            bounded_area_ratio = float(cq.Face(bounded).Area()) / max(
                model.patch.area,
                tolerance**2,
            )
            if 0.5 <= bounded_area_ratio <= 1.5:
                return _orient_face(bounded, model.patch, mesh)
        return _uv_boundary_face(model, mesh, tolerance)
    fixer = ShapeFix_Face(raw_face)
    fixer.SetPrecision(tolerance)
    fixer.SetMaxTolerance(tolerance * 5)
    fixer.FixOrientation()
    fixer.Perform()
    face = fixer.Face()
    if not BRepCheck_Analyzer(face).IsValid():
        return _rectangular_cylinder_face(model, mesh, tolerance)
    face = _orient_face(face, model.patch, mesh)
    area_ratio = float(cq.Face(face).Area()) / max(
        model.patch.area,
        tolerance**2,
    )
    return face if 0.5 <= area_ratio <= 1.5 else None


def _planar_boundary_face(
    model: _SurfaceModel,
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Trim a plane from its ordered mesh-node boundary loops."""

    patch = model.patch
    if not isinstance(patch, PlanarPatch):
        return None
    loops = getattr(patch, "boundary_loops_3d", [])
    candidates: list[tuple[float, TopoDS_Wire]] = []
    for raw_loop in loops:
        loop = np.asarray(raw_loop, dtype=float)
        if len(loop) < 4 or np.linalg.norm(loop[0] - loop[-1]) > tolerance * 5:
            continue
        trim = _fallback_curve_edge(loop, tolerance)
        if trim is None:
            continue
        maker = BRepBuilderAPI_MakeWire(trim.edge)
        if not maker.IsDone() or not maker.Wire().Closed():
            continue
        projected = np.column_stack(
            (
                (loop - patch.origin) @ patch.x_direction,
                (loop - patch.origin) @ patch.y_direction,
            )
        )
        signed_area = float(
            np.sum(projected[:-1, 0] * projected[1:, 1] - projected[:-1, 1] * projected[1:, 0]) / 2
        )
        candidates.append((abs(signed_area), maker.Wire()))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    try:
        maker = BRepBuilderAPI_MakeFace(model.surface, candidates[0][1], True)
        if not maker.IsDone():
            return None
        for _, wire in candidates[1:]:
            maker.Add(wire)
        if not maker.IsDone():
            return None
        fixer = ShapeFix_Face(maker.Face())
        fixer.SetPrecision(tolerance)
        fixer.SetMaxTolerance(tolerance * 5)
        fixer.FixOrientation()
        fixer.Perform()
        face = _orient_face(fixer.Face(), patch, mesh)
    except Exception:
        return None
    if not _usable_face(face, tolerance):
        return None
    area_ratio = float(cq.Face(face).Area()) / max(patch.area, tolerance**2)
    return face if 0.5 <= area_ratio <= 1.5 else None


def _sample_surface_nodes(points: np.ndarray, maximum: int = 48) -> np.ndarray:
    """Select well-distributed mesh nodes without using triangle interiors."""

    points = np.asarray(points, dtype=float)
    if len(points) <= maximum:
        return points
    selected = [int(np.argmax(np.linalg.norm(points - np.mean(points, axis=0), axis=1)))]
    distances = np.linalg.norm(points - points[selected[0]], axis=1)
    while len(selected) < maximum:
        index = int(np.argmax(distances))
        selected.append(index)
        distances = np.minimum(distances, np.linalg.norm(points - points[index], axis=1))
    return points[np.asarray(selected, dtype=np.int64)]


def _interior_patch_nodes(patch: SurfacePatch, mesh: object) -> np.ndarray:
    triangles = np.asarray(mesh.faces[patch.face_indices], dtype=np.int64)
    edges = np.sort(
        np.vstack((triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]])),
        axis=1,
    )
    unique_edges, counts = np.unique(edges, axis=0, return_counts=True)
    boundary_vertices = np.unique(unique_edges[counts == 1])
    all_vertices = np.unique(triangles)
    interior_vertices = np.setdiff1d(all_vertices, boundary_vertices, assume_unique=True)
    return np.asarray(mesh.vertices[interior_vertices], dtype=float)


def _polyline_boundary_edges(
    patch: SurfacePatch,
    tolerance: float,
) -> list[_TrimEdge]:
    """Build robust trimming edges through boundary nodes, never mesh faces."""

    edges: list[_TrimEdge] = []
    for raw_loop in getattr(patch, "boundary_loops_3d", patch.boundary_loops):
        loop = np.asarray(raw_loop, dtype=float)
        if len(loop) < 3:
            continue
        if np.linalg.norm(loop[0] - loop[-1]) <= tolerance * 3:
            loop = loop[:-1]
        if len(loop) < 3:
            continue
        maximum_segments = 256
        if len(loop) > maximum_segments:
            loop = loop[np.linspace(0, len(loop) - 1, maximum_segments, dtype=int)]
        for index, start in enumerate(loop):
            end = loop[(index + 1) % len(loop)]
            if np.linalg.norm(end - start) <= max(tolerance, 1e-12):
                continue
            maker = BRepBuilderAPI_MakeEdge(_point(start), _point(end))
            if maker.IsDone():
                points = np.vstack((start, end))
                edges.append(
                    _TrimEdge(
                        _set_edge_tolerance(maker.Edge(), tolerance * 5),
                        points,
                    )
                )
    return edges


def _fill_surface_from_nodes(
    boundary_edges: list[_TrimEdge],
    nodes: np.ndarray,
    tolerance: float,
    *,
    minimum_area: float | None = None,
) -> TopoDS_Face | None:
    if not boundary_edges:
        return None
    try:
        filling = BRepFill_Filling(
            3,
            15,
            3,
            False,
            max(tolerance * 0.1, 1e-10),
            tolerance,
            0.01,
            max(tolerance * 10, 1e-8),
            8,
            12,
        )
        for edge in boundary_edges:
            filling.Add(edge.edge, GeomAbs_C0, True)
        for node in nodes:
            filling.Add(_point(node))
        filling.Build()
        if not filling.IsDone():
            return None
        face = filling.Face()
    except Exception:
        return None
    required_area = tolerance**2 if minimum_area is None else minimum_area
    return face if _valid_face_with_minimum_area(face, required_area) else None


def _point_fitted_face(
    patch: SurfacePatch,
    edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Fit one C1 B-spline/plate face to patch nodes and shared boundaries."""

    loops = [
        np.asarray(loop, dtype=float)
        for loop in getattr(patch, "boundary_loops_3d", patch.boundary_loops)
    ]
    length_groups = [
        np.linalg.norm(np.diff(loop, axis=0), axis=1) for loop in loops if len(loop) > 1
    ]
    segment_lengths = np.concatenate(length_groups) if length_groups else np.empty(0, dtype=float)
    segment_lengths = segment_lengths[segment_lengths > 1e-12]
    vertex_points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
    local_diagonal = float(np.linalg.norm(np.ptp(vertex_points, axis=0)))
    fit_tolerance = min(
        tolerance,
        max(
            local_diagonal * 1e-6,
            float(np.min(segment_lengths)) * 0.1 if len(segment_lengths) else 0.0,
            2e-7,
        ),
    )
    interior_nodes = _interior_patch_nodes(patch, mesh)
    node_sets: list[np.ndarray] = []
    seen_counts: set[int] = set()
    # OCCT plate filling solves a dense constraint system whose cost rises
    # steeply with point count. Even 16 interior constraints made ordinary
    # loft patches spend tens of seconds in Build(); canonical trim curves
    # carry the dominant shape information and four interior anchors prevent
    # the minimum-energy fill from bowing away from the node field.
    maxima = (0,) if len(patch.vertex_indices) > 500 else (4, 0)
    for maximum in maxima:
        nodes = (
            _sample_surface_nodes(interior_nodes, maximum)
            if maximum
            else np.empty((0, 3), dtype=float)
        )
        if len(nodes) not in seen_counts:
            node_sets.append(nodes)
            seen_counts.add(len(nodes))
    smooth_boundary_edges: list[_TrimEdge] = []
    for loop in loops:
        edge = _fallback_curve_edge(loop, fit_tolerance)
        if edge is not None:
            smooth_boundary_edges.append(edge)
    candidates = [
        list(edges),
        smooth_boundary_edges,
        _polyline_boundary_edges(patch, fit_tolerance),
    ]
    fitted_face: TopoDS_Face | None = None
    for candidate in candidates:
        if not candidate:
            continue
        for nodes in node_sets:
            fitted_face = _fill_surface_from_nodes(candidate, nodes, fit_tolerance)
            if fitted_face is not None:
                area_ratio = abs(float(cq.Face(fitted_face).Area())) / max(patch.area, 1e-15)
                if 0.25 <= area_ratio <= 2.0:
                    break
                fitted_face = None
        if fitted_face is not None:
            break
    if fitted_face is None:
        return None

    face = _orient_face(fitted_face, patch, mesh)
    if _usable_face(face, fit_tolerance):
        return face
    # Orientation is not allowed to turn an otherwise usable node-fitted patch
    # into invalid topology. Keep the valid parameterization during recognition;
    # consistent outward orientation can be resolved after boundary joining.
    face.Reverse()
    return face if _usable_face(face, fit_tolerance) else None


def _graph_bspline_face_impl(
    patch: FreeformPatch,
    edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
    *,
    shared_topology: bool = True,
) -> TopoDS_Face | None:
    """Fit a bounded C2 surface when a residual is a graph over one plane.

    Many CAD transition sheets are too large for OCCT's dense plate solver but
    are still single-valued in a stable principal frame. Verify that every
    projected triangle has one orientation, fit only the scalar height field,
    and trim the resulting tensor-product B-spline in its known UV frame. This
    bounded path avoids both unconstrained plate bowing and per-triangle output.
    """

    vertex_indices = np.asarray(patch.vertex_indices, dtype=np.int64)
    if len(vertex_indices) < 8 or len(vertex_indices) > 1500:
        return None
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    center = np.mean(points, axis=0)
    try:
        _, _, basis = np.linalg.svd(points - center, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    coordinates = (points - center) @ basis[:2].T
    heights = (points - center) @ basis[2]
    constrained_heights = heights.copy()
    constraint_tree = cKDTree(points)
    analytic_constraint_points: list[np.ndarray] = []
    for edge in edges:
        if edge.source_points is None or len(edge.source_points) != len(edge.points):
            continue
        distances, indices = constraint_tree.query(edge.source_points, k=1)
        for distance, vertex_index, exact_point in zip(
            np.atleast_1d(distances),
            np.atleast_1d(indices),
            edge.points,
            strict=True,
        ):
            if float(distance) <= tolerance * 2:
                constrained_heights[int(vertex_index)] = float(
                    (np.asarray(exact_point, dtype=float) - center) @ basis[2]
                )
        try:
            midpoint = cq.Edge(edge.edge).positionAt(0.5)
            analytic_constraint_points.append(
                np.asarray(midpoint.toTuple(), dtype=float)
            )
        except Exception:
            pass
    fit_coordinates = coordinates
    fit_heights = constrained_heights
    if analytic_constraint_points:
        constraint_points = np.asarray(analytic_constraint_points, dtype=float)
        fit_coordinates = np.vstack(
            (fit_coordinates, (constraint_points - center) @ basis[:2].T)
        )
        fit_heights = np.r_[
            fit_heights,
            (constraint_points - center) @ basis[2],
        ]
        validation_points = np.vstack((points, constraint_points))
    else:
        validation_points = points
    spans = np.ptp(coordinates, axis=0)
    if np.min(spans) <= max(tolerance, 1e-10):
        return None

    local_index = np.full(len(mesh.vertices), -1, dtype=np.int64)
    local_index[vertex_indices] = np.arange(len(vertex_indices), dtype=np.int64)
    triangles = local_index[np.asarray(mesh.faces[patch.face_indices], dtype=np.int64)]
    if np.any(triangles < 0):
        return None
    projected = coordinates[triangles]
    signed_area = (
        (projected[:, 1, 0] - projected[:, 0, 0])
        * (projected[:, 2, 1] - projected[:, 0, 1])
        - (projected[:, 1, 1] - projected[:, 0, 1])
        * (projected[:, 2, 0] - projected[:, 0, 0])
    )
    area_floor = max(float(np.prod(spans)) * 1e-14, 1e-14)
    if not (np.all(signed_area > area_floor) or np.all(signed_area < -area_floor)):
        return None

    lower = np.min(coordinates, axis=0)
    upper = np.max(coordinates, axis=0)
    try:
        height_field = RBFInterpolator(
            fit_coordinates,
            fit_heights,
            kernel="thin_plate_spline",
            smoothing=max((tolerance * 1e-4) ** 2, 1e-14),
        )
        surface_candidates: list[tuple[float, float, Geom_Surface]] = []
        for u_count, minimum_v_count in ((36, 14), (52, 22)):
            v_count = max(
                minimum_v_count,
                int(round(u_count * spans[1] / spans[0])),
            )
            u_values = np.linspace(lower[0], upper[0], u_count)
            v_values = np.linspace(lower[1], upper[1], v_count)
            grid_u, grid_v = np.meshgrid(u_values, v_values, indexing="ij")
            grid_parameters = np.column_stack((grid_u.ravel(), grid_v.ravel()))
            grid_heights = height_field(grid_parameters)
            grid_points = (
                center
                + grid_parameters[:, :1] * basis[0]
                + grid_parameters[:, 1:] * basis[1]
                + grid_heights[:, None] * basis[2]
            )
            array = TColgp_Array2OfPnt(1, u_count, 1, v_count)
            for u_index in range(u_count):
                for v_index in range(v_count):
                    array.SetValue(
                        u_index + 1,
                        v_index + 1,
                        _point(grid_points[u_index * v_count + v_index]),
                    )
            fitted = GeomAPI_PointsToBSplineSurface(
                array,
                3,
                8,
                GeomAbs_C2,
                max(tolerance * 0.01, 1e-8),
            )
            if not fitted.IsDone():
                continue
            candidate = fitted.Surface()
            candidate_deviations: list[float] = []
            for point in validation_points:
                projection = GeomAPI_ProjectPointOnSurf(_point(point), candidate)
                if projection.NbPoints() == 0:
                    candidate_deviations = []
                    break
                candidate_deviations.append(float(projection.LowerDistance()))
            if not candidate_deviations:
                continue
            percentile = float(np.percentile(candidate_deviations, 95))
            maximum = max(candidate_deviations)
            surface_candidates.append((percentile, maximum, candidate))
        if not surface_candidates:
            return None
        percentile, maximum, surface = min(
            surface_candidates,
            key=lambda item: (item[0], item[1]),
        )
    except Exception:
        return None
    if percentile > tolerance or maximum > tolerance * 3:
        return None

    wires: list[TopoDS_Wire] = []
    try:
        support = BRepBuilderAPI_MakeFace(surface, tolerance).Face()
        remaining_edges = list(edges) if shared_topology else []
        for raw_loop in patch.boundary_loops:
            loop = np.asarray(raw_loop, dtype=float)
            if len(loop) < 4 or np.linalg.norm(loop[0] - loop[-1]) > tolerance * 3:
                return None
            loop = loop[:-1]
            parameters: list[tuple[float, float]] = []
            for boundary_point in loop:
                projection = GeomAPI_ProjectPointOnSurf(
                    _point(boundary_point),
                    surface,
                )
                if not projection.IsDone() or projection.NbPoints() == 0:
                    return None
                u_value, v_value = projection.LowerDistanceParameters()
                parameters.append((float(u_value), float(v_value)))
            parameter_array = np.asarray(parameters, dtype=float)
            parameter_vertices: list[TopoDS_Vertex] = []
            vertex_builder = BRep_Builder()
            for u_value, v_value in parameter_array:
                vertex = BRepBuilderAPI_MakeVertex(
                    surface.Value(float(u_value), float(v_value))
                ).Vertex()
                vertex_builder.UpdateVertex(vertex, tolerance)
                parameter_vertices.append(vertex)
            ordered_edges: list[TopoDS_Edge] = []
            pcurve_builder = BRep_Builder()
            for index, start in enumerate(parameter_array):
                end = parameter_array[(index + 1) % len(parameter_array)]
                if np.linalg.norm(end - start) <= 1e-12:
                    return None
                source_start = loop[index]
                source_end = loop[(index + 1) % len(loop)]
                best: tuple[float, int, bool] | None = None
                for edge_index, candidate in enumerate(remaining_edges):
                    if len(candidate.points) < 2:
                        continue
                    candidate_first = TopExp.FirstVertex_s(candidate.edge, True)
                    candidate_last = TopExp.LastVertex_s(candidate.edge, True)
                    if candidate_first.IsNull() or candidate_last.IsNull():
                        continue
                    candidate_start = np.asarray(
                        cq.Vertex(candidate_first).toTuple(),
                        dtype=float,
                    )
                    candidate_end = np.asarray(
                        cq.Vertex(candidate_last).toTuple(),
                        dtype=float,
                    )
                    direct = max(
                        float(np.linalg.norm(source_start - candidate_start)),
                        float(np.linalg.norm(source_end - candidate_end)),
                    )
                    reverse = max(
                        float(np.linalg.norm(source_start - candidate_end)),
                        float(np.linalg.norm(source_end - candidate_start)),
                    )
                    score = min(direct, reverse)
                    if best is None or score < best[0]:
                        best = (score, edge_index, reverse < direct)
                edge: TopoDS_Edge | None = None
                if best is not None and best[0] <= tolerance * 2:
                    _, edge_index, reverse = best
                    candidate = remaining_edges.pop(edge_index)
                    try:
                        adaptor = BRepAdaptor_Curve(candidate.edge)
                        pcurve = GeomProjLib.Curve2d_s(
                            adaptor.Curve().Curve(),
                            float(adaptor.FirstParameter()),
                            float(adaptor.LastParameter()),
                            surface,
                            tolerance,
                        )
                        pcurve_builder.UpdateEdge(
                            candidate.edge,
                            pcurve,
                            surface,
                            TopLoc_Location(),
                            tolerance,
                        )
                        edge = (
                            _reverse_edge(candidate.edge)
                            if reverse
                            else candidate.edge
                        )
                    except Exception:
                        edge = None
                if edge is None:
                    segment = GCE2d_MakeSegment(
                        gp_Pnt2d(float(start[0]), float(start[1])),
                        gp_Pnt2d(float(end[0]), float(end[1])),
                    )
                    edge_maker = BRepBuilderAPI_MakeEdge(
                        segment.Value(),
                        surface,
                        parameter_vertices[index],
                        parameter_vertices[(index + 1) % len(parameter_vertices)],
                    )
                    if not edge_maker.IsDone():
                        return None
                    edge = edge_maker.Edge()
                ordered_edges.append(edge)
            wire = TopoDS_Wire()
            wire_builder = BRep_Builder()
            wire_builder.MakeWire(wire)
            for edge in ordered_edges:
                wire_builder.Add(wire, edge)
            # A wire can report Closed() while its constituent edge
            # orientations are inconsistent. Normalize every canonical wire,
            # not only geometrically open ones, before it trims the surface.
            wire_fixer = ShapeFix_Wire(wire, support, tolerance)
            wire_fixer.SetPrecision(tolerance)
            wire_fixer.SetMaxTolerance(tolerance * 10)
            wire_fixer.FixReorder()
            wire_fixer.FixConnected(tolerance * 10)
            wire_fixer.FixClosed(tolerance * 10)
            wire = wire_fixer.Wire()
            if not wire.Closed():
                return None
            wires.append(wire)
        if not wires:
            return None
        face_maker = BRepBuilderAPI_MakeFace(surface, wires[0], True)
        for wire in wires[1:]:
            face_maker.Add(wire)
        if not face_maker.IsDone():
            return None
        face = face_maker.Face()
        BRepLib.BuildCurves3d_s(face, tolerance, GeomAbs_C2, 12, 30)
        if not _usable_face(face, tolerance):
            fixer = ShapeFix_Face(face)
            fixer.SetPrecision(tolerance)
            fixer.SetMaxTolerance(tolerance * 10)
            fixer.FixOrientation()
            fixer.FixWireTool().FixSelfIntersection()
            fixer.Perform()
            shape_fixer = ShapeFix_Shape(fixer.Face())
            shape_fixer.SetPrecision(tolerance)
            shape_fixer.SetMaxTolerance(tolerance * 10)
            shape_fixer.Perform()
            repaired = shape_fixer.Shape()
            if repaired.ShapeType() != TopAbs_FACE:
                return None
            face = TopoDS.Face_s(repaired)
    except Exception:
        return None
    if not _usable_face(face, tolerance):
        return None
    area_ratio = abs(float(cq.Face(face).Area())) / max(patch.area, tolerance**2)
    if not 0.75 <= area_ratio <= 1.25:
        return None
    return _orient_face(face, patch, mesh)


def _graph_bspline_face(
    patch: FreeformPatch,
    edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Prefer shared topology, then retain the validated independent trim."""

    fitted = _graph_bspline_face_impl(patch, edges, mesh, tolerance)
    if fitted is not None or not edges:
        return fitted
    return _graph_bspline_face_impl(
        patch,
        edges,
        mesh,
        tolerance,
        shared_topology=False,
    )


def _faceted_residual_faces(
    patch: SurfacePatch,
    trim_edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
) -> list[TopoDS_Face]:
    """Rebuild a failed region from its mesh topology with conforming borders.

    Interior source triangles stay planar. At an analytic interface, source
    boundary vertices move to the exact on-support trim chain and each touching
    triangle is filled as a C0 curved triangular patch. This is the same
    split-then-snap rule used by mesh repair software, expressed as valid B-rep
    topology. The operation is atomic: a partial facet set is never returned.
    """

    face_indices = np.asarray(patch.face_indices, dtype=np.int64)
    triangles = np.asarray(mesh.faces[face_indices], dtype=np.int64)
    if len(triangles) == 0:
        return []
    patch_vertex_ids = np.unique(triangles)
    source_vertices = np.asarray(mesh.vertices, dtype=float)
    local_tree = cKDTree(source_vertices[patch_vertex_ids])
    topology_tolerance = max(tolerance * 0.01, 1e-9)

    conforming_edges: dict[tuple[int, int], tuple[TopoDS_Edge, tuple[int, int]]] = {}
    canonical_vertices: dict[int, tuple[TopoDS_Vertex, np.ndarray]] = {}
    for trim in trim_edges:
        if trim.source_points is None or len(trim.source_points) != 2:
            continue
        distances, local_indices = local_tree.query(trim.source_points, k=1)
        if np.max(distances) > tolerance * 2:
            continue
        first_id, second_id = (
            int(patch_vertex_ids[int(local_indices[0])]),
            int(patch_vertex_ids[int(local_indices[1])]),
        )
        if first_id == second_id:
            continue
        key = tuple(sorted((first_id, second_id)))
        first_vertex = TopExp.FirstVertex_s(trim.edge, True)
        second_vertex = TopExp.LastVertex_s(trim.edge, True)
        if first_vertex.IsNull() or second_vertex.IsNull():
            continue
        conforming_edges[key] = (trim.edge, (first_id, second_id))
        for vertex_id, vertex, point in (
            (first_id, first_vertex, trim.points[0]),
            (second_id, second_vertex, trim.points[-1]),
        ):
            existing = canonical_vertices.get(vertex_id)
            if existing is not None and np.linalg.norm(existing[1] - point) > tolerance * 10:
                return []
            canonical_vertices.setdefault(
                vertex_id,
                (vertex, np.asarray(point, dtype=float)),
            )

    builder = BRep_Builder()
    vertex_shapes: dict[int, TopoDS_Vertex] = {}
    for vertex_id in patch_vertex_ids:
        identifier = int(vertex_id)
        canonical = canonical_vertices.get(identifier)
        if canonical is not None:
            vertex = canonical[0]
        else:
            point = np.asarray(source_vertices[identifier], dtype=float)
            vertex = BRepBuilderAPI_MakeVertex(_point(point)).Vertex()
            builder.UpdateVertex(vertex, topology_tolerance)
        vertex_shapes[identifier] = vertex

    edge_shapes: dict[tuple[int, int], tuple[TopoDS_Edge, tuple[int, int]]] = dict(conforming_edges)

    def oriented_edge(first_id: int, second_id: int) -> TopoDS_Edge | None:
        key = tuple(sorted((first_id, second_id)))
        stored = edge_shapes.get(key)
        if stored is None:
            low, high = key
            maker = BRepBuilderAPI_MakeEdge(vertex_shapes[low], vertex_shapes[high])
            if not maker.IsDone():
                return None
            stored = (
                _set_edge_tolerance(maker.Edge(), topology_tolerance),
                (low, high),
            )
            edge_shapes[key] = stored
        edge, direction = stored
        return edge if direction == (first_id, second_id) else _reverse_edge(edge)

    result: list[TopoDS_Face] = []
    for triangle in triangles:
        identifiers = tuple(int(value) for value in triangle)
        ordered: list[TopoDS_Edge] = []
        has_curved_boundary = False
        for first_id, second_id in (
            (identifiers[0], identifiers[1]),
            (identifiers[1], identifiers[2]),
            (identifiers[2], identifiers[0]),
        ):
            edge = oriented_edge(first_id, second_id)
            if edge is None:
                return []
            ordered.append(edge)
            has_curved_boundary |= tuple(sorted((first_id, second_id))) in conforming_edges

        face: TopoDS_Face | None = None
        if has_curved_boundary:
            try:
                filling = BRepFill_Filling(
                    3,
                    8,
                    2,
                    False,
                    max(tolerance * 0.1, 1e-10),
                    tolerance,
                    0.01,
                    max(tolerance * 10, 1e-8),
                    6,
                    6,
                )
                for edge in ordered:
                    filling.Add(edge, GeomAbs_C0, True)
                filling.Build()
                if filling.IsDone():
                    face = filling.Face()
            except Exception:
                face = None
        else:
            try:
                wire_maker = BRepBuilderAPI_MakeWire()
                for edge in ordered:
                    wire_maker.Add(edge)
                if wire_maker.IsDone() and wire_maker.Wire().Closed():
                    face_maker = BRepBuilderAPI_MakeFace(wire_maker.Wire())
                    if face_maker.IsDone():
                        face = face_maker.Face()
            except Exception:
                face = None
        if face is None or not _usable_face(face, tolerance * 0.01):
            return []

        expected = np.cross(
            source_vertices[identifiers[1]] - source_vertices[identifiers[0]],
            source_vertices[identifiers[2]] - source_vertices[identifiers[0]],
        )
        try:
            normal = np.asarray(cq.Face(face).normalAt().toTuple(), dtype=float)
            if float(normal @ expected) < 0:
                face.Reverse()
        except Exception:
            return []
        result.append(face)

    rebuilt_area = sum(float(cq.Face(face).Area()) for face in result)
    area_ratio = rebuilt_area / max(float(patch.area), tolerance**2)
    if len(result) != len(triangles) or not 0.5 <= area_ratio <= 1.5:
        return []
    return result


def _linear_extrusion_face(
    patch: LinearExtrusionPatch,
    edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
) -> TopoDS_Face | None:
    """Build a bounded general-profile extrusion from its end-profile nodes."""

    profile = np.asarray(patch.profile_points, dtype=float)
    if len(profile) < 4:
        return None
    maximum_points = 128
    if len(profile) > maximum_points:
        profile = profile[np.linspace(0, len(profile) - 1, maximum_points, dtype=int)]
    closed = bool(np.linalg.norm(profile[0] - profile[-1]) <= tolerance * 3)
    if closed and np.linalg.norm(profile[0] - profile[-1]) > 1e-12:
        profile = np.vstack((profile, profile[0]))
    array = TColgp_Array1OfPnt(1, len(profile))
    for index, point in enumerate(profile, start=1):
        array.SetValue(index, _point(point))
    curve_scale = float(np.linalg.norm(np.ptp(profile, axis=0)))
    # Use the same approximation contract as the canonical adjacency edges.
    # Otherwise the face's isoparametric profile and its shared trim edge are
    # two slightly different B-splines fitted to the same STL node chain.
    curve_tolerance = max(tolerance * 0.5, curve_scale * 1e-5, 1e-8)
    try:
        fit = GeomAPI_PointsToBSpline(
            array,
            3,
            8,
            GeomAbs_C2,
            curve_tolerance,
        )
        if not fit.IsDone():
            return None
        curve = fit.Curve()
        surface = Geom_SurfaceOfLinearExtrusion(
            curve,
            _direction(patch.direction),
        )
        bounded = BRepBuilderAPI_MakeFace(
            surface,
            float(curve.FirstParameter()),
            float(curve.LastParameter()),
            0.0,
            float(patch.end - patch.start),
            tolerance,
        )
        if not bounded.IsDone():
            return None
        support = bounded.Face()

        # The adjacency graph owns the canonical edge geometry. Attach a
        # parameter-space representation of those same TShapes to this swept
        # support, then use them for the face wire. This keeps neighboring
        # planes and extrusions topologically identical instead of hoping that
        # independently fitted boundary curves fall within sewing tolerance.
        edge_fixer = ShapeFix_Edge()
        for trim_edge in edges:
            try:
                edge_fixer.FixAddPCurve(
                    trim_edge.edge,
                    support,
                    False,
                    tolerance,
                )
            except Exception:
                pass
        candidates: list[TopoDS_Face] = []
        for wire, _ in _wire_groups(edges, tolerance):
            maker = BRepBuilderAPI_MakeFace(surface, wire, True)
            if not maker.IsDone():
                continue
            fixer = ShapeFix_Face(maker.Face())
            fixer.SetPrecision(tolerance)
            fixer.SetMaxTolerance(tolerance * 5)
            fixer.FixOrientation()
            fixer.Perform()
            candidate = fixer.Face()
            if _usable_face(candidate, tolerance):
                candidates.append(candidate)
        if candidates:
            face = min(
                candidates,
                key=lambda item: abs(float(cq.Face(item).Area()) - patch.area),
            )
        else:
            face = support
        face = _orient_face(face, patch, mesh)
    except Exception:
        return None
    if not _usable_face(face, tolerance):
        return None
    area_ratio = float(cq.Face(face).Area()) / max(patch.area, 1e-15)
    return face if 0.5 <= area_ratio <= 1.5 else None


def _surface_of_revolution_face(
    patch: SurfaceOfRevolutionPatch,
    edges: list[_TrimEdge],
    mesh: object,
    tolerance: float,
    use_angular_bounds: bool = False,
) -> TopoDS_Face | None:
    """Build a general-profile revolved face using canonical trim edges."""

    profile = np.asarray(patch.profile_points, dtype=float)
    if len(profile) < 4:
        return None
    # A profile inferred from tessellated nodes can contain millimetre-scale
    # backtracking even when the underlying CAD generatrix is simple. Revolving
    # that self-intersecting polyline makes OCCT reject the surface. Simplify
    # only in the natural meridian plane, within the reconstruction tolerance,
    # and retain the first topology-preserving simple path.
    relative_profile = profile - patch.origin
    axial_profile = relative_profile @ patch.axis
    radial_profile = relative_profile @ patch.radial_direction
    meridian = LineString(np.column_stack((axial_profile, radial_profile)))
    if not meridian.is_simple:
        for factor in (0.5, 0.75, 1.0):
            simplified = meridian.simplify(
                max(tolerance * factor, 1e-9),
                preserve_topology=True,
            )
            coordinates = np.asarray(simplified.coords, dtype=float)
            if simplified.is_simple and len(coordinates) >= 4:
                profile = (
                    patch.origin
                    + coordinates[:, :1] * patch.axis
                    + coordinates[:, 1:] * patch.radial_direction
                )
                break
    endpoint_relative = profile[[0, -1]] - patch.origin
    endpoint_axial = endpoint_relative @ patch.axis
    endpoint_radial = np.linalg.norm(
        endpoint_relative - np.outer(endpoint_axial, patch.axis),
        axis=1,
    )
    # Canonicalize the meridian parameter direction. STEP readers are less
    # tolerant than in-memory OCCT of a periodic face whose meridian and wire
    # orientations disagree; ordering from larger to smaller terminal radius
    # removes that representation ambiguity without changing the geometry.
    if endpoint_radial[0] < endpoint_radial[1]:
        profile = profile[::-1]
    maximum_points = 96
    if len(profile) > maximum_points:
        profile = profile[np.linspace(0, len(profile) - 1, maximum_points, dtype=int)]
    array = TColgp_Array1OfPnt(1, len(profile))
    for index, point in enumerate(profile, start=1):
        array.SetValue(index, _point(point))
    curve_scale = float(np.linalg.norm(np.ptp(profile, axis=0)))
    curve_tolerance = max(tolerance * 0.5, curve_scale * 1e-5, 1e-8)
    try:
        fit = GeomAPI_PointsToBSpline(
            array,
            3,
            3,
            GeomAbs_C2,
            curve_tolerance,
        )
        if not fit.IsDone():
            return None
        curve = fit.Curve()
        surface = Geom_SurfaceOfRevolution(
            curve,
            gp_Ax1(_point(patch.origin), _direction(patch.axis)),
        )
        patch_points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
        relative = patch_points - patch.origin
        second_direction = np.cross(patch.axis, patch.radial_direction)
        angles = np.mod(
            np.arctan2(
                relative @ second_direction,
                relative @ patch.radial_direction,
            ),
            2 * math.pi,
        )
        ordered_angles = np.sort(angles)
        gaps = np.diff(np.r_[ordered_angles, ordered_angles[0] + 2 * math.pi])
        gap_index = int(np.argmax(gaps))
        u_min = float(ordered_angles[(gap_index + 1) % len(ordered_angles)])
        unwrapped_angles = np.mod(angles - u_min, 2 * math.pi) + u_min
        u_max = float(np.max(unwrapped_angles))
        full_angle = _angular_coverage(
            patch_points,
            patch.origin,
            patch.axis,
        ) >= math.radians(330)
        if full_angle or not use_angular_bounds:
            u_min, u_max = 0.0, 2 * math.pi
        bounded = BRepBuilderAPI_MakeFace(
            surface,
            u_min,
            u_max,
            float(curve.FirstParameter()),
            float(curve.LastParameter()),
            tolerance,
        )
        if not bounded.IsDone():
            return None
        support = bounded.Face()
        edge_fixer = ShapeFix_Edge()
        for trim_edge in edges:
            try:
                edge_fixer.FixAddPCurve(
                    trim_edge.edge,
                    support,
                    False,
                    tolerance,
                )
            except Exception:
                pass
        if full_angle:
            # A complete revolution already has the correct meridian bounds.
            # Re-trimming it by its lone outer circle can select only the
            # annular side and discard a legitimate degenerate pole/disk.
            face = support
            closed_trim_edges = [edge for edge in edges if edge.closed]
            support_edges = [edge for edge in cq.Face(support).Edges() if len(edge.Vertices()) <= 1]
            if closed_trim_edges and support_edges:
                reshaper = BRepTools_ReShape()
                unused = list(closed_trim_edges)
                for support_edge in support_edges:
                    match = min(
                        unused,
                        key=lambda item: abs(
                            float(cq.Edge(item.edge).Length()) - support_edge.Length()
                        ),
                        default=None,
                    )
                    if match is None:
                        continue
                    length_error = abs(float(cq.Edge(match.edge).Length()) - support_edge.Length())
                    if length_error > max(tolerance * 20, support_edge.Length() * 0.01):
                        continue
                    reshaper.Replace(support_edge.wrapped, match.edge)
                    unused.remove(match)
                replaced = reshaper.Apply(face)
                if replaced.ShapeType() == TopAbs_FACE:
                    candidate = TopoDS.Face_s(replaced)
                    if _usable_face(candidate, tolerance):
                        face = candidate
        else:
            candidates: list[TopoDS_Face] = []
            revolution_groups = _wire_groups(edges, tolerance)
            for wire, _ in revolution_groups:
                maker = BRepBuilderAPI_MakeFace(surface, wire, True)
                if not maker.IsDone():
                    continue
                fixer = ShapeFix_Face(maker.Face())
                fixer.SetPrecision(tolerance)
                fixer.SetMaxTolerance(tolerance * 5)
                fixer.FixOrientation()
                fixer.Perform()
                candidate = fixer.Face()
                if _usable_face(candidate, tolerance):
                    candidates.append(candidate)
            face = (
                min(
                    candidates,
                    key=lambda item: abs(float(cq.Face(item).Area()) - patch.area),
                )
                if candidates
                else support
            )
        face = _orient_face(face, patch, mesh)
        try:
            converted = BRepBuilderAPI_NurbsConvert(face, False)
            if converted.IsDone() and converted.Shape().ShapeType() == TopAbs_FACE:
                face = TopoDS.Face_s(converted.Shape())
        except Exception:
            # Conversion is an export optimization, not a validity
            # requirement. Certain tightly spaced trim knots are valid on the
            # original swept support but cannot be reparameterized as one
            # NURBS face; retain the valid original in that case.
            pass
    except Exception:
        return None
    if not _usable_face(face, tolerance):
        return None
    area_ratio = float(cq.Face(face).Area()) / max(patch.area, 1e-15)
    return face if 0.5 <= area_ratio <= 1.5 else None


def _extract_shells(shape: object) -> list[object]:
    if shape.ShapeType() == TopAbs_SHELL:
        return [TopoDS.Shell_s(shape)]
    if shape.ShapeType() == TopAbs_FACE:
        shell = TopoDS_Shell()
        builder = BRep_Builder()
        builder.MakeShell(shell)
        builder.Add(shell, TopoDS.Face_s(shape))
        shell.Closed(True)
        return [shell]
    explorer = TopExp_Explorer(shape, TopAbs_SHELL)
    shells: list[object] = []
    while explorer.More():
        shells.append(TopoDS.Shell_s(explorer.Current()))
        explorer.Next()
    return shells


def _face_with_reversed_edge(
    face: TopoDS_Face,
    target: TopoDS_Edge,
) -> TopoDS_Face | None:
    outer = BRepTools.OuterWire_s(face)
    wires: list[TopoDS_Wire] = []
    explorer = TopExp_Explorer(face, TopAbs_WIRE)
    while explorer.More():
        wires.append(TopoDS.Wire_s(explorer.Current()))
        explorer.Next()
    wires.sort(key=lambda wire: 0 if wire.IsSame(outer) else 1)
    rebuilt_wires: list[TopoDS_Wire] = []
    changed = False
    for wire in wires:
        wire_maker = BRepBuilderAPI_MakeWire()
        ordered = BRepTools_WireExplorer(wire, face)
        while ordered.More():
            edge = TopoDS.Edge_s(ordered.Current())
            if edge.IsSame(target):
                edge = TopoDS.Edge_s(edge.Reversed())
                changed = True
            wire_maker.Add(edge)
            ordered.Next()
        if not wire_maker.IsDone():
            return None
        rebuilt_wires.append(wire_maker.Wire())
    if not changed or not rebuilt_wires:
        return None
    maker = BRepBuilderAPI_MakeFace(
        BRep_Tool.Surface_s(face),
        rebuilt_wires[0],
        True,
    )
    for wire in rebuilt_wires[1:]:
        maker.Add(wire)
    if not maker.IsDone():
        return None
    rebuilt = maker.Face()
    rebuilt.Orientation(face.Orientation())
    if not BRepCheck_Analyzer(rebuilt).IsValid():
        return None
    original_area = max(float(cq.Face(face).Area()), 1e-15)
    area_ratio = float(cq.Face(rebuilt).Area()) / original_area
    return rebuilt if 0.95 <= area_ratio <= 1.05 else None


def _repair_closed_shell_orientation(
    shell: object,
    tolerance: float,
) -> object:
    """Repair isolated same-direction edge uses in an otherwise closed shell."""

    current = TopoDS.Shell_s(shell)
    for _ in range(4):
        analysis = ShapeAnalysis_Shell()
        analysis.LoadShells(current)
        analysis.CheckOrientedShells(current, False, False)
        if not analysis.HasBadEdges():
            return current
        bad_edges = cq.Shape(analysis.BadEdges()).Edges()
        if not bad_edges:
            return current
        bad_edge = TopoDS.Edge_s(bad_edges[0].wrapped)
        faces: list[TopoDS_Face] = []
        owners: list[int] = []
        explorer = TopExp_Explorer(current, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            face_index = len(faces)
            faces.append(face)
            edge_explorer = TopExp_Explorer(face, TopAbs_EDGE)
            while edge_explorer.More():
                if edge_explorer.Current().IsSame(bad_edge):
                    owners.append(face_index)
                    break
                edge_explorer.Next()
            explorer.Next()
        if len(owners) != 2:
            return current
        # Prefer rebuilding the richer periodic/fillet face rather than a
        # simple cap. This preserves the cap's reliably oriented outer wire.
        owner = max(owners, key=lambda index: len(cq.Face(faces[index]).Edges()))
        rebuilt = _face_with_reversed_edge(faces[owner], bad_edge)
        if rebuilt is None:
            return current
        faces[owner] = rebuilt
        sewing = BRepBuilderAPI_Sewing(tolerance, True, True, True, False)
        for face in faces:
            sewing.Add(face)
        sewing.Perform()
        shells = _extract_shells(sewing.SewedShape())
        if len(shells) != 1 or sewing.NbFreeEdges() != 0:
            return current
        current = TopoDS.Shell_s(shells[0])
    return current


def _solid_from_shell_once(shell: object, tolerance: float) -> object | None:
    shell_fixer = ShapeFix_Shell(TopoDS.Shell_s(shell))
    shell_fixer.SetPrecision(tolerance)
    shell_fixer.SetMaxTolerance(tolerance * 3)
    shell_fixer.Perform()
    try:
        shell_fixer.FixFaceOrientation(shell_fixer.Shell(), True, False)
    except Exception:
        pass
    fixed_shell = shell_fixer.Shell()
    maker = BRepBuilderAPI_MakeSolid(fixed_shell)
    if not maker.IsDone():
        return None
    solid_fixer = ShapeFix_Solid(maker.Solid())
    solid_fixer.SetPrecision(tolerance)
    solid_fixer.SetMaxTolerance(tolerance * 3)
    solid_fixer.Perform()
    solid = solid_fixer.Solid()
    if solid.ShapeType() == TopAbs_SOLID and BRepCheck_Analyzer(solid).IsValid():
        return solid
    try:
        shape_fixer = ShapeFix_Shape(solid)
        shape_fixer.SetPrecision(tolerance)
        shape_fixer.SetMaxTolerance(tolerance * 3)
        shape_fixer.Perform()
        repaired = shape_fixer.Shape()
        if repaired.ShapeType() == TopAbs_SOLID and BRepCheck_Analyzer(repaired).IsValid():
            return TopoDS.Solid_s(repaired)
    except Exception:
        pass
    return None


def _solid_from_shell(shell: object, tolerance: float) -> object | None:
    # ShapeFix_Solid can normalize shared face/edge TShapes while the solid
    # created from their pre-repair topology remains invalid. Let the first
    # pass fully leave scope, then rebuild once from the now-normalized source
    # shell. This occurs in dense but edge-closed fillet networks.
    repaired_shell = _repair_closed_shell_orientation(shell, tolerance)
    direct = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(repaired_shell))
    if direct.IsDone():
        direct_solid = direct.Solid()
        if direct_solid.ShapeType() == TopAbs_SOLID and BRepCheck_Analyzer(direct_solid).IsValid():
            return direct_solid
    for _ in range(2):
        solid = _solid_from_shell_once(repaired_shell, tolerance)
        if solid is not None:
            return solid
    return None


def _sew_faces(
    faces: list[TopoDS_Face],
    tolerance: float,
    non_manifold: bool = False,
    *,
    make_solids: bool = True,
) -> tuple[BRepBuilderAPI_Sewing, list[object]]:
    sewing = BRepBuilderAPI_Sewing(
        tolerance,
        True,
        True,
        True,
        non_manifold,
    )
    # Copy the complete face complex once. Copying each face independently
    # duplicates canonical edges and vertices, undoing the shared topology that
    # was deliberately constructed above. A single compound copy is isolated
    # from sewing mutations while preserving sub-shape identity.
    source = cq.Compound.makeCompound([cq.Shape(face) for face in faces]).wrapped
    copied = BRepBuilderAPI_Copy(source).Shape()
    explorer = TopExp_Explorer(copied, TopAbs_FACE)
    while explorer.More():
        sewing.Add(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    sewing.Perform()
    shells = _extract_shells(sewing.SewedShape())
    # An open sewn shell cannot become a valid solid. Running ShapeFix_Solid on
    # every open retry is extremely expensive and, on dense trim networks, can
    # consume minutes before returning the same open topology. Reconcile or
    # fill the free boundaries first; invoke solid repair only once the sewn
    # edge graph is closed.
    solids = (
        [
            solid
            for shell in shells
            if (solid := _solid_from_shell(shell, tolerance)) is not None
        ]
        if make_solids and sewing.NbFreeEdges() == 0
        else []
    )
    # Sewing may legally merge coincident sliver faces, so face-count equality
    # is not a completeness invariant. Compare represented area instead: a
    # missing model-scale face is rejected, while a merged zero-area seam does
    # not invalidate an otherwise complete closed shell.
    if solids:
        source_area = sum(abs(float(cq.Face(face).Area())) for face in faces)
        solid_area = sum(abs(float(cq.Shape(solid).Area())) for solid in solids)
        area_error = abs(solid_area - source_area)
        if area_error > max(source_area * 1e-5, tolerance**2 * 10):
            solids = []
    return sewing, solids


def build_faceted_brep(
    data: MeshData,
    graph: SurfaceGraph | None = None,
    reason: str = "analytic reconstruction did not pass validation",
    visualization_faceted_patch_ids: list[str] | None = None,
) -> SurfaceBRepResult:
    """Build an exact manifold B-rep carrier from a watertight source mesh.

    This is deliberately different from making independent triangle faces and
    asking sewing to infer connectivity. One TopoDS vertex is created for each
    mesh node and one TopoDS edge for each undirected mesh edge. Every oriented
    triangle wire reuses those same subshapes, so the shell is manifold by
    construction and does not depend on a geometric welding tolerance.
    """

    mesh = data.mesh
    if not bool(mesh.is_watertight and mesh.is_winding_consistent):
        raise ValueError(
            "A faceted solid fallback requires a watertight, consistently "
            "oriented source mesh"
        )
    tolerance = max(data.diagonal * 1e-8, 1e-9)
    builder = BRep_Builder()
    vertices: list[TopoDS_Vertex] = []
    for raw_point in np.asarray(mesh.vertices, dtype=float):
        vertex = BRepBuilderAPI_MakeVertex(_point(raw_point)).Vertex()
        builder.UpdateVertex(vertex, tolerance)
        vertices.append(vertex)

    edge_registry: dict[
        tuple[int, int], tuple[TopoDS_Edge, tuple[int, int]]
    ] = {}
    faces: list[TopoDS_Face] = []
    for raw_triangle in np.asarray(mesh.faces, dtype=np.int64):
        triangle = tuple(int(value) for value in raw_triangle)
        ordered_edges: list[TopoDS_Edge] = []
        for first, second in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            key = tuple(sorted((first, second)))
            stored = edge_registry.get(key)
            if stored is None:
                low, high = key
                maker = BRepBuilderAPI_MakeEdge(vertices[low], vertices[high])
                if not maker.IsDone():
                    raise ValueError("Could not construct a source mesh edge")
                stored = (maker.Edge(), (low, high))
                edge_registry[key] = stored
            edge, direction = stored
            ordered_edges.append(
                edge
                if direction == (first, second)
                else TopoDS.Edge_s(edge.Reversed())
            )
        wire_maker = BRepBuilderAPI_MakeWire()
        for edge in ordered_edges:
            wire_maker.Add(edge)
        if not wire_maker.IsDone() or not wire_maker.Wire().Closed():
            raise ValueError("Could not construct a closed source triangle wire")
        face_maker = BRepBuilderAPI_MakeFace(wire_maker.Wire())
        if not face_maker.IsDone():
            raise ValueError("Could not construct a source triangle face")
        faces.append(face_maker.Face())

    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for face in faces:
        builder.Add(shell, face)
    shell.Closed(True)
    solid_maker = BRepBuilderAPI_MakeSolid(shell)
    if not solid_maker.IsDone():
        raise ValueError("Could not construct a solid from source mesh topology")
    solid = solid_maker.Solid()
    shape = cq.Shape(solid)
    if not BRepCheck_Analyzer(solid).IsValid() or not shape.isValid():
        raise ValueError("Source mesh topology produced an invalid faceted solid")

    retained_graph = graph or SurfaceGraph()
    counts = {
        "plane": len(retained_graph.planar_patches),
        "cylinder": len(retained_graph.cylindrical_patches),
        "cone": len(retained_graph.conical_patches),
        "sphere": len(retained_graph.spherical_patches),
        "torus": len(retained_graph.toroidal_patches),
        "extrusion": len(retained_graph.extrusion_patches),
        "revolution": len(retained_graph.revolution_patches),
        "freeform": len(retained_graph.freeform_patches),
    }
    return SurfaceBRepResult(
        shape=shape,
        graph=retained_graph,
        surface_counts=counts,
        face_count=len(faces),
        solid_count=1,
        free_edge_count=0,
        sewing_tolerance=tolerance,
        valid=True,
        closed=True,
        faceted_fallback=True,
        topology_vertex_count=len(vertices),
        topology_edge_count=len(edge_registry),
        warnings=[
            "Used the exact source-mesh topology as a watertight faceted "
            f"B-rep fallback because {reason}."
        ],
        faceted_patch_count=1,
        faceted_face_count=len(faces),
        faceted_patch_ids=list(visualization_faceted_patch_ids or []),
        source_mesh_fallback=True,
    )


def _free_boundary_fill_faces(
    sewing: BRepBuilderAPI_Sewing,
    tolerance: float,
) -> list[TopoDS_Face]:
    """Fill only closed free-bound loops from an otherwise valid sewn shell."""

    try:
        analysis = ShapeAnalysis_FreeBounds(
            sewing.SewedShape(),
            tolerance,
            False,
            True,
        )
        closed_wires = analysis.GetClosedWires()
    except Exception:
        return []
    source_area = 0.0
    source_explorer = TopExp_Explorer(sewing.SewedShape(), TopAbs_FACE)
    while source_explorer.More():
        source_area += float(cq.Face(TopoDS.Face_s(source_explorer.Current())).Area())
        source_explorer.Next()

    def usable_gap_face(face: TopoDS_Face) -> bool:
        # The accepted sewing tolerance is intentionally broad enough to
        # reconcile independently fitted surfaces. A real crack bounded by
        # that topology can be much smaller than tolerance**2, particularly
        # where a cone, cylinder, and plane meet. Reject only numerically empty
        # faces here; the perimeter/source-area checks below still prevent a
        # large invented cap from being accepted.
        minimum_area = max(tolerance**2 * 1e-8, 1e-15)
        return _valid_face_with_minimum_area(face, minimum_area)

    def face_from_wire(wire: TopoDS_Wire) -> TopoDS_Face | None:
        edge_explorer = TopExp_Explorer(wire, TopAbs_EDGE)
        boundary: list[_TrimEdge] = []
        while edge_explorer.More():
            edge = TopoDS.Edge_s(edge_explorer.Current())
            first = TopExp.FirstVertex_s(edge, True)
            last = TopExp.LastVertex_s(edge, True)
            points: list[np.ndarray] = []
            for vertex in (first, last):
                if vertex.IsNull():
                    continue
                point = cq.Vertex(vertex).toTuple()
                points.append(np.asarray(point, dtype=float))
            boundary.append(
                _TrimEdge(
                    edge,
                    np.asarray(points, dtype=float) if points else np.empty((0, 3), dtype=float),
                )
            )
            edge_explorer.Next()
        if not boundary or len(boundary) > 64:
            return None
        minimum_gap_area = max(tolerance**2 * 1e-8, 1e-15)
        face: TopoDS_Face | None = _fill_surface_from_nodes(
            boundary,
            np.empty((0, 3), dtype=float),
            tolerance,
            minimum_area=minimum_gap_area,
        )
        try:
            planar = BRepBuilderAPI_MakeFace(wire, True) if face is None else None
            if planar is not None and planar.IsDone() and usable_gap_face(planar.Face()):
                face = planar.Face()
        except Exception:
            pass
        if face is not None:
            perimeter = sum(float(cq.Edge(item.edge).Length()) for item in boundary)
            maximum_area = min(
                max(perimeter**2 * 2, tolerance**2 * 10),
                max(source_area, tolerance**2 * 10),
            )
            if float(cq.Face(face).Area()) > maximum_area:
                return None
        return face

    result: list[TopoDS_Face] = []
    wire_explorer = TopExp_Explorer(closed_wires, TopAbs_WIRE)
    while wire_explorer.More():
        wire = TopoDS.Wire_s(wire_explorer.Current())
        face = face_from_wire(wire)
        if face is not None:
            result.append(face)
        wire_explorer.Next()
    if result:
        return result

    # ShapeAnalysis_FreeBounds does not classify a valid two-edge loop as a
    # closed wire on every OCCT build. Sewing still exposes those edges
    # directly. Reassemble them with the same vertex-aware ordering used by
    # analytic trims, but accept only wires that are topologically closed.
    raw_edges: list[_TrimEdge] = []
    try:
        for edge_index in range(1, sewing.NbFreeEdges() + 1):
            edge = sewing.FreeEdge(edge_index)
            first = TopExp.FirstVertex_s(edge, True)
            last = TopExp.LastVertex_s(edge, True)
            if first.IsNull() or last.IsNull():
                continue
            raw_edges.append(
                _TrimEdge(
                    edge,
                    np.asarray(
                        [cq.Vertex(first).toTuple(), cq.Vertex(last).toTuple()],
                        dtype=float,
                    ),
                )
            )
    except (AttributeError, RuntimeError):
        return []
    if len(raw_edges) == 4:
        first, second = sorted(
            raw_edges,
            key=lambda item: -float(cq.Edge(item.edge).Length()),
        )[:2]
        same_direction_gap = float(
            np.linalg.norm(first.start - second.start) + np.linalg.norm(first.end - second.end)
        )
        reverse_direction_gap = float(
            np.linalg.norm(first.start - second.end) + np.linalg.norm(first.end - second.start)
        )
        ruled_second = (
            second.edge
            if same_direction_gap <= reverse_direction_gap
            else _reverse_edge(second.edge)
        )
        try:
            ruled_face = BRepFill.Face_s(first.edge, ruled_second)
        except Exception:
            ruled_face = None
        if ruled_face is not None and usable_gap_face(ruled_face):
            return [ruled_face]
    if len(raw_edges) >= 4 and len(raw_edges) % 2 == 0:
        unmatched = list(raw_edges)
        ruled_faces: list[TopoDS_Face] = []
        while unmatched:
            first = unmatched.pop(0)
            best: tuple[float, int, bool] | None = None
            for index, candidate in enumerate(unmatched):
                forward_gap = max(
                    float(np.linalg.norm(first.end - candidate.start)),
                    float(np.linalg.norm(candidate.end - first.start)),
                )
                reverse_gap = max(
                    float(np.linalg.norm(first.end - candidate.end)),
                    float(np.linalg.norm(candidate.start - first.start)),
                )
                choice = min(forward_gap, reverse_gap)
                if best is None or choice < best[0]:
                    best = (choice, index, reverse_gap < forward_gap)
            if best is None or best[0] > tolerance * 2:
                continue
            _, index, reverse = best
            second = unmatched.pop(index)
            if reverse:
                second = _TrimEdge(_reverse_edge(second.edge), second.points[::-1])
            first_start = TopExp.FirstVertex_s(first.edge, True)
            first_end = TopExp.LastVertex_s(first.edge, True)
            second_start = TopExp.FirstVertex_s(second.edge, True)
            second_end = TopExp.LastVertex_s(second.edge, True)
            try:
                reshaper = BRepTools_ReShape()
                reshaper.Replace(second_start, first_end)
                reshaper.Replace(second_end, first_start)
                second_edge = TopoDS.Edge_s(reshaper.Apply(second.edge))
                ruled_face = BRepFill.Face_s(
                    first.edge,
                    _reverse_edge(second_edge),
                )
            except Exception:
                continue
            if not usable_gap_face(ruled_face):
                continue
            ruled_faces.append(ruled_face)
        if ruled_faces:
            return ruled_faces
    if len(raw_edges) == 2:
        first, second = raw_edges
        forward_gap = max(
            float(np.linalg.norm(first.end - second.start)),
            float(np.linalg.norm(second.end - first.start)),
        )
        reverse_gap = max(
            float(np.linalg.norm(first.end - second.end)),
            float(np.linalg.norm(second.start - first.start)),
        )
        if min(forward_gap, reverse_gap) <= tolerance * 2:
            if reverse_gap < forward_gap:
                second = _TrimEdge(_reverse_edge(second.edge), second.points[::-1])
            first_start = TopExp.FirstVertex_s(first.edge, True)
            first_end = TopExp.LastVertex_s(first.edge, True)
            second_start = TopExp.FirstVertex_s(second.edge, True)
            second_end = TopExp.LastVertex_s(second.edge, True)
            try:
                reshaper = BRepTools_ReShape()
                reshaper.Replace(second_start, first_end)
                reshaper.Replace(second_end, first_start)
                second_edge = TopoDS.Edge_s(reshaper.Apply(second.edge))
                # BRepFill expects corresponding curve directions, whereas a
                # boundary wire traverses the second edge in the opposite
                # direction. Reversing it prevents a twisted ruled patch with
                # two long diagonal side edges.
                for ruled_second in (_reverse_edge(second_edge),):
                    try:
                        ruled_face = BRepFill.Face_s(first.edge, ruled_second)
                    except Exception:
                        continue
                    if usable_gap_face(ruled_face):
                        return [ruled_face]
                maker = BRepBuilderAPI_MakeWire()
                maker.Add(first.edge)
                maker.Add(second_edge)
                if maker.IsDone() and maker.Wire().Closed():
                    face = face_from_wire(maker.Wire())
                    if face is not None:
                        return [face]
                    split = _split_edge_at_middle(first.edge)
                    if split is not None:
                        split_maker = BRepBuilderAPI_MakeWire()
                        split_maker.Add(split[0])
                        split_maker.Add(split[1])
                        split_maker.Add(second_edge)
                        split_face = (
                            face_from_wire(split_maker.Wire())
                            if split_maker.IsDone() and split_maker.Wire().Closed()
                            else None
                        )
                        if split_face is not None:
                            return [split_face]
            except Exception:
                pass
    for wire, _ in _wire_groups(raw_edges, tolerance):
        if not wire.Closed():
            try:
                fixer = ShapeFix_Wire()
                fixer.Load(wire)
                fixer.SetPrecision(tolerance)
                fixer.SetMaxTolerance(tolerance * 2)
                fixer.FixReorder()
                fixer.FixConnected(tolerance)
                fixer.FixClosed(tolerance)
                wire = fixer.Wire()
            except Exception:
                continue
        if not wire.Closed():
            continue
        face = face_from_wire(wire)
        if face is not None:
            result.append(face)
    if not result and len(raw_edges) == 2:
        first, second = raw_edges
        forward_gap = max(
            float(np.linalg.norm(first.end - second.start)),
            float(np.linalg.norm(second.end - first.start)),
        )
        reverse_gap = max(
            float(np.linalg.norm(first.end - second.end)),
            float(np.linalg.norm(second.start - first.start)),
        )
        if min(forward_gap, reverse_gap) <= tolerance * 2:
            ordered = (
                [first, second]
                if forward_gap <= reverse_gap
                else [first, _TrimEdge(_reverse_edge(second.edge), second.points[::-1])]
            )
            face = _fill_surface_from_nodes(
                ordered,
                np.empty((0, 3), dtype=float),
                tolerance,
            )
            if face is not None:
                result.append(face)
    return result


def _merge_coincident_free_edges(
    sewing: BRepBuilderAPI_Sewing,
    tolerance: float,
) -> list[TopoDS_Face]:
    """Unify duplicate crack edges only when their full curves coincide."""

    if not hasattr(sewing, "FreeEdge") or not 2 <= sewing.NbFreeEdges() <= 256:
        return []
    records: list[tuple[TopoDS_Edge, np.ndarray, np.ndarray]] = []
    try:
        for index in range(1, sewing.NbFreeEdges() + 1):
            edge = sewing.FreeEdge(index)
            first = TopExp.FirstVertex_s(edge, True)
            last = TopExp.LastVertex_s(edge, True)
            endpoints = np.asarray(
                [cq.Vertex(first).toTuple(), cq.Vertex(last).toTuple()],
                dtype=float,
            )
            sampled_points, _ = cq.Edge(edge).sample(12)
            samples = np.asarray(
                [point.toTuple() for point in sampled_points],
                dtype=float,
            )
            records.append((edge, endpoints, samples))
    except Exception:
        return []

    remaining = list(range(len(records)))
    pairs: list[tuple[int, int, bool]] = []
    # This helper receives the accepted sewing tolerance, which may already be
    # a 12x reconciliation retry. Only collapse curves within roughly the base
    # fit tolerance; wider lens-shaped gaps need an explicit ruled patch.
    merge_limit = max(tolerance * 0.1, 1e-7)
    while remaining:
        first_index = remaining.pop(0)
        _, first_endpoints, first_samples = records[first_index]
        best: tuple[float, int, bool] | None = None
        for position, second_index in enumerate(remaining):
            _, second_endpoints, second_samples = records[second_index]
            forward_endpoint_error = float(
                np.max(np.linalg.norm(first_endpoints - second_endpoints, axis=1))
            )
            reverse_endpoint_error = float(
                np.max(np.linalg.norm(first_endpoints - second_endpoints[::-1], axis=1))
            )
            reverse = reverse_endpoint_error < forward_endpoint_error
            endpoint_error = min(forward_endpoint_error, reverse_endpoint_error)
            if endpoint_error > merge_limit:
                continue
            # CadQuery discretization follows the underlying curve parameter,
            # which is independent of the TopoDS edge orientation used above.
            curve_error = min(
                float(np.max(np.linalg.norm(first_samples - second_samples, axis=1))),
                float(np.max(np.linalg.norm(first_samples - second_samples[::-1], axis=1))),
            )
            score = max(endpoint_error, curve_error)
            if score <= merge_limit and (best is None or score < best[0]):
                best = (score, position, reverse)
        if best is None:
            continue
        _, position, reverse = best
        second_index = remaining.pop(position)
        pairs.append((first_index, second_index, reverse))
    if not pairs:
        return []

    merged = sewing.SewedShape()
    replacements = 0
    for first_index, second_index, reverse in pairs:
        first_edge = records[first_index][0]
        second_edge = records[second_index][0]
        replacement = _reverse_edge(first_edge) if reverse else first_edge
        owner = None
        current_faces: list[TopoDS_Face] = []
        explorer = TopExp_Explorer(merged, TopAbs_FACE)
        while explorer.More():
            current_faces.append(TopoDS.Face_s(explorer.Current()))
            explorer.Next()
        for face in current_faces:
            edge_explorer = TopExp_Explorer(face, TopAbs_EDGE)
            while edge_explorer.More():
                if edge_explorer.Current().IsSame(second_edge):
                    owner = face
                    break
                edge_explorer.Next()
            if owner is not None:
                break
        if owner is None:
            continue
        try:
            edge_fixer = ShapeFix_Edge()
            edge_fixer.FixAddPCurve(replacement, owner, False, tolerance)
            edge_fixer.FixSameParameter(replacement, owner, tolerance)
            edge_fixer.FixVertexTolerance(replacement, owner)
            reshaper = BRepTools_ReShape()
            reshaper.Replace(second_edge, replacement)
            candidate = reshaper.Apply(merged)
        except Exception:
            continue
        candidate_faces: list[TopoDS_Face] = []
        explorer = TopExp_Explorer(candidate, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            if not _usable_face(face, tolerance):
                candidate_faces = []
                break
            candidate_faces.append(face)
            explorer.Next()
        if not candidate_faces:
            continue
        merged = candidate
        replacements += 1
    if replacements == 0:
        # If substituting the shared TShape would invalidate a face p-curve,
        # let sewing reconcile the already coincident curves by explicitly
        # recording their measured fit tolerance on both edges and endpoints.
        builder = BRep_Builder()
        for first_index, second_index, _ in pairs:
            for edge in (records[first_index][0], records[second_index][0]):
                builder.UpdateEdge(edge, merge_limit)
                first = TopExp.FirstVertex_s(edge, True)
                last = TopExp.LastVertex_s(edge, True)
                if not first.IsNull():
                    builder.UpdateVertex(first, merge_limit)
                if not last.IsNull():
                    builder.UpdateVertex(last, merge_limit)
        merged = sewing.SewedShape()
        replacements = len(pairs)
    result: list[TopoDS_Face] = []
    explorer = TopExp_Explorer(merged, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        result.append(face)
        explorer.Next()
    return result


def _serialize_patch(patch: SurfacePatch) -> dict[str, object]:
    functions = {
        "plane": "dot(normal, point - origin) = 0",
        "cylinder": "norm((I - axis*axis^T) * (point - origin)) - radius = 0",
        "cone": "radial_distance - abs(axial_distance) * tan(semi_angle) = 0",
        "sphere": "norm(point - center) - radius = 0",
        "torus": "hypot(radial_distance - major_radius, axial_distance) - minor_radius = 0",
        "extrusion": "profile(u) + direction * v",
        "revolution": "rotate(profile(v), axis, u)",
        "freeform": "B-spline surface constrained by boundary and interior mesh nodes",
    }
    result: dict[str, object] = {
        "id": patch.patch_id,
        "kind": patch.kind,
        "fit_basis": "mesh_nodes",
        "surface_function": functions[patch.kind],
        "support_node_count": int(len(patch.vertex_indices)),
        "support_face_count": int(len(patch.face_indices)),
        "area_mm2": float(patch.area),
    }
    for name in (
        "origin",
        "normal",
        "direction",
        "axis",
        "apex",
        "center",
        "radius",
        "major_radius",
        "minor_radius",
        "semi_angle",
        "start",
        "end",
        "rms_error",
        "max_error",
        "normal_error_degrees",
    ):
        if not hasattr(patch, name):
            continue
        value = getattr(patch, name)
        result[name] = value.tolist() if isinstance(value, np.ndarray) else float(value)
    return result


def surface_graph_json(
    graph: SurfaceGraph,
    *,
    faceted_patch_ids: list[str] | None = None,
    source_mesh_fallback: bool = False,
) -> dict[str, object]:
    faceted_ids = set(faceted_patch_ids or [])
    faceted_face_indices = (
        []
        if source_mesh_fallback and not faceted_ids
        else sorted(
            {
                int(face_index)
                for patch in graph.patches
                if patch.patch_id in faceted_ids
                for face_index in patch.face_indices
            }
        )
    )
    surfaces: list[dict[str, object]] = []
    for patch in graph.patches:
        serialized = _serialize_patch(patch)
        serialized["representation"] = (
            "faceted" if patch.patch_id in faceted_ids else "fitted"
        )
        surfaces.append(serialized)
    return {
        "surface_taxonomy": {
            "elementary_recognized": [
                "plane",
                "cylinder",
                "cone",
                "sphere",
                "torus",
            ],
            "swept_recognized": ["extrusion", "revolution"],
            "other_cad_families": [
                "bezier_surface",
                "bspline_surface",
                "surface_of_revolution",
                "offset_surface",
                "ruled_surface",
            ],
            "residual_representation": "node_fitted_bspline_surface",
        },
        "surfaces": surfaces,
        "adjacency": [
            {
                "first": item.first_patch_id,
                "second": item.second_patch_id,
                "boundary_count": len(item.boundary_curves),
            }
            for item in graph.adjacency
        ],
        "visualization": {
            "source_mesh_carrier": source_mesh_fallback,
            "global_faceted_fallback": source_mesh_fallback and not faceted_ids,
            "faceted_patch_ids": sorted(faceted_ids),
            "faceted_source_face_indices": faceted_face_indices,
            "surface_boundaries": [
                np.asarray(curve, dtype=float).tolist()
                for item in graph.adjacency
                for curve in item.boundary_curves
                if len(curve) >= 2
            ],
        },
    }


def build_surface_brep(
    data: MeshData,
    progress_callback: Callable[[str], None] | None = None,
) -> SurfaceBRepResult:
    graph = detect_surface_graph(data)
    if progress_callback is not None:
        progress_callback(
            f"surface_detection_done surfaces={len(graph.patches)} "
            f"adjacency={len(graph.adjacency)} "
            f"planes={len(graph.planar_patches)} "
            f"cylinders={len(graph.cylindrical_patches)} "
            f"cones={len(graph.conical_patches)} "
            f"spheres={len(graph.spherical_patches)} "
            f"tori={len(graph.toroidal_patches)} "
            f"extrusions={len(graph.extrusion_patches)} "
            f"revolutions={len(graph.revolution_patches)} "
            f"freeforms={len(graph.freeform_patches)}"
        )
    tolerance = max(data.diagonal * 0.0006, 2e-7)
    models: dict[str, _SurfaceModel] = {}
    for patch in graph.patches:
        try:
            model = _surface_model(patch, data.mesh)
        except Exception:
            model = None
        if model is not None:
            models[patch.patch_id] = model
    trim_edges, topology_vertex_count, topology_edge_count = _shared_trim_edges(
        graph,
        models,
        tolerance,
    )
    if progress_callback is not None:
        progress_callback(
            "surface_intersections_done "
            f"vertices={topology_vertex_count} edges={topology_edge_count}"
        )
    faces: list[TopoDS_Face] = []
    warnings: list[str] = []
    analytic_failures: list[str] = []
    point_fitted_face_count = 0
    faceted_face_count = 0
    faceted_patch_count = 0
    faceted_patch_ids: list[str] = []
    unfitted_patches: list[str] = []

    def mark_unfitted(patch: SurfacePatch) -> None:
        unfitted_patches.append(patch.patch_id)
        if progress_callback is not None:
            details = (
                f" area={patch.area:.9g} faces={len(patch.face_indices)}"
                f" nodes={len(patch.vertex_indices)}"
            )
            if isinstance(patch, SurfaceOfRevolutionPatch):
                coverage = math.degrees(
                    _angular_coverage(
                        np.asarray(data.mesh.vertices[patch.vertex_indices], dtype=float),
                        patch.origin,
                        patch.axis,
                    )
                )
                details += f" profile={len(patch.profile_points)} coverage_deg={coverage:.6g}"
            progress_callback(
                f"surface_face_unfitted patch={patch.patch_id} kind={patch.kind}" + details
            )

    def add_faceted_or_mark(patch: SurfacePatch) -> bool:
        nonlocal faceted_face_count, faceted_patch_count
        try:
            faceted = _faceted_residual_faces(
                patch,
                trim_edges.get(patch.patch_id, []),
                data.mesh,
                tolerance,
            )
            if not faceted:
                # Dense STL transitions often contain edges shorter than the
                # analytic sewing tolerance. Giving those tiny triangles the
                # neighboring surface's broad canonical vertex tolerances can
                # make otherwise valid source facets self-intersect in OCCT.
                # Keep the exact source triangles in that case; final sewing
                # may still join their boundary to the fitted cylinder within
                # the recognition tolerance, and the geometry/STEP gates below
                # remain authoritative.
                faceted = _faceted_residual_faces(
                    patch,
                    [],
                    data.mesh,
                    tolerance,
                )
        except Exception:
            faceted = []
        if not faceted:
            mark_unfitted(patch)
            return False
        faces.extend(faceted)
        faceted_face_count += len(faceted)
        faceted_patch_count += 1
        faceted_patch_ids.append(patch.patch_id)
        if progress_callback is not None:
            progress_callback(
                f"surface_face_faceted patch={patch.patch_id} kind={patch.kind} "
                f"faces={len(faceted)}"
            )
        return True

    for patch in graph.patches:
        if progress_callback is not None:
            progress_callback(f"surface_face_start patch={patch.patch_id} kind={patch.kind}")
        model = models.get(patch.patch_id)
        if model is not None:
            if isinstance(patch, ToroidalPatch) and patch.boundary_loops:
                try:
                    face = (
                        _rectangular_torus_face(
                            model,
                            data.mesh,
                            tolerance,
                        )
                        or _analytic_face(
                            model,
                            trim_edges.get(patch.patch_id, []),
                            data.mesh,
                            tolerance,
                        )
                        or _uv_boundary_face(
                            model,
                            data.mesh,
                            tolerance,
                        )
                        or _toroidal_revolution_face(
                            patch,
                            data.mesh,
                            tolerance,
                        )
                    )
                except Exception:
                    face = None
            else:
                try:
                    face = _analytic_face(
                        model,
                        trim_edges.get(patch.patch_id, []),
                        data.mesh,
                        tolerance,
                    )
                except Exception:
                    face = None
            if face is None and isinstance(patch, PlanarPatch):
                face = _planar_boundary_face(model, data.mesh, tolerance)
            if face is None:
                try:
                    face = _uv_boundary_face(model, data.mesh, tolerance)
                except Exception:
                    face = None
            if face is not None and _usable_face(face, tolerance):
                faces.append(face)
                continue
            if isinstance(patch, (PlanarPatch, ToroidalPatch)) and getattr(
                patch,
                "boundary_loops",
                None,
            ):
                # Shared intersections can contain a tiny dangling chain or a
                # curve whose pcurve is unsuitable for this support even when
                # the recognized mathematical surface is exact. Retry with one
                # fitted curve per node boundary, then its ordered polyline as
                # a conservative last resort. Both retain the analytic support
                # and avoid BRepFill_Filling's dense constraint solve.
                if isinstance(patch, ToroidalPatch):
                    face = None
                else:
                    boundary_edges = [
                        edge
                        for loop in getattr(
                            patch,
                            "boundary_loops_3d",
                            patch.boundary_loops,
                        )
                        if (edge := _fallback_curve_edge(loop, tolerance)) is not None
                    ]
                    try:
                        face = _analytic_face(
                            model,
                            boundary_edges,
                            data.mesh,
                            tolerance,
                        )
                    except Exception:
                        face = None
                if face is None or not _usable_face(face, tolerance):
                    try:
                        face = _analytic_face(
                            model,
                            _polyline_boundary_edges(patch, tolerance),
                            data.mesh,
                            tolerance,
                        )
                    except Exception:
                        face = None
                if face is not None and _usable_face(face, tolerance):
                    faces.append(face)
                    continue
            if isinstance(patch, PlanarPatch):
                # A tiny planar sliver can be smaller than the global sewing
                # tolerance, making an otherwise exact one-wire trim
                # degenerate in OCCT. Retain its source triangle subdivision
                # on the recognized plane instead of relabeling the region as
                # a freeform B-spline. The local-topology builder uses a
                # scale-aware tolerance and every emitted face remains PLANE.
                planar_faces = _faceted_residual_faces(
                    patch,
                    [],
                    data.mesh,
                    tolerance,
                )
                if planar_faces and all(
                    cq.Face(planar_face).geomType() == "PLANE"
                    and _usable_face(planar_face, tolerance * 0.01)
                    for planar_face in planar_faces
                ):
                    faces.extend(planar_faces)
                    continue
            analytic_failures.append(patch.kind)
            if isinstance(patch, ToroidalPatch):
                add_faceted_or_mark(patch)
                continue
            fallback = FreeformPatch(
                patch_id=patch.patch_id,
                face_indices=patch.face_indices,
                vertex_indices=np.unique(data.mesh.faces[patch.face_indices]),
                area=patch.area,
                boundary_loops=getattr(patch, "boundary_loops_3d", patch.boundary_loops),
            )
            longest_boundary = max(
                (len(loop) for loop in fallback.boundary_loops),
                default=0,
            )
            fitted = (
                _point_fitted_face(
                    fallback,
                    trim_edges.get(patch.patch_id, []),
                    data.mesh,
                    tolerance,
                )
                or _point_fitted_face(fallback, [], data.mesh, tolerance)
                if len(fallback.vertex_indices) <= 500
                and len(fallback.face_indices) <= 300
                and longest_boundary <= 32
                else None
            )
            if fitted is not None:
                faces.append(fitted)
                point_fitted_face_count += 1
            else:
                add_faceted_or_mark(patch)
        elif isinstance(patch, LinearExtrusionPatch):
            fitted = _linear_extrusion_face(
                patch,
                trim_edges.get(patch.patch_id, []),
                data.mesh,
                tolerance,
            )
            if fitted is not None:
                faces.append(fitted)
            else:
                fallback = FreeformPatch(
                    patch_id=patch.patch_id,
                    face_indices=patch.face_indices,
                    vertex_indices=patch.vertex_indices,
                    area=patch.area,
                    boundary_loops=patch.boundary_loops,
                )
                longest_boundary = max(
                    (len(loop) for loop in fallback.boundary_loops),
                    default=0,
                )
                fitted = (
                    _point_fitted_face(
                        fallback,
                        trim_edges.get(patch.patch_id, []),
                        data.mesh,
                        tolerance,
                    )
                    if len(fallback.vertex_indices) <= 500
                    and len(fallback.face_indices) <= 300
                    and longest_boundary <= 32
                    else None
                )
                if fitted is not None:
                    faces.append(fitted)
                    point_fitted_face_count += 1
                else:
                    add_faceted_or_mark(patch)
        elif isinstance(patch, SurfaceOfRevolutionPatch):
            fitted = _surface_of_revolution_face(
                patch,
                trim_edges.get(patch.patch_id, []),
                data.mesh,
                tolerance,
            )
            if (
                fitted is None
                and len(patch.vertex_indices) <= 500
                and len(patch.face_indices) <= 300
            ):
                fitted = _surface_of_revolution_face(
                    patch,
                    trim_edges.get(patch.patch_id, []),
                    data.mesh,
                    tolerance,
                    use_angular_bounds=True,
                )
            if fitted is None:
                fitted = _surface_of_revolution_face(
                    patch,
                    _polyline_boundary_edges(patch, tolerance),
                    data.mesh,
                    tolerance,
                    use_angular_bounds=True,
                )
            if fitted is None:
                canonical_boundary = trim_edges.get(patch.patch_id, [])
                if 3 <= len(canonical_boundary) <= 32:
                    boundary_fill = _fill_surface_from_nodes(
                        canonical_boundary,
                        np.empty((0, 3), dtype=float),
                        tolerance,
                    )
                    if boundary_fill is not None:
                        area_ratio = float(cq.Face(boundary_fill).Area()) / max(
                            patch.area,
                            tolerance**2,
                        )
                        if 0.25 <= area_ratio <= 4.0:
                            fitted = _orient_face(boundary_fill, patch, data.mesh)
                            point_fitted_face_count += 1
            if (
                fitted is None
                and len(patch.vertex_indices) <= 500
                and len(patch.face_indices) <= 300
            ):
                fallback = FreeformPatch(
                    patch_id=patch.patch_id,
                    face_indices=patch.face_indices,
                    vertex_indices=patch.vertex_indices,
                    area=patch.area,
                    boundary_loops=patch.boundary_loops,
                )
                fitted = _point_fitted_face(
                    fallback,
                    trim_edges.get(patch.patch_id, []),
                    data.mesh,
                    tolerance,
                ) or _point_fitted_face(fallback, [], data.mesh, tolerance)
                if fitted is not None:
                    point_fitted_face_count += 1
            if fitted is not None:
                faces.append(fitted)
            else:
                add_faceted_or_mark(patch)
        elif isinstance(patch, FreeformPatch):
            longest_boundary = max(
                (len(loop) for loop in patch.boundary_loops),
                default=0,
            )
            if (
                len(patch.vertex_indices) > 500
                or len(patch.face_indices) > 300
                or longest_boundary > 32
            ):
                fitted = _graph_bspline_face(
                    patch,
                    trim_edges.get(patch.patch_id, []),
                    data.mesh,
                    tolerance,
                )
                if fitted is not None:
                    faces.append(fitted)
                    point_fitted_face_count += 1
                else:
                    add_faceted_or_mark(patch)
                continue
            # Arbitrary intersection-edge mixtures can drive OCCT's plate
            # solver into a native failure. Fit residual charts from their own
            # ordered node boundary; analytic neighbors are reconciled later.
            fitted = _point_fitted_face(patch, [], data.mesh, tolerance)
            if fitted is not None:
                faces.append(fitted)
                point_fitted_face_count += 1
            else:
                add_faceted_or_mark(patch)

    if not faces:
        raise ValueError("Surface recognition did not produce any buildable faces")
    if unfitted_patches:
        preview = ", ".join(unfitted_patches[:8])
        suffix = "..." if len(unfitted_patches) > 8 else ""
        warnings.append(
            f"{len(unfitted_patches)} residual patches could not be point-fitted "
            f"({preview}{suffix})."
        )
    if analytic_failures:
        failure_counts = Counter(analytic_failures)
        summary = ", ".join(
            f"{count} {kind} patch{'es' if count != 1 else ''}"
            for kind, count in sorted(failure_counts.items())
        )
        warnings.append(
            "Analytic trimming failed for "
            + summary
            + "; those regions used the node-fitted or conforming-facet fallback."
        )
    if faceted_patch_count:
        warnings.append(
            f"Retained {faceted_patch_count} unfitted residual "
            f"region{'s' if faceted_patch_count != 1 else ''} as "
            f"{faceted_face_count} B-rep facets. Analytic boundary conformance "
            "was used where topologically safe; tiny transition facets kept "
            "their local source topology for sewing."
        )
    if progress_callback is not None:
        progress_callback(f"trimmed_faces_done faces={len(faces)}")
    sewing, solids = (
        _sew_faces(faces, tolerance, make_solids=False)
        if unfitted_patches
        else _sew_faces(faces, tolerance)
    )
    effective_tolerance = tolerance
    if not unfitted_patches and (not solids or sewing.NbFreeEdges() > 0):
        # Independent least-squares surfaces can differ by a few fit
        # tolerances along a mathematically shared boundary. Retry sewing with
        # a tightly capped reconciliation tolerance and retain a retry only
        # when it strictly improves the topological result.
        for factor in (1.5, 2.0, 3.0, 5.0, 8.0, 10.0, 12.0):
            retry_tolerance = tolerance * factor
            retry, retry_solids = _sew_faces(faces, retry_tolerance)
            if retry.NbFreeEdges() < sewing.NbFreeEdges() or (retry_solids and not solids):
                sewing, solids = retry, retry_solids
                effective_tolerance = retry_tolerance
            if solids:
                break
    filled_gap_count = 0
    if not solids and 0 < sewing.NbFreeEdges() <= 64:
        # Repair the partially sewn face complex before classifying its free
        # boundaries. ShapeAnalysis can otherwise expose transient seam edges
        # from an invalid p-curve graph, causing a cap to be built against the
        # wrong loop. Re-sewing the shape-fixed faces gives the boundary repair
        # the same stable topology that will later be exported.
        try:
            sewed_shape = cq.Shape(sewing.SewedShape())
            if not sewed_shape.isValid():
                fixer = ShapeFix_Shape(sewing.SewedShape())
                fixer.SetPrecision(tolerance)
                fixer.Perform()
                fixed_faces: list[TopoDS_Face] = []
                fixed_explorer = TopExp_Explorer(fixer.Shape(), TopAbs_FACE)
                while fixed_explorer.More():
                    fixed_faces.append(TopoDS.Face_s(fixed_explorer.Current()))
                    fixed_explorer.Next()
                fixed_sewing, fixed_solids = _sew_faces(
                    fixed_faces,
                    effective_tolerance,
                )
                if fixed_sewing.NbFreeEdges() <= sewing.NbFreeEdges():
                    faces = fixed_faces
                    sewing, solids = fixed_sewing, fixed_solids
        except Exception:
            pass
    # Filling one closed free-bound loop can expose a smaller nested loop after
    # the new face is sewn. Recompute free bounds after every accepted repair,
    # and stop unless the free-edge count strictly decreases.
    for _ in range(4):
        if solids or sewing.NbFreeEdges() == 0 or sewing.NbFreeEdges() > 64:
            break
        gap_faces = _free_boundary_fill_faces(sewing, effective_tolerance)
        if not gap_faces:
            break
        sewed_faces: list[TopoDS_Face] = []
        explorer = TopExp_Explorer(sewing.SewedShape(), TopAbs_FACE)
        while explorer.More():
            sewed_faces.append(TopoDS.Face_s(explorer.Current()))
            explorer.Next()
        repaired_faces = [*sewed_faces, *gap_faces]
        gap_sewing_tolerance = max(tolerance, effective_tolerance * 0.1)
        repaired, repaired_solids = _sew_faces(
            repaired_faces,
            gap_sewing_tolerance,
        )
        if repaired.NbFreeEdges() == 0 and not repaired_solids:
            reversed_gap_faces = [TopoDS.Face_s(face.Reversed()) for face in gap_faces]
            reversed_faces = [*sewed_faces, *reversed_gap_faces]
            reversed_sewing, reversed_solids = _sew_faces(
                reversed_faces,
                gap_sewing_tolerance,
            )
            if reversed_solids:
                repaired = reversed_sewing
                repaired_solids = reversed_solids
                repaired_faces = reversed_faces
        if repaired.NbFreeEdges() >= sewing.NbFreeEdges():
            break
        sewing, solids = repaired, repaired_solids
        faces = repaired_faces
        filled_gap_count += len(gap_faces)
        point_fitted_face_count += len(gap_faces)
    if filled_gap_count:
        warnings.append(
            f"Filled {filled_gap_count} closed residual boundary "
            f"loop{'s' if filled_gap_count != 1 else ''} with C0 surfaces."
        )
    if not solids and 0 < sewing.NbFreeEdges() <= 256:
        merged_faces = _merge_coincident_free_edges(sewing, effective_tolerance)
        if merged_faces:
            merged, merged_solids = _sew_faces(
                merged_faces,
                effective_tolerance,
            )
            if merged.NbFreeEdges() < sewing.NbFreeEdges():
                sewing, solids = merged, merged_solids
                faces = merged_faces
                warnings.append(
                    "Unified coincident residual crack edges without changing their fitted curves."
                )
                # Merging coincident cracks can expose a smaller nested loop.
                # Recompute after each accepted fill instead of assuming that
                # one pass sees the final free boundary.
                for _ in range(4):
                    if solids or sewing.NbFreeEdges() == 0:
                        break
                    final_gap_faces = _free_boundary_fill_faces(
                        sewing,
                        effective_tolerance,
                    )
                    if not final_gap_faces:
                        break
                    final_candidates: list[
                        tuple[
                            BRepBuilderAPI_Sewing,
                            list[TopoDS_Solid],
                            list[TopoDS_Face],
                        ]
                    ] = []
                    for oriented_gaps in (
                        final_gap_faces,
                        [TopoDS.Face_s(face.Reversed()) for face in final_gap_faces],
                    ):
                        candidate_faces = [*faces, *oriented_gaps]
                        candidate_sewing, candidate_solids = _sew_faces(
                            candidate_faces,
                            effective_tolerance,
                        )
                        final_candidates.append(
                            (candidate_sewing, candidate_solids, candidate_faces)
                        )
                    final_sewing, final_solids, final_faces = min(
                        final_candidates,
                        key=lambda item: (
                            not bool(item[1]),
                            item[0].NbFreeEdges(),
                        ),
                    )
                    if final_sewing.NbFreeEdges() < sewing.NbFreeEdges():
                        sewing, solids = final_sewing, final_solids
                        faces = final_faces
                        point_fitted_face_count += len(final_gap_faces)
                    else:
                        break
    if not solids and 0 < sewing.NbFreeEdges() <= 32:
        try:
            permissive, permissive_solids = _sew_faces(
                faces,
                effective_tolerance,
                non_manifold=True,
            )
        except TypeError:
            permissive, permissive_solids = sewing, solids
        if permissive.NbFreeEdges() < sewing.NbFreeEdges() and permissive_solids:
            sewing, solids = permissive, permissive_solids
    if len(solids) > 1:
        volumes = [abs(float(cq.Shape(solid).Volume())) for solid in solids]
        largest_volume = max(volumes, default=0.0)
        artifact_limit = max(
            largest_volume * 1e-8,
            effective_tolerance**3 * 10,
        )
        retained_solids = [
            solid
            for solid, volume in zip(solids, volumes, strict=True)
            if volume > artifact_limit
        ]
        if retained_solids and len(retained_solids) < len(solids):
            removed_count = len(solids) - len(retained_solids)
            solids = retained_solids
            warnings.append(
                f"Discarded {removed_count} microscopic closed sewing "
                f"artifact{'s' if removed_count != 1 else ''}; all retained "
                "source-scale geometry remains in the reconstructed solid."
            )
    # A valid, complete repaired solid has no topological free boundary even
    # if the pre-repair sewing diagnostic still reports the seams it repaired.
    free_edge_count = 0 if solids else int(sewing.NbFreeEdges())
    closed = bool(solids and free_edge_count == 0)
    if progress_callback is not None:
        progress_callback(
            f"surface_sewing_done solids={len(solids)} free_edges={sewing.NbFreeEdges()}"
        )
    if closed:
        shape = (
            cq.Shape(solids[0])
            if len(solids) == 1
            else cq.Compound.makeCompound([cq.Shape(solid) for solid in solids])
        )
        joined_shape: cq.Shape | None = shape
    else:
        # Preserve every valid join that sewing found even when the result is an
        # open shell. If OCCT rejects the partially sewn topology, retain the
        # independently valid fitted faces as a diagnostic compound.
        sewed_shape = cq.Shape(sewing.SewedShape())
        if not sewed_shape.isValid():
            try:
                fixer = ShapeFix_Shape(sewing.SewedShape())
                fixer.SetPrecision(tolerance)
                fixer.Perform()
                fixed = cq.Shape(fixer.Shape())
                if fixed.isValid() and fixed.Faces():
                    sewed_shape = fixed
            except Exception:
                pass
        joined_shape = (
            sewed_shape
            if sewed_shape.isValid()
            and sewed_shape.Faces()
            and all(face.isValid() for face in sewed_shape.Faces())
            else None
        )
        # The recognition artifact must remain readable even if a partially
        # joined shell develops invalid STEP p-curves. Export the independently
        # valid fitted faces as the primary test model and keep the joined shell
        # as a separate topology diagnostic.
        shape = cq.Compound.makeCompound([cq.Shape(face) for face in faces])
        if free_edge_count:
            warnings.append(
                f"The fitted surfaces retain {free_edge_count} open boundary edges. "
                "Watertight closure was not forced during recognition testing."
            )
        else:
            warnings.append(
                "The sewn shell has no free edges but failed OpenCascade solid "
                "validity checks; it was not exported as a watertight solid."
            )
    valid = bool(shape.isValid() and shape.Faces())
    counts = {
        "plane": len(graph.planar_patches),
        "cylinder": len(graph.cylindrical_patches),
        "cone": len(graph.conical_patches),
        "sphere": len(graph.spherical_patches),
        "torus": len(graph.toroidal_patches),
        "extrusion": len(graph.extrusion_patches),
        "revolution": len(graph.revolution_patches),
        "freeform": len(graph.freeform_patches),
    }
    return SurfaceBRepResult(
        shape=shape,
        graph=graph,
        surface_counts=counts,
        face_count=len(shape.Faces()),
        solid_count=len(shape.Solids()),
        free_edge_count=free_edge_count,
        sewing_tolerance=effective_tolerance,
        valid=valid,
        closed=closed,
        faceted_fallback=bool(faceted_patch_count),
        point_fitted_face_count=point_fitted_face_count,
        topology_vertex_count=topology_vertex_count,
        topology_edge_count=topology_edge_count,
        joined_shape=joined_shape,
        warnings=warnings,
        faceted_patch_count=faceted_patch_count,
        faceted_face_count=faceted_face_count,
        faceted_patch_ids=faceted_patch_ids,
        unfitted_patch_ids=unfitted_patches,
    )


def export_surface_brep(
    result: SurfaceBRepResult,
    directory: Path,
    source_stl_path: Path | None = None,
    *,
    verify_roundtrip: bool = True,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    step_path = directory / "reconstruction.step"
    cq.exporters.export(result.shape, str(step_path))
    if result.closed and verify_roundtrip:
        try:
            roundtrip = cq.importers.importStep(str(step_path)).val()
            exchange_valid = bool(roundtrip.isValid() and roundtrip.Solids())
        except Exception:
            exchange_valid = False
        if not exchange_valid:
            # Kernel-valid periodic faces can still fail while their p-curves
            # are translated into STEP. Never advertise that artifact as a
            # watertight result merely because the in-memory shell passed.
            result.joined_shape = result.shape
            result.closed = False
            result.solid_count = 0
            result.warnings.append(
                "The in-memory solid did not survive STEP export/import validity "
                "checks and was downgraded to a topology diagnostic."
            )
    if not result.closed and result.joined_shape is not None:
        cq.exporters.export(
            result.joined_shape,
            str(directory / "joined_surfaces.step"),
        )
    reconstruction_stl = directory / "reconstruction.stl"
    if result.source_mesh_fallback and source_stl_path is not None:
        shutil.copy2(source_stl_path, reconstruction_stl)
    else:
        cq.exporters.export(
            result.shape,
            str(reconstruction_stl),
            tolerance=max(min(result.sewing_tolerance * 0.5, 0.01), 1e-7),
            angularTolerance=0.04,
        )
    (directory / "surface_graph.json").write_text(
        json.dumps(
            surface_graph_json(
                result.graph,
                faceted_patch_ids=result.faceted_patch_ids,
                source_mesh_fallback=result.source_mesh_fallback,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

"""Measure native geometry extents independently of cached tessellation metadata.

OCCT's geometry algorithm still has numerical precision limits. Excluding mesh
deflection and shape-tolerance padding is not a claim of geometric equivalence
or of smaller boundary uncertainty. Topology tolerances are reported separately.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cadquery as cq
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib

Point3D = tuple[float, float, float]


class GeometryMeasurementError(ValueError):
    """Finite positive three-dimensional geometry extents are unavailable."""


@dataclass(frozen=True, slots=True)
class GeometryBounds:
    minimum_mm: Point3D
    maximum_mm: Point3D

    def __post_init__(self) -> None:
        for point in (self.minimum_mm, self.maximum_mm):
            if not isinstance(point, tuple) or len(point) != 3 or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in point
            ):
                raise GeometryMeasurementError('Bounds require three finite numbers per endpoint')
        if any(not math.isfinite(value) or value <= 0 for value in self.dimensions_mm):
            raise GeometryMeasurementError('Native geometry extents must be finite and positive')
        if not math.isfinite(self.diagonal_mm):
            raise GeometryMeasurementError('Native geometry diagonal must be finite')

    @property
    def dimensions_mm(self) -> Point3D:
        return tuple(high - low for low, high in zip(
            self.minimum_mm, self.maximum_mm, strict=True,
        ))

    @property
    def diagonal_mm(self) -> float:
        return math.hypot(*self.dimensions_mm)


def native_geometry_bounds(shape: cq.Shape) -> GeometryBounds:
    """Bound the actual trimmed native geometry without altering its shape/mesh."""
    box = Bnd_Box()
    try:
        # False/False is essential: cached mesh deflection and topological
        # tolerance envelopes are not the physical size being compared.
        BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
        if box.IsVoid() or box.IsOpen():
            raise GeometryMeasurementError('Native geometry bounds are empty or unbounded')
        values = box.Get()
        return GeometryBounds(tuple(values[:3]), tuple(values[3:]))
    except GeometryMeasurementError:
        raise
    except Exception as exc:
        raise GeometryMeasurementError(f'Native geometry bounds unavailable: {exc}') from exc


def topology_tolerance_diagnostics(shape: cq.Shape) -> dict:
    """Read uncertainty metadata only; missing/invalid values never become zero."""
    try:
        result = {}
        for name, entities in (('max_edge_tolerance_mm', shape.Edges()),
                               ('max_vertex_tolerance_mm', shape.Vertices())):
            values = [float(BRep_Tool.Tolerance_s(entity.wrapped)) for entity in entities]
            if any(not math.isfinite(value) or value < 0 for value in values):
                return {'available': False, 'reason': 'nonfinite_or_negative_native_tolerance'}
            result[name] = max(values) if values else None
        return {'available': True, **result}
    except Exception as exc:
        return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}

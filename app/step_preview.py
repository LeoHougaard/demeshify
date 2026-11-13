"""Complete preview tessellation, independent of reconstruction and grading.

Coordinates and face orientation are retained. Deflections are absolute native
shape units; the 0.01 default is millimetres only for a shape already in mm.
The caller owns unit conversion, output scale, and any intentional orientation.
"""
from __future__ import annotations

import math
import os
import struct
import tempfile
from pathlib import Path

import cadquery as cq
import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.StlAPI import StlAPI_Writer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FORWARD, TopAbs_REVERSED, TopAbs_VERTEX, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_IndexedMapOfShape


def _complete_triangles(shape: cq.Shape) -> int:
    faces = shape.Faces()
    if not faces:
        raise ValueError("A preview requires at least one B-rep face")
    # STL cannot represent loose curves/points. Do not silently discard them.
    for kind in (TopAbs_WIRE, TopAbs_EDGE, TopAbs_VERTEX):
        present, owned = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape.wrapped, kind, present)
        for face in faces:
            TopExp.MapShapes_s(face.wrapped, kind, owned)
        if present.Extent() != owned.Extent():
            raise ValueError("The preview contains geometry outside its faces")
    count = 0
    for face in faces:
        if face.wrapped.Orientation() not in (TopAbs_FORWARD, TopAbs_REVERSED):
            raise ValueError("A preview face has no supported orientation")
        location = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face.wrapped, location)
        if poly is None or poly.NbNodes() < 3 or poly.NbTriangles() < 1:
            raise ValueError("Preview tessellation omitted a face")
        transform = location.Transformation()
        points = np.asarray([
            (p.X(), p.Y(), p.Z())
            for p in (poly.Node(i).Transformed(transform) for i in range(1, poly.NbNodes() + 1))
        ])
        with np.errstate(over="ignore", invalid="ignore"):
            encoded = points.astype(np.float32)
            if not np.all(np.isfinite(encoded)):
                raise ValueError("Preview coordinates exceed the binary STL range")
        triangles = np.asarray([
            (t.Value(1) - 1, t.Value(2) - 1, t.Value(3) - 1) for t in poly.Triangles()
        ], dtype=np.int64)
        if np.any(triangles < 0) or np.any(triangles >= len(points)):
            raise ValueError("Preview tessellation contains invalid triangle indices")
        # Validate the coordinates the binary STL will actually contain.
        corners = encoded.astype(float)[triangles]
        cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        if not np.all(np.isfinite(cross)) or np.any(np.linalg.norm(cross, axis=1) == 0):
            raise ValueError("Preview tessellation contains a degenerate triangle")
        count += len(triangles)
    return count


def export_stl_preview(
    shape: cq.Shape,
    destination: Path,
    *,
    linear_deflection: float = 0.01,
    angular_deflection: float = 0.04,
) -> int:
    """Write a complete binary STL from a separate native copy; return triangles.

    This does not scale, repair, reorient or modify the caller's B-rep. Existing
    output survives meshing/writer failure. Native geometry acceptance and STEP
    export remain the caller's responsibility; this is only a preview helper.
    """
    if (not math.isfinite(linear_deflection) or linear_deflection <= 0
            or not math.isfinite(angular_deflection) or angular_deflection <= 0):
        raise ValueError("Preview deflections must be finite and positive")
    copied = cq.Shape.cast(BRepBuilderAPI_Copy(shape.wrapped, True, False).Shape())
    if not copied.isValid():
        raise ValueError("An invalid B-rep cannot produce a complete preview")
    parameters = IMeshTools_Parameters()
    parameters.Deflection, parameters.Angle = linear_deflection, angular_deflection
    parameters.Relative, parameters.InParallel = False, False
    parameters.ControlSurfaceDeflection = True
    parameters.ForceFaceDeflection = True
    parameters.EnableControlSurfaceDeflectionAllSurfaces = True
    mesher = BRepMesh_IncrementalMesh(copied.wrapped, parameters)
    if not mesher.IsDone():
        raise ValueError("Preview tessellation did not complete")
    triangles = _complete_triangles(copied)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".step-preview-", suffix=".stl", delete=False,
    ) as temporary:
        staged = Path(temporary.name)
    try:
        writer = StlAPI_Writer()
        writer.ASCIIMode = False
        if not writer.Write(copied.wrapped, str(staged)):
            raise ValueError("The native preview STL writer failed")
        with staged.open("rb") as stream:
            stream.seek(80)
            encoded_count = stream.read(4)
        if (len(encoded_count) != 4 or struct.unpack("<I", encoded_count)[0] != triangles
                or staged.stat().st_size != 84 + triangles * 50):
            raise ValueError("The preview STL does not contain every tessellated triangle")
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return triangles

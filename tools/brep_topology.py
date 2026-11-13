"""Evaluation-only topology diagnostics; never a reconstruction acceptance gate.

For a closed orientable cellular shell, chi = V - E + sum(2 - face_wires).
Seams count once as edges, degenerate pole edges do not count, and face holes
subtract cells. Ambiguous periodic domains and non-manifold/open shells return
indeterminate genus rather than a plausible-looking integer.
"""
from __future__ import annotations

import math

import cadquery as cq
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve2d, BRepAdaptor_Surface
from OCP.BRepClass3d import BRepClass3d, BRepClass3d_SolidClassifier
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FORWARD, TopAbs_OUT, TopAbs_REVERSED, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape

from app.step import unowned_geometry_counts


def _face_cells(face: cq.Face) -> tuple[int | None, str | None]:
    wires = len(face.Wires())
    if not wires:
        # No wire does not establish which part of a closed support is used.
        return None, 'face_without_boundary_cells'
    surface = BRepAdaptor_Surface(face.wrapped)
    if surface.IsUClosed() and surface.IsVClosed():
        directions = []
        for edge in face.Edges():
            if not BRep_Tool.IsClosed_s(edge.wrapped, face.wrapped):
                continue
            curve = BRepAdaptor_Curve2d(edge.wrapped, face.wrapped)
            first, last = curve.Value(curve.FirstParameter()), curve.Value(curve.LastParameter())
            directions.append((last.X() - first.X(), last.Y() - first.Y()))
        independent = any(
            abs(a * d - b * c) > 1e-7 * math.hypot(a, b) * math.hypot(c, d)
            for i, (a, b) in enumerate(directions) for c, d in directions[i + 1:])
        if not independent:
            return None, 'uncut_doubly_periodic_domain'
    return 2 - wires, None


def shell_topology(shell: cq.Shell) -> dict:
    """Report a shell's cell counts and genus only when invariants establish it."""
    faces = shell.Faces()
    edge_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shell.wrapped, TopAbs_EDGE, edge_map)
    degenerate = set()
    for index in range(1, edge_map.Extent() + 1):
        if BRep_Tool.Degenerated_s(TopoDS.Edge_s(edge_map.FindKey(index))):
            degenerate.add(index)
    uses: dict[int, list[int]] = {i: [] for i in range(1, edge_map.Extent() + 1)
                                if i not in degenerate}
    reasons = []
    face_cells = []
    for face in faces:
        cells, reason = _face_cells(face)
        face_cells.append(cells)
        if reason:
            reasons.append(reason)
        explorer = TopExp_Explorer(face.wrapped, TopAbs_EDGE)
        while explorer.More():
            edge = TopoDS.Edge_s(explorer.Current())
            index = edge_map.FindIndex(edge)
            if index not in degenerate:
                orientation = edge.Orientation()
                uses[index].append(1 if orientation == TopAbs_FORWARD else
                                   -1 if orientation == TopAbs_REVERSED else 0)
            explorer.Next()
    valid = bool(shell.isValid())
    closed = bool(BRep_Tool.IsClosed_s(shell.wrapped))
    manifold = all(len(orientations) == 2 for orientations in uses.values())
    coherent = all(sorted(orientations) == [-1, 1] for orientations in uses.values())
    if not valid:
        reasons.append('invalid_shell')
    if not closed:
        reasons.append('open_shell')
    if not manifold:
        reasons.append('non_manifold_edge_incidence')
    if not coherent:
        reasons.append('incoherent_edge_orientations')
    if not faces:
        reasons.append('empty_shell')
    vertices = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shell.wrapped, TopAbs_VERTEX, vertices)
    vertex_count = vertices.Extent()
    edge_count = len(uses)
    chi = (vertex_count - edge_count + sum(face_cells)
           if all(value is not None for value in face_cells) else None)
    if chi is not None and (chi > 2 or chi % 2):
        reasons.append('non_orientable_or_inconsistent_cell_count')
    supported = not reasons
    return {'valid': valid, 'closed': closed, 'manifold_edges': manifold,
            'coherent_edge_orientations': coherent, 'vertex_count': vertex_count,
            'nondegenerate_edge_count': edge_count,
            'degenerate_edge_count': len(degenerate), 'face_count': len(faces),
            'face_wire_counts': [len(face.Wires()) for face in faces],
            'cell_euler_characteristic': chi,
            'supported': supported, 'euler_characteristic': chi if supported else None,
            'genus': (2 - chi) // 2 if supported else None,
            'indeterminate_reasons': sorted(set(reasons))}


def topology_metrics(shape: cq.Shape) -> dict:
    """Group finite solid shell topology without claiming feature-history equality.

    A sorted signature keeps shell ownership and outer/cavity roles while
    ignoring model ordering and incidental face splitting. Geometric placement,
    dimensions and deviations still need separate checks.
    """
    solids = []
    signatures = []
    for solid in shape.Solids():
        outer = BRepClass3d.OuterShell_s(solid.wrapped)
        classifier = BRepClass3d_SolidClassifier(solid.wrapped)
        classifier.PerformInfinitePoint(1e-7)
        finite_material = classifier.State() == TopAbs_OUT
        shells = []
        for shell in solid.Shells():
            row = shell_topology(shell)
            row['role'] = 'outer' if shell.wrapped.IsSame(outer) else 'cavity'
            shells.append(row)
        solid_supported = bool(finite_material and shells
                               and sum(s['role'] == 'outer' for s in shells) == 1
                               and all(s['supported'] for s in shells))
        signature = (sorted((s['role'], s['euler_characteristic']) for s in shells)
                     if solid_supported else [])
        solids.append({'supported': solid_supported, 'finite_material': finite_material,
                       'shells': shells})
        signatures.append(signature)
    unowned = unowned_geometry_counts(shape)
    supported = bool(shape.isValid() and solids and not unowned
                     and all(solid['supported'] for solid in solids))
    return {'schema': 1, 'valid_brep': bool(shape.isValid()), 'solid_count': len(solids),
            'shell_count': len(shape.Shells()), 'unowned_geometry_counts': unowned,
            'supported': supported, 'solids': solids,
            'signature': sorted(signatures) if supported else None}


def compare_topology(reference: cq.Shape, output: cq.Shape) -> dict:
    """Return an honest three-state comparison: match, mismatch, indeterminate."""
    original, candidate = topology_metrics(reference), topology_metrics(output)
    determinate = original['supported'] and candidate['supported']
    return {'reference': original, 'output': candidate,
            'topology_match': original['signature'] == candidate['signature']
            if determinate else None,
            'status': ('match' if original['signature'] == candidate['signature'] else 'mismatch')
            if determinate else 'indeterminate'}

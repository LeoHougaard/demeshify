from pathlib import Path

import cadquery as cq
from OCP.TopAbs import (
    TopAbs_EDGE,
    TopAbs_FACE,
    TopAbs_SHELL,
    TopAbs_VERTEX,
    TopAbs_WIRE,
)
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape


def load_step_shape(path: Path) -> cq.Shape:
    """Read every transfer root without discarding or merging its geometry."""

    roots = cq.importers.importStep(str(path)).vals()
    if not roots:
        raise ValueError("The STEP contains no transferred geometry")
    return roots[0] if len(roots) == 1 else cq.Compound.makeCompound(roots)


def unowned_geometry_counts(shape: cq.Shape) -> dict[str, int]:
    """Count topology outside the solids, including free wires and vertices.

    Compare native topology identities, rather than bounds or sampled surfaces.
    A map unions shared subshapes so multi-solid assemblies are not double counted.
    Compound containers are allowed; every geometric subshape must belong to a solid.
    """

    solids = shape.Solids()
    counts = {}
    for name, kind in (
        ("shell", TopAbs_SHELL),
        ("face", TopAbs_FACE),
        ("wire", TopAbs_WIRE),
        ("edge", TopAbs_EDGE),
        ("vertex", TopAbs_VERTEX),
    ):
        delivered = TopTools_IndexedMapOfShape()
        owned = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape.wrapped, kind, delivered)
        for solid in solids:
            TopExp.MapShapes_s(solid.wrapped, kind, owned)
        difference = delivered.Extent() - owned.Extent()
        if difference:
            counts[name] = difference
    return counts

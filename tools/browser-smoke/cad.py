"""Generate STL inputs and check independently imported STEP B-reps in native OCCT."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

# (outer radius, bore radius, height), all in mm
CASES = {"spacer": (12, 4, 6), "pin": (7, 0, 15)}


def generate(directory):
    for name, (outer, bore, height) in CASES.items():
        profile = cq.Workplane("XY").circle(outer)
        if bore:
            profile = profile.circle(bore)
        source = profile.extrude(height)
        source.val().exportStl(str(directory / f"{name}.stl"), 0.03, 0.12)


def verify(name, path):
    outer, bore, height = CASES[name]
    model = cq.importers.importStep(str(path)).val()
    assert model.isValid() and len(model.Solids()) == 1, "Expected one valid solid"
    radii = []
    for face in model.Faces():
        if face.geomType() == "CYLINDER":
            radii.append(BRepAdaptor_Surface(face.wrapped).Cylinder().Radius())
    for radius in filter(None, (outer, bore)):
        assert any(abs(r - radius) < 0.1 for r in radii), (radius, radii)
    box = model.BoundingBox()
    dims = [box.xlen, box.ylen, box.zlen]
    for actual, expected in zip(dims, [2 * outer, 2 * outer, height], strict=True):
        assert abs(actual - expected) < 0.15, dims
    volume = model.Volume()
    expected = math.pi * (outer**2 - bore**2) * height
    assert abs(volume / expected - 1) < 0.01, (volume, expected)
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "volume_mm3": volume,
        "expected_mm3": expected,
        "dimensions_mm": dims,
        "cylinder_radii_mm": radii,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("generate", "verify"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.action == "generate":
        generate(args.directory)
    else:
        print(json.dumps([verify(n, args.directory / f"{n}.step") for n in CASES], indent=2))

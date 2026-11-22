"""Generate STL inputs and check independently imported STEP B-reps in native OCCT."""

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

# (outer radius, bore radius, height), all in mm
CASES = {"spacer": (12, 4, 6), "pin": (7, 0, 15)}


def verify_corpus(directory, sources, units):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.mesh import load_mesh
    from app.scoring import score_exported_shape
    from app.verification import acceptance_threshold, passes_geometry_gate

    results = []
    for source in sources:
        target = directory / f"{source.stem}.step"
        if not target.is_file():
            results.append({"file": source.name, "verified": False, "error": "No STEP downloaded"})
            continue
        job = directory / source.stem
        job.mkdir(exist_ok=True)
        (job / "reconstruction.step").write_bytes(target.read_bytes())
        try:
            mesh = load_mesh(source, source.name, units)
            score = score_exported_shape(mesh, job)
            model = cq.importers.importStep(str(target)).val()
            results.append({
                "file": source.name,
                "verified": passes_geometry_gate(score, acceptance_threshold(mesh.diagonal)),
                "triangles": mesh.report.triangle_count,
                "solids": len(model.Solids()),
                "shells": len(model.Shells()),
                "faces": dict(sorted(Counter(
                    face.geomType() for face in model.Faces()
                ).items())),
                "max_deviation_mm": score.local_max_mm,
                "volume_error_percent": score.volume_error_percent,
            })
        except Exception as error:
            results.append({"file": source.name, "verified": False, "error": str(error)})
    return results


def generate(directory):
    for name, (outer, bore, height) in CASES.items():
        profile = cq.Workplane("XY").circle(outer)
        if bore:
            profile = profile.circle(bore)
        source = profile.extrude(height)
        source.val().exportStl(str(directory / f"{name}.stl"), 0.03, 0.12)


def generate_extended(directory):
    import trimesh
    from trimesh.transformations import rotation_matrix

    directory.mkdir(parents=True, exist_ok=True)
    generate(directory)
    tilt = rotation_matrix(0.67, [1, 2, 3])
    tilt[:3, 3] = [17, -13, 31]
    spacer = trimesh.load_mesh(directory / "spacer.stl")
    spacer.apply_transform(tilt)
    spacer.export(directory / "tilted-spacer.stl")

    plate = trimesh.creation.box([20, 12, 3])
    separate = plate.copy()
    separate.apply_transform(tilt)
    separate.apply_translation([30, 0, 0])
    trimesh.util.concatenate([plate, separate]).export(directory / "two-separated-plates.stl")

    sphere = trimesh.creation.icosphere(subdivisions=2, radius=5)
    sphere.apply_translation([0, 80, 0])
    trimesh.util.concatenate([plate, sphere]).export(directory / "plate-and-sphere.stl")

    outer = trimesh.creation.box([20, 20, 20])
    cavity = trimesh.creation.box([6, 5, 4])
    cavity.invert()
    trimesh.util.concatenate([outer, cavity]).export(directory / "box-with-cavity.stl")
    plate.update_faces(list(range(1, len(plate.faces))))
    plate.export(directory / "open-plate.stl")


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
    parser.add_argument(
        "action", choices=("generate", "generate-extended", "verify", "verify-corpus"),
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("sources", nargs="*", type=Path)
    parser.add_argument("--units", default="mm", choices=("mm", "m", "cm", "in"))
    args = parser.parse_intermixed_args()
    if args.action == "generate":
        generate(args.directory)
    elif args.action == "generate-extended":
        generate_extended(args.directory)
    elif args.action == "verify":
        print(json.dumps([verify(n, args.directory / f"{n}.step") for n in CASES], indent=2))
    else:
        print(json.dumps(verify_corpus(args.directory, args.sources, args.units), indent=2))

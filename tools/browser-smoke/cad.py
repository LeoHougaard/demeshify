"""Generate STL inputs and check independently imported STEP B-reps in native OCCT."""

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from types import SimpleNamespace

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


@cache
def corpus_references(folder):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8-sig"))
    return {Path(case["stl"]).name: folder / case["source_step"] for case in manifest["cases"]}


def audit_references(directory, sources):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.benchmark_surface_corpus import (
        analytic_quality_metrics,
        is_clean_analytic_reconstruction,
        step_surface_metrics,
    )

    scores = {
        record["file"]: record for record in json.loads((directory / "geometry.json").read_text())
    }
    results = []
    for source in sources:
        result = {"file": source.name, "geometry_verified": scores[source.name]["verified"]}
        results.append(result)
        try:
            reference = step_surface_metrics(corpus_references(source.parent)[source.name])
            delivery = step_surface_metrics(directory / f"{source.stem}.step")
            quality = analytic_quality_metrics(reference, delivery)
            expected = reference["shape"]
            actual = delivery["shape"]
            matched_topology = len(expected.Solids()) == len(actual.Solids()) and (
                len(expected.Shells()) == len(actual.Shells())
            )
            result.update({
                "reference_faces": reference["face_counts"],
                "delivered_faces": delivery["face_counts"],
                "reference_topology_match": matched_topology,
                **quality,
                "strict_cad": None,
            })
            report_path = directory / f"{source.stem}.report.json"
            if report_path.is_file():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                surface = report.get("surface") or {}
                flags = SimpleNamespace(**{
                    key: surface.get(key, default) for key, default in (
                        ("faceted_fallback", False), ("source_mesh_fallback", False),
                        ("faceted_patch_count", 0), ("faceted_face_count", 0),
                    )
                })
                result["faceted_fallback"] = bool(
                    flags.faceted_fallback or flags.source_mesh_fallback
                )
                clean = is_clean_analytic_reconstruction(
                    flags if surface else None, quality, report.get("warnings", []),
                )
                result["strict_cad"] = result["geometry_verified"] and matched_topology and clean
        except Exception as error:
            result["error"] = str(error)
    return results


def generate(directory):
    for name, (outer, bore, height) in CASES.items():
        profile = cq.Workplane("XY").circle(outer)
        if bore:
            profile = profile.circle(bore)
        source = profile.extrude(height)
        source.val().exportStl(str(directory / f"{name}.stl"), 0.03, 0.12)


def generate_chamfered_slot(directory):
    stock = cq.Workplane("XY").rect(62, 36).extrude(11).edges("|Z").fillet(3)
    slot = cq.Workplane("XY").center(-10, 0).slot2D(18, 6).extrude(11)
    stock = stock.cut(slot).faces(">Z or <Z").edges().chamfer(0.4)
    for y in (-8, 8):
        bore = cq.Solid.makeCylinder(2, 11, (16, y, 0))
        lower = cq.Solid.makeCone(2.8, 2, 0.8, (16, y, 0))
        upper = cq.Solid.makeCone(2, 3.5, 1.5, (16, y, 9.5))
        stock = stock.cut(bore.fuse(lower, upper))
    stock.val().exportStep(str(directory / "chamfered-slot.step"))
    stock.val().exportStl(str(directory / "chamfered-slot.stl"), 0.02, 0.08)


def generate_extended(directory):
    import trimesh
    from trimesh.transformations import rotation_matrix

    directory.mkdir(parents=True, exist_ok=True)
    generate(directory)
    generate_chamfered_slot(directory)
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
        "action",
        choices=("generate", "generate-extended", "verify", "verify-corpus", "audit-references"),
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
    elif args.action == "verify-corpus":
        print(json.dumps(verify_corpus(args.directory, args.sources, args.units), indent=2))
    else:
        print(json.dumps(audit_references(args.directory, args.sources), indent=2))

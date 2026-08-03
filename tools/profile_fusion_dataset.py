from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cadquery as cq
import numpy as np
import trimesh

SEGMENT_NAMES = (
    "ExtrudeSide",
    "ExtrudeEnd",
    "CutSide",
    "CutEnd",
    "Fillet",
    "Chamfer",
    "RevolveSide",
    "RevolveEnd",
)


def _official_splits(root: Path) -> dict[str, str]:
    split_path = root / "train_test.json"
    if not split_path.is_file():
        return {}
    split_data = json.loads(split_path.read_text(encoding="utf-8"))
    return {
        identifier: split
        for split, identifiers in split_data.items()
        for identifier in identifiers
    }


def _feature_family(feature_counts: Counter[str]) -> str:
    extrudes = feature_counts["ExtrudeFeature"]
    fillets = feature_counts["FilletFeature"]
    chamfers = feature_counts["ChamferFeature"]
    revolves = feature_counts["RevolveFeature"]
    if revolves and extrudes:
        return "revolve_mixed"
    if revolves:
        return "revolve"
    if fillets and chamfers:
        return "fillet_chamfer"
    if fillets:
        return "fillet"
    if chamfers:
        return "chamfer"
    if extrudes >= 2:
        return "multi_extrude"
    if extrudes == 1:
        return "single_extrude"
    return "other"


def _complexity_tier(feature_count: int, face_count: int) -> str:
    if feature_count <= 2 and face_count <= 20:
        return "simple"
    if feature_count <= 6 and face_count <= 80:
        return "medium"
    return "complex"


def metadata_record(path: Path, split: str = "unknown") -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    features = list(data.get("features", {}).values())
    feature_counts = Counter(feature.get("type", "UnknownFeature") for feature in features)
    operation_counts = Counter(
        feature["operation"] for feature in features if feature.get("operation")
    )
    properties = data.get("body", {}).get("properties", {})
    face_count = int(properties.get("face_count", len(data.get("body", {}).get("faces", []))))
    feature_count = len(features)
    return {
        "id": path.stem,
        "split": split,
        "component_name": data.get("metadata", {}).get("component_name", ""),
        "feature_count": feature_count,
        "feature_type_counts": dict(sorted(feature_counts.items())),
        "operation_counts": dict(sorted(operation_counts.items())),
        "face_count": face_count,
        "edge_count": int(properties.get("edge_count", 0)),
        "shell_count": int(properties.get("shell_count", 0)),
        "family": _feature_family(feature_counts),
        "complexity": _complexity_tier(feature_count, face_count),
    }


def scan_metadata(root: Path) -> list[dict[str, Any]]:
    splits = _official_splits(root)
    return [
        metadata_record(path, splits.get(path.stem, "unknown"))
        for path in sorted((root / "timeline_info").glob("*.json"))
    ]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    feature_types: Counter[str] = Counter()
    operations: Counter[str] = Counter()
    families: Counter[str] = Counter()
    complexities: Counter[str] = Counter()
    splits: Counter[str] = Counter()
    for record in records:
        feature_types.update(record["feature_type_counts"])
        operations.update(record["operation_counts"])
        families[record["family"]] += 1
        complexities[record["complexity"]] += 1
        splits[record["split"]] += 1
    return {
        "model_count": len(records),
        "feature_type_counts": dict(feature_types.most_common()),
        "operation_counts": dict(operations.most_common()),
        "family_counts": dict(families.most_common()),
        "complexity_counts": dict(complexities.most_common()),
        "split_counts": dict(splits.most_common()),
    }


def _selection_key(identifier: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{identifier}".encode()).hexdigest()


def stratified_sample(
    records: list[dict[str, Any]],
    size: int,
    split: str,
    seed: int,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if split == "all" or record["split"] == split:
            buckets[(record["family"], record["complexity"])].append(record)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: _selection_key(item["id"], seed))

    selected: list[dict[str, Any]] = []
    keys = sorted(buckets)
    cursor = 0
    while keys and len(selected) < size:
        key = keys[cursor % len(keys)]
        bucket = buckets[key]
        selected.append(bucket.pop())
        if not bucket:
            keys.remove(key)
            cursor = 0
        else:
            cursor += 1
    return selected


def _shape_geometry(step_path: Path) -> tuple[cq.Workplane, dict[str, Any]]:
    model = cq.importers.importStep(str(step_path))
    if not model.vals():
        raise ValueError("STEP file contains no importable shape")
    shape = model.val()
    face_types = Counter(face.geomType() for face in shape.Faces())
    edge_types = Counter(edge.geomType() for edge in shape.Edges())
    bounds = shape.BoundingBox()
    planarity_tolerance = max(
        (bounds.xlen**2 + bounds.ylen**2 + bounds.zlen**2) ** 0.5 * 1e-6,
        1e-5,
    )
    effectively_planar_bspline_count = 0
    for face in shape.Faces():
        if face.geomType() != "BSPLINE":
            continue
        vertices, _ = face.tessellate(planarity_tolerance * 10)
        points = np.asarray([[vertex.x, vertex.y, vertex.z] for vertex in vertices])
        if len(points) < 3:
            effectively_planar_bspline_count += 1
            continue
        centered = points - np.mean(points, axis=0)
        _, _, vectors = np.linalg.svd(centered, full_matrices=False)
        residual = float(np.max(np.abs(centered @ vectors[-1])))
        if residual <= planarity_tolerance:
            effectively_planar_bspline_count += 1
    return model, {
        "solid_count": len(shape.Solids()),
        "valid_brep": bool(shape.isValid()),
        "volume": float(shape.Volume()),
        "dimensions": [float(bounds.xlen), float(bounds.ylen), float(bounds.zlen)],
        "brep_face_type_counts": dict(face_types.most_common()),
        "brep_edge_type_counts": dict(edge_types.most_common()),
        "effectively_planar_bspline_count": effectively_planar_bspline_count,
        "analytic_curved_face_count": sum(
            count for kind, count in face_types.items() if kind != "PLANE"
        ),
        "analytic_curved_edge_count": sum(
            count for kind, count in edge_types.items() if kind != "LINE"
        ),
    }


def _segmentation_counts(path: Path) -> dict[str, int]:
    labels = Counter(
        int(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    return {
        SEGMENT_NAMES[index]: count
        for index, count in sorted(labels.items())
        if 0 <= index < len(SEGMENT_NAMES)
    }


def materialize(
    root: Path,
    selected: list[dict[str, Any]],
    output: Path,
    tolerance: float,
    angular_tolerance: float,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, record in enumerate(selected, start=1):
        identifier = record["id"]
        step_path = root / "breps" / "step" / f"{identifier}.stp"
        seg_path = root / "breps" / "seg" / f"{identifier}.seg"
        try:
            model, geometry = _shape_geometry(step_path)
            if geometry["solid_count"] != 1:
                raise ValueError(f"expected one solid, found {geometry['solid_count']}")
            stl_name = f"{identifier}.stl"
            stl_path = output / stl_name
            cq.exporters.export(
                model,
                str(stl_path),
                tolerance=tolerance,
                angularTolerance=angular_tolerance,
            )
            mesh = trimesh.load(stl_path, force="mesh", process=True)
            ground_truth = {
                **record,
                **geometry,
                "segmentation_face_counts": _segmentation_counts(seg_path),
            }
            cases.append(
                {
                    "id": identifier,
                    "source_step": str(step_path.resolve()),
                    "source_license": (
                        "Fusion 360 Gallery Dataset License "
                        "(non-commercial research)"
                    ),
                    "stl": stl_name,
                    "tessellation_tolerance_mm": tolerance,
                    "angular_tolerance_rad": angular_tolerance,
                    "triangles": int(len(mesh.faces)),
                    "watertight": bool(mesh.is_watertight),
                    "ground_truth": ground_truth,
                }
            )
            print(
                f"[{index}/{len(selected)}] {identifier}: "
                f"{record['family']}/{record['complexity']}"
            )
        except Exception as exc:
            failures.append({"id": identifier, "error": str(exc)})
            print(f"[{index}/{len(selected)}] {identifier}: failed: {exc}")

    manifest = {
        "format_version": 2,
        "dataset": "Fusion 360 Gallery Extended STEP s2.0.1",
        "license": "non-commercial research; see dataset source LICENSE.md",
        "selection": {
            "split": selected[0]["split"] if selected else None,
            "stratified_by": ["family", "complexity"],
        },
        "case_count": len(cases),
        "failure_count": len(failures),
        "cases": cases,
        "failures": failures,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile and materialize a stratified Fusion 360 Gallery benchmark."
    )
    parser.add_argument("root", type=Path, help="Extracted s2.0.1_extended_step directory")
    parser.add_argument("--output", type=Path, default=Path("datasets/profiles/fusion_s2.0.1.json"))
    parser.add_argument("--sample-size", type=int, default=0)
    parser.add_argument("--sample-split", choices=("train", "test", "all"), default="test")
    parser.add_argument("--seed", type=int, default=360)
    parser.add_argument("--materialize", type=Path)
    parser.add_argument(
        "--refresh-manifest",
        type=Path,
        help="Refresh STEP-derived ground-truth geometry without re-exporting STL files.",
    )
    parser.add_argument("--tolerance", type=float, default=0.02)
    parser.add_argument("--angular-tolerance", type=float, default=0.06)
    arguments = parser.parse_args()

    if arguments.refresh_manifest is not None:
        manifest = json.loads(arguments.refresh_manifest.read_text(encoding="utf-8"))
        for index, case in enumerate(manifest["cases"], start=1):
            _, geometry = _shape_geometry(Path(case["source_step"]))
            case["ground_truth"].update(geometry)
            print(f"[{index}/{len(manifest['cases'])}] refreshed {case['id']}")
        arguments.refresh_manifest.write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        return

    records = scan_metadata(arguments.root)
    profile = {
        "format_version": 1,
        "dataset": "Fusion 360 Gallery Extended STEP s2.0.1",
        "summary": summarize(records),
        "records": records,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(json.dumps(profile["summary"], indent=2))

    if arguments.sample_size:
        selected = stratified_sample(
            records,
            arguments.sample_size,
            arguments.sample_split,
            arguments.seed,
        )
        if arguments.materialize is None:
            raise ValueError("--materialize is required when --sample-size is non-zero")
        manifest = materialize(
            arguments.root,
            selected,
            arguments.materialize,
            arguments.tolerance,
            arguments.angular_tolerance,
        )
        print(
            f"Materialized {manifest['case_count']} cases with "
            f"{manifest['failure_count']} failures"
        )


if __name__ == "__main__":
    main()

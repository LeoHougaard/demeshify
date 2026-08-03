from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import cadquery as cq
import trimesh
from profile_fusion_dataset import _segmentation_counts

MANUAL_SCOPE_OVERRIDES = {
    "106991_26a8f869_0": (
        "The benchmark STL is non-watertight and STEP inspection shows four "
        "genuine spatial B-spline faces plus ellipse edges in a blended "
        "intersection. Damaged/open meshes and free-form spatial blends are "
        "outside the verified watertight line/arc mechanical scope."
    ),
    "109857_1d24326e_22": (
        "The benchmark STL is non-watertight, and manual STEP review shows a "
        "fully curved blade-like body whose only four faces are two genuine "
        "spatial B-splines and two cylinders. Its source timeline contains "
        "only fillet operations and no recoverable line/arc base profile; "
        "damaged/open free-form bodies are outside the verified watertight "
        "line/arc mechanical scope."
    ),
}

IN_SCOPE_FAMILIES = {
    "single_extrude",
    "multi_extrude",
    "fillet",
    "chamfer",
    "fillet_chamfer",
}
REVOLVE_FAMILIES = {"revolve", "revolve_mixed"}


def _shape_geometry_fast(step_path: Path) -> tuple[cq.Workplane, dict[str, Any]]:
    """Read exact STEP topology without tessellating B-spline faces.

    The general dataset profiler estimates whether B-spline faces are planar by
    tessellating every such face.  That diagnostic is unnecessary for this
    scope audit and is prohibitively expensive for decorative spline models.
    """
    model = cq.importers.importStep(str(step_path))
    if not model.vals():
        raise ValueError("STEP file contains no importable shape")
    shape = model.val()
    face_types = Counter(face.geomType() for face in shape.Faces())
    edge_types = Counter(edge.geomType() for edge in shape.Edges())
    bounds = shape.BoundingBox()
    return model, {
        "solid_count": len(shape.Solids()),
        "valid_brep": bool(shape.isValid()),
        "volume": float(shape.Volume()),
        "dimensions": [float(bounds.xlen), float(bounds.ylen), float(bounds.zlen)],
        "brep_face_type_counts": dict(face_types.most_common()),
        "brep_edge_type_counts": dict(edge_types.most_common()),
        "effectively_planar_bspline_count": None,
        "analytic_curved_face_count": sum(
            count for kind, count in face_types.items() if kind != "PLANE"
        ),
        "analytic_curved_edge_count": sum(
            count for kind, count in edge_types.items() if kind != "LINE"
        ),
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.1)


def _spline_scope_reason(
    record: dict[str, Any],
    geometry: dict[str, Any],
) -> str | None:
    bspline_faces = int(geometry["brep_face_type_counts"].get("BSPLINE", 0))
    bspline_edges = int(geometry["brep_edge_type_counts"].get("BSPLINE", 0))
    fillet_count = int(record["feature_type_counts"].get("FilletFeature", 0))
    if bspline_faces and not fillet_count:
        return (
            "STEP inspection found an extrusion-family timeline with "
            f"{bspline_faces} genuine B-spline faces and {bspline_edges} "
            "B-spline edges but no fillet feature; this is a spline-profile "
            "or decorative surface outside the declared line/arc mechanical "
            "scope."
        )
    return None


def _materialize_one(
    record: dict[str, Any],
    root_value: str,
    output_value: str,
    tolerance: float,
    angular_tolerance: float,
) -> dict[str, Any]:
    root = Path(root_value)
    output = Path(output_value)
    identifier = str(record["id"])
    step_path = root / "breps" / "step" / f"{identifier}.stp"
    seg_path = root / "breps" / "seg" / f"{identifier}.seg"
    try:
        model, geometry = _shape_geometry_fast(step_path)
        ground_truth = {
            **record,
            **geometry,
            "segmentation_face_counts": _segmentation_counts(seg_path),
        }
        if geometry["solid_count"] != 1:
            return {
                "kind": "excluded",
                "id": identifier,
                "family": record["family"],
                "reason": (
                    "STEP inspection found "
                    f"{geometry['solid_count']} solids; the declared scope "
                    "requires one connected solid."
                ),
                "ground_truth": ground_truth,
            }
        if not geometry["valid_brep"]:
            return {
                "kind": "excluded",
                "id": identifier,
                "family": record["family"],
                "reason": (
                    "The source STEP is not a valid B-rep; the declared scope "
                    "requires a valid source solid."
                ),
                "ground_truth": ground_truth,
            }
        spline_reason = _spline_scope_reason(record, geometry)
        if spline_reason:
            return {
                "kind": "excluded",
                "id": identifier,
                "family": record["family"],
                "reason": spline_reason,
                "ground_truth": ground_truth,
            }

        stl_name = f"{identifier}.stl"
        stl_path = output / stl_name
        if not stl_path.is_file():
            cq.exporters.export(
                model,
                str(stl_path),
                tolerance=tolerance,
                angularTolerance=angular_tolerance,
            )
        mesh = trimesh.load(stl_path, force="mesh", process=True)
        if override_reason := MANUAL_SCOPE_OVERRIDES.get(identifier):
            ground_truth["scope_override"] = "out_of_scope"
            ground_truth["scope_override_reason"] = override_reason
        return {
            "kind": "case",
            "id": identifier,
            "source_step": str(step_path.resolve()),
            "source_license": (
                "Fusion 360 Gallery Dataset License (non-commercial research)"
            ),
            "stl": stl_name,
            "tessellation_tolerance_mm": tolerance,
            "angular_tolerance_rad": angular_tolerance,
            "triangles": int(len(mesh.faces)),
            "watertight": bool(mesh.is_watertight),
            "ground_truth": ground_truth,
        }
    except Exception as exc:
        return {
            "kind": "failure",
            "id": identifier,
            "family": record["family"],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _manifest(
    records: list[dict[str, Any]],
    timeline_exclusions: list[dict[str, Any]],
    completed: dict[str, dict[str, Any]],
    failures: dict[str, dict[str, Any]],
    limit: int | None,
) -> dict[str, Any]:
    ordered = [completed[record["id"]] for record in records if record["id"] in completed]
    cases = [item for item in ordered if item["kind"] == "case"]
    geometry_exclusions = [
        item for item in ordered if item["kind"] == "excluded"
    ]
    return {
        "format_version": 2,
        "dataset": "Fusion 360 Gallery Extended STEP s2.0.1",
        "license": "non-commercial research; see dataset source LICENSE.md",
        "selection": {
            "split": "test",
            "timeline_families": sorted(IN_SCOPE_FAMILIES),
            "limit": limit,
            "scope": (
                "single-solid extrusion-family mechanical parts with line/arc "
                "profiles and optional fillets/chamfers"
            ),
        },
        "official_test_model_count": len(records) + len(timeline_exclusions),
        "timeline_candidate_count": len(records),
        "processed_candidate_count": len(ordered),
        "case_count": len(cases),
        "geometry_exclusion_count": len(geometry_exclusions),
        "timeline_exclusion_count": len(timeline_exclusions),
        "failure_count": len(failures),
        "cases": cases,
        "geometry_exclusions": geometry_exclusions,
        "timeline_exclusions": timeline_exclusions,
        "failures": [
            failures[record["id"]]
            for record in records
            if record["id"] in failures
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize every in-scope Fusion 360 official-test candidate "
            "with resumable parallel STEP inspection and STL export."
        )
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tolerance", type=float, default=0.02)
    parser.add_argument("--angular-tolerance", type=float, default=0.06)
    parser.add_argument("--no-resume", action="store_true")
    arguments = parser.parse_args()

    profile = json.loads(arguments.profile.read_text(encoding="utf-8"))
    test_records = [
        record for record in profile["records"] if record["split"] == "test"
    ]
    records = [
        record
        for record in test_records
        if record["family"] in IN_SCOPE_FAMILIES
    ]
    records.sort(key=lambda item: item["id"])
    timeline_exclusions = [
        {
            "id": record["id"],
            "family": record["family"],
            "reason": (
                "Source timeline contains revolve operations; the declared "
                "scope is extrusion-dominant mechanical parts."
            ),
        }
        for record in sorted(test_records, key=lambda item: item["id"])
        if record["family"] in REVOLVE_FAMILIES
    ]
    if arguments.limit is not None:
        records = records[: arguments.limit]
        timeline_exclusions = []

    arguments.output.mkdir(parents=True, exist_ok=True)
    manifest_path = arguments.output / "manifest.json"
    completed: dict[str, dict[str, Any]] = {}
    failures: dict[str, dict[str, Any]] = {}
    if manifest_path.is_file() and not arguments.no_resume:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in [
            *previous.get("cases", []),
            *previous.get("geometry_exclusions", []),
        ]:
            completed[str(item["id"])] = item
        failures = {
            str(item["id"]): item for item in previous.get("failures", [])
        }

    pending = [record for record in records if record["id"] not in completed]
    total = len(records)
    print(
        f"Official test: {len(test_records)} total, {len(records)} timeline "
        f"candidates, {len(timeline_exclusions)} revolve exclusions, "
        f"{len(pending)} pending."
    )
    with ProcessPoolExecutor(max_workers=max(1, arguments.workers)) as executor:
        futures = {
            executor.submit(
                _materialize_one,
                record,
                str(arguments.dataset_root.resolve()),
                str(arguments.output.resolve()),
                arguments.tolerance,
                arguments.angular_tolerance,
            ): record
            for record in pending
        }
        for completed_count, future in enumerate(as_completed(futures), start=1):
            record = futures[future]
            try:
                item = future.result()
            except Exception as exc:
                item = {
                    "kind": "failure",
                    "id": record["id"],
                    "family": record["family"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            identifier = str(item["id"])
            if item["kind"] == "failure":
                failures[identifier] = item
            else:
                completed[identifier] = item
                failures.pop(identifier, None)
            current = _manifest(
                records,
                timeline_exclusions,
                completed,
                failures,
                arguments.limit,
            )
            _atomic_json(manifest_path, current)
            done = len(completed)
            print(
                f"[{done}/{total}] {identifier}: {item['kind']} "
                f"(batch completion {completed_count}/{len(pending)})",
                flush=True,
            )

    final = _manifest(
        records,
        timeline_exclusions,
        completed,
        failures,
        arguments.limit,
    )
    _atomic_json(manifest_path, final)
    print(
        f"Materialized {final['case_count']} cases; excluded "
        f"{final['geometry_exclusion_count']} by geometry and "
        f"{final['timeline_exclusion_count']} by timeline; "
        f"{final['failure_count']} inspection failures."
    )


if __name__ == "__main__":
    main()

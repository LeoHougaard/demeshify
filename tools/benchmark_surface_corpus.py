from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cadquery as cq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.surface_reconstruction import reconstruct_surfaces  # noqa: E402
from tools.analyze_surface_failures import (  # noqa: E402
    analyze_results,
    markdown_report,
)

ANALYTIC_CURVED_TYPES = {"CYLINDER", "CONE", "SPHERE", "TORUS"}


def case_has_analytic_curves(case: dict[str, object]) -> bool:
    ground_truth = case.get("ground_truth", {})
    counts = (
        ground_truth.get("brep_face_type_counts", {})
        if isinstance(ground_truth, dict)
        else {}
    )
    return bool(
        isinstance(counts, dict)
        and any(int(counts.get(kind, 0) or 0) > 0 for kind in ANALYTIC_CURVED_TYPES)
    )


def step_surface_metrics(path: Path) -> dict[str, object]:
    imported = cq.importers.importStep(str(path)).val()
    face_counts: Counter[str] = Counter()
    face_areas: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    edge_lengths: Counter[str] = Counter()
    for face in imported.Faces():
        kind = str(face.geomType())
        face_counts[kind] += 1
        face_areas[kind] += abs(float(face.Area()))
    for edge in imported.Edges():
        kind = str(edge.geomType())
        edge_counts[kind] += 1
        edge_lengths[kind] += abs(float(edge.Length()))
    return {
        "shape": imported,
        "face_counts": dict(face_counts),
        "face_areas_mm2": dict(face_areas),
        "edge_counts": dict(edge_counts),
        "edge_lengths_mm": dict(edge_lengths),
    }


def analytic_quality_metrics(
    source: dict[str, object],
    output: dict[str, object],
) -> dict[str, object]:
    source_areas = source.get("face_areas_mm2", {})
    output_areas = output.get("face_areas_mm2", {})
    source_counts = source.get("face_counts", {})
    output_counts = output.get("face_counts", {})
    assert isinstance(source_areas, dict)
    assert isinstance(output_areas, dict)
    assert isinstance(source_counts, dict)
    assert isinstance(output_counts, dict)
    expected_types = sorted(
        kind
        for kind in ANALYTIC_CURVED_TYPES
        if float(source_areas.get(kind, 0.0) or 0.0) > 1e-9
    )
    area_recall_by_type = {
        kind: min(
            float(output_areas.get(kind, 0.0) or 0.0)
            / float(source_areas[kind]),
            1.0,
        )
        for kind in expected_types
    }
    area_precision_by_type = {
        kind: min(
            float(source_areas[kind])
            / max(float(output_areas.get(kind, 0.0) or 0.0), 1e-15),
            1.0,
        )
        for kind in expected_types
    }
    missing_types = [
        kind
        for kind in expected_types
        if float(output_areas.get(kind, 0.0) or 0.0) <= 1e-9
    ]
    source_edge_lengths = source.get("edge_lengths_mm", {})
    output_edge_lengths = output.get("edge_lengths_mm", {})
    assert isinstance(source_edge_lengths, dict)
    assert isinstance(output_edge_lengths, dict)
    source_circle_length = float(source_edge_lengths.get("CIRCLE", 0.0) or 0.0)
    output_circle_length = float(output_edge_lengths.get("CIRCLE", 0.0) or 0.0)
    # Exact swept surfaces are analytic CAD geometry too. OCCT may preserve a
    # source profile sweep as EXTRUSION/REVOLUTION even when another STEP
    # writer encoded the same support as a B-spline. Only generic residual
    # surfaces should fail the representation-quality gate.
    analytic_types = {
        "PLANE",
        "EXTRUSION",
        "REVOLUTION",
        *ANALYTIC_CURVED_TYPES,
    }
    source_nonanalytic = {
        kind for kind, count in source_counts.items()
        if kind not in analytic_types and int(count or 0) > 0
    }
    unexpected_nonanalytic = sorted(
        kind
        for kind, count in output_counts.items()
        if kind not in analytic_types
        and kind not in source_nonanalytic
        and int(count or 0) > 0
    )
    return {
        "expected_analytic_surface_types": expected_types,
        "missing_analytic_surface_types": missing_types,
        "analytic_area_recall_by_type": area_recall_by_type,
        "analytic_area_precision_by_type": area_precision_by_type,
        "minimum_analytic_area_recall": min(area_recall_by_type.values(), default=1.0),
        "minimum_analytic_area_precision": min(
            area_precision_by_type.values(),
            default=1.0,
        ),
        "circular_edge_length_recall": (
            min(output_circle_length / source_circle_length, 1.0)
            if source_circle_length > 1e-9
            else 1.0
        ),
        "unexpected_nonanalytic_surface_types": unexpected_nonanalytic,
    }


def is_clean_analytic_reconstruction(
    surface: object | None,
    analytic_metrics: dict[str, object],
    warnings: list[str],
) -> bool:
    """Reject geometric lookalikes that used a fallback representation."""

    warning_text = " ".join(warnings).lower()
    return bool(
        surface
        and not surface.faceted_fallback
        and not surface.source_mesh_fallback
        and surface.faceted_patch_count == 0
        and surface.faceted_face_count == 0
        and analytic_metrics["minimum_analytic_area_recall"] >= 0.95
        and analytic_metrics["minimum_analytic_area_precision"] >= 0.95
        and not analytic_metrics["missing_analytic_surface_types"]
        and not analytic_metrics["unexpected_nonanalytic_surface_types"]
        and "analytic trimming failed" not in warning_text
        and "source-mesh" not in warning_text
        and "c0 surfaces" not in warning_text
    )


def benchmark_summary(results: list[dict[str, object]]) -> dict[str, object]:
    total = len(results)
    statuses = Counter(str(result.get("status", "unknown")) for result in results)
    closed = sum(bool(result.get("closed")) for result in results)
    valid_brep = sum(bool(result.get("valid_brep")) for result in results)
    step_roundtrip = sum(bool(result.get("step_roundtrip_valid")) for result in results)
    accepted = sum(bool(result.get("accepted")) for result in results)
    geometric_accepted = sum(
        bool(result.get("geometric_accepted")) for result in results
    )
    clean_analytic = sum(bool(result.get("clean_analytic")) for result in results)
    free_edges = [
        int(result["free_edge_count"])
        for result in results
        if isinstance(result.get("free_edge_count"), int)
    ]
    return {
        "case_count": total,
        "status_counts": dict(statuses),
        "valid_brep_rate": valid_brep / max(total, 1),
        "step_roundtrip_rate": step_roundtrip / max(total, 1),
        "watertight_rate": closed / max(total, 1),
        "accepted_rate": accepted / max(total, 1),
        "geometric_accepted_rate": geometric_accepted / max(total, 1),
        "clean_analytic_rate": clean_analytic / max(total, 1),
        "total_free_edges": sum(free_edges),
        "maximum_free_edges": max(free_edges, default=0),
    }


def benchmark_case(
    case: dict[str, object],
    manifest_parent: Path,
    artifacts_root: Path | None = None,
) -> dict[str, object]:
    source = manifest_parent / str(case["stl"])
    ground_truth = case.get("ground_truth", {})
    with tempfile.TemporaryDirectory(prefix="meshmind-surface-benchmark-") as temporary:
        working = Path(temporary)
        input_path = working / "input.stl"
        shutil.copy2(source, input_path)
        report = reconstruct_surfaces(
            "surfacebench",
            input_path,
            source.name,
        )
        step_path = working / "reconstruction.step"
        step_roundtrip_valid = False
        step_roundtrip_solids = 0
        output_metrics: dict[str, object] = {}
        if step_path.is_file():
            output_metrics = step_surface_metrics(step_path)
            imported = output_metrics["shape"]
            step_roundtrip_valid = bool(imported.isValid() and imported.Faces())
            step_roundtrip_solids = len(imported.Solids())

        source_metrics: dict[str, object] = {}
        source_step = case.get("source_step")
        if source_step and Path(str(source_step)).is_file():
            source_metrics = step_surface_metrics(Path(str(source_step)))
        analytic_metrics = (
            analytic_quality_metrics(source_metrics, output_metrics)
            if source_metrics and output_metrics
            else {
                "expected_analytic_surface_types": [],
                "missing_analytic_surface_types": [],
                "analytic_area_recall_by_type": {},
                "analytic_area_precision_by_type": {},
                "minimum_analytic_area_recall": 1.0,
                "minimum_analytic_area_precision": 1.0,
                "circular_edge_length_recall": 1.0,
                "unexpected_nonanalytic_surface_types": [],
            }
        )

        surface = report.surface
        score = report.score
        diagonal = sum(value * value for value in report.mesh.dimensions_mm) ** 0.5
        p95_limit = max(0.12, diagonal * 0.003)
        geometric_accepted = bool(
            surface
            and score
            and surface.closed
            and surface.free_edge_count == 0
            and score.valid_brep
            and score.valid_solid
            and score.chamfer_p95_mm <= p95_limit
            and step_roundtrip_valid
            and step_roundtrip_solids > 0
        )
        clean_analytic = is_clean_analytic_reconstruction(
            surface,
            analytic_metrics,
            report.warnings,
        )
        accepted = geometric_accepted and clean_analytic and report.status == "complete"
        result = {
            "id": case["id"],
            "family": (
                ground_truth.get("family", "unknown")
                if isinstance(ground_truth, dict)
                else "unknown"
            ),
            "complexity": (
                ground_truth.get("complexity", "unknown")
                if isinstance(ground_truth, dict)
                else "unknown"
            ),
            "triangles": report.mesh.triangle_count,
            "status": report.status,
            "recognized_surface_count": (
                surface.recognized_surface_count if surface else 0
            ),
            "surface_counts": surface.surface_counts if surface else {},
            "point_fitted_face_count": (
                surface.point_fitted_face_count if surface else 0
            ),
            "faceted_fallback": bool(surface and surface.faceted_fallback),
            "source_mesh_fallback": bool(surface and surface.source_mesh_fallback),
            "faceted_patch_count": surface.faceted_patch_count if surface else 0,
            "faceted_face_count": surface.faceted_face_count if surface else 0,
            "canonical_vertex_count": (
                surface.topology_vertex_count if surface else 0
            ),
            "canonical_edge_count": surface.topology_edge_count if surface else 0,
            "free_edge_count": surface.free_edge_count if surface else None,
            "closed": bool(surface and surface.closed),
            "valid_brep": bool(score and score.valid_brep),
            "valid_solid": bool(score and score.valid_solid),
            "p95_mm": score.chamfer_p95_mm if score else None,
            "maximum_mm": score.chamfer_max_mm if score else None,
            "p95_limit_mm": p95_limit,
            "step_roundtrip_valid": step_roundtrip_valid,
            "step_roundtrip_solids": step_roundtrip_solids,
            "output_face_type_counts": output_metrics.get("face_counts", {}),
            "output_face_type_areas_mm2": output_metrics.get(
                "face_areas_mm2",
                {},
            ),
            "output_edge_type_counts": output_metrics.get("edge_counts", {}),
            **analytic_metrics,
            "geometric_accepted": geometric_accepted,
            "clean_analytic": clean_analytic,
            "accepted": accepted,
            "warnings": report.warnings,
        }
        if artifacts_root is not None and not accepted:
            case_directory = artifacts_root / str(case["id"])
            case_directory.mkdir(parents=True, exist_ok=True)
            for name in (
                "input.stl",
                "reconstruction.stl",
                "reconstruction.step",
                "surface_graph.json",
                "report.json",
            ):
                artifact = working / name
                if artifact.is_file():
                    shutil.copy2(artifact, case_directory / name)
            (case_directory / "benchmark_result.json").write_text(
                json.dumps(result, indent=2),
                encoding="utf-8",
            )
        return result


def _worker(
    manifest_path: Path,
    identifier: str,
    output_path: Path,
    artifacts_root: Path | None = None,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    case = next(case for case in manifest["cases"] if str(case["id"]) == identifier)
    try:
        result = benchmark_case(case, manifest_path.parent, artifacts_root)
    except Exception as exc:
        ground_truth = case.get("ground_truth", {})
        result = {
            "id": identifier,
            "family": (
                ground_truth.get("family", "unknown")
                if isinstance(ground_truth, dict)
                else "unknown"
            ),
            "complexity": (
                ground_truth.get("complexity", "unknown")
                if isinstance(ground_truth, dict)
                else "unknown"
            ),
            "status": "crashed",
            "error": f"{type(exc).__name__}: {exc}",
            "geometric_accepted": False,
            "clean_analytic": False,
            "accepted": False,
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _bounded_case(
    case: dict[str, object],
    manifest_path: Path,
    cache_directory: Path,
    timeout: int,
    resume: bool,
    artifacts_root: Path | None = None,
) -> dict[str, object]:
    identifier = str(case["id"])
    cache_path = cache_directory / f"{identifier}.json"
    if not (resume and cache_path.is_file()):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            str(manifest_path.resolve()),
            "--worker-id",
            identifier,
            "--worker-output",
            str(cache_path.resolve()),
        ]
        if artifacts_root is not None:
            command.extend(["--artifacts-root", str(artifacts_root.resolve())])
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
        except subprocess.TimeoutExpired:
            cache_path.write_text(
                json.dumps(
                    {
                        "id": identifier,
                        "family": (
                            case.get("ground_truth", {}).get("family", "unknown")
                            if isinstance(case.get("ground_truth"), dict)
                            else "unknown"
                        ),
                        "status": "timeout",
                        "error": f"Exceeded {timeout} seconds",
                        "geometric_accepted": False,
                        "clean_analytic": False,
                        "accepted": False,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except subprocess.CalledProcessError as exc:
            cache_path.write_text(
                json.dumps(
                    {
                        "id": identifier,
                        "family": (
                            case.get("ground_truth", {}).get("family", "unknown")
                            if isinstance(case.get("ground_truth"), dict)
                            else "unknown"
                        ),
                        "status": "crashed",
                        "error": (exc.stderr or str(exc))[-4000:],
                        "geometric_accepted": False,
                        "clean_analytic": False,
                        "accepted": False,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    return json.loads(cache_path.read_text(encoding="utf-8"))


def _write_output(path: Path, results: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"summary": benchmark_summary(results), "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )
    failures = analyze_results(results, path.name)
    failure_json = path.with_name(f"{path.stem}_failures.json")
    failure_markdown = path.with_name(f"{path.stem}_failures.md")
    failure_json.write_text(json.dumps(failures, indent=2), encoding="utf-8")
    failure_markdown.write_text(markdown_report(failures), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark fitted-surface STL-to-STEP reconstruction.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/surface_latest.json"),
    )
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--max-triangles",
        type=int,
        default=0,
        help="Skip cases above this source triangle count (0 keeps all cases).",
    )
    parser.add_argument("--case-timeout", type=int, default=180)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--curved-only", action="store_true")
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--worker-id", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    if arguments.worker_id:
        if arguments.worker_output is None:
            parser.error("--worker-output is required with --worker-id")
        _worker(
            arguments.manifest,
            arguments.worker_id,
            arguments.worker_output,
            arguments.artifacts_root,
        )
        return

    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    identifiers = set(arguments.id)
    if identifiers:
        cases = [case for case in cases if str(case["id"]) in identifiers]
    if arguments.max_triangles > 0:
        cases = [
            case
            for case in cases
            if int(case.get("triangles", 0)) <= arguments.max_triangles
        ]
    if arguments.curved_only:
        cases = [case for case in cases if case_has_analytic_curves(case)]
    if arguments.limit > 0:
        cases = cases[: arguments.limit]

    cache_directory = arguments.output.parent / f"{arguments.output.stem}_cases"
    cache_directory.mkdir(parents=True, exist_ok=True)
    results_by_index: dict[int, dict[str, object]] = {}
    with ThreadPoolExecutor(max_workers=max(arguments.workers, 1)) as executor:
        futures = {
            executor.submit(
                _bounded_case,
                case,
                arguments.manifest,
                cache_directory,
                arguments.case_timeout,
                not arguments.no_resume,
                arguments.artifacts_root,
            ): index
            for index, case in enumerate(cases)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            result = future.result()
            results_by_index[index] = result
            ordered = [results_by_index[item] for item in sorted(results_by_index)]
            _write_output(arguments.output, ordered)
            print(
                f"[{completed}/{len(cases)}] {result['id']}: "
                f"{result['status']}, closed={result.get('closed')}, "
                f"free={result.get('free_edge_count')}, "
                f"P95={result.get('p95_mm')}"
            )

    results = [results_by_index[item] for item in sorted(results_by_index)]
    _write_output(arguments.output, results)
    print(json.dumps(benchmark_summary(results), indent=2))


if __name__ == "__main__":
    main()

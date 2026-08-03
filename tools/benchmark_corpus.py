from __future__ import annotations

import argparse
import json
import shutil
import statistics
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

import app.reconstruction as reconstruction_module  # noqa: E402
from app.reconstruction import reconstruct  # noqa: E402
from app.schemas import (  # noqa: E402
    ArcSegment,
    CircleProfile,
    PathProfile,
    Profile,
    ReconstructionPlan,
    SplineProfile,
)


def count_arcs(profile: Profile) -> int:
    if not isinstance(profile, PathProfile):
        return 0
    return sum(isinstance(segment, ArcSegment) for segment in profile.segments)


def plan_metrics(plan: ReconstructionPlan) -> dict[str, int]:
    arcs = 0
    circles = 0
    polygons = 0
    splines = 0
    profiles = [
        getattr(plan.base, "outer", None),
        *getattr(plan.base, "holes", []),
        *getattr(plan.base, "additional_regions", []),
    ]
    for operation in plan.operations:
        profiles.extend(
            [
                getattr(operation, "outer", None),
                *getattr(operation, "holes", []),
                *getattr(operation, "additional_regions", []),
            ]
        )
    for profile in profiles:
        if profile is not None:
            arcs += count_arcs(profile)
            circles += int(isinstance(profile, CircleProfile))
            polygons += int(profile.kind == "polygon")
            splines += int(isinstance(profile, SplineProfile))
    return {
        "feature_count": 1 + len(plan.operations),
        "analytic_arc_count": arcs,
        "analytic_circle_count": circles,
        "polygon_profile_count": polygons,
        "spline_profile_count": splines,
    }


def multiset_precision_recall(
    expected: dict[str, int],
    actual: dict[str, int],
) -> tuple[float, float, float]:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    overlap = sum((expected_counts & actual_counts).values())
    precision = overlap / max(sum(actual_counts.values()), 1)
    recall = overlap / max(sum(expected_counts.values()), 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    return precision, recall, f1


def step_topology(path: Path) -> dict[str, object]:
    shape = cq.importers.importStep(str(path)).val()
    faces = Counter(face.geomType() for face in shape.Faces())
    edges = Counter(edge.geomType() for edge in shape.Edges())
    return {
        "output_face_type_counts": dict(faces.most_common()),
        "output_edge_type_counts": dict(edges.most_common()),
        "output_face_count": sum(faces.values()),
        "output_edge_count": sum(edges.values()),
    }


def strict_metrics(
    result: dict[str, object],
    ground_truth: dict[str, object],
    output_topology: dict[str, object],
) -> dict[str, object]:
    expected_faces = ground_truth.get("brep_face_type_counts", {})
    actual_faces = output_topology["output_face_type_counts"]
    assert isinstance(expected_faces, dict)
    assert isinstance(actual_faces, dict)
    face_precision, face_recall, face_f1 = multiset_precision_recall(
        expected_faces,
        actual_faces,
    )

    expected_curves = {
        kind: count for kind, count in expected_faces.items() if kind != "PLANE"
    }
    actual_curves = {
        kind: count for kind, count in actual_faces.items() if kind != "PLANE"
    }
    curve_precision, curve_recall, curve_f1 = multiset_precision_recall(
        expected_curves,
        actual_curves,
    )
    expected_feature_count = int(ground_truth.get("feature_count", 0))
    actual_feature_count = int(result.get("feature_count", 0))
    feature_count_ratio = actual_feature_count / max(expected_feature_count, 1)
    # A dimensionally close result is not a clean reconstruction when a small
    # source timeline has been replaced by dozens of section slabs.  Allow
    # some expansion because one multi-profile Fusion feature can legitimately
    # become several explicit hole/cut features in the portable schema, but
    # make large feature explosions fail the auditable gate.
    feature_count_limit = max(
        expected_feature_count * 3,
        expected_feature_count + 8,
    )
    feature_count_clean = actual_feature_count <= feature_count_limit
    actual_types = {kind for kind, count in actual_faces.items() if count}
    required_types: dict[str, set[str]] = {
        "PLANE": {"PLANE"},
        # A revolved straight profile is an equally analytic, editable
        # representation of a cylindrical wall. OCCT reports that STEP face
        # as REVOLUTION even though the construction preserves the exact
        # cylinder-generating line and axis.
        "CYLINDER": {"CYLINDER", "REVOLUTION"},
        "CONE": {"CONE"},
        "TORUS": {"TORUS"},
        "SPHERE": {"SPHERE"},
        # STEP exporters frequently encode a smooth extrusion as a B-spline
        # surface. A dimensionally accurate analytic arc/cylinder replacement
        # is cleaner CAD and should satisfy the smooth-curve requirement.
        "BSPLINE": {
            "BSPLINE",
            "BEZIER",
            "EXTRUSION",
            "CYLINDER",
            "CONE",
            "TORUS",
            "SPHERE",
        },
    }
    expected_types = {kind for kind, count in expected_faces.items() if count}
    # Older official records use JSON null when this optional derived count
    # was not materialized. Treat missing and null identically instead of
    # turning a successful reconstruction into a benchmark-process crash.
    planar_bspline_count = int(
        ground_truth.get("effectively_planar_bspline_count") or 0
    )
    if planar_bspline_count >= int(expected_faces.get("BSPLINE", 0)):
        expected_types.discard("BSPLINE")
    missing_types = sorted(
        kind
        for kind in expected_types
        if not (required_types.get(kind, {kind}) & actual_types)
    )
    analytic_type_coverage = (
        (len(expected_types) - len(missing_types)) / len(expected_types)
        if expected_types
        else 1.0
    )
    strict_complete = bool(
        result["status"] == "complete"
        and result.get("valid_solid")
        and analytic_type_coverage == 1.0
        and feature_count_clean
    )
    return {
        **output_topology,
        "surface_type_precision": face_precision,
        "surface_type_recall": face_recall,
        "surface_type_f1": face_f1,
        "curved_surface_precision": curve_precision,
        "curved_surface_recall": curve_recall,
        "curved_surface_f1": curve_f1,
        "ground_truth_feature_count": expected_feature_count,
        "feature_count_ratio": feature_count_ratio,
        "feature_count_limit": feature_count_limit,
        "feature_count_overage": max(
            actual_feature_count - feature_count_limit,
            0,
        ),
        "feature_count_clean": feature_count_clean,
        "analytic_type_coverage": analytic_type_coverage,
        "missing_analytic_surface_types": missing_types,
        "strict_complete": strict_complete,
    }


def refresh_result_metrics(result: dict[str, object]) -> dict[str, object]:
    ground_truth = result.get("ground_truth")
    output_faces = result.get("output_face_type_counts")
    if isinstance(ground_truth, dict) and isinstance(output_faces, dict):
        output_topology = {
            "output_face_type_counts": output_faces,
            "output_edge_type_counts": result.get("output_edge_type_counts", {}),
            "output_face_count": result.get("output_face_count", sum(output_faces.values())),
            "output_edge_count": result.get("output_edge_count", 0),
        }
        result.update(strict_metrics(result, ground_truth, output_topology))

    family = str(result.get("family", "unknown"))
    scope_override = (
        ground_truth.get("scope_override")
        if isinstance(ground_truth, dict)
        else None
    )
    if scope_override == "out_of_scope":
        result["scope_status"] = "out_of_scope"
        result["scope_reason"] = str(
            ground_truth.get(
                "scope_override_reason",
                "The source contains geometry outside the declared mechanical scope.",
            )
        )
    elif family in {"revolve", "revolve_mixed"}:
        result["scope_status"] = "out_of_scope"
        result["scope_reason"] = (
            "Source timeline contains revolve operations; current declared scope is "
            "extrusion-dominant mechanical parts."
        )
    else:
        result["scope_status"] = "in_scope"
        result["scope_reason"] = (
            "Source timeline uses extrusions with optional fillets/chamfers and is "
            "within the declared mechanical scope."
        )
    return result


def grouped_summary(
    results: list[dict[str, object]],
    field: str,
) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for result in results:
        label = str(result.get(field, "unknown"))
        groups.setdefault(label, []).append(result)
    return {
        label: {
            "count": len(items),
            "geometric_complete_rate": sum(
                item.get("status") == "complete" for item in items
            )
            / len(items),
            "strict_complete_rate": sum(
                bool(item.get("strict_complete")) for item in items
            )
            / len(items),
        }
        for label, items in sorted(groups.items())
    }


def benchmark_summary(results: list[dict[str, object]]) -> dict[str, object]:
    measured = [item for item in results if isinstance(item.get("p95_mm"), int | float)]
    in_scope = [item for item in results if item.get("scope_status") == "in_scope"]
    return {
        "case_count": len(results),
        "complete_count": sum(item["status"] == "complete" for item in results),
        "best_effort_count": sum(item["status"] == "best_effort" for item in results),
        "failed_or_crashed_count": sum(
            item["status"] in {"failed", "crashed", "timeout"} for item in results
        ),
        "valid_solid_rate": (
            sum(bool(item.get("valid_solid")) for item in results) / max(len(results), 1)
        ),
        "median_p95_mm": (
            statistics.median(float(item["p95_mm"]) for item in measured) if measured else None
        ),
        "median_volume_error_percent": (
            statistics.median(float(item["volume_error_percent"]) for item in measured)
            if measured
            else None
        ),
        "strict_complete_count": sum(bool(item.get("strict_complete")) for item in results),
        "strict_complete_rate": sum(bool(item.get("strict_complete")) for item in results)
        / max(len(results), 1),
        "in_scope_count": len(in_scope),
        "in_scope_strict_complete_count": sum(
            bool(item.get("strict_complete")) for item in in_scope
        ),
        "in_scope_strict_complete_rate": sum(
            bool(item.get("strict_complete")) for item in in_scope
        )
        / max(len(in_scope), 1),
        "median_surface_type_f1": (
            statistics.median(
                float(item["surface_type_f1"])
                for item in results
                if isinstance(item.get("surface_type_f1"), int | float)
            )
            if any(isinstance(item.get("surface_type_f1"), int | float) for item in results)
            else None
        ),
        "median_feature_count_ratio": (
            statistics.median(
                float(item["feature_count_ratio"])
                for item in results
                if isinstance(item.get("feature_count_ratio"), int | float)
            )
            if any(isinstance(item.get("feature_count_ratio"), int | float) for item in results)
            else None
        ),
        "by_family": grouped_summary(results, "family"),
        "by_complexity": grouped_summary(results, "complexity"),
    }


def _write_aggregate(output_path: Path, results: list[dict[str, object]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"summary": benchmark_summary(results), "results": results}, indent=2),
        encoding="utf-8",
    )


def _preserve_failure_artifacts(
    working: Path,
    result: dict[str, object],
    artifacts_root: Path,
) -> None:
    case_directory = artifacts_root / str(result["id"])
    case_directory.mkdir(parents=True, exist_ok=True)
    for name in (
        "input.stl",
        "reconstruction.step",
        "reconstruction.stl",
        "reconstruction.py",
        "plan.json",
    ):
        source = working / name
        if source.is_file():
            shutil.copy2(source, case_directory / name)
    (case_directory / "benchmark_result.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )


def benchmark_case(
    case: dict[str, object],
    manifest_parent: Path,
    run_index: int,
    artifacts_root: Path | None = None,
) -> dict[str, object]:
    reconstruction_module.save_report = lambda report: None
    source = manifest_parent / str(case["stl"])
    with tempfile.TemporaryDirectory(prefix="meshmind-benchmark-") as temporary:
        working = Path(temporary)
        input_path = working / "input.stl"
        shutil.copy2(source, input_path)
        report = reconstruct(
            f"bench{run_index:07d}",
            input_path,
            source.name,
        )
        result: dict[str, object] = {
            "id": case["id"],
            "status": report.status,
            "elapsed_seconds": report.elapsed_seconds,
            "valid_solid": bool(report.score and report.score.valid_solid),
            "p95_mm": (report.score.chamfer_p95_mm if report.score is not None else None),
            "max_mm": (report.score.chamfer_max_mm if report.score is not None else None),
            "volume_error_percent": (
                report.score.volume_error_percent if report.score is not None else None
            ),
            "warnings": report.warnings,
            "plan": report.plan.model_dump(mode="json") if report.plan is not None else None,
        }
        if report.plan is not None:
            result.update(plan_metrics(report.plan))
        ground_truth = case.get("ground_truth")
        if isinstance(ground_truth, dict):
            result["family"] = ground_truth.get("family", "unknown")
            result["complexity"] = ground_truth.get("complexity", "unknown")
            result["ground_truth"] = ground_truth
            reconstruction_step = working / "reconstruction.step"
            if reconstruction_step.is_file():
                result.update(
                    strict_metrics(
                        result,
                        ground_truth,
                        step_topology(reconstruction_step),
                    )
                )
        if artifacts_root is not None and not result.get("strict_complete"):
            _preserve_failure_artifacts(working, result, artifacts_root)
        return refresh_result_metrics(result)


def _worker(
    manifest_path: Path,
    identifier: str,
    output_path: Path,
    artifacts_root: Path | None,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    case = next(case for case in manifest["cases"] if case["id"] == identifier)
    try:
        result = benchmark_case(case, manifest_path.parent, 1, artifacts_root)
    except Exception as exc:
        ground_truth = case.get("ground_truth", {})
        result = {
            "id": identifier,
            "status": "crashed",
            "error": f"{type(exc).__name__}: {exc}",
            "family": ground_truth.get("family", "unknown"),
            "complexity": ground_truth.get("complexity", "unknown"),
            "ground_truth": ground_truth,
            "strict_complete": False,
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _run_bounded_case(
    case: dict[str, object],
    manifest_path: Path,
    cache_directory: Path,
    case_timeout: int,
    resume: bool,
    artifacts_root: Path | None,
    retry_statuses: set[str],
) -> dict[str, object]:
    identifier = str(case["id"])
    cache_path = cache_directory / f"{identifier}.json"
    should_run = not (resume and cache_path.is_file())
    if not should_run and retry_statuses:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        should_run = str(cached.get("status", "")) in retry_statuses
    if should_run:
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
            command.extend(["--artifacts-dir", str(artifacts_root.resolve())])
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=case_timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired:
            ground_truth = case.get("ground_truth", {})
            timeout_result = {
                "id": identifier,
                "status": "timeout",
                "error": f"Exceeded {case_timeout} seconds",
                "family": ground_truth.get("family", "unknown"),
                "complexity": ground_truth.get("complexity", "unknown"),
                "ground_truth": ground_truth,
                "strict_complete": False,
            }
            cache_path.write_text(
                json.dumps(timeout_result, indent=2),
                encoding="utf-8",
            )
        except subprocess.CalledProcessError as exc:
            ground_truth = case.get("ground_truth", {})
            crash_result = {
                "id": identifier,
                "status": "crashed",
                "error": (exc.stderr or str(exc))[-4000:],
                "family": ground_truth.get("family", "unknown"),
                "complexity": ground_truth.get("complexity", "unknown"),
                "ground_truth": ground_truth,
                "strict_complete": False,
            }
            cache_path.write_text(
                json.dumps(crash_result, indent=2),
                encoding="utf-8",
            )
    result = json.loads(cache_path.read_text(encoding="utf-8"))
    # The manifest is authoritative for source metadata and manual scope
    # audits.  Cached geometry results may predate a reviewed scope override;
    # never let stale embedded metadata silently undo that decision.
    ground_truth = case.get("ground_truth", {})
    result["ground_truth"] = ground_truth
    if isinstance(ground_truth, dict):
        result["family"] = ground_truth.get("family", result.get("family", "unknown"))
        result["complexity"] = ground_truth.get(
            "complexity",
            result.get("complexity", "unknown"),
        )
    result = refresh_result_metrics(result)
    cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def benchmark(
    manifest_path: Path,
    output_path: Path,
    limit: int = 0,
    identifiers: set[str] | None = None,
    case_timeout: int = 0,
    resume: bool = True,
    artifacts_root: Path | None = None,
    workers: int = 1,
    retry_statuses: set[str] | None = None,
    start_after: str | None = None,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = select_cases(
        manifest["cases"],
        identifiers=identifiers,
        limit=limit,
        start_after=start_after,
    )
    _benchmark_selected_cases(
        cases,
        manifest_path,
        output_path,
        case_timeout,
        resume,
        artifacts_root,
        workers,
        retry_statuses,
    )


def select_cases(
    cases: list[dict[str, object]],
    identifiers: set[str] | None = None,
    limit: int = 0,
    start_after: str | None = None,
) -> list[dict[str, object]]:
    """Select an ordered manifest suffix without changing final-run defaults."""
    selected = cases
    if start_after is not None:
        frontier = next(
            (
                index
                for index, case in enumerate(selected)
                if str(case["id"]) == start_after
            ),
            None,
        )
        if frontier is None:
            raise ValueError(f"Start-after case ID not found: {start_after}")
        selected = selected[frontier + 1 :]
    if identifiers:
        selected = [case for case in selected if case["id"] in identifiers]
    if limit > 0:
        selected = selected[:limit]
    return selected


def _benchmark_selected_cases(
    cases: list[dict[str, object]],
    manifest_path: Path,
    output_path: Path,
    case_timeout: int,
    resume: bool,
    artifacts_root: Path | None,
    workers: int,
    retry_statuses: set[str] | None,
) -> None:
    results: list[dict[str, object]] = []
    retry_statuses = retry_statuses or set()
    cache_directory = output_path.parent / f"{output_path.stem}_cases"
    if case_timeout:
        cache_directory.mkdir(parents=True, exist_ok=True)

    if case_timeout and workers > 1:
        ordered_results: dict[int, dict[str, object]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_bounded_case,
                    case,
                    manifest_path,
                    cache_directory,
                    case_timeout,
                    resume,
                    artifacts_root,
                    retry_statuses,
                ): (index, case)
                for index, case in enumerate(cases, start=1)
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                index, case = futures[future]
                result = future.result()
                ordered_results[index] = result
                print(
                    f"[{completed}/{len(cases)}] {case['id']}: {result['status']}, "
                    f"strict={result.get('strict_complete')}, "
                    f"P95={result.get('p95_mm')}"
                )
                partial = [ordered_results[item] for item in sorted(ordered_results)]
                _write_aggregate(output_path, partial)
        results = [ordered_results[item] for item in sorted(ordered_results)]
        _write_aggregate(output_path, results)
        print(json.dumps(benchmark_summary(results), indent=2))
        return

    for index, case in enumerate(cases, start=1):
        identifier = str(case["id"])
        if case_timeout:
            result = _run_bounded_case(
                case,
                manifest_path,
                cache_directory,
                case_timeout,
                resume,
                artifacts_root,
                retry_statuses,
            )
        else:
            try:
                result = benchmark_case(
                    case,
                    manifest_path.parent,
                    index,
                    artifacts_root,
                )
            except Exception as exc:
                result = {
                    "id": identifier,
                    "status": "crashed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "strict_complete": False,
                }
        results.append(result)
        print(
            f"[{index}/{len(cases)}] {identifier}: {result['status']}, "
            f"strict={result.get('strict_complete')}, P95={result.get('p95_mm')}"
        )
        _write_aggregate(output_path, results)

    summary = benchmark_summary(results)
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark MeshMind against a generated STEP-to-STL corpus."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/latest.json"),
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--start-after",
        help=(
            "Run only cases after this manifest ID. Intended for diagnostic "
            "frontier discovery; omit it for final full-corpus evidence."
        ),
    )
    parser.add_argument(
        "--id",
        action="append",
        default=[],
        help="Run only this case ID; may be supplied more than once.",
    )
    parser.add_argument(
        "--case-timeout",
        type=int,
        default=0,
        help="Run each case in a disposable subprocess with this timeout in seconds.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of isolated cases to run concurrently.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore cached per-case results and run selected cases again.",
    )
    parser.add_argument(
        "--retry-status",
        action="append",
        default=[],
        choices=["crashed", "timeout", "failed", "best_effort"],
        help=(
            "Rerun cached cases with this status while preserving successful "
            "resume entries; may be supplied more than once."
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Preserve reconstruction files for every strict failure.",
    )
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
            arguments.artifacts_dir,
        )
        return
    benchmark(
        arguments.manifest,
        arguments.output,
        arguments.limit,
        set(arguments.id) or None,
        arguments.case_timeout,
        not arguments.no_resume,
        arguments.artifacts_dir,
        max(arguments.workers, 1),
        set(arguments.retry_status),
        arguments.start_after,
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def classify_failure(result: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    status = str(result.get("status", "unknown"))
    error = str(result.get("error", "")).lower()
    warnings = " ".join(str(item) for item in result.get("warnings", [])).lower()
    free_edges = result.get("free_edge_count")
    closed = bool(result.get("closed", result.get("valid_solid", False)))

    if status in {"timeout", "crashed", "failed", "error"}:
        categories.append("execution_failure")
    if "timeout" in error or "exceeded" in error:
        categories.append("timeout")
    if "crash" in error or "access violation" in error:
        categories.append("native_kernel_crash")
    if isinstance(free_edges, int) and free_edges > 0:
        categories.append("open_boundaries")
    if free_edges == 0 and not closed:
        categories.append("edge_closed_invalid_shell")
    if result.get("step_roundtrip_valid") is False:
        categories.append("step_roundtrip_invalid")
    if result.get("valid_brep") is False:
        categories.append("invalid_brep")
    if result.get("valid_solid") is False:
        categories.append("invalid_solid")
    p95 = result.get("p95_mm")
    p95_limit = result.get("p95_limit_mm")
    if isinstance(p95, (int, float)) and isinstance(p95_limit, (int, float)):
        if p95 > p95_limit:
            categories.append("surface_deviation")
    if "analytic trimming failed" in warnings:
        categories.append("analytic_trim_failure")
    if "residual" in warnings and "b-spline" in warnings:
        categories.append("residual_bspline")
    if "p-curve" in warnings or "pcurve" in warnings:
        categories.append("pcurve_failure")
    if result.get("faceted_fallback"):
        categories.append("faceted_surface_fallback")
    if result.get("source_mesh_fallback"):
        categories.append("global_source_mesh_fallback")
    if result.get("clean_analytic") is False:
        categories.append("unclean_analytic_representation")
    if result.get("missing_analytic_surface_types"):
        categories.append("missing_analytic_surface_types")
    recall = result.get("minimum_analytic_area_recall")
    if isinstance(recall, (int, float)) and recall < 0.95:
        categories.append("analytic_area_recall")
    precision = result.get("minimum_analytic_area_precision")
    if isinstance(precision, (int, float)) and precision < 0.95:
        categories.append("analytic_area_precision")
    return list(dict.fromkeys(categories or ["acceptance_gate_only"]))


def analyze_results(
    results: list[dict[str, Any]],
    benchmark_name: str,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    for result in results:
        accepted = bool(result.get("accepted", result.get("status") == "complete"))
        if accepted:
            continue
        categories = classify_failure(result)
        category_counts.update(categories)
        failures.append(
            {
                "benchmark": benchmark_name,
                "id": result.get("id", "unknown"),
                "family": result.get("family", "unknown"),
                "status": result.get("status", "unknown"),
                "categories": categories,
                "recognized_surface_count": result.get("recognized_surface_count"),
                "surface_counts": result.get("surface_counts", {}),
                "free_edge_count": result.get("free_edge_count"),
                "p95_mm": result.get("p95_mm"),
                "p95_limit_mm": result.get("p95_limit_mm"),
                "faceted_face_count": result.get("faceted_face_count"),
                "minimum_analytic_area_recall": result.get(
                    "minimum_analytic_area_recall"
                ),
                "error": result.get("error"),
                "warnings": result.get("warnings", []),
            }
        )
    return {
        "benchmark_count": 1,
        "failure_count": len(failures),
        "category_counts": dict(category_counts.most_common()),
        "failures": failures,
    }


def analyze(paths: list[Path]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        per_file = analyze_results(document.get("results", []), path.name)
        failures.extend(per_file["failures"])
        category_counts.update(per_file["category_counts"])
    return {
        "benchmark_count": len(paths),
        "failure_count": len(failures),
        "category_counts": dict(category_counts.most_common()),
        "failures": failures,
    }


def markdown_report(analysis: dict[str, Any]) -> str:
    lines = [
        "# Surface reconstruction failure ledger",
        "",
        "Generated from benchmark reports. A case may appear in more than one "
        "category because failures often cascade.",
        "",
        "## Common failure categories",
        "",
        "| Category | Cases |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| `{category}` | {count} |"
        for category, count in analysis["category_counts"].items()
    )
    lines.extend(
        [
            "",
            "## Individual failures",
            "",
            "| Benchmark | Case | Family | Categories | Facets | Analytic recall | Error/warning |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for failure in analysis["failures"]:
        recall = failure["minimum_analytic_area_recall"]
        recall_text = f"{recall:.3f}" if isinstance(recall, (int, float)) else "—"
        message = failure["error"] or next(iter(failure["warnings"]), "—")
        message = str(message).replace("|", "\\|")
        facets = failure["faceted_face_count"]
        lines.append(
            f"| {failure['benchmark']} | `{failure['id']}` | "
            f"{failure['family']} | {', '.join(failure['categories'])} | "
            f"{facets if facets is not None else '—'} | "
            f"{recall_text} | {message} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify every failed fitted-surface benchmark case."
    )
    parser.add_argument("benchmarks", type=Path, nargs="+")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    arguments = parser.parse_args()
    result = analyze(arguments.benchmarks)
    if arguments.json_output:
        arguments.json_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_output.write_text(
            json.dumps(result, indent=2),
            encoding="utf-8",
        )
    if arguments.markdown_output:
        arguments.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.markdown_output.write_text(
            markdown_report(result),
            encoding="utf-8",
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from os import environ
from pathlib import Path

import cadquery as cq
import numpy as np

from .ai import is_configured, revise_plan
from .cad import build_plan, export_plan
from .mesh import MeshData, load_mesh
from .profiles import (
    PlanCandidate,
    generate_adaptive_layer_candidates,
    generate_analytic_cylinder_separation_candidates,
    generate_arbitrary_axis_prismatic_candidates,
    generate_axial_circle_transition_candidates,
    generate_axial_radius_candidates,
    generate_change_weighted_layer_candidates,
    generate_circular_end_finish_candidates,
    generate_compact_axial_cap_candidates,
    generate_conical_add_candidates,
    generate_conical_boundary_candidates,
    generate_conical_hole_candidates,
    generate_constant_stock_cylinder_cut_candidates,
    generate_cross_axis_residual_candidates,
    generate_cylindrical_stock_candidates,
    generate_embedded_circle_promotion_candidates,
    generate_end_finish_candidates,
    generate_envelope_candidates,
    generate_feature_round_finish_candidates,
    generate_internal_circular_finish_candidates,
    generate_layer_envelope_candidates,
    generate_layered_candidates,
    generate_local_tangent_envelope_candidates,
    generate_oriented_cylinder_candidates,
    generate_partial_cone_adaptive_candidates,
    generate_prismatic_candidates,
    generate_profiled_endcap_cylinder_candidates,
    generate_revolved_profile_candidates,
    generate_round_boundary_feature_candidates,
    generate_round_hole_candidates,
    generate_smooth_loft_candidates,
    generate_spherical_corner_finish_candidates,
    generate_spherical_patch_candidates,
    generate_tapered_profile_candidates,
    mesh_has_conical_patch,
    mesh_has_spatially_curved_patch,
    mesh_has_toroidal_patch,
    rank_curve_aligned_axes,
    section_shape,
)
from .schemas import (
    Axis,
    BooleanExtrudeFeature,
    CircleProfile,
    ConicalAddFeature,
    ConicalHoleFeature,
    CylinderFeature,
    EdgeFinishFeature,
    ExtrudeFeature,
    OrientedCylinderFeature,
    OrientedExtrudeFeature,
    ReconstructionPlan,
    ReconstructionReport,
    RevolveFeature,
    RoundHoleFeature,
    SphereFeature,
    TaperedAddFeature,
)
from .scoring import ScoredCandidate, score_plan
from .storage import save_report


def _acceptance_threshold(diagonal: float) -> float:
    return max(0.12, diagonal * 0.003)


def reconstruct(
    run_id: str,
    stl_path: Path,
    original_name: str,
    input_units: str = "mm",
    prompt: str = "",
    progress_callback: Callable[[str], None] | None = None,
) -> ReconstructionReport:
    started = time.perf_counter()
    destination = stl_path.parent

    trace_value = environ.get("MESHMIND_TRACE_PATH", "").strip()
    trace_path = Path(trace_value) if trace_value else None

    def trace_stage(label: str) -> None:
        if progress_callback is not None:
            progress_callback(label)
        if trace_path is None:
            return
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.perf_counter() - started:.6f}\t{label}\n")
            stream.flush()

    trace_stage("reconstruction_started")
    data = load_mesh(stl_path, original_name, input_units)
    trace_stage(
        f"mesh_loaded triangles={data.report.triangle_count} "
        f"watertight={data.report.watertight}"
    )
    warnings: list[str] = []
    mesh_detector_cache: dict[str, bool] = {}

    def detected_mesh_property(
        name: str,
        detector: Callable[[MeshData], bool],
    ) -> bool:
        if name not in mesh_detector_cache:
            mesh_detector_cache[name] = bool(detector(data))
        return mesh_detector_cache[name]

    def has_conical_patch() -> bool:
        return detected_mesh_property("cone", mesh_has_conical_patch)

    def has_toroidal_patch() -> bool:
        return detected_mesh_property("torus", mesh_has_toroidal_patch)

    def has_spatially_curved_patch() -> bool:
        return detected_mesh_property(
            "spatial",
            mesh_has_spatially_curved_patch,
        )
    if not data.report.watertight:
        warnings.append("The input mesh is open; volume checks are unavailable.")
    if data.report.body_count != 1:
        warnings.append(
            f"The mesh contains {data.report.body_count} bodies; this version reconstructs "
            "their combined silhouette as one part."
        )

    name = Path(original_name).stem
    prismatic = generate_prismatic_candidates(data, name)
    trace_stage(f"prismatic_generated count={len(prismatic)}")
    layered = generate_layered_candidates(data, name)
    trace_stage(f"layered_generated count={len(layered)}")
    axial = generate_axial_radius_candidates(data, name)
    trace_stage(f"axial_generated count={len(axial)}")
    tapered = generate_tapered_profile_candidates(data, name)
    trace_stage(f"tapered_generated count={len(tapered)}")
    revolved = generate_revolved_profile_candidates(data, name)
    trace_stage(f"revolved_generated count={len(revolved)}")
    arbitrary_axis = generate_arbitrary_axis_prismatic_candidates(data, name)
    trace_stage(f"arbitrary_axis_generated count={len(arbitrary_axis)}")
    separated_cylinders = generate_analytic_cylinder_separation_candidates(
        data,
        name,
    )
    trace_stage(f"separated_cylinders_generated count={len(separated_cylinders)}")
    if axial:
        # A body that slices as one concentric circle across every axial interval
        # has an unambiguous turned profile.  Generic prismatic/edge-finish
        # alternatives only add inferior plans and can ask OpenCascade to fillet
        # hundreds of tessellation-derived edges.
        generated = list(axial)
    elif revolved:
        generated = list(revolved)
        generated.extend(prismatic)
        generated.extend(layered)
        generated.extend(tapered)
        generated.extend(
            generate_end_finish_candidates(data, [*prismatic, *layered])
        )
        generated.extend(
            generate_circular_end_finish_candidates(
                data,
                [*prismatic, *layered],
            )
        )
        generated.extend(arbitrary_axis)
        generated.extend(separated_cylinders)
    else:
        generated = list(prismatic)
        generated.extend(layered)
        generated.extend(tapered)
        generated.extend(arbitrary_axis)
        generated.extend(separated_cylinders)
        # Dense curved meshes are routed through the bounded adaptive search
        # below. Enumerating every generic end-finish permutation first can
        # consume the entire case budget without scoring a candidate.
        if data.report.triangle_count < 50000:
            generated.extend(
                generate_end_finish_candidates(data, [*prismatic, *layered])
            )
            generated.extend(
                generate_circular_end_finish_candidates(
                    data,
                    [*prismatic, *layered],
                )
            )
    if any(
        candidate.section_consistency >= 0.85
        for candidate in prismatic
    ):
        generated.extend(
            PlanCandidate(
                plan=plan,
                heuristic_score=-0.6,
                section_consistency=1.0,
            )
            for plan in generate_constant_stock_cylinder_cut_candidates(
                data,
                name,
            )
        )
        generated.extend(
            PlanCandidate(
                plan=plan,
                heuristic_score=-0.5,
                section_consistency=1.0,
            )
            for plan in generate_profiled_endcap_cylinder_candidates(data, name)
        )
    generated.sort(key=lambda candidate: candidate.heuristic_score)
    if data.report.triangle_count >= 5000 and len(generated) > 12:
        ranked = generated
        semantic_shortlist: list[PlanCandidate] = []
        selected_ids: set[int] = set()

        def retain(candidate: PlanCandidate | None) -> None:
            if candidate is not None and id(candidate) not in selected_ids:
                semantic_shortlist.append(candidate)
                selected_ids.add(id(candidate))

        retain(ranked[0])
        for axis in Axis:
            retain(
                next(
                    (
                        candidate
                        for candidate in ranked
                        if getattr(candidate.plan.base, "axis", None) == axis
                        and any(
                            isinstance(operation, TaperedAddFeature)
                            for operation in candidate.plan.operations
                        )
                    ),
                    None,
                )
            )
        for axis in Axis:
            retain(
                next(
                    (
                        candidate
                        for candidate in ranked
                        if getattr(candidate.plan.base, "axis", None) == axis
                    ),
                    None,
                )
            )

        axis_index = {Axis.X: 0, Axis.Y: 1, Axis.Z: 2}
        thin_axis = min(
            Axis,
            key=lambda axis: float(data.mesh.extents[axis_index[axis]]),
        )
        finish_signatures: set[
            tuple[Axis, tuple[tuple[str, str, str], ...]]
        ] = set()
        finish_candidates = sorted(
            ranked,
            key=lambda candidate: (
                getattr(candidate.plan.base, "axis", None) != thin_axis,
                candidate.heuristic_score,
            ),
        )
        for candidate in finish_candidates:
            finishes = [
                operation
                for operation in candidate.plan.operations
                if isinstance(operation, EdgeFinishFeature)
            ]
            if not finishes:
                continue
            signature = (
                getattr(candidate.plan.base, "axis", thin_axis),
                tuple(
                    (
                        finish.selector,
                        finish.mode,
                        finish.end,
                    )
                    for finish in finishes
                ),
            )
            if signature not in finish_signatures:
                retain(candidate)
                finish_signatures.add(signature)
            if len(semantic_shortlist) >= 12:
                break

        for candidate in ranked:
            if len(semantic_shortlist) >= 12:
                break
            retain(candidate)
        warnings.append(
            f"Pruned {len(ranked) - len(semantic_shortlist)} lower-ranked "
            "initial constructions while retaining axis and analytic-feature "
            "coverage."
        )
        generated = semantic_shortlist
    elif len(generated) > 24:
        warnings.append(
            f"Pruned {len(generated) - 24} lower-ranked initial constructions."
        )
        generated = generated[:24]
    trace_stage(f"initial_shortlist_ready count={len(generated)}")
    # Search effort must not shrink merely because another reconstruction is
    # sharing the machine. Process CPU time makes candidate coverage stable
    # across browser use and parallel benchmark workers; the outer worker
    # timeout remains the wall-clock safety boundary.
    search_seconds = max(
        15.0,
        float(environ.get("MESHMIND_MAX_SEARCH_SECONDS", "120")),
    )
    search_deadline = time.process_time() + search_seconds
    budget_warning_added = False

    def search_budget_available() -> bool:
        nonlocal budget_warning_added
        if time.process_time() <= search_deadline:
            return True
        if not budget_warning_added:
            warnings.append(
                f"The bounded refinement search reached {search_seconds:g} "
                "seconds; the best valid construction found so far was exported."
            )
            budget_warning_added = True
        return False

    axis_index = {Axis.X: 0, Axis.Y: 1, Axis.Z: 2}

    def mesh_has_non_prismatic_normals(axis: Axis) -> bool:
        components = np.abs(data.mesh.face_normals[:, axis_index[axis]])
        transition = (components > 0.02) & (components < 0.98)
        transition_area = float(np.sum(data.mesh.area_faces[transition]))
        return transition_area > max(float(data.mesh.area) * 1e-5, 1e-8)

    scored: list[ScoredCandidate] = []
    dense_adaptive_complete = False
    for index, candidate in enumerate(generated):
        if not search_budget_available():
            break
        candidate_directory = destination / "candidates" / f"{index:02d}"
        trace_stage(
            f"initial_score_start index={index} "
            f"base={candidate.plan.base.kind} "
            f"operations={len(candidate.plan.operations)}"
        )
        try:
            scored_candidate = score_plan(
                data,
                candidate.plan,
                candidate_directory,
                candidate_count=len(generated),
            )
            scored.append(scored_candidate)
            trace_stage(
                f"initial_score_done index={index} "
                f"p95={scored_candidate.report.chamfer_p95_mm:.9g} "
                f"valid={scored_candidate.report.valid_solid} "
                f"operations={len(scored_candidate.plan.operations)} "
                f"preview={candidate_directory.relative_to(destination).as_posix()}"
                "/reconstruction.stl"
            )
            high_confidence_tolerance = max(0.03, data.diagonal * 0.0015)
            if (
                len(candidate.plan.operations) <= 5
                and any(
                    "uncut constant" in assumption
                    or "Recovered constant" in assumption
                    for assumption in candidate.plan.assumptions
                )
                and scored_candidate.report.valid_solid
                and scored_candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and scored_candidate.report.volume_error_percent <= 2.0
            ):
                warnings.append(
                    "Stopped the initial search after the compact measured-endcap "
                    "and analytic-cylinder construction passed the geometry gate."
                )
                break
            if (
                len(candidate.plan.operations) <= 2
                and (
                    (
                        not candidate.plan.operations
                        and isinstance(candidate.plan.base, ExtrudeFeature)
                        and not mesh_has_non_prismatic_normals(
                            candidate.plan.base.axis
                        )
                    )
                    or any(
                        isinstance(operation, EdgeFinishFeature)
                        for operation in candidate.plan.operations
                    )
                )
                and scored_candidate.report.valid_solid
                and scored_candidate.report.chamfer_p95_mm
                <= high_confidence_tolerance
                and scored_candidate.report.volume_error_percent <= 1.5
            ):
                warnings.append(
                    "Stopped the initial search after a compact analytic "
                    "construction met the high-confidence geometry gate."
                )
                break
            if (
                len(candidate.plan.operations) > 12
                and scored_candidate.report.valid_solid
                and scored_candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and scored_candidate.report.volume_error_percent <= 2.0
            ):
                warnings.append(
                    "Paused the initial search after an accurate but complex "
                    "construction so compact stock-and-residual recovery could run."
                )
                break
        except Exception as exc:
            trace_stage(
                f"initial_score_failed index={index} error={type(exc).__name__}"
            )
            warnings.append(f"Candidate {index + 1} could not be built: {exc}")

    if not scored:
        report = ReconstructionReport(
            id=run_id,
            status="failed",
            mesh=data.report,
            plan=None,
            score=None,
            warnings=warnings
            + [
                "No supported sketch-and-extrusion construction was found. The current "
                "engine supports layered linear extrusions, analytic arcs, cylinders, "
                "and aligned openings."
            ],
            elapsed_seconds=time.perf_counter() - started,
            prompt=prompt,
        )
        save_report(report)
        return report

    scored.sort(key=lambda item: item.report.score)
    trace_stage(f"initial_scoring_complete count={len(scored)}")
    initially_acceptable = [
        candidate
        for candidate in scored
        if (
            candidate.report.valid_solid
            and candidate.report.chamfer_p95_mm
            <= _acceptance_threshold(data.diagonal)
            and candidate.report.volume_error_percent <= 2.0
        )
    ]
    best_initial_score = min(
        (candidate.report.score for candidate in initially_acceptable),
        default=float("inf"),
    )
    concise_acceptable = [
        candidate
        for candidate in initially_acceptable
        if candidate.report.score <= best_initial_score + 0.0035
    ]
    # Once candidates meet the manufacturing-tolerance gate, prefer the
    # smallest editable timeline.  Pure score minimization otherwise chooses
    # dozens of thin slabs for a tiny volume improvement over a one-sketch
    # analytic extrusion.  Later refinement passes can still add measured
    # holes, cones, and edge finishes when they preserve or improve geometry.
    best = (
        min(
            concise_acceptable,
            key=lambda candidate: (
                len(candidate.plan.operations),
                -sum(
                    isinstance(operation, EdgeFinishFeature)
                    for operation in candidate.plan.operations
                ),
                not isinstance(
                    candidate.plan.base,
                    (CylinderFeature, RevolveFeature),
                ),
                candidate.report.score,
            ),
        )
        if initially_acceptable
        else scored[0]
    )
    analytic_refinement_sources: list[ReconstructionPlan] = []

    compact_constant_extrusion = bool(
        isinstance(best.plan.base, ExtrudeFeature)
        and not best.plan.operations
        and best.report.valid_solid
        and best.report.chamfer_p95_mm
        <= max(0.03, data.diagonal * 0.0015)
        and best.report.volume_error_percent <= 1.5
        and not mesh_has_non_prismatic_normals(best.plan.base.axis)
    )
    if compact_constant_extrusion:
        warnings.append(
            "Confirmed a constant-profile analytic extrusion from its surface "
            "normals; skipped unrelated refinement searches."
        )

    compact_residual_complete = False
    weighted_layer_complete = False

    def is_turning_plan() -> bool:
        return (
            compact_constant_extrusion
            or compact_residual_complete
            or weighted_layer_complete
            or bool(axial)
            or any(
                "uncut constant" in assumption
                or "Recovered constant" in assumption
                for assumption in best.plan.assumptions
            )
            or isinstance(
                best.plan.base,
                (RevolveFeature, OrientedExtrudeFeature),
            )
        )

    def geometry_is_acceptable(candidate: ScoredCandidate) -> bool:
        return (
            candidate.report.valid_solid
            and candidate.report.chamfer_p95_mm
            <= _acceptance_threshold(data.diagonal)
            and candidate.report.volume_error_percent <= 2.0
        )

    pre_envelope_residual_attempted = False

    def compact_result_found() -> bool:
        return geometry_is_acceptable(best) and len(best.plan.operations) <= 12

    def exported_surface_types(candidate: ScoredCandidate) -> set[str]:
        try:
            return {
                face.geomType()
                for face in cq.importers.importStep(
                    str(candidate.directory / "reconstruction.step")
                )
                .val()
                .Faces()
            }
        except Exception:
            return set()

    def preserve_detected_torus(label: str, *, force: bool = False) -> None:
        nonlocal best
        trace_stage(f"torus_preservation_start label={label} force={force}")
        torus_detected = has_toroidal_patch()
        trace_stage(
            f"torus_detection_done label={label} detected={torus_detected}"
        )
        if not torus_detected or (
            not force and not search_budget_available()
        ):
            return
        current_types = exported_surface_types(best)
        trace_stage(
            f"torus_current_types label={label} "
            f"types={','.join(sorted(current_types))}"
        )
        already_has_torus = "TORUS" in current_types
        if already_has_torus:
            return
        required_existing_types = current_types.intersection(
            {"CYLINDER", "CONE", "SPHERE"}
        )
        circular_finish_scored: list[tuple[ScoredCandidate, int]] = []
        trace_stage(f"torus_finish_generation_start label={label}")
        finish_plans = generate_internal_circular_finish_candidates(
            data,
            best.plan,
        )
        if force:
            baseline_count = len(best.plan.operations)
            feature_finishes = generate_feature_round_finish_candidates(
                data,
                best.plan,
                include_all_groups=True,
            )
            finish_plans.extend(
                plan
                for plan in feature_finishes[:16]
                if any(
                    isinstance(operation, EdgeFinishFeature)
                    and operation.mode == "fillet"
                    for operation in plan.operations[baseline_count:]
                )
            )
        trace_stage(
            f"torus_finish_generation_done label={label} "
            f"count={len(finish_plans)}"
        )
        if not finish_plans:
            # A shallow internal round may be measured most reliably only
            # after its underlying circular cut is restored. Score that
            # topology-preserving pair together instead of rejecting the hole
            # first for a small localized distance increase.
            for hole_plan in generate_round_hole_candidates(data, best.plan)[:4]:
                finish_plans.extend(
                    generate_internal_circular_finish_candidates(
                        data,
                        hole_plan,
                    )
                )
        for index, finish_plan in enumerate(finish_plans):
            if not force and not search_budget_available():
                break
            try:
                trace_stage(
                    f"torus_finish_score_start label={label} index={index} "
                    f"operations={len(finish_plan.operations)}"
                )
                candidate = score_plan(
                    data,
                    finish_plan,
                    destination
                    / "candidates"
                    / f"internal-circular-finish-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                candidate_types = exported_surface_types(candidate)
                if (
                    "TORUS" in candidate_types
                    and required_existing_types.issubset(candidate_types)
                ):
                    torus_count = sum(
                        face.geomType() == "TORUS"
                        for face in cq.importers.importStep(
                            str(candidate.directory / "reconstruction.step")
                        )
                        .val()
                        .Faces()
                    )
                    circular_finish_scored.append((candidate, torus_count))
                trace_stage(
                    f"torus_finish_score_done label={label} index={index}"
                )
            except Exception as exc:
                trace_stage(
                    f"torus_finish_score_failed label={label} index={index} "
                    f"error={type(exc).__name__}"
                )
                warnings.append(
                    "Internal circular-finish candidate "
                    f"{index + 1} could not be built: {exc}"
                )
        acceptable_finishes = [
            item
            for item in circular_finish_scored
            if geometry_is_acceptable(item[0])
        ]
        if acceptable_finishes:
            minimum_score = min(
                candidate.report.score
                for candidate, _ in acceptable_finishes
            )
            topology_equivalent = [
                item
                for item in acceptable_finishes
                if item[0].report.score <= minimum_score + 0.0001
            ]
            best, _ = min(
                topology_equivalent,
                key=lambda item: (
                    -item[1],
                    len(item[0].plan.operations),
                    item[0].report.score,
                ),
            )
            warnings.append(
                "Preserved a detected circular shoulder as an analytic "
                "toroidal fillet before bounded residual search."
            )

    def preserve_detected_cones(label: str, *, force: bool = False) -> None:
        """Restore a detected cone without discarding other analytic topology.

        The normal bounded search intentionally considers only the dominant
        repeated circular boundary.  On layered parts, however, a smaller
        repeated hole can carry the chamfer while a larger outside boundary
        carries a fillet.  If the mesh contains a conical patch but the final
        STEP does not, inspect every repeated feature-local circular boundary
        and retain the smallest geometrically acceptable plan that restores an
        exact OpenCascade CONE face.
        """

        nonlocal best
        trace_stage(f"cone_preservation_start label={label} force={force}")
        cone_detected = has_conical_patch()
        trace_stage(
            f"cone_detection_done label={label} detected={cone_detected}"
        )
        if not cone_detected or (
            not force and not search_budget_available()
        ):
            return
        current_types = exported_surface_types(best)
        if "CONE" in current_types:
            return

        baseline_count = len(best.plan.operations)
        feature_finishes = generate_feature_round_finish_candidates(
            data,
            best.plan,
            include_all_groups=True,
        )
        feature_plans = [
            plan
            for plan in feature_finishes
            if any(
                isinstance(operation, EdgeFinishFeature)
                and operation.mode == "chamfer"
                for operation in plan.operations[baseline_count:]
            )
        ]
        grouped_feature_plans: dict[
            tuple[
                tuple[
                    str,
                    str | None,
                    tuple[float, float] | None,
                    float | None,
                    int | None,
                ],
                ...,
            ],
            list[ReconstructionPlan],
        ] = {}
        for plan in feature_plans:
            signature = tuple(
                (
                    operation.axis.value,
                    operation.end,
                    operation.center,
                    operation.radius,
                    operation.feature_index,
                )
                for operation in plan.operations[baseline_count:]
                if isinstance(operation, EdgeFinishFeature)
            )
            grouped_feature_plans.setdefault(signature, []).append(plan)
        # A separated-cylinder stock plan is deliberately treated as a
        # turning-style result to skip broad residual searches.  Tiny
        # countersinks on those cylinders still need their focused recovery
        # pass, so include the direct varying-radius hole fit here rather than
        # relying on the later generic hole loop.
        explicit_conical_plans = generate_conical_hole_candidates(
            data,
            best.plan,
        )[:4]
        compact_finish_plans = [
            plans[0] for plans in grouped_feature_plans.values()
        ]
        compact_finish_plans.extend(
            generate_conical_boundary_candidates(data, best.plan)[:8]
        )
        compact_finish_plans.sort(key=lambda plan: len(plan.operations))
        primary_plans = [*explicit_conical_plans, *compact_finish_plans]
        fallback_plans = [
            plan
            for plans in grouped_feature_plans.values()
            for plan in plans[1:]
        ]
        fallback_plans.sort(key=lambda plan: len(plan.operations))

        required_existing_types = current_types.intersection(
            {"CYLINDER", "SPHERE", "TORUS"}
        )
        conical: list[tuple[ScoredCandidate, int]] = []
        candidate_index = 0

        def score_cone_plans(plans: list[ReconstructionPlan]) -> None:
            nonlocal candidate_index
            for cone_plan in plans:
                if not force and not search_budget_available():
                    break
                index = candidate_index
                candidate_index += 1
                try:
                    candidate = score_plan(
                        data,
                        cone_plan,
                        destination
                        / "candidates"
                        / f"preserve-cone-{label}-{index:02d}",
                        candidate_count=len(generated) + index + 1,
                    )
                    surface_types = exported_surface_types(candidate)
                    if (
                        geometry_is_acceptable(candidate)
                        and "CONE" in surface_types
                        and required_existing_types.issubset(surface_types)
                    ):
                        cone_count = sum(
                            face.geomType() == "CONE"
                            for face in cq.importers.importStep(
                                str(candidate.directory / "reconstruction.step")
                            )
                            .val()
                            .Faces()
                        )
                        conical.append((candidate, cone_count))
                except Exception as exc:
                    warnings.append(
                        f"Cone-preservation candidate {index + 1} could not be "
                        f"built: {exc}"
                    )

        # Probe the smallest measured chamfer for every distinct repeated
        # boundary first. Only expand the size sweep if none of those probes
        # restores a valid cone. This avoids rebuilding the same layered stock
        # five times per boundary in the common case.
        score_cone_plans(primary_plans[:12])
        if not conical or not any(
            len(candidate.plan.operations) <= 10
            for candidate, _ in conical
        ):
            score_cone_plans(fallback_plans[: max(0, 20 - candidate_index)])
        if conical:
            clean_conical = [
                item
                for item in conical
                if len(item[0].plan.operations) <= 10
            ]
            semantic_conical = [
                item
                for item in (clean_conical or conical)
                if any(
                    isinstance(
                        operation,
                        (ConicalAddFeature, ConicalHoleFeature),
                    )
                    for operation in item[0].plan.operations
                )
            ]
            selection_pool = semantic_conical or clean_conical or conical
            minimum_score = min(
                candidate.report.score
                for candidate, _ in selection_pool
            )
            topology_equivalent = [
                item
                for item in selection_pool
                if item[0].report.score <= minimum_score + 0.0001
            ]
            best, _ = min(
                topology_equivalent,
                key=lambda item: (
                    -item[1],
                    len(item[0].plan.operations),
                    item[0].report.score,
                ),
            )
            warnings.append(
                "Preserved a detected repeated circular chamfer as an exact "
                "analytic conical face in the final STEP topology."
            )

    def preserve_detected_cylinders(label: str) -> None:
        nonlocal best
        existing_surface_types = exported_surface_types(best)
        required_existing_types = existing_surface_types.intersection(
            {"CONE", "SPHERE", "TORUS"}
        )
        cylinder_operation_indices = {
            index
            for index, operation in enumerate(best.plan.operations)
            if isinstance(operation, OrientedCylinderFeature)
        }
        if cylinder_operation_indices and any(
            isinstance(operation, EdgeFinishFeature)
            and operation.feature_index in cylinder_operation_indices
            for operation in best.plan.operations
        ):
            # A feature-local finish deliberately preserves the cylinder and
            # its analytic transition together. Adding a duplicate round cut
            # can consume the tiny conical strip in the final boolean result.
            return
        measured_cylinder_plans = generate_oriented_cylinder_candidates(
            data,
            best.plan,
            cuts_only=True,
        )
        if "CYLINDER" in existing_surface_types and not measured_cylinder_plans:
            return
        # The generator returns the all-patches plan first, then cardinal-axis
        # groups, then one-patch diagnostics.  Score the combined plan and the
        # small set of axis groups: promoting circles on the body's layering
        # axis can be redundant, while an orthogonal group is exactly the
        # multi-plane feature set that must replace fragmented holes.
        candidate_plans = measured_cylinder_plans[:4]
        if "CYLINDER" not in existing_surface_types:
            candidate_plans.extend(
                generate_round_boundary_feature_candidates(data, best.plan)[:12]
            )
        baseline_round_holes = [
            operation
            for operation in best.plan.operations
            if isinstance(operation, RoundHoleFeature)
        ]
        round_hole_tolerance = max(data.diagonal * 0.002, 0.025)
        analytic: list[tuple[ScoredCandidate, int]] = []
        for index, cylinder_plan in enumerate(candidate_plans):
            recovered_feature_count = sum(
                1
                for operation in cylinder_plan.operations
                if isinstance(operation, RoundHoleFeature)
                and not any(
                    existing.axis == operation.axis
                    and np.linalg.norm(
                        np.asarray(existing.center)
                        - np.asarray(operation.center)
                    )
                    <= round_hole_tolerance
                    and abs(existing.diameter - operation.diameter)
                    <= round_hole_tolerance * 2
                    and existing.start <= operation.start + round_hole_tolerance
                    and existing.start + existing.depth
                    >= operation.start + operation.depth - round_hole_tolerance
                    for existing in baseline_round_holes
                )
            )
            try:
                candidate = score_plan(
                    data,
                    cylinder_plan,
                    destination
                    / "candidates"
                    / f"final-cylinder-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                candidate_surface_types = exported_surface_types(candidate)
                if (
                    geometry_is_acceptable(candidate)
                    and "CYLINDER" in candidate_surface_types
                    and required_existing_types.issubset(candidate_surface_types)
                    and candidate.report.chamfer_p95_mm
                    <= best.report.chamfer_p95_mm
                    + max(0.01, data.diagonal * 0.0002)
                    and candidate.report.chamfer_rms_mm
                    <= best.report.chamfer_rms_mm
                    + max(0.01, data.diagonal * 0.0002)
                    and candidate.report.volume_error_percent
                    <= best.report.volume_error_percent + 0.05
                ):
                    analytic.append((candidate, recovered_feature_count))
            except Exception as exc:
                warnings.append(
                    f"Final cylinder candidate {index + 1} could not be built: {exc}"
                )
        if not analytic:
            return
        # Among the fidelity-qualified combined/axis-group candidates, retain
        # the one with greatest measured coverage. Choosing by operation count
        # used to prefer a single cut, leaving neighboring bores trapped as
        # slightly different circles in adjacent slab profiles. Those rings
        # render as stepped holes and are not useful editable CAD.
        maximum_recovered_count = max(count for _, count in analytic)
        cylinder_best, _ = min(
            analytic,
            key=lambda item: (
                -item[1],
                item[0].report.score,
                len(item[0].plan.operations),
            ),
        )
        # Do not run the generic one-operation-at-a-time deletion sweep here.
        # Besides rebuilding large models dozens of times, that sweep can
        # trade semantic feature coverage for a tiny score change. Dedicated
        # compaction passes run before this topology-preservation invariant.
        best = cylinder_best
        warnings.append(
            f"Preserved {maximum_recovered_count} detected smooth round "
            "patches as continuous analytic cylinders in the final STEP "
            "feature tree."
        )

    def promote_embedded_profile_circles(label: str) -> None:
        """Make every layered circle one editable, plane-specific feature."""

        nonlocal best
        plans = generate_embedded_circle_promotion_candidates(data, best.plan)
        if not plans:
            return
        try:
            candidate = score_plan(
                data,
                plans[0],
                destination / "candidates" / f"profile-circles-{label}",
                candidate_count=len(generated) + 1,
            )
        except Exception as exc:
            warnings.append(
                f"Profile-circle promotion could not be built: {exc}"
            )
            return
        # Circle lifting is a topology/parameterization refinement.  Its B-rep
        # volume should be nearly identical, while coarse STL resampling can
        # move a tail-distance statistic slightly.  Keep strict solid/volume
        # guards and a bounded geometric allowance so semantic reconstruction
        # is not defeated by tessellation noise.
        if (
            candidate.report.valid_solid
            and candidate.report.volume_error_percent
            <= best.report.volume_error_percent + 0.05
            and candidate.report.chamfer_p95_mm
            <= best.report.chamfer_p95_mm
            + max(0.02, data.diagonal * 0.00035)
            and candidate.report.chamfer_rms_mm
            <= best.report.chamfer_rms_mm
            + max(0.03, data.diagonal * 0.0005)
        ):
            best = candidate
            promoted_count = sum(
                isinstance(operation, RoundHoleFeature)
                for operation in best.plan.operations
            )
            warnings.append(
                f"Promoted layered circle sketches into {promoted_count} "
                "ordered, editable hole features on their measured planes."
            )

    def recover_local_tangent_envelopes(label: str) -> None:
        """Repair only alternate-plane spline bands in a layered body."""

        nonlocal best
        recovered: list[ScoredCandidate] = []
        for index, plan in enumerate(
            generate_local_tangent_envelope_candidates(data, best.plan)
        ):
            try:
                candidate = score_plan(
                    data,
                    plan,
                    destination
                    / "candidates"
                    / f"local-tangent-envelope-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
            except Exception as exc:
                warnings.append(
                    f"Local tangent-envelope candidate {index + 1} could "
                    f"not be built: {exc}"
                )
                continue
            if (
                candidate.report.valid_solid
                and candidate.report.chamfer_p95_mm
                <= best.report.chamfer_p95_mm
                + max(0.01, data.diagonal * 0.0002)
                and candidate.report.chamfer_rms_mm
                <= best.report.chamfer_rms_mm
                + max(0.01, data.diagonal * 0.0002)
                and candidate.report.volume_error_percent
                <= best.report.volume_error_percent + 0.02
                and (
                    candidate.report.score < best.report.score
                    or candidate.report.volume_error_percent
                    < best.report.volume_error_percent - 0.02
                )
            ):
                recovered.append(candidate)
        if recovered:
            best = min(recovered, key=lambda candidate: candidate.report.score)
            warnings.append(
                "Replaced stepped tangent boundary bands with local editable "
                "spline sketches on their measured construction planes."
            )

    def preserve_detected_spheres(label: str) -> None:
        nonlocal best
        if (
            "SPHERE" in exported_surface_types(best)
            or not has_spatially_curved_patch()
        ):
            return
        spherical: list[tuple[ScoredCandidate, int]] = []
        patch_plans = generate_spherical_patch_candidates(data, best.plan)
        target_sphere_count = (
            len(patch_plans[0].operations) - len(best.plan.operations)
            if patch_plans
            else 1
        )
        sphere_plans = list(patch_plans)
        sphere_plans.extend(
            generate_spherical_corner_finish_candidates(data, best.plan)
        )
        required_existing_types = exported_surface_types(best).intersection(
            {"CYLINDER", "CONE", "TORUS"}
        )
        for index, sphere_plan in enumerate(sphere_plans):
            try:
                candidate = score_plan(
                    data,
                    sphere_plan,
                    destination
                    / "candidates"
                    / f"spherical-corner-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                candidate_types = exported_surface_types(candidate)
                if (
                    geometry_is_acceptable(candidate)
                    and "SPHERE" in candidate_types
                    and required_existing_types.issubset(
                        candidate_types
                    )
                ):
                    sphere_count = sum(
                        face.geomType() == "SPHERE"
                        for face in cq.importers.importStep(
                            str(candidate.directory / "reconstruction.step")
                        )
                        .val()
                        .Faces()
                    )
                    spherical.append((candidate, sphere_count))
            except Exception as exc:
                warnings.append(
                    f"Spherical-corner candidate {index + 1} could not be built: {exc}"
                )
        if spherical:
            best, _ = min(
                spherical,
                key=lambda item: (
                    abs(item[1] - target_sphere_count),
                    item[0].report.score,
                    len(item[0].plan.operations),
                ),
            )
            warnings.append(
                "Preserved an equal-radius rolling-ball corner as an analytic "
                "sphere in the final STEP topology."
            )

    def recover_compact_axial_cap(label: str) -> None:
        nonlocal best
        if len(best.plan.operations) <= 12 or "CONE" in exported_surface_types(best):
            return
        scored_caps: list[ScoredCandidate] = []
        for index, cap_plan in enumerate(
            generate_compact_axial_cap_candidates(data, name)
        ):
            try:
                candidate = score_plan(
                    data,
                    cap_plan,
                    destination
                    / "candidates"
                    / f"compact-axial-cap-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                if (
                    geometry_is_acceptable(candidate)
                    and "CONE" in exported_surface_types(candidate)
                    and len(candidate.plan.operations) < len(best.plan.operations)
                ):
                    scored_caps.append(candidate)
            except Exception as exc:
                warnings.append(
                    f"Compact axial-cap candidate {index + 1} could not be built: {exc}"
                )
        if scored_caps:
            best = min(
                scored_caps,
                key=lambda candidate: (
                    len(candidate.plan.operations),
                    candidate.report.score,
                ),
            )
            warnings.append(
                "Replaced a dense axial slab approximation with compact "
                "radius-change loft features and analytic conical faces."
            )

    def recover_curve_aligned_layering(label: str) -> None:
        """Rebuild shallow tangent arcs in sketches normal to their axis.

        A shallow partial cylinder is not a full-cylinder feature.  Modeling it
        with slabs normal to another axis creates the visible staircase seen on
        tangent wing curves.  When an alternate cardinal section repeatedly
        contains fitted arcs, build the ordered layers in that axis instead,
        then recover every measured cross-axis cylinder and cone explicitly.
        """

        nonlocal best
        base_axis = getattr(best.plan.base, "axis", None)
        if (
            not weighted_layer_complete
            or not isinstance(base_axis, Axis)
            or len(best.plan.operations) < 15
            or not has_spatially_curved_patch()
            or has_toroidal_patch()
        ):
            return

        ranked_axes = rank_curve_aligned_axes(data, excluded_axis=base_axis)
        if not ranked_axes or ranked_axes[0][1] < 2:
            return
        curve_axis = ranked_axes[0][0]
        plans = generate_change_weighted_layer_candidates(
            data,
            name,
            curve_axis,
            layer_budget=30,
        )
        if not plans:
            return
        curve_plan = plans[0]
        cylinder_plans = generate_oriented_cylinder_candidates(data, curve_plan)
        if cylinder_plans:
            curve_plan = cylinder_plans[0]
        cone_plans = generate_conical_hole_candidates(data, curve_plan)
        if cone_plans:
            curve_plan = cone_plans[0]
        try:
            candidate = score_plan(
                data,
                curve_plan,
                destination / "candidates" / f"curve-aligned-{label}",
                candidate_count=len(generated) + 1,
            )
        except Exception as exc:
            warnings.append(
                f"Curve-aligned reconstruction could not be built: {exc}"
            )
            return
        if (
            geometry_is_acceptable(candidate)
            and candidate.report.volume_error_percent <= 1.5
            and candidate.report.chamfer_p95_mm
            <= _acceptance_threshold(data.diagonal)
            and candidate.report.chamfer_p95_mm
            <= best.report.chamfer_p95_mm
            + max(0.01, data.diagonal * 0.0002)
            and candidate.report.chamfer_rms_mm
            <= best.report.chamfer_rms_mm
            + max(0.01, data.diagonal * 0.0002)
            and candidate.report.volume_error_percent
            <= best.report.volume_error_percent + 0.05
        ):
            best = candidate
            warnings.append(
                f"Reoriented the ordered construction to {curve_axis.value} so "
                "shallow tangent curves remain analytic sketch arcs, then "
                "recovered cross-axis holes as continuous cylinders."
            )

    def recover_smooth_loft(label: str) -> None:
        nonlocal best
        if (
            (weighted_layer_complete and geometry_is_acceptable(best))
            or len(best.plan.operations) <= 9
            or "CONE" in exported_surface_types(best)
            or not has_spatially_curved_patch()
            or has_conical_patch()
        ):
            return
        lofts: list[ScoredCandidate] = []
        loft_plans = generate_smooth_loft_candidates(data, name)
        for index, loft_plan in enumerate(loft_plans):
            try:
                candidate = score_plan(
                    data,
                    loft_plan,
                    destination / "candidates" / f"smooth-loft-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                if (
                    geometry_is_acceptable(candidate)
                    and (
                        len(candidate.plan.operations) < len(best.plan.operations)
                        or candidate.report.chamfer_p95_mm
                        < best.report.chamfer_p95_mm
                    )
                ):
                    lofts.append(candidate)
                    if len(candidate.plan.operations) == 1:
                        break
            except Exception as exc:
                warnings.append(
                    f"Smooth-loft candidate {index + 1} could not be built: {exc}"
                )
        if lofts:
            best = min(
                lofts,
                key=lambda candidate: (
                    candidate.report.score,
                    len(candidate.plan.operations),
                ),
            )
            warnings.append(
                "Replaced continuous portions of a changing-profile slab stack "
                "with editable tangent lofts while retaining sharp topology "
                "changes."
            )

    def recover_profiled_endcap_cylinder(label: str) -> None:
        nonlocal best
        if len(best.plan.operations) <= 9:
            return
        recovered: list[ScoredCandidate] = []
        for index, plan in enumerate(
            generate_profiled_endcap_cylinder_candidates(data, name)
        ):
            try:
                candidate = score_plan(
                    data,
                    plan,
                    destination
                    / "candidates"
                    / f"profiled-endcap-cylinder-{label}-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                surface_types = exported_surface_types(candidate)
                if (
                    geometry_is_acceptable(candidate)
                    and "CYLINDER" in surface_types
                    and "CONE" in surface_types
                    and len(candidate.plan.operations) < len(best.plan.operations)
                ):
                    recovered.append(candidate)
            except Exception as exc:
                warnings.append(
                    "Profiled endcap/cylinder candidate "
                    f"{index + 1} could not be built: {exc}"
                )
        if recovered:
            best = min(
                recovered,
                key=lambda candidate: (
                    len(candidate.plan.operations),
                    candidate.report.score,
                ),
            )
            warnings.append(
                "Replaced a dense slab stack with an editable constant sketch, "
                "measured end transitions, and analytic cylindrical cut."
            )

    def recover_cylindrical_stock(label: str) -> None:
        nonlocal best
        if len(best.plan.operations) <= 9:
            return
        recovered: list[ScoredCandidate] = []
        candidate_index = 0
        for stock_plan in generate_cylindrical_stock_candidates(data, name):
            for extension_factor in (0.5, 0.75, 1.0, 1.5):
                for residual_plan in generate_cross_axis_residual_candidates(
                    data,
                    stock_plan,
                    cut_extension_factor=extension_factor,
                ):
                    try:
                        candidate = score_plan(
                            data,
                            residual_plan,
                            destination
                            / "candidates"
                            / f"cylindrical-stock-{label}-{candidate_index:02d}",
                            candidate_count=len(generated) + candidate_index + 1,
                        )
                        if (
                            geometry_is_acceptable(candidate)
                            and len(candidate.plan.operations)
                            < len(best.plan.operations)
                        ):
                            recovered.append(candidate)
                    except Exception as exc:
                        warnings.append(
                            "Cylindrical-stock residual candidate "
                            f"{candidate_index + 1} could not be built: {exc}"
                        )
                    candidate_index += 1
        if recovered:
            best = min(
                recovered,
                key=lambda candidate: (
                    len(candidate.plan.operations),
                    candidate.report.score,
                ),
            )
            warnings.append(
                "Replaced a dense slab stack with dominant cylindrical stock and "
                "one compact cross-axis residual feature."
            )

    def compact_round_hole_patterns(label: str) -> None:
        nonlocal best
        groups: dict[tuple[Axis, float, float], list[RoundHoleFeature]] = {}
        for operation in best.plan.operations:
            if isinstance(operation, RoundHoleFeature):
                key = (
                    operation.axis,
                    round(operation.start, 5),
                    round(operation.depth, 5),
                )
                groups.setdefault(key, []).append(operation)
        repeated = {key: items for key, items in groups.items() if len(items) >= 2}
        if not repeated:
            return
        compacted = best.plan.model_copy(deep=True)
        emitted: set[tuple[Axis, float, float]] = set()
        operations = []
        for operation in compacted.operations:
            if not isinstance(operation, RoundHoleFeature):
                operations.append(operation)
                continue
            key = (
                operation.axis,
                round(operation.start, 5),
                round(operation.depth, 5),
            )
            if key not in repeated:
                operations.append(operation)
                continue
            if key in emitted:
                continue
            emitted.add(key)
            holes = repeated[key]
            circles = [
                CircleProfile(
                    center=item.center,
                    radius=item.diameter / 2.0,
                )
                for item in holes
            ]
            operations.append(
                BooleanExtrudeFeature(
                    mode="cut",
                    axis=operation.axis,
                    start=operation.start,
                    depth=operation.depth,
                    outer=circles[0],
                    additional_regions=circles[1:],
                )
            )
        compacted.operations = operations
        try:
            candidate = score_plan(
                data,
                compacted,
                destination / "candidates" / f"round-hole-patterns-{label}",
                candidate_count=len(generated) + 1,
            )
            if (
                geometry_is_acceptable(candidate)
                and len(candidate.plan.operations) < len(best.plan.operations)
            ):
                removed = len(best.plan.operations) - len(candidate.plan.operations)
                best = candidate
                warnings.append(
                    f"Combined {removed} repeated round-hole operations into "
                    "shared multi-circle sketch features."
                )
        except Exception as exc:
            warnings.append(f"Round-hole pattern compaction could not be built: {exc}")

    def compact_coplanar_regions(label: str) -> None:
        nonlocal best
        groups: dict[
            tuple[str, Axis, float, float],
            list[BooleanExtrudeFeature],
        ] = {}
        for operation in best.plan.operations:
            if isinstance(operation, BooleanExtrudeFeature) and not operation.holes:
                key = (
                    operation.mode,
                    operation.axis,
                    round(operation.start, 6),
                    round(operation.depth, 6),
                )
                groups.setdefault(key, []).append(operation)
        repeated = {key: items for key, items in groups.items() if len(items) >= 2}
        if not repeated:
            return
        compacted = best.plan.model_copy(deep=True)
        emitted: set[tuple[str, Axis, float, float]] = set()
        operations = []
        for operation in compacted.operations:
            if not isinstance(operation, BooleanExtrudeFeature) or operation.holes:
                operations.append(operation)
                continue
            key = (
                operation.mode,
                operation.axis,
                round(operation.start, 6),
                round(operation.depth, 6),
            )
            if key not in repeated:
                operations.append(operation)
                continue
            if key in emitted:
                continue
            emitted.add(key)
            regions = repeated[key]
            operation.additional_regions = [
                *operation.additional_regions,
                *(
                    profile
                    for item in regions[1:]
                    for profile in [item.outer, *item.additional_regions]
                ),
            ]
            operations.append(operation)
        compacted.operations = operations
        try:
            candidate = score_plan(
                data,
                compacted,
                destination / "candidates" / f"coplanar-regions-{label}",
                candidate_count=len(generated) + 1,
            )
            if (
                geometry_is_acceptable(candidate)
                and len(candidate.plan.operations) < len(best.plan.operations)
            ):
                removed = len(best.plan.operations) - len(candidate.plan.operations)
                best = candidate
                warnings.append(
                    f"Combined {removed} coplanar region operations into shared "
                    "multi-region sketch features."
                )
        except Exception as exc:
            warnings.append(f"Coplanar region compaction could not be built: {exc}")

    def refine_terminal_circular_round(label: str) -> None:
        nonlocal best
        base = best.plan.base
        if not isinstance(base, CylinderFeature):
            return
        additions = [
            operation
            for operation in best.plan.operations
            if isinstance(operation, BooleanExtrudeFeature)
            and operation.mode == "add"
            and operation.axis == base.axis
            and isinstance(operation.outer, CircleProfile)
        ]
        finishes = [
            operation
            for operation in best.plan.operations
            if isinstance(operation, EdgeFinishFeature)
            and operation.mode == "fillet"
            and operation.axis == base.axis
        ]
        if len(additions) != 1 or len(finishes) != 1:
            return
        addition = additions[0]
        end = addition.start + addition.depth
        positions = [
            addition.start + addition.depth * fraction
            for fraction in (0.03, 0.08, 0.15, 0.25, 0.5, 0.75, 0.9, 0.98, 0.995)
        ]
        sections = [section_shape(data, base.axis, position) for position in positions]
        circles = [
            section.outer
            for section in sections
            if section is not None
            and isinstance(section.outer, CircleProfile)
            and not section.holes
            and not section.additional_regions
        ]
        end_section = section_shape(
            data,
            base.axis,
            end - addition.depth * 0.0005,
        )
        if (
            len(circles) < 3
            or end_section is None
            or not isinstance(end_section.outer, CircleProfile)
        ):
            return
        outer_radius = round(max(circle.radius for circle in circles), 2)
        cap_radius = round(end_section.outer.radius, 2)
        fillet_radius = outer_radius - cap_radius
        if fillet_radius <= max(data.diagonal * 1e-4, 0.01):
            return
        refined = best.plan.model_copy(deep=True)
        refined_addition = next(
            operation
            for operation in refined.operations
            if isinstance(operation, BooleanExtrudeFeature)
            and operation.mode == "add"
            and operation.axis == base.axis
            and isinstance(operation.outer, CircleProfile)
        )
        refined_finish = next(
            operation
            for operation in refined.operations
            if isinstance(operation, EdgeFinishFeature)
            and operation.mode == "fillet"
            and operation.axis == base.axis
        )
        assert isinstance(refined_addition.outer, CircleProfile)
        refined_addition.outer.radius = outer_radius
        refined_finish.size = fillet_radius
        try:
            candidate = score_plan(
                data,
                refined,
                destination / "candidates" / f"terminal-circular-round-{label}",
                candidate_count=len(generated) + 1,
            )
            if geometry_is_acceptable(candidate) and (
                not geometry_is_acceptable(best)
                or candidate.report.score < best.report.score
            ):
                best = candidate
                warnings.append(
                    "Refined a terminal circular shoulder from measured stock and "
                    "cap radii, retaining one exact analytic torus."
                )
        except Exception as exc:
            warnings.append(f"Terminal circular-round refinement failed: {exc}")

    def refine_edge_finish_sizes(label: str) -> None:
        nonlocal best
        if geometry_is_acceptable(best):
            return
        finish_indices = [
            index
            for index, operation in enumerate(best.plan.operations)
            if isinstance(operation, EdgeFinishFeature)
        ]
        if not finish_indices or len(finish_indices) > 4:
            return
        refinements: list[ScoredCandidate] = []
        for scale_index, scale in enumerate((1.1, 1.25, 1.5, 1.75, 2.0, 2.5)):
            refined = best.plan.model_copy(deep=True)
            for finish_index in finish_indices:
                operation = refined.operations[finish_index]
                assert isinstance(operation, EdgeFinishFeature)
                operation.size *= scale
                if operation.size2 is not None:
                    operation.size2 *= scale
            try:
                candidate = score_plan(
                    data,
                    refined,
                    destination
                    / "candidates"
                    / f"edge-finish-scale-{label}-{scale_index:02d}",
                    candidate_count=len(generated) + scale_index + 1,
                )
                if geometry_is_acceptable(candidate):
                    refinements.append(candidate)
            except Exception:
                pass
        if refinements:
            best = min(refinements, key=lambda candidate: candidate.report.score)
            warnings.append(
                "Refined edge-finish radii against the full 3D mesh after the "
                "section estimate under-sized the measured round."
            )

    def recover_bounded_adaptive_layers(label: str) -> None:
        nonlocal best
        if geometry_is_acceptable(best):
            if len(best.plan.operations) <= 10 or any(
                isinstance(
                    operation,
                    (
                        ConicalAddFeature,
                        ConicalHoleFeature,
                        OrientedCylinderFeature,
                        SphereFeature,
                    ),
                )
                for operation in best.plan.operations
            ):
                return
        recovered: list[ScoredCandidate] = []
        candidate_index = 0
        for axis in Axis:
            for slice_count in (7, 9, 12):
                for adaptive_plan in generate_adaptive_layer_candidates(
                    data,
                    name,
                    axis,
                    slice_count=slice_count,
                )[:1]:
                    if len(adaptive_plan.operations) > 29:
                        continue
                    try:
                        candidate = score_plan(
                            data,
                            adaptive_plan,
                            destination
                            / "candidates"
                            / f"bounded-adaptive-{label}-{candidate_index:02d}",
                            candidate_count=len(generated) + candidate_index + 1,
                        )
                        if geometry_is_acceptable(candidate):
                            recovered.append(candidate)
                    except Exception as exc:
                        warnings.append(
                            f"Bounded adaptive candidate {candidate_index + 1} "
                            f"could not be built: {exc}"
                        )
                    candidate_index += 1
        if recovered:
            best = min(
                recovered,
                key=lambda candidate: (
                    len(candidate.plan.operations),
                    candidate.report.score,
                ),
            )
            warnings.append(
                "Recovered the remaining multi-extrusion detail with a bounded "
                "adaptive analytic-profile fallback."
            )

    preserve_detected_torus("initial")
    trace_stage("initial_torus_preservation_complete")
    preserve_detected_cones("initial", force=True)
    trace_stage("initial_cone_preservation_complete")

    if (
        not compact_constant_extrusion
        and not bool(axial)
        and (
            not geometry_is_acceptable(best)
            or len(best.plan.operations) > 12
        )
        and has_spatially_curved_patch()
        and search_budget_available()
    ):
        trace_stage("weighted_search_start")
        # Localized rounds need far more uniform slabs than their editable
        # feature budget permits. Probe a fine stack once, then spend layers
        # only where the cross-section changes. The optional final edge finish
        # restores an analytic torus instead of leaving the round faceted.
        base_axis = getattr(best.plan.base, "axis", None)
        weighted_scored: list[ScoredCandidate] = []
        detected_cone = has_conical_patch()
        detected_torus = has_toroidal_patch()

        def weighted_is_acceptable(candidate: ScoredCandidate) -> bool:
            if geometry_is_acceptable(candidate):
                return True
            return (
                detected_torus
                and candidate.report.valid_solid
                and candidate.report.volume_error_percent <= 2.0
                and candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal) * 1.5
                and any(
                    isinstance(operation, EdgeFinishFeature)
                    and operation.mode == "fillet"
                    for operation in candidate.plan.operations
                )
            )
        thin_axis = min(
            Axis,
            key=lambda axis: float(data.mesh.extents[axis_index[axis]]),
        )
        weighted_axes = (
            [base_axis]
            if isinstance(base_axis, Axis)
            else [thin_axis, *(axis for axis in Axis if axis != thin_axis)]
        )
        if (
            isinstance(best.plan.base, OrientedExtrudeFeature)
            and not geometry_is_acceptable(best)
        ):
            layer_budgets = (26,)
        elif detected_torus:
            # Toroidal rounds need meaningful section density. The 12-layer
            # variant is commonly no better than 10 while consuming the score
            # slot needed to reach 24 within the bounded search.
            layer_budgets = (10, 18, 24, 26)
        else:
            layer_budgets = (10, 12, 18, 24, 25, 26)
        weighted_index = 0
        for weighted_axis in weighted_axes:
            for layer_budget in layer_budgets:
                trace_stage(
                    f"weighted_generation_start axis={weighted_axis.value} "
                    f"layers={layer_budget}"
                )
                weighted_candidates = generate_change_weighted_layer_candidates(
                    data,
                    name,
                    weighted_axis,
                    layer_budget=layer_budget,
                )
                trace_stage(
                    f"weighted_generation_done axis={weighted_axis.value} "
                    f"layers={layer_budget} count={len(weighted_candidates)}"
                )
                # Only build finish combinations supported by the target
                # normal field.  Besides avoiding false topology, this keeps
                # the expensive BRep scoring set bounded for large models.
                filtered_weighted: list[ReconstructionPlan] = []
                for weighted_plan in weighted_candidates:
                    finish_modes = {
                        operation.mode
                        for operation in weighted_plan.operations
                        if isinstance(operation, EdgeFinishFeature)
                    }
                    if not finish_modes:
                        filtered_weighted.append(weighted_plan)
                    elif detected_cone and detected_torus:
                        if finish_modes >= {"chamfer", "fillet"}:
                            filtered_weighted.append(weighted_plan)
                    elif detected_cone:
                        if finish_modes == {"chamfer"}:
                            filtered_weighted.append(weighted_plan)
                    elif detected_torus and finish_modes == {"fillet"}:
                        filtered_weighted.append(weighted_plan)
                weighted_candidates = filtered_weighted
                if detected_torus:
                    torus_candidates = [
                        weighted_plan
                        for weighted_plan in weighted_candidates
                        if any(
                            isinstance(operation, EdgeFinishFeature)
                            and operation.mode == "fillet"
                            for operation in weighted_plan.operations
                        )
                    ]
                    if torus_candidates:
                        # A positively detected toroidal target must retain a
                        # fillet. Scoring the otherwise identical plain slab
                        # plan at every layer budget wastes half the bounded
                        # search without satisfying the topology constraint.
                        weighted_candidates = torus_candidates[:1]
                for weighted_plan in weighted_candidates:
                    if not search_budget_available():
                        break
                    try:
                        trace_stage(
                            f"weighted_score_start index={weighted_index} "
                            f"axis={weighted_axis.value} layers={layer_budget} "
                            f"operations={len(weighted_plan.operations)}"
                        )
                        weighted_scored.append(
                            score_plan(
                                data,
                                weighted_plan,
                                destination
                                / "candidates"
                                / f"change-weighted-{weighted_index:02d}",
                                candidate_count=len(generated) + weighted_index + 1,
                            )
                        )
                        trace_stage(
                            f"weighted_score_done index={weighted_index} "
                            f"p95={weighted_scored[-1].report.chamfer_p95_mm:.9g} "
                            f"operations={len(weighted_scored[-1].plan.operations)} "
                            f"preview={weighted_scored[-1].directory.relative_to(destination).as_posix()}"
                            "/reconstruction.stl"
                        )
                    except Exception as exc:
                        trace_stage(
                            f"weighted_score_failed index={weighted_index} "
                            f"error={type(exc).__name__}"
                        )
                        warnings.append(
                            "Change-weighted candidate "
                            f"{weighted_index + 1} could not be built: {exc}"
                        )
                    weighted_index += 1
                if any(
                    weighted_is_acceptable(candidate)
                    and len(candidate.plan.operations) <= 26
                    and any(
                        isinstance(operation, EdgeFinishFeature)
                        for operation in candidate.plan.operations
                    )
                    for candidate in weighted_scored
                ):
                    break
                if not search_budget_available():
                    break
            if any(
                weighted_is_acceptable(candidate)
                and len(candidate.plan.operations) <= 26
                and any(
                    isinstance(operation, EdgeFinishFeature)
                    for operation in candidate.plan.operations
                )
                for candidate in weighted_scored
            ):
                break
        acceptable_weighted = [
            candidate
            for candidate in weighted_scored
            if weighted_is_acceptable(candidate)
            and len(candidate.plan.operations) <= 26
        ]
        if acceptable_weighted:
            analytic_weighted = [
                candidate
                for candidate in acceptable_weighted
                if any(
                    isinstance(operation, EdgeFinishFeature)
                    for operation in candidate.plan.operations
                )
            ]
            mixed_weighted = [
                candidate
                for candidate in analytic_weighted
                if {
                    operation.mode
                    for operation in candidate.plan.operations
                    if isinstance(operation, EdgeFinishFeature)
                }
                >= {"fillet", "chamfer"}
            ]
            conical_weighted = [
                candidate
                for candidate in analytic_weighted
                if any(
                    isinstance(operation, EdgeFinishFeature)
                    and operation.mode == "chamfer"
                    for operation in candidate.plan.operations
                )
            ]
            toroidal_weighted = [
                candidate
                for candidate in analytic_weighted
                if any(
                    isinstance(operation, EdgeFinishFeature)
                    and operation.mode == "fillet"
                    for operation in candidate.plan.operations
                )
            ]
            preferred_pool = (
                mixed_weighted
                if detected_cone and detected_torus and mixed_weighted
                else conical_weighted
                if detected_cone and conical_weighted
                else toroidal_weighted
                if detected_torus and toroidal_weighted
                else analytic_weighted or acceptable_weighted
            )
            weighted_best = min(
                preferred_pool,
                key=lambda candidate: (
                    len(candidate.plan.operations),
                    candidate.report.score,
                ),
            )
            if (
                not geometry_is_acceptable(best)
                or len(weighted_best.plan.operations) < len(best.plan.operations)
                or weighted_best.report.score < best.report.score
            ):
                best = weighted_best
                weighted_layer_complete = True
                warnings.append(
                    "Selected a change-weighted layered construction with an "
                    "analytic curved edge; skipped uniform slab refinements."
                )
        trace_stage(
            f"weighted_search_complete scored={len(weighted_scored)} "
            f"selected={weighted_layer_complete}"
        )

    preserve_detected_torus("post-weighted")
    trace_stage("post_weighted_torus_preservation_complete")

    if (
        not is_turning_plan()
        and (
            not geometry_is_acceptable(best)
            or len(best.plan.operations) > 12
        )
        and search_budget_available()
    ):
        # Cross-axis holes make every direct slice look different. Recovering
        # the small set of stock layers first and subtracting their residuals
        # is both cleaner and more faithful than immediately accepting dozens
        # of adaptive slabs.
        pre_envelope_residual_attempted = True
        compact_envelopes = [
            candidate.plan
            for candidate in prismatic
            if candidate.section_consistency >= 0.85
            and len(candidate.plan.operations) <= 4
        ]
        # A clean cardinal stock candidate can be geometrically close while
        # failing only the volume gate because a perpendicular plate or slot
        # is absent.  It is a better residual source than a later slab
        # envelope and can recover the missing feature in a handful of
        # editable operations.  Keep the shortlist bounded and deterministic.
        compact_envelopes.extend(
            sorted(
                generate_layer_envelope_candidates(data, name),
                key=lambda plan: len(plan.operations),
            )
        )
        pre_residual_index = 0
        for envelope_plan in compact_envelopes:
            for cut_extension_factor in (1.5, 12.0):
                for residual_plan in generate_cross_axis_residual_candidates(
                    data,
                    envelope_plan,
                    cut_extension_factor=cut_extension_factor,
                ):
                    if not search_budget_available():
                        break
                    try:
                        residual_candidate = score_plan(
                            data,
                            residual_plan,
                            destination
                            / "candidates"
                            / f"compact-cross-axis-{pre_residual_index:02d}",
                            candidate_count=len(generated) + pre_residual_index + 1,
                        )
                        if (
                            residual_candidate.report.score < best.report.score
                            or (
                                geometry_is_acceptable(residual_candidate)
                                and len(residual_candidate.plan.operations)
                                < len(best.plan.operations)
                            )
                        ):
                            best = residual_candidate
                    except Exception as exc:
                        warnings.append(
                            "Compact cross-axis candidate "
                            f"{pre_residual_index + 1} could not be built: {exc}"
                        )
                    pre_residual_index += 1
                    if compact_result_found():
                        break
                if compact_result_found() or not search_budget_available():
                    break
            if compact_result_found() or not search_budget_available():
                break
        compact_residual_complete = compact_result_found()
        if compact_residual_complete:
            warnings.append(
                "Selected a compact stock-and-residual construction; skipped "
                "slab and duplicate analytic refinements."
            )

    hole_score_index = 0
    for _hole_pass in range(4):
        if (
            is_turning_plan()
            or dense_adaptive_complete
            or not search_budget_available()
        ):
            break
        accepted_hole_group = False
        # Regenerate after each accepted group. Candidates from the previous
        # plan are alternatives, not deltas; continuing through that stale
        # list used to let a later single hole replace an already accepted
        # repeated-diameter group.
        hole_plans = generate_round_hole_candidates(data, best.plan)
        for hole_plan in hole_plans:
            if not search_budget_available():
                break
            try:
                hole_candidate = score_plan(
                    data,
                    hole_plan,
                    destination
                    / "candidates"
                    / f"holes-{hole_score_index:02d}",
                    candidate_count=len(generated) + hole_score_index + 1,
                )
                geometrically_equivalent = (
                    hole_candidate.report.valid_solid
                    and hole_candidate.report.chamfer_p95_mm
                    <= best.report.chamfer_p95_mm
                    + max(
                        # Open meshes have no volume check and often omit one
                        # side of tiny drilled details. Give a detected
                        # analytic hole enough surface-distance headroom to
                        # win while requiring the completed solid to remain
                        # inside the normal gate.
                        0.02 if not data.report.watertight else 0.01,
                        data.diagonal
                        * (0.001 if not data.report.watertight else 0.0002),
                    )
                    and hole_candidate.report.volume_error_percent
                    <= best.report.volume_error_percent + 0.05
                    and geometry_is_acceptable(hole_candidate)
                )
                if (
                    hole_candidate.report.score < best.report.score
                    or geometrically_equivalent
                ):
                    best = hole_candidate
                    accepted_hole_group = True
                    hole_score_index += 1
                    break
            except Exception as exc:
                warnings.append(
                    f"Round-hole candidate {hole_score_index + 1} "
                    f"could not be built: {exc}"
                )
            hole_score_index += 1
        if not accepted_hole_group:
            break

    for index, conical_plan in enumerate(
        []
        if is_turning_plan() or dense_adaptive_complete
        else generate_conical_hole_candidates(data, best.plan)
    ):
        if not search_budget_available():
            break
        try:
            conical_candidate = score_plan(
                data,
                conical_plan,
                destination / "candidates" / f"cones-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            geometrically_equivalent = (
                conical_candidate.report.valid_solid
                and conical_candidate.report.chamfer_p95_mm
                <= best.report.chamfer_p95_mm + max(0.01, data.diagonal * 0.0002)
                and conical_candidate.report.volume_error_percent
                <= best.report.volume_error_percent + 0.05
            )
            if (
                conical_candidate.report.score < best.report.score
                or geometrically_equivalent
            ):
                best = conical_candidate
            if conical_candidate.report.valid_solid:
                analytic_refinement_sources.append(conical_candidate.plan)
        except Exception as exc:
            warnings.append(
                f"Conical-hole candidate {index + 1} could not be built: {exc}"
            )

    for index, conical_plan in enumerate(
        []
        if is_turning_plan() or dense_adaptive_complete
        else generate_conical_add_candidates(data, best.plan)
    ):
        if not search_budget_available():
            break
        try:
            conical_candidate = score_plan(
                data,
                conical_plan,
                destination / "candidates" / f"cone-adds-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            if conical_candidate.report.score < best.report.score:
                best = conical_candidate
        except Exception as exc:
            warnings.append(
                f"Conical-add candidate {index + 1} could not be built: {exc}"
            )

    for index, cylinder_plan in enumerate(
        []
        if (is_turning_plan() and not compact_residual_complete)
        or dense_adaptive_complete
        else generate_oriented_cylinder_candidates(data, best.plan)
    ):
        if not search_budget_available():
            break
        try:
            cylinder_candidate = score_plan(
                data,
                cylinder_plan,
                destination / "candidates" / f"oriented-cylinders-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            if cylinder_candidate.report.score < best.report.score:
                best = cylinder_candidate
        except Exception as exc:
            warnings.append(
                f"Oriented-cylinder candidate {index + 1} could not be built: "
                f"{exc}"
            )

    if (
        not is_turning_plan()
        and not dense_adaptive_complete
        and data.report.triangle_count >= 5000
        and (
            not best.report.valid_solid
            or best.report.chamfer_p95_mm
            > _acceptance_threshold(data.diagonal) * 1.5
        )
    ):
        adaptive_specs = [(best.plan.base.axis, 24)]
        adaptive_specs.extend(
            (axis, 32) for axis in Axis if axis != best.plan.base.axis
        )
        for spec_index, (adaptive_axis, slice_count) in enumerate(adaptive_specs):
            for candidate_index, adaptive_plan in enumerate(
                generate_adaptive_layer_candidates(
                    data,
                    name,
                    adaptive_axis,
                    slice_count,
                )
            ):
                if not search_budget_available():
                    break
                try:
                    adaptive_candidate = score_plan(
                        data,
                        adaptive_plan,
                        destination
                        / "candidates"
                        / (
                            f"adaptive-{adaptive_axis.value.lower()}-"
                            f"{slice_count}-{candidate_index:02d}"
                        ),
                        candidate_count=len(generated) + candidate_index + 1,
                    )
                    topology_preserving_equivalent = (
                        any(
                            isinstance(operation, EdgeFinishFeature)
                            for operation in adaptive_candidate.plan.operations
                        )
                        and adaptive_candidate.report.valid_solid
                        and adaptive_candidate.report.chamfer_p95_mm
                        <= best.report.chamfer_p95_mm
                        + max(0.01, data.diagonal * 0.0002)
                        and adaptive_candidate.report.volume_error_percent
                        <= best.report.volume_error_percent + 0.05
                    )
                    if (
                        adaptive_candidate.report.score < best.report.score
                        or topology_preserving_equivalent
                    ):
                        best = adaptive_candidate
                except Exception as exc:
                    warnings.append(
                        "Adaptive-layer candidate "
                        f"{spec_index + 1}.{candidate_index + 1} "
                        f"could not be built: {exc}"
                    )
            if (
                best.report.valid_solid
                and best.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and best.report.volume_error_percent <= 2.0
                and any(
                    isinstance(operation, EdgeFinishFeature)
                    for operation in best.plan.operations
                )
            ):
                break

    if (
        not is_turning_plan()
        and (
            best.report.chamfer_p95_mm
            > _acceptance_threshold(data.diagonal) * 1.5
            or best.report.volume_error_percent > 4.0
            or (
                analytic_refinement_sources
                and best.report.chamfer_p95_mm
                > _acceptance_threshold(data.diagonal)
            )
        )
        and not (
            best.report.valid_solid
            and best.report.chamfer_p95_mm <= _acceptance_threshold(data.diagonal)
            and best.report.volume_error_percent <= 2.0
        )
    ):
        residual_index = 0

        def score_residual_source(residual_source: ReconstructionPlan) -> None:
            nonlocal best, residual_index
            for cut_extension_factor in (1.5, 12.0):
                for residual_plan in generate_cross_axis_residual_candidates(
                    data,
                    residual_source,
                    cut_extension_factor=cut_extension_factor,
                ):
                    if not search_budget_available():
                        return
                    try:
                        residual_candidate = score_plan(
                            data,
                            residual_plan,
                            destination
                            / "candidates"
                            / f"cross-axis-{residual_index:02d}",
                            candidate_count=len(generated) + residual_index + 1,
                        )
                        if residual_candidate.report.score < best.report.score:
                            best = residual_candidate
                    except Exception as exc:
                        warnings.append(
                            "Cross-axis residual candidate "
                            f"{residual_index + 1} could not be built: {exc}"
                        )
                    residual_index += 1

        # Refine the already-selected analytic plan first.  In particular, this
        # lets a detected cone/counterbore combine with an orthogonal slot before
        # the much more expensive stock-envelope fallbacks consume the budget.
        score_residual_source(best.plan)

        if not geometry_is_acceptable(best) and search_budget_available():
            for analytic_plan in analytic_refinement_sources:
                score_residual_source(analytic_plan)
                if geometry_is_acceptable(best) or not search_budget_available():
                    break

        if not geometry_is_acceptable(best) and search_budget_available():
            for envelope_plan in generate_envelope_candidates(data, name):
                score_residual_source(envelope_plan)
                if geometry_is_acceptable(best) or not search_budget_available():
                    break

        if (
            not pre_envelope_residual_attempted
            and not geometry_is_acceptable(best)
            and search_budget_available()
        ):
            for envelope_plan in generate_layer_envelope_candidates(data, name):
                score_residual_source(envelope_plan)
                if geometry_is_acceptable(best) or not search_budget_available():
                    break

    if (
        not is_turning_plan()
        and not geometry_is_acceptable(best)
        and search_budget_available()
    ):
        for index, cone_candidate in enumerate(
            generate_partial_cone_adaptive_candidates(data, name)
        ):
            if not search_budget_available():
                break
            try:
                scored_cone_candidate = score_plan(
                    data,
                    cone_candidate.plan,
                    destination
                    / "candidates"
                    / f"partial-cone-adaptive-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                if scored_cone_candidate.report.score < best.report.score:
                    best = scored_cone_candidate
            except Exception as exc:
                warnings.append(
                    f"Partial-cone candidate {index + 1} could not be built: "
                    f"{exc}"
                )

    def spatial_curves_are_encoded(candidate: ScoredCandidate) -> bool:
        return isinstance(
            candidate.plan.base,
            (RevolveFeature, OrientedExtrudeFeature),
        ) or any(
            isinstance(
                operation,
                (
                    ConicalAddFeature,
                    ConicalHoleFeature,
                    EdgeFinishFeature,
                    OrientedCylinderFeature,
                    TaperedAddFeature,
                ),
            )
            for operation in candidate.plan.operations
        )

    analytic_patch_types_satisfied = False
    if geometry_is_acceptable(best):
        current_surface_types = exported_surface_types(best)
        analytic_patch_types_satisfied = (
            (not has_conical_patch() or "CONE" in current_surface_types)
            and (not has_toroidal_patch() or "TORUS" in current_surface_types)
        )
    skip_curve_search = analytic_patch_types_satisfied or (
        geometry_is_acceptable(best)
        and spatial_curves_are_encoded(best)
        and not has_spatially_curved_patch()
    )
    circular_refinements = (
        []
        if is_turning_plan() or skip_curve_search
        else generate_circular_end_finish_candidates(
            data,
            [
                PlanCandidate(
                    plan=best.plan,
                    heuristic_score=0.0,
                    section_consistency=1.0,
                )
            ],
        )
    )
    acceptable_refinements: list[ScoredCandidate] = []
    for index, refinement in enumerate(circular_refinements[:12]):
        if not search_budget_available():
            break
        try:
            refined_candidate = score_plan(
                data,
                refinement.plan,
                destination / "candidates" / f"circle-finishes-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            acceptable = (
                refined_candidate.report.valid_solid
                and refined_candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and refined_candidate.report.volume_error_percent
                <= 2.0
            )
            if acceptable:
                acceptable_refinements.append(refined_candidate)
        except Exception as exc:
            warnings.append(
                f"Circular edge-finish refinement {index + 1} could not be built: "
                f"{exc}"
            )
    if acceptable_refinements:
        best = min(
            acceptable_refinements,
            key=lambda candidate: candidate.report.score,
        )

    feature_refinements: list[ScoredCandidate] = []
    for index, refinement in enumerate(
        []
        if is_turning_plan() or skip_curve_search
        else generate_feature_round_finish_candidates(data, best.plan)[:12]
    ):
        if not search_budget_available():
            break
        try:
            candidate = score_plan(
                data,
                refinement,
                destination / "candidates" / f"feature-finishes-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            if (
                candidate.report.valid_solid
                and candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and candidate.report.volume_error_percent <= 2.0
            ):
                feature_refinements.append(candidate)
        except Exception as exc:
            warnings.append(
                f"Feature-local finish refinement {index + 1} could not be built: "
                f"{exc}"
            )
    if feature_refinements:
        best_feature = min(
            feature_refinements,
            key=lambda candidate: candidate.report.score,
        )
        best = best_feature
        second_feature_refinements: list[ScoredCandidate] = []
        for index, refinement in enumerate(
            generate_feature_round_finish_candidates(data, best.plan)[:12]
        ):
            if not search_budget_available():
                break
            try:
                candidate = score_plan(
                    data,
                    refinement,
                    destination
                    / "candidates"
                    / f"feature-finishes-second-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                if geometry_is_acceptable(candidate):
                    second_feature_refinements.append(candidate)
            except Exception as exc:
                warnings.append(
                    "Second feature-local finish refinement "
                    f"{index + 1} could not be built: {exc}"
                )
        if second_feature_refinements:
            best = min(
                second_feature_refinements,
                key=lambda candidate: (
                    -len(
                        {
                            operation.mode
                            for operation in candidate.plan.operations
                            if isinstance(operation, EdgeFinishFeature)
                        }
                    ),
                    candidate.report.score,
                ),
            )

    boundary_refinements: list[ScoredCandidate] = []
    for index, refinement in enumerate(
        []
        if is_turning_plan() or skip_curve_search
        else generate_round_boundary_feature_candidates(data, best.plan)[:12]
    ):
        if not search_budget_available():
            break
        try:
            candidate = score_plan(
                data,
                refinement,
                destination / "candidates" / f"round-boundaries-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            if (
                candidate.report.valid_solid
                and candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and candidate.report.volume_error_percent <= 2.0
            ):
                boundary_refinements.append(candidate)
        except Exception as exc:
            warnings.append(
                f"Round-boundary refinement {index + 1} could not be built: {exc}"
            )
    if boundary_refinements:
        best = min(
            boundary_refinements,
            key=lambda candidate: candidate.report.score,
        )

    conical_boundary_refinements: list[ScoredCandidate] = []
    for index, refinement in enumerate(
        []
        if is_turning_plan() or skip_curve_search
        else generate_conical_boundary_candidates(data, best.plan)[:12]
    ):
        if not search_budget_available():
            break
        try:
            candidate = score_plan(
                data,
                refinement,
                destination / "candidates" / f"conical-boundaries-{index:02d}",
                candidate_count=len(generated) + index + 1,
            )
            if (
                candidate.report.valid_solid
                and candidate.report.chamfer_p95_mm
                <= _acceptance_threshold(data.diagonal)
                and candidate.report.volume_error_percent <= 2.0
            ):
                conical_boundary_refinements.append(candidate)
        except Exception as exc:
            warnings.append(
                f"Conical-boundary refinement {index + 1} could not be built: "
                f"{exc}"
            )
    if conical_boundary_refinements:
        best = min(
            conical_boundary_refinements,
            key=lambda candidate: candidate.report.score,
        )

    if (
        geometry_is_acceptable(best)
        and not compact_constant_extrusion
        and not compact_residual_complete
        and not isinstance(
            best.plan.base,
            (RevolveFeature, OrientedExtrudeFeature),
        )
        and not any(
            isinstance(operation, EdgeFinishFeature)
            for operation in best.plan.operations
        )
    ):
        transition_candidates: list[ScoredCandidate] = []
        for index, refinement in enumerate(
            generate_axial_circle_transition_candidates(data, best.plan)
        ):
            if not search_budget_available():
                break
            try:
                candidate = score_plan(
                    data,
                    refinement,
                    destination
                    / "candidates"
                    / f"axial-circle-transition-{index:02d}",
                    candidate_count=len(generated) + index + 1,
                )
                if (
                    geometry_is_acceptable(candidate)
                    and candidate.report.chamfer_p95_mm
                    <= best.report.chamfer_p95_mm
                    + max(0.01, data.diagonal * 0.0002)
                    and candidate.report.volume_error_percent
                    <= best.report.volume_error_percent + 0.05
                ):
                    transition_candidates.append(candidate)
            except Exception as exc:
                warnings.append(
                    f"Axial circular-transition candidate {index + 1} "
                    f"could not be built: {exc}"
                )
        if transition_candidates:
            best = min(
                transition_candidates,
                key=lambda candidate: candidate.report.score,
            )

    # Some analytic circular edges only exist after residual and axial feature
    # recovery.  Give that final exported plan one small, topology-focused pass
    # even when the broader exploratory search has consumed its CPU budget.
    final_refinements: list[tuple[str, Callable[[], None]]] = [
        (
            "recover_curve_aligned_layering",
            lambda: recover_curve_aligned_layering("final"),
        ),
        (
            "promote_embedded_profile_circles",
            lambda: promote_embedded_profile_circles("final"),
        ),
        (
            "recover_local_tangent_envelopes",
            lambda: recover_local_tangent_envelopes("final"),
        ),
        ("compact_coplanar_regions", lambda: compact_coplanar_regions("final")),
        ("compact_round_hole_patterns", lambda: compact_round_hole_patterns("final")),
        (
            "refine_terminal_circular_round",
            lambda: refine_terminal_circular_round("final"),
        ),
        ("refine_edge_finish_sizes", lambda: refine_edge_finish_sizes("final")),
        (
            "recover_bounded_adaptive_layers",
            lambda: recover_bounded_adaptive_layers("final"),
        ),
        ("recover_compact_axial_cap", lambda: recover_compact_axial_cap("final")),
        (
            "recover_profiled_endcap_cylinder",
            lambda: recover_profiled_endcap_cylinder("final"),
        ),
        ("recover_smooth_loft", lambda: recover_smooth_loft("final")),
        ("recover_cylindrical_stock", lambda: recover_cylindrical_stock("final")),
        ("preserve_detected_spheres", lambda: preserve_detected_spheres("final")),
        (
            "preserve_detected_cylinders",
            lambda: preserve_detected_cylinders("final"),
        ),
        (
            "preserve_detected_torus",
            lambda: preserve_detected_torus("final", force=True),
        ),
        (
            "preserve_detected_cones",
            lambda: preserve_detected_cones("final", force=True),
        ),
    ]
    for refinement_name, refinement in final_refinements:
        trace_stage(
            f"final_refinement_start name={refinement_name} "
            f"operations={len(best.plan.operations)}"
        )
        refinement()
        trace_stage(
            f"final_refinement_done name={refinement_name} "
            f"operations={len(best.plan.operations)} "
            f"preview={best.directory.relative_to(destination).as_posix()}"
            "/reconstruction.stl"
        )

    edge_finish_count = sum(
        isinstance(operation, EdgeFinishFeature)
        for operation in best.plan.operations
    )
    if edge_finish_count and search_budget_available():
        stripped_plan = best.plan.model_copy(deep=True)
        stripped_plan.operations = [
            operation
            for operation in stripped_plan.operations
            if not isinstance(operation, EdgeFinishFeature)
        ]
        try:
            stripped_candidate = score_plan(
                data,
                stripped_plan,
                destination / "candidates" / "redundant-finishes-removed",
                candidate_count=len(generated) + 1,
            )
            numerical_tolerance = max(0.00025, data.diagonal * 0.00001)
            full_surface_types = {
                face.geomType() for face in build_plan(best.plan).val().Faces()
            }
            stripped_surface_types = {
                face.geomType()
                for face in build_plan(stripped_plan).val().Faces()
            }
            analytic_types = {
                "BSPLINE",
                "BEZIER",
                "CONE",
                "CYLINDER",
                "EXTRUSION",
                "SPHERE",
                "TORUS",
            }
            preserves_analytic_types = not (
                (full_surface_types & analytic_types)
                - (stripped_surface_types & analytic_types)
            )
            if (
                geometry_is_acceptable(stripped_candidate)
                and preserves_analytic_types
                and stripped_candidate.report.chamfer_p95_mm
                <= best.report.chamfer_p95_mm + numerical_tolerance
                and stripped_candidate.report.chamfer_rms_mm
                <= best.report.chamfer_rms_mm + numerical_tolerance
                and stripped_candidate.report.volume_error_percent
                <= best.report.volume_error_percent - 0.001
                and stripped_candidate.report.score < best.report.score
            ):
                best = stripped_candidate
                warnings.append(
                    f"Removed {edge_finish_count} geometrically redundant edge "
                    "finishes after the simpler solid measured closer to the mesh."
                )
        except Exception as exc:
            warnings.append(
                f"The redundant edge-finish check could not be built: {exc}"
            )

    if prompt.strip():
        if is_configured():
            ai_result = revise_plan(data.report, best.plan, prompt.strip())
            if ai_result.warning:
                warnings.append(ai_result.warning)
            if ai_result.plan is not None:
                try:
                    ai_candidate = score_plan(
                        data,
                        ai_result.plan,
                        destination / "candidates" / "ai",
                        candidate_count=len(generated) + 1,
                    )
                    if ai_candidate.report.score <= best.report.score * 1.08:
                        best = ai_candidate
                    else:
                        warnings.append(
                            "The AI revision reduced geometric fidelity and was not selected."
                        )
                except Exception as exc:
                    warnings.append(f"The AI plan could not produce valid CAD: {exc}")
        else:
            warnings.append(
                "The instruction was saved, but no LLM is configured; deterministic "
                "reconstruction was used."
            )

    trace_stage(f"export_start operations={len(best.plan.operations)}")
    export_plan(best.plan, destination)
    threshold = _acceptance_threshold(data.diagonal)
    passed = (
        best.report.valid_solid
        and best.report.chamfer_p95_mm <= threshold
        and best.report.volume_error_percent <= 2.0
        and best.plan.representation == "semantic"
    )
    if best.plan.representation == "sampled_approximation":
        warnings.append(
            "The closest geometry is a sampled approximation, not a clean "
            "semantic feature reconstruction; it was not marked complete."
        )
    if not passed:
        warnings.append(
            "The closest supported editable construction does not meet the automatic "
            "high-confidence tolerance; inspect the preview before manufacturing."
        )

    report = ReconstructionReport(
        id=run_id,
        status="complete" if passed else "best_effort",
        mesh=data.report,
        plan=best.plan,
        score=best.report,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - started,
        prompt=prompt,
    )
    save_report(report)
    trace_stage(
        f"reconstruction_complete operations={len(best.plan.operations)} "
        "preview=reconstruction.stl"
    )

    candidates = destination / "candidates"
    if candidates.exists():
        shutil.rmtree(candidates)
    return report

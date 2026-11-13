"""Resume isolated conversions followed by a pinned retained-output grader.

Only standard-library imports belong here: the production bootstrap verifies the
grader before importing application code, then uses its sealed application tree.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = (
    "frozen_evaluation_controller.py",
    "isolated_conversion.py",
    "windows_evaluation_sandbox.py",
    "freeze_evaluation_converter.py",
    "freeze_evaluation_grader.py",
    "process_resources.py",
    "multires_summary.py",
    "analyze_surface_failures.py",
    "sealed_runtime.py",
    "locked_artifact_identity.py",
)
POOLS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)
CONVERSION_TIMEOUT, CLEANUP_TIMEOUT, GRADING_TIMEOUT = 240, 10, 360
CASE_TIMEOUT = 360
CACHE_SCHEMA = "frozen-controller-completed-v1"


@dataclass(frozen=True)
class Configuration:
    manifest: Path
    converter: Path
    converter_sha256: str
    grader: Path
    grader_sha256: str
    profile: str
    owned_root: Path
    output: Path
    artifacts: Path | None = None
    batch_cases: int | None = None
    split: str | None = None


class ControllerError(RuntimeError):
    pass


class FixtureError(ControllerError):
    pass


class GraderError(ControllerError):
    pass


class CaseTimeout(TimeoutError):
    pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CaseTimeout("Original 360-second whole-case deadline exhausted")
    return remaining


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_bytes(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _hash(value) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _inventory_hash(value) -> str:
    # Matches freeze_evaluation_grader.ENTRY's explicit runtime-inventory hash.
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _linked(path: Path) -> bool:
    info = path.stat(follow_symlinks=False)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _owned(path: Path, root: Path, *, create_parent=False) -> Path:
    root = Path(root)
    if _linked(root) or not root.is_dir():
        raise ControllerError("Use an existing, unlinked task-owned root")
    root = root.resolve(strict=True)
    raw = Path(path).absolute()
    resolved = raw.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ControllerError("Controller writes must stay below the explicit owned root")
    for ancestor in (raw, *raw.parents):
        if ancestor == root:
            break
        if ancestor.exists() and _linked(ancestor):
            raise ControllerError("Controller paths cannot traverse reparse points")
    if create_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _plain(path: Path, root: Path) -> None:
    _owned(path, root)
    info = path.stat(follow_symlinks=False)
    if _linked(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ControllerError("Expected an unlinked regular controller/output file")


def _scratch_root(root: Path) -> str:
    root = Path(root)
    if not root.is_dir() or _linked(root):
        raise ControllerError("Grader scratch root must be an existing unlinked owned directory")
    return str(root.resolve(strict=True))


def _atomic(path: Path, value, root: Path, *, exclusive=False) -> None:
    path = _owned(path, root, create_parent=True)
    # Keep the sibling temporary short: a valid 64-character cache filename
    # can otherwise exceed Windows MAX_PATH when copied into the temp name.
    temporary = path.with_name(".write-" + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    if exclusive:
        if os.name == "nt":
            os.rename(temporary, path)  # Windows refuses an existing destination.
        else:
            os.link(temporary, path)
            temporary.unlink()  # Only this successful write's own temporary file.
    else:
        if path.exists():
            _plain(path, root)
        os.replace(temporary, path)


def _retained_hashes(retained: Path, root: Path) -> dict[str, str]:
    result = {}
    for path in retained.iterdir():
        _plain(path, root)
        result[path.name] = _digest(path)
    return dict(sorted(result.items()))


def _resolved_case(case: dict, parent: Path) -> dict:
    result = dict(case)
    for field in ("stl", "source_step"):
        path = Path(case[field])
        result[field] = str((path if path.is_absolute() else parent / path).resolve())
    result.setdefault("input_units", "mm")
    return result


def _cases(manifest: dict, parent: Path, split: str | None) -> list[dict]:
    rows = manifest["cases"]
    ids, pairs, sources, families = set(), set(), {}, {}
    for row in rows:
        required = (
            "id",
            "source_model_id",
            "preset",
            "split",
            "stl",
            "source_step",
            "input_sha256",
            "source_sha256",
        )
        if any(not row.get(field) for field in required):
            raise ControllerError(
                "Frozen cases require source, preset, split and pinned fixture fields"
            )
        pair = (row["source_model_id"], row["preset"])
        if row["id"] in ids or pair in pairs:
            raise ControllerError("Duplicate case or source/preset identity")
        ids.add(row["id"])
        pairs.add(pair)
        if row["split"] not in ("development", "validation", "holdout"):
            raise ControllerError("Unknown frozen split")
        for groups, key in (
            (sources, row["source_model_id"]),
            (families, row.get("geometry_family", row["source_model_id"])),
        ):
            if key in groups and groups[key] != row["split"]:
                raise ControllerError("Source model or geometry family crosses splits")
            groups[key] = row["split"]
    return [_resolved_case(row, parent) for row in rows if split is None or row["split"] == split]


def _fixture(case: dict) -> dict[str, str]:
    actual = {}
    try:
        for field, expected in (("stl", "input_sha256"), ("source_step", "source_sha256")):
            actual[field] = _digest(Path(case[field]))
            if actual[field] != case[expected]:
                raise FixtureError("Frozen source/input differs from its pinned hash")
    except OSError as exc:
        raise FixtureError(f"Frozen fixture is unavailable: {type(exc).__name__}") from exc
    return actual


def _tool_identity() -> dict[str, str]:
    return {"tools/" + name: _digest(ROOT / "tools" / name) for name in TOOLS}


def _load_tool(name: str):
    expected = ROOT / "tools" / (name + ".py")
    package = importlib.import_module("tools")
    module_name = "tools." + name
    existing = sys.modules.get(module_name)
    if existing is not None and Path(existing.__file__).resolve() == expected.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(module_name, expected)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    setattr(package, name, module)
    return module


def _assert_app_origins(application: Path) -> None:
    for name, module in tuple(sys.modules.items()):
        if name == "app" or name.startswith("app."):
            origin = getattr(module, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(application):
                raise ControllerError(
                    "Application code was loaded outside the sealed grader artifact"
                )


def _require_runtime(metadata: dict) -> None:
    runtime = Path(metadata["python_runtime"]).resolve(strict=True)
    executable = runtime / ("python.exe" if sys.platform == "win32" else "bin/python")
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ControllerError("Launch the pinned grader Python with -I -B")
    if Path(sys.executable).resolve() != executable or Path(sys.prefix).resolve() != runtime:
        raise ControllerError("Controller must use the pinned grader runtime interpreter")


def _configure_process(root: Path) -> dict:
    if os.name != "nt":
        raise ControllerError("The production AppContainer controller requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = handle
    kernel.GetProcessAffinityMask.argtypes = [
        handle,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.SetProcessAffinityMask.argtypes = [handle, ctypes.c_size_t]
    kernel.SetPriorityClass.argtypes = [handle, ctypes.c_ulong]
    process = kernel.GetCurrentProcess()
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(process, ctypes.byref(mask), ctypes.byref(system)):
        raise ctypes.WinError(ctypes.get_last_error())
    chosen = mask.value & -mask.value
    if not chosen or not kernel.SetProcessAffinityMask(process, chosen):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.SetPriorityClass(process, 0x4000):
        raise ctypes.WinError(ctypes.get_last_error())
    for name in POOLS:
        os.environ[name] = "1"
    state = root / ("controller-process-" + uuid.uuid4().hex)
    state.mkdir(exist_ok=False)
    for name, folder in (
        ("STL_TO_STEP_RUNS_DIR", "runs"),
        ("MPLCONFIGDIR", "mpl"),
        ("TEMP", "tmp"),
        ("TMP", "tmp"),
        ("TMPDIR", "tmp"),
    ):
        target = state / folder
        target.mkdir(exist_ok=True)
        os.environ[name] = str(target)
    return {
        "cpu_count": 1,
        "affinity_mask": chosen,
        "priority": "below_normal",
        "thread_pools": {name: os.environ[name] for name in POOLS},
    }


class _NativeBackend:
    def initialise(self, configuration: Configuration):
        self._contexts = ExitStack()
        self.configuration = configuration
        sys.path.insert(0, str(ROOT))  # Trusted controller tools; no app imported here.
        self.converter_builder = _load_tool("freeze_evaluation_converter")
        self.grader_builder = _load_tool("freeze_evaluation_grader")
        self.sealed = _load_tool("sealed_runtime")
        self.locked = _load_tool("locked_artifact_identity")
        self.grader_metadata, grader_catalogs = self.locked.artifact_catalogs(
            configuration.grader, configuration.grader_sha256, expected_kind="grader"
        )
        self.converter_metadata, converter_catalogs = self.locked.artifact_catalogs(
            configuration.converter, configuration.converter_sha256, expected_kind="converter"
        )
        _require_runtime(self.grader_metadata)
        catalogs = {}
        for roots in (grader_catalogs, converter_catalogs):
            for root, files in roots.items():
                _owned(root, configuration.owned_root)
                if root in catalogs and catalogs[root] != files:
                    raise ControllerError("Shared runtime catalogs disagree")
                catalogs[root] = files
        self.verification_catalogs = catalogs
        self.verification_session = self._contexts.enter_context(
            self.sealed.SealedRuntimeSession(
                catalogs, module_sha256=_digest(Path(self.sealed.__file__))
            )
        )
        self.grader_metadata, self.session_trace = self.locked.verify_locked_artifact(
            configuration.grader,
            configuration.grader_sha256,
            self.verification_session,
            expected_kind="grader",
        )
        self.converter_metadata, converter_trace = self.locked.verify_locked_artifact(
            configuration.converter,
            configuration.converter_sha256,
            self.verification_session,
            expected_kind="converter",
        )
        if converter_trace != self.session_trace:
            raise ControllerError("Artifact roles do not share the same live verification session")
        if self.grader_metadata.get("entrypoint") != "application/grader.pyc":
            raise ControllerError("Unsupported frozen grader entrypoint")
        if (
            self.grader_metadata["entry_source_sha256"]
            != hashlib.sha256(self.grader_builder.ENTRY.encode()).hexdigest()
        ):
            raise ControllerError("Pinned grader ENTRY differs from this controller contract")
        self.application = (configuration.grader / "application").resolve(strict=True)
        _assert_app_origins(self.application)
        self.resources = _load_tool("process_resources")
        _load_tool("analyze_surface_failures")
        self.aggregate = _load_tool("multires_summary").aggregate_details
        self.process_contract = _configure_process(configuration.owned_root)
        sys.path.insert(0, str(self.application))
        importlib.invalidate_caches()
        self.adapter = _load_tool("isolated_conversion")
        self.runner = importlib.import_module("app.jobs").run_bounded_process
        _assert_app_origins(self.application)
        self.allowed_outputs = set(self.grader_builder.OUTPUT_FILES)

    def verify(self):
        # Both complete artifacts (including their pinned manifests/licenses)
        # are held by this same live seal. Check their union once rather than
        # scanning the shared runtime separately for each role on every case.
        trace = self.verification_session.require_catalogs(self.verification_catalogs)
        if trace != self.session_trace:
            raise ControllerError("Live verification session identity changed")
        _assert_app_origins(self.application)

    def close(self):
        contexts = getattr(self, "_contexts", None)
        if contexts is not None:
            contexts.close()  # Final check and close of this backend's owned handles only.

    def identity(self) -> dict:
        config = self.configuration
        return {
            "backend": "isolated-converter-frozen-grader-controller-v1",
            "tools_sha256": _tool_identity(),
            "converter_manifest_sha256": config.converter_sha256,
            "grader_manifest_sha256": config.grader_sha256,
            "converter_source_revision": self.converter_metadata["source_revision"],
            "grader_source_revision": self.grader_metadata["source_revision"],
            "converter_runtime_inventory_sha256": _inventory_hash(
                self.converter_metadata["runtime_files"]
            ),
            "grader_runtime_inventory_sha256": _inventory_hash(
                self.grader_metadata["runtime_files"]
            ),
            "grader_dependencies_sha256": _inventory_hash(self.grader_metadata["dependency_files"]),
            "platform": {
                "python": sys.version,
                "platform": platform.platform(),
                "machine": platform.machine(),
            },
            "process": self.process_contract,
            "profile": config.profile,
            "deadlines": {
                "whole_case": CASE_TIMEOUT,
                "conversion_maximum": CONVERSION_TIMEOUT,
                "cleanup": CLEANUP_TIMEOUT,
                "grading_maximum": GRADING_TIMEOUT,
                "grading_policy": "remaining whole-case budget",
            },
            "retain_failure_artifacts": config.artifacts is not None,
            "verification_policy": {
                "kind": "live-write-delete-blocking-session-v1",
                "catalog_sha256": self.session_trace,
                "initial_full_hash": True,
                "per_case": "live exact-catalog and directory-notification checks",
                "child_grader_full_verification": True,
            },
        }

    def convert(self, *, input_stl, input_sha256, units, output_dir, case_deadline):
        config = self.configuration
        return self.adapter.convert_isolated(
            config.converter,
            config.converter_sha256,
            profile=config.profile,
            owned_root=config.owned_root,
            input_stl=input_stl,
            input_sha256=input_sha256,
            output_dir=output_dir,
            units=units,
            case_deadline=case_deadline,
            verification_session=self.verification_session,
        )

    def grade(
        self,
        case_configuration: Path,
        retained: Path,
        destination: Path,
        artifacts: Path | None,
        *,
        timeout: float,
    ):
        config = self.configuration
        runtime = Path(self.grader_metadata["python_runtime"])
        command = [
            str(runtime / "python.exe"),
            "-I",
            "-B",
            str(config.grader / self.grader_metadata["entrypoint"]),
            str(case_configuration),
            str(retained),
            str(destination),
        ]
        if artifacts is not None:
            command.append(str(artifacts))
        environment = dict(os.environ)
        environment.pop("DEMESHIFY_EVALUATION_JOB", None)
        code = self.runner(
            command,
            cwd=self.application,
            timeout=min(GRADING_TIMEOUT, timeout),
            stdout_path=destination.with_suffix(".stdout.log"),
            stderr_path=destination.with_suffix(".stderr.log"),
            env=environment,
        )
        if code:
            raise GraderError(f"Frozen grader exited with code {code}")
        if not destination.is_file():
            raise GraderError("Frozen grader produced no result")
        return json.loads(destination.read_text())

    def sample(self):
        return self.resources.sampled_resources()

    def readiness(self, case_configuration: Path, destination: Path, *, timeout: float):
        config = self.configuration
        runtime = Path(self.grader_metadata["python_runtime"])
        command = [
            str(runtime / "python.exe"),
            "-I",
            "-B",
            str(config.grader / self.grader_metadata["entrypoint"]),
            "--reference-readiness",
            str(case_configuration),
            str(destination),
        ]
        environment = dict(os.environ)
        environment.pop("DEMESHIFY_EVALUATION_JOB", None)
        code = self.runner(
            command,
            cwd=self.application,
            timeout=min(GRADING_TIMEOUT, timeout),
            stdout_path=destination.with_suffix(".stdout.log"),
            stderr_path=destination.with_suffix(".stderr.log"),
            env=environment,
        )
        if code or not destination.is_file():
            raise GraderError(f"Frozen reference preparation failed with exit code {code}")
        return json.loads(destination.read_text())


def _summary(results: list[dict], expected: list[dict], backend, manifest: dict) -> dict:
    certified = sum(row.get("certified") is True for row in results)
    passed = sum(row.get("accepted") is True for row in results)
    details = backend.aggregate(results, expected)
    scored = [
        row
        for row in results
        if row.get("certified") is True and row.get("solver_outcome_scored") is True
    ]
    scored_details = backend.aggregate(scored, expected)
    details["source_model_rates"] = scored_details["source_model_rates"]
    details["solver_coverage"] = scored_details["coverage"]
    return {
        "expected_cases": len(expected),
        "evaluated_cases": len(results),
        "certified_cases": certified,
        "execution_certified_cases": sum(row.get("execution_certified") is True for row in results),
        "unscored_cases": sum(row.get("solver_outcome_scored") is not True for row in results),
        "fresh_fixture_preparations": sum(
            row.get("fixture_preparation_fresh") is True for row in results
        ),
        "fresh_evaluations": sum(
            not row["reused"] or row.get("fixture_preparation_fresh") is True for row in results
        ),
        "reference_fixture_unready_cases": sum(
            row.get("fixture_readiness", {}).get("fixture_ready") is False for row in results
        ),
        "complete": len(results) == len(expected) and certified == len(expected),
        "fresh_cases": sum(row["reused"] is False for row in results),
        "reused_cases": sum(row["reused"] is True for row in results),
        "strict_case_passes": passed,
        "strict_case_rate_among_attempted": passed / len(results) if results else None,
        "strict_case_rate_among_certified": passed / certified if certified else None,
        "failure_kind_counts": dict(Counter(row.get("failure_kind", "none") for row in results)),
        "corpus_manifest_complete": manifest.get("complete") is True,
        "full_1000x10_evaluation_complete": (
            len(expected) == 10000
            and certified == 10000
            and len({case["source_model_id"] for case in expected}) == 1000
            and details["coverage"]["source_models_with_all_ten_evaluated"] == 1000
        ),
        "workflow_qualified": False,
        "readiness_note": (
            "Fixture-unready and reference-unverified attempts are not solver failures; "
            "strict source rates use scored cases only."
        ),
        **details,
    }


def _check_identities(backend, identity: dict) -> None:
    backend.verify()
    if backend.identity() != identity:
        raise ControllerError("Frozen controller/artifact/runtime conditions changed")


def _validate_attestation(
    converted, case: dict, identity: dict, retained: Path, root: Path
) -> dict:
    data = converted.attestation
    if (
        data.get("permissions_revoked") is not True
        or data.get("input_unchanged") is not True
        or data.get("sealed_converter_unchanged") is not True
        or data.get("second_conversion_not_run") is not True
        or data.get("launch", {}).get("job_drained") is not True
        or data.get("input_sha256") != case["input_sha256"]
        or data.get("input_units") != case.get("input_units", "mm")
        or data.get("converter_manifest_sha256") != identity["converter_manifest_sha256"]
    ):
        raise ControllerError("Conversion lifecycle/input attestation is incomplete")
    actual = _retained_hashes(retained, root)
    expected = data.get("output_sha256")
    if (
        not isinstance(expected, dict)
        or not {"reconstruction.step", "report.json"} <= expected.keys()
        or any(actual.get(name) != value for name, value in expected.items())
        or set(actual) != set(expected) | {"isolation-attestation.json"}
    ):
        raise ControllerError("Actual retained outputs differ from their sealed attestation")
    if json.loads((retained / "isolation-attestation.json").read_text()) != data:
        raise ControllerError("Retained lifecycle attestation differs from actual adapter result")
    code = data.get("code_sha256", {})
    for name, filename in (
        ("adapter", "isolated_conversion.py"),
        ("sandbox", "windows_evaluation_sandbox.py"),
        ("verifier", "freeze_evaluation_converter.py"),
    ):
        if code.get(name) != identity["tools_sha256"]["tools/" + filename]:
            raise ControllerError("Adapter used a different host/verifier source identity")
    return actual


def _validate_grade(result: dict, config: dict, case_path: Path | None, identity: dict) -> None:
    case = config["case"]
    expected_configuration = _hash(config)
    if case_path is not None and _digest(case_path) != expected_configuration:
        raise GraderError("Frozen grading configuration changed")
    if (
        not isinstance(result.get("accepted"), bool)
        or any(
            result.get(field) != case[field]
            for field in ("id", "source_model_id", "split", "preset")
        )
        or result.get("second_conversion_not_run") is not True
        or result.get("grader_manifest_sha256") != identity["grader_manifest_sha256"]
        or result.get("grader_source_revision") != identity["grader_source_revision"]
        or result.get("grader_case_sha256") != expected_configuration
        or result.get("grader_runtime_files_sha256") != identity["grader_runtime_inventory_sha256"]
        or result.get("actual_output_sha256") != config["retained_sha256"]
        or result.get("fixture_sha256")
        != {"stl": case["input_sha256"], "source_step": case["source_sha256"]}
    ):
        raise GraderError("Frozen grader result does not attest the requested retained case")
    ready = result.get("fixture_readiness", {}).get("fixture_ready")
    if result.get("evaluation_failure_kind") == "fixture":
        if result["accepted"] is not False or ready is not False:
            raise GraderError("Unready reference fixture cannot be scored as a solver result")
    elif ready is not True:
        raise GraderError("Frozen grader did not qualify reference fixture readiness")
    try:
        _json_bytes(result)
    except (ValueError, TypeError) as exc:
        raise GraderError("Frozen grader result contains nonfinite or unsupported data") from exc


def _load_completed(path: Path, fingerprint: str, root: Path) -> dict | None:
    upgraded = path.with_name(path.stem + ".ready.json")
    if upgraded.exists():
        path = upgraded
    if not path.exists():
        return None
    _plain(path, root)
    envelope = json.loads(path.read_text())
    checksum = envelope.pop("record_sha256", None)
    if (
        envelope.get("schema") != CACHE_SCHEMA
        or envelope.get("fingerprint") != fingerprint
        or checksum != _hash(envelope)
        or envelope["result"].get("execution_certified") is not True
    ):
        raise ControllerError("Completed cache record is foreign, changed or uncertified")
    retained = Path(envelope["retained"])
    if _retained_hashes(retained, root) != envelope["retained_sha256"]:
        raise ControllerError("Completed retained output changed; preserve evidence")
    return envelope["result"]


def _save_completed(
    path: Path, fingerprint: str, result: dict, retained: Path, hashes: dict, root: Path
) -> None:
    envelope = {
        "schema": CACHE_SCHEMA,
        "fingerprint": fingerprint,
        "result": result,
        "retained": str(retained),
        "retained_sha256": hashes,
    }
    envelope["record_sha256"] = _hash(envelope)
    _atomic(path, envelope, root, exclusive=True)


def _source_contract(case: dict, identity: dict) -> dict:
    return {
        "schema": 1,
        "source_sha256": case["source_sha256"],
        "expected_solid_count": case.get("ground_truth", {}).get("solid_count"),
        "grader_manifest_sha256": identity["grader_manifest_sha256"],
        "grader_runtime_inventory_sha256": identity["grader_runtime_inventory_sha256"],
        "platform": identity.get("platform"),
        "process": identity.get("process"),
    }


def _validate_readiness(result: dict, configuration: dict, identity: dict) -> None:
    case = configuration["case"]
    ready = result.get("fixture_readiness", {}).get("fixture_ready")
    if (
        result.get("accepted") is not False
        or result.get("reference_readiness_only") is not True
        or result.get("second_conversion_not_run") is not True
        or any(
            result.get(field) != case[field]
            for field in ("id", "source_model_id", "split", "preset")
        )
        or result.get("grader_manifest_sha256") != identity["grader_manifest_sha256"]
        or result.get("grader_source_revision") != identity["grader_source_revision"]
        or result.get("grader_runtime_files_sha256") != identity["grader_runtime_inventory_sha256"]
        or result.get("grader_case_sha256") != _hash(configuration)
        or result.get("fixture_sha256") != {"source_step": case["source_sha256"]}
        or result.get("actual_output_sha256") != {}
        or type(ready) is not bool
        or (ready is False and result.get("evaluation_failure_kind") != "fixture")
    ):
        raise GraderError("Source-only preparation lacks the pinned grader/source/case attestation")
    _json_bytes(result)


def _read_receipt(path: Path, contract: dict, identity: dict, root: Path) -> dict | None:
    if not path.exists():
        return None
    _plain(path, root)
    receipt = json.loads(path.read_text())
    checksum = receipt.pop("record_sha256", None)
    if (
        receipt.get("schema") != 1
        or receipt.get("contract") != contract
        or checksum != _hash(receipt)
    ):
        raise ControllerError("Reference readiness receipt changed or has the wrong source/grader")
    configuration, proof = receipt["configuration"], receipt["proof"]
    if _source_contract(configuration["case"], identity) != contract:
        raise ControllerError("Readiness witness source settings differ from their receipt")
    if receipt["mode"] == "source_only":
        _validate_readiness(proof, configuration, identity)
    elif receipt["mode"] == "retained_output":
        _validate_grade(proof, configuration, None, identity)
    else:
        raise ControllerError("Unknown readiness receipt mode")
    return receipt


def _store_receipt(
    path: Path,
    contract: dict,
    configuration: dict,
    proof: dict,
    mode: str,
    identity: dict,
    root: Path,
) -> dict:
    previous = _read_receipt(path, contract, identity, root)
    if previous is not None:
        if (
            previous["proof"]["fixture_readiness"]["fixture_ready"]
            != proof["fixture_readiness"]["fixture_ready"]
        ):
            raise ControllerError("The same pinned source/grader produced conflicting readiness")
        return previous
    receipt = {
        "schema": 1,
        "contract": contract,
        "configuration": configuration,
        "proof": proof,
        "mode": mode,
    }
    receipt["record_sha256"] = _hash(receipt)
    _atomic(path, receipt, root, exclusive=True)
    return receipt


def _apply_readiness(row: dict, diagnostics: dict, receipt: Path) -> None:
    row["fixture_readiness"] = diagnostics
    row["reference_readiness_receipt"] = str(receipt)
    if diagnostics["fixture_ready"] is True:
        row["solver_outcome_scored"] = True
        row["certified"] = row["execution_certified"]
    else:
        row["original_execution_status"] = row["status"]
        row["unscored_execution_failure_kind"] = row.get("failure_kind")
        row.update(
            accepted=False,
            certified=False,
            solver_outcome_scored=False,
            status="fixture_error",
            failure_kind="fixture",
        )


def _qualify_failed_execution(
    row: dict,
    case: dict,
    identity: dict,
    configuration: Configuration,
    backend,
    receipt_cache: Path,
    preparations: Path,
    root: Path,
) -> bool:
    """Qualify fixture independently; never rerun conversion or change its timing."""
    if (
        not row.get("execution_certified")
        or row.get("solver_outcome_scored") is True
        or row.get("fixture_preparation_failure_kind") == "grader_resource_limit"
        or type(row.get("fixture_readiness", {}).get("fixture_ready")) is bool
        or row.get("failure_kind") not in ("converter_exit", "converter_timeout", "case_timeout")
    ):
        return False
    contract = _source_contract(case, identity)
    receipt_path = receipt_cache / (_hash(contract) + ".json")
    if row.get("fixture_preparation_error"):
        row["previous_fixture_preparation_error"] = row.pop("fixture_preparation_error")
    lookup_started = time.perf_counter()
    receipt = _read_receipt(receipt_path, contract, identity, root)
    row["source_readiness_lookup_wall_seconds"] = time.perf_counter() - lookup_started
    row["fixture_preparation_fresh"] = False
    if receipt is not None:
        _apply_readiness(row, receipt["proof"]["fixture_readiness"], receipt_path)
        row["fixture_preparation_timing_provenance"] = "reused_source_readiness_receipt"
        return True
    attempt = preparations / uuid.uuid4().hex
    attempt.mkdir(exist_ok=False)
    data = {
        "grader_manifest_sha256": configuration.grader_sha256,
        "case": case,
        "manifest_parent": str(configuration.manifest.parent.resolve()),
        "scratch_root": _scratch_root(root),
    }
    request = attempt / "reference-case.json"
    _atomic(request, data, root, exclusive=True)
    started = time.perf_counter()
    row["fixture_preparation_fresh"] = True
    row["fixture_preparation_timing_provenance"] = "fresh_independent_fixture_preparation"
    row["fixture_preparation_attempt"] = str(attempt)
    try:
        _check_identities(backend, identity)
        _fixture(case)
        with backend.sample() as resources:
            proof = backend.readiness(request, attempt / "readiness.json", timeout=GRADING_TIMEOUT)
        _validate_readiness(proof, data, identity)
        if _digest(request) != _hash(data):
            raise ControllerError("Reference preparation configuration changed")
        _fixture(case)
        _check_identities(backend, identity)
        _store_receipt(receipt_path, contract, data, proof, "source_only", identity, root)
        _apply_readiness(row, proof["fixture_readiness"], receipt_path)
        row["fixture_preparation_resources"] = resources
    except Exception as exc:
        row["fixture_preparation_error"] = f"{type(exc).__name__}: {exc}"
        if getattr(exc, "classification", None) == "grader_resource_limit":
            row["fixture_preparation_failure_kind"] = "grader_resource_limit"
            row["fixture_preparation_resource_evidence"] = getattr(exc, "resource_evidence", {})
            row.update(accepted=False, certified=False, solver_outcome_scored=False)
        # Ordinary readiness errors remain retryable. A resource-limited source
        # needs a separately qualified policy; do not repeat expensive preparation.
    row["fixture_preparation_wall_seconds"] = time.perf_counter() - started
    _atomic(
        attempt / "preparation-record.json",
        {
            "source_contract": contract,
            "case_id": case["id"],
            "scored": row.get("solver_outcome_scored") is True,
            "error": row.get("fixture_preparation_error"),
            "fixture_preparation_wall_seconds": row["fixture_preparation_wall_seconds"],
        },
        root,
        exclusive=True,
    )
    return type(row.get("fixture_readiness", {}).get("fixture_ready")) is bool


def _record_fixture_exclusion(path: Path, row: dict, case: dict, identity: dict, root: Path):
    diagnostics = row.get("fixture_readiness", {})
    if diagnostics.get("fixture_ready") is not False:
        return
    if path.exists():
        _plain(path, root)
        ledger = json.loads(path.read_text())
    else:
        ledger = {
            "schema": 1,
            "action": "exclude/replace deficient fixtures before final corpus; inputs unchanged",
            "sources": {},
        }
    key = _hash(
        {
            "source_sha256": case["source_sha256"],
            "grader_manifest_sha256": identity["grader_manifest_sha256"],
        }
    )
    if key not in ledger["sources"]:
        ledger["sources"][key] = {
            "source_model_id": case["source_model_id"],
            "source_sha256": case["source_sha256"],
            "geometry_family": case.get("geometry_family"),
            "first_case_id": case["id"],
            "attempt": row["attempt"],
            "grader_manifest_sha256": identity["grader_manifest_sha256"],
            "fixture_readiness": diagnostics,
        }
        _atomic(path, ledger, root)


def run_controller(configuration: Configuration, *, _backend=None) -> dict:
    """Production uses the native backend; the private seam supports protocol tests."""
    backend = _backend if _backend is not None else _NativeBackend()
    try:
        return _run_controller(configuration, backend)
    finally:
        cleanup = getattr(backend, "close", None)
        if cleanup is not None:
            cleanup()


def _load_scored_cached_prefix(
    expected: list[dict], identity: dict, cache: Path, root: Path, backend,
    manifest: Path, manifest_hash: str,
) -> tuple[list[dict], str | None]:
    """Read certified history as one transaction; publish only after full checks.

    The live seal continuously locks bytes and monitors all directory changes.
    No grading, readiness preparation or conversion runs inside this transaction.
    Cache envelopes, retained output bytes and both fixtures are still checked
    for every row. Incomplete/unscored records return to the normal case loop.
    """
    rows, halt = [], None
    for case in expected:
        if shutil.disk_usage(cache.parent).free < 15 * 1024**3:
            halt = "storage_floor"
            break
        started = time.perf_counter()
        fingerprint = _hash({"schema": CACHE_SCHEMA, "case": case, "identity": identity})
        completed = cache / (fingerprint + ".json")
        try:
            _fixture(case)
            reused = _load_completed(completed, fingerprint, root)
        except FixtureError:
            break  # Preserve the existing case loop's fixture-error handling.
        if (reused is None or reused.get("certified") is not True
                or reused.get("execution_certified") is not True
                or reused.get("solver_outcome_scored") is not True
                or reused.get("fixture_readiness", {}).get("fixture_ready") is not True):
            break
        if not rows:
            _check_identities(backend, identity)
        _fixture(case)
        row = {
            **reused, "reused": True, "reused_from": str(completed),
            "timing_provenance": "historical_verified_case",
            "reuse_lookup_wall_seconds": time.perf_counter() - started,
            "fixture_preparation_fresh": False,
            "cache_lookup_protocol": "certified-prefix-full-boundary-checks-v1",
        }
        if "fixture_preparation_wall_seconds" in row:
            row["fixture_preparation_timing_provenance"] = "historical_fixture_preparation"
        rows.append(row)
    if rows:
        _check_identities(backend, identity)
        if _digest(manifest) != manifest_hash:
            raise ControllerError("Frozen corpus manifest changed during cached prefix lookup")
    return rows, halt


def _run_controller(configuration: Configuration, backend) -> dict:
    if configuration.batch_cases is not None and configuration.batch_cases <= 0:
        raise ValueError("--batch-cases must be positive")
    root = configuration.owned_root.resolve(strict=True)
    output = _owned(configuration.output, root, create_parent=True)
    if configuration.artifacts is not None:
        _owned(configuration.artifacts, root, create_parent=True)
    manifest_bytes = configuration.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    expected = _cases(manifest, configuration.manifest.parent.resolve(), configuration.split)
    backend.initialise(configuration)
    _check_identities(backend, backend.identity())
    identity = backend.identity()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    contract = {
        "schema": 1,
        "identity": identity,
        "manifest_sha256": manifest_hash,
        "split": configuration.split,
        "expected_cases": len(expected),
    }
    contract_path = output.with_suffix(".contract.json")
    if contract_path.exists() and json.loads(contract_path.read_text()) != contract:
        raise ControllerError("Run contract changed; preserve this run and choose a new output")
    _atomic(contract_path, contract, root)
    cache = output.parent / "frozen-controller-completed-v1"
    attempts = output.parent / (output.stem + "-attempts")
    receipt_cache = output.parent / "frozen-reference-readiness-v1"
    preparations = output.parent / (output.stem + "-fixture-preparations")
    cache.mkdir(exist_ok=True)
    attempts.mkdir(exist_ok=True)
    receipt_cache.mkdir(exist_ok=True)
    preparations.mkdir(exist_ok=True)
    launches_path = output.with_suffix(".launches.json")
    launches = json.loads(launches_path.read_text()) if launches_path.exists() else []
    launches.append(
        {
            "started_unix_seconds": time.time(),
            "batch_cases": configuration.batch_cases,
            "interpreter": sys.executable,
            "controller_sha256": _digest(Path(__file__)),
        }
    )
    _atomic(launches_path, launches, root)
    results, halt = _load_scored_cached_prefix(
        expected, identity, cache, root, backend, configuration.manifest, manifest_hash,
    )
    fresh = 0
    for case in expected[len(results):]:
        if halt:
            break
        if shutil.disk_usage(output.parent).free < 15 * 1024**3:
            halt = "storage_floor"
            break
        before = time.perf_counter()
        deadline = time.monotonic() + CASE_TIMEOUT
        _check_identities(backend, identity)
        fingerprint = _hash({"schema": CACHE_SCHEMA, "case": case, "identity": identity})
        completed = cache / (fingerprint + ".json")
        # Never reuse a result when its original fixture is unavailable/changed.
        try:
            _fixture(case)
            reused = _load_completed(completed, fingerprint, root)
        except FixtureError:
            reused = None
        if reused is not None:
            _check_identities(backend, identity)
            _fixture(case)
            reused_row = {
                **reused,
                "reused": True,
                "reused_from": str(completed),
                "timing_provenance": "historical_verified_case",
                "reuse_lookup_wall_seconds": time.perf_counter() - before,
                "fixture_preparation_fresh": False,
            }
            if "fixture_preparation_wall_seconds" in reused_row:
                reused_row["fixture_preparation_timing_provenance"] = (
                    "historical_fixture_preparation"
                )
            if (
                not reused_row.get("solver_outcome_scored")
                and type(reused_row.get("fixture_readiness", {}).get("fixture_ready")) is not bool
            ):
                try:
                    upgraded = _qualify_failed_execution(
                        reused_row,
                        case,
                        identity,
                        configuration,
                        backend,
                        receipt_cache,
                        preparations,
                        root,
                    )
                    _check_identities(backend, identity)
                    _fixture(case)
                    if upgraded:
                        upgrade_path = completed.with_name(completed.stem + ".ready.json")
                        if not upgrade_path.exists():
                            retained = Path(reused_row["attempt"]) / "retained"
                            _save_completed(
                                upgrade_path,
                                fingerprint,
                                reused_row,
                                retained,
                                _retained_hashes(retained, root),
                                root,
                            )
                except Exception as exc:
                    reused_row["fixture_preparation_error"] = f"{type(exc).__name__}: {exc}"
                    _check_identities(backend, identity)
                if reused_row.get("fixture_preparation_fresh") is True:
                    fresh += 1
            results.append(reused_row)
        else:
            attempt = attempts / uuid.uuid4().hex
            attempt.mkdir(exist_ok=False)
            retained = attempt / "retained"
            retained.mkdir()
            hashes, attestation = {}, None
            conversion_validated = False
            stage = "fixture"
            result = {key: case[key] for key in ("id", "source_model_id", "preset", "split")}
            result.update(
                accepted=False,
                certified=False,
                execution_certified=False,
                solver_outcome_scored=False,
                status="error",
                evaluation_fingerprint=fingerprint,
                attempt=str(attempt),
            )
            with backend.sample() as resources:
                try:
                    _fixture(case)
                    neutral_input = attempt / "input.stl"
                    shutil.copyfile(case["stl"], neutral_input)
                    if _digest(neutral_input) != case["input_sha256"]:
                        raise FixtureError("Neutral input copy differs from frozen STL")
                    stage = "conversion"
                    _remaining(deadline)
                    converted = backend.convert(
                        input_stl=neutral_input,
                        input_sha256=case["input_sha256"],
                        units=case["input_units"],
                        output_dir=retained,
                        case_deadline=deadline,
                    )
                    attestation = converted.attestation
                    stage = "isolation"
                    hashes = _validate_attestation(converted, case, identity, retained, root)
                    conversion_validated = True
                    # Preserve a validated conversion even if host grading later times out.
                    result.update(
                        conversion_attestation=attestation,
                        conversion_wall_seconds=attestation["conversion_wall_seconds"],
                        artifact_copy_wall_seconds=attestation["artifact_copy_wall_seconds"],
                        adapter_wall_seconds=attestation.get("adapter_wall_seconds"),
                    )
                    _remaining(deadline)
                    configuration_data = {
                        "grader_manifest_sha256": configuration.grader_sha256,
                        "case": case,
                        "manifest_parent": str(configuration.manifest.parent.resolve()),
                        "scratch_root": _scratch_root(root),
                        "retained_sha256": {
                            name: value
                            for name, value in hashes.items()
                            if name in backend.allowed_outputs
                        },
                    }
                    case_path = attempt / "grader-case.json"
                    _atomic(case_path, configuration_data, root, exclusive=True)
                    grade_artifacts = (
                        configuration.artifacts / attempt.name
                        if configuration.artifacts is not None
                        else None
                    )
                    stage = "grader"
                    grade_started = time.perf_counter()
                    grade = backend.grade(
                        case_path,
                        retained,
                        attempt / "grade.json",
                        grade_artifacts,
                        timeout=min(GRADING_TIMEOUT, _remaining(deadline)),
                    )
                    grade_wall = time.perf_counter() - grade_started
                    _validate_grade(grade, configuration_data, case_path, identity)
                    result.update(grade)
                    result["solver_outcome_scored"] = (
                        grade.get("fixture_readiness", {}).get("fixture_ready") is True
                    )
                    if grade.get("evaluation_failure_kind") == "fixture":
                        result.update(
                            accepted=False, failure_kind="fixture", status="fixture_error"
                        )
                    result.update(
                        frozen_grader_wall_seconds=grade_wall,
                        grader_artifact_copy_wall_seconds=grade.get("artifact_copy_wall_seconds"),
                        artifact_copy_wall_seconds=attestation["artifact_copy_wall_seconds"],
                        conversion_wall_seconds=attestation["conversion_wall_seconds"],
                        adapter_wall_seconds=attestation.get("adapter_wall_seconds"),
                        conversion_attestation=attestation,
                    )
                except Exception as exc:
                    classification = getattr(exc, "classification", None)
                    if classification == "grader_resource_limit" and stage == "grader":
                        kind = "grader_resource_limit"
                    elif isinstance(exc, CaseTimeout):
                        kind = "case_timeout"
                    elif classification == "isolation" or stage == "isolation":
                        kind = "isolation"
                    elif isinstance(exc, FixtureError) or stage == "fixture":
                        kind = "fixture"
                    elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
                        kind = "grader_timeout" if stage == "grader" else "converter_timeout"
                    elif stage == "grader":
                        kind = "grader"
                    else:
                        kind = classification or "converter_protocol"
                    status = (
                        "timeout"
                        if kind.endswith("_timeout")
                        else "crash"
                        if kind == "converter_exit"
                        else kind + "_error"
                    )
                    result.update(
                        accepted=False,
                        status=status,
                        failure_kind=kind,
                        failure_origin=kind,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    if kind == "grader_resource_limit":
                        result.update(
                            resource_evidence=getattr(exc, "resource_evidence", {}),
                            accepted=False, certified=False, solver_outcome_scored=False,
                        )
                    error_attestation = getattr(exc, "attestation", None)
                    if error_attestation:
                        result["conversion_attestation"] = error_attestation
                        for timing in (
                            "conversion_wall_seconds",
                            "adapter_wall_seconds",
                            "effective_conversion_timeout_seconds",
                        ):
                            if timing in error_attestation:
                                result[timing] = error_attestation[timing]
                    if kind in ("converter_exit", "converter_timeout"):
                        if (
                            not error_attestation
                            or error_attestation.get("permissions_revoked") is not True
                            or error_attestation.get("input_sha256") != case["input_sha256"]
                            or error_attestation.get("converter_manifest_sha256")
                            != configuration.converter_sha256
                        ):
                            result.update(
                                failure_kind="isolation",
                                status="isolation_error",
                                error="Converter failure lacks confirmed cleanup/input identity",
                            )
                    if kind not in ("isolation", "fixture") and attestation is None:
                        try:
                            hashes = _retained_hashes(retained, root)
                        except Exception as sealing_error:
                            result.update(
                                failure_kind="isolation",
                                status="isolation_error",
                                error=f"Unsafe failed outputs: {type(sealing_error).__name__}",
                            )
                    if getattr(exc, "job", None) is not None:
                        result["failed_job"] = str(exc.job)
                result.update(resources)
            # Only stable, fully checked attempts become completed cache records.
            try:
                stage = "fixture"
                _fixture(case)
                if (attempt / "input.stl").exists() and _digest(attempt / "input.stl") != case[
                    "input_sha256"
                ]:
                    raise FixtureError("Neutral input changed during evaluation")
                stage = "isolation"
                _check_identities(backend, identity)
                if _retained_hashes(retained, root) != hashes:
                    raise ControllerError("Retained outputs changed after grading/failure")
                if _digest(configuration.manifest) != manifest_hash:
                    raise ControllerError("Frozen corpus manifest changed during evaluation")
                # Decide timeout certification from the final classification. Never
                # disguise isolation, fixture or non-timeout grader errors as solves.
                if time.monotonic() > deadline and result.get("failure_kind") in (
                    None, "converter_exit", "converter_timeout", "case_timeout", "grader_timeout",
                ):
                    result.setdefault(
                        "failure_origin", result.get("failure_kind") or result["status"],
                    )
                    result.update(
                        accepted=False,
                        status="timeout",
                        failure_kind="case_timeout",
                        error="Original 360-second whole-case deadline exhausted",
                    )
                failure_kind = result.get("failure_kind")
                result["execution_certified"] = failure_kind in (
                    None,
                    "converter_exit",
                    "converter_timeout",
                    "case_timeout",
                ) or (
                    failure_kind == "fixture"
                    and result.get("fixture_readiness", {}).get("fixture_ready") is False
                ) or (
                    failure_kind == "grader_resource_limit"
                    and conversion_validated
                    and result.get("conversion_attestation") is not None
                    and isinstance(result.get("resource_evidence"), dict)
                    and result["resource_evidence"].get("worker_drained") is True
                )
                if failure_kind == "grader_resource_limit":
                    # Host resource exhaustion cannot qualify a solver outcome,
                    # even when another export already established source readiness.
                    result.update(accepted=False, solver_outcome_scored=False)
                result["certified"] = (
                    result["execution_certified"] and result.get("solver_outcome_scored") is True
                )
                if not result["certified"]:
                    result["accepted"] = False
            except Exception as exc:
                result.update(
                    accepted=False,
                    certified=False,
                    execution_certified=False,
                    solver_outcome_scored=False,
                    status="fixture_error" if stage == "fixture" else "isolation_error",
                    failure_kind=stage,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if stage == "isolation":
                    halt = "frozen_identity_or_output_changed"
            result.update(
                case_wall_seconds=time.perf_counter() - before,
                reused=False,
                reused_from=None,
                timing_provenance="fresh",
                frozen_converter_manifest_sha256=configuration.converter_sha256,
                frozen_grader_manifest_sha256=configuration.grader_sha256,
                retained_output_sha256=hashes,
            )
            if result["execution_certified"]:
                try:
                    if (
                        type(result.get("fixture_readiness", {}).get("fixture_ready")) is bool
                        and (attempt / "grade.json").exists()
                    ):
                        source_contract = _source_contract(case, identity)
                        _store_receipt(
                            receipt_cache / (_hash(source_contract) + ".json"),
                            source_contract,
                            json.loads((attempt / "grader-case.json").read_text()),
                            json.loads((attempt / "grade.json").read_text()),
                            "retained_output",
                            identity,
                            root,
                        )
                    _qualify_failed_execution(
                        result,
                        case,
                        identity,
                        configuration,
                        backend,
                        receipt_cache,
                        preparations,
                        root,
                    )
                except Exception as exc:
                    result["fixture_preparation_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    _check_identities(backend, identity)
                    _fixture(case)
                    if _retained_hashes(retained, root) != hashes:
                        raise ControllerError("Retained outputs changed during fixture preparation")
                except Exception as exc:
                    result.update(
                        accepted=False,
                        certified=False,
                        execution_certified=False,
                        solver_outcome_scored=False,
                        failure_kind="isolation",
                        status="isolation_error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    halt = "frozen_identity_or_output_changed"
            result["processing_wall_seconds"] = time.perf_counter() - before
            _atomic(attempt / "attempt-result.json", result, root, exclusive=True)
            if result["execution_certified"]:
                _save_completed(completed, fingerprint, result, retained, hashes, root)
            results.append(result)
            fresh += 1
        summary = _summary(results, expected, backend, manifest)
        _record_fixture_exclusion(
            output.with_suffix(".fixture-exclusions.json"), results[-1], case, identity, root
        )
        _atomic(output.with_suffix(".summary.json"), summary, root)
        batch_done = configuration.batch_cases is not None and fresh >= configuration.batch_cases
        if (
            len(results) == 1
            or len(results) % 100 == 0
            or len(results) == len(expected)
            or batch_done
            or halt
        ):
            _atomic(
                output,
                {"summary": summary, "identity": identity, "results": results, "halt_reason": halt},
                root,
            )
        print(
            f"[{len(results)}/{len(expected)}] {case['id']}: "
            f"{results[-1]['status']}, strict={results[-1]['accepted']}, "
            f"reused={results[-1]['reused']}, "
            f"fixture_prepared={results[-1].get('fixture_preparation_fresh', False)}",
            flush=True,
        )
        if batch_done or halt:
            break
    final = {
        "summary": _summary(results, expected, backend, manifest),
        "identity": identity,
        "results": results,
        "halt_reason": halt,
    }
    _atomic(output, final, root)
    _atomic(output.with_suffix(".summary.json"), final["summary"], root)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--converter", required=True, type=Path)
    parser.add_argument("--converter-sha256", required=True)
    parser.add_argument("--grader", required=True, type=Path)
    parser.add_argument("--grader-sha256", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--owned-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--batch-cases", type=int)
    parser.add_argument("--split", choices=("development", "validation", "holdout"))
    args = parser.parse_args()
    configuration = Configuration(
        args.manifest.resolve(),
        args.converter.resolve(),
        args.converter_sha256,
        args.grader.resolve(),
        args.grader_sha256,
        args.profile,
        args.owned_root.resolve(),
        args.output.resolve(),
        args.artifacts.resolve() if args.artifacts else None,
        args.batch_cases,
        args.split,
    )
    result = run_controller(configuration)
    if result["halt_reason"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

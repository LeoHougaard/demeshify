"""Convert one copied STL with a pinned AppContainer converter, then retain outputs.

Reference CAD and corpus metadata are deliberately absent from this API. Geometry
grading, frozen grader identity and certified-cache publication belong to callers.
"""

from __future__ import annotations

import ctypes as C
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from ctypes import wintypes as W
from dataclasses import asdict, dataclass
from pathlib import Path

from app.schemas import ReconstructionReport
from tools import freeze_evaluation_converter as frozen
from tools import locked_artifact_identity as locked
from tools import sealed_runtime
from tools import windows_evaluation_sandbox as sandbox

BACKEND = "windows-appcontainer-frozen-v1"
HOST_SHA256 = "f5d5fc02427af7e0d8a5df1a5b14372e930809dac96f257cd141a7b0da43db36"
CONVERSION_TIMEOUT = 240
CLEANUP_TIMEOUT = 10
OUTPUT_FILES = (
    "reconstruction.step",
    "reconstruction.stl",
    "report.json",
    "surface_graph.json",
    "bootstrap-result.json",
    "bootstrap.stdout.log",
    "bootstrap.stderr.log",
    "joined_surfaces.step",
    "analytic_diagnostic.step",
    "analytic_diagnostic.stl",
    "analytic_diagnostic_joined_surfaces.step",
    "analytic_diagnostic_report.json",
    "analytic_diagnostic_surface_graph.json",
    "closed_shell_roundtrip_diagnostic.step",
    "closed_shell_roundtrip_score.json",
    "geometry-worker.stdout.log",
    "geometry-worker.stderr.log",
    "surface-worker-progress.log",
    "surface-worker.stdout.log",
    "surface-worker.stderr.log",
)
REQUIRED_OUTPUTS = set(OUTPUT_FILES[:5])
ATTESTATION = "isolation-attestation.json"


class AdapterError(RuntimeError):
    classification = "converter_protocol"

    def __init__(self, message: str, *, job: Path | None = None, attestation=None):
        super().__init__(message)
        self.job = job
        self.attestation = attestation or {}


class IsolationFailure(AdapterError):
    classification = "isolation"


class ConversionTimeout(AdapterError, TimeoutError):
    classification = "timeout"


class ConverterExit(AdapterError):
    classification = "converter_exit"


@dataclass(frozen=True)
class ConvertedArtifact:
    report: ReconstructionReport
    attestation: dict


def _existing_profile_sid(profile: str) -> str:
    if sys.platform != "win32":
        raise IsolationFailure("The existing-profile adapter requires Windows")
    userenv = C.WinDLL("userenv", use_last_error=True)
    advapi = C.WinDLL("advapi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    derive = userenv.DeriveAppContainerSidFromAppContainerName
    derive.argtypes, derive.restype = [W.LPCWSTR, C.POINTER(C.c_void_p)], C.c_long
    convert = advapi.ConvertSidToStringSidW
    convert.argtypes, convert.restype = [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL
    advapi.FreeSid.argtypes, advapi.FreeSid.restype = [C.c_void_p], C.c_void_p
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p], C.c_void_p
    sid = C.c_void_p()
    hr = derive(profile, C.byref(sid))
    if hr:
        raise IsolationFailure(f"Cannot derive existing profile SID: {hr & 0xFFFFFFFF:08x}")
    text = W.LPWSTR()
    try:
        if not convert(sid, C.byref(text)):
            raise C.WinError(C.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(text)
        advapi.FreeSid(sid)


class _JobGrant:
    """Only a fresh job receives a grant; no runtime/reference ACL is touched."""

    def __init__(self, job: Path, root: Path, profile: str):
        self.job = sandbox.owned_path(job, root)
        self.root = root
        self.sid = _existing_profile_sid(profile)
        self.attempted = False

    def _command(self, *arguments):
        subprocess.run(
            ["icacls", str(sandbox.owned_path(self.job, self.root)), *arguments],
            check=True,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=30,
        )

    def enable(self):
        self.attempted = True  # Also revoke if a partially successful setup raises.
        self._command("/grant:r", "*" + self.sid + ":(OI)(CI)(M)", "/Q")
        self._command("/setintegritylevel", "(OI)(CI)L", "/Q")

    def revoke(self):
        if self.attempted:
            try:
                _validate_tree(self.job)
            except Exception as exc:
                # Never recursively mutate ACLs through an unexpected link.
                # Revoke the owned root's grant, then fail closed rather than
                # touching a linked external object's security descriptor.
                self._command("/remove:g", "*" + self.sid, "/L", "/Q")
                raise IsolationFailure(
                    "Unsafe job tree; recursive grant revocation refused"
                ) from exc
            # /L applies an operation to links themselves, never their targets.
            # Output validation below separately refuses all reparse points.
            self._command("/remove:g", "*" + self.sid, "/T", "/L", "/Q")


def _reparse(info) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _plain_file(path: Path, root: Path):
    info = path.stat(follow_symlinks=False)
    if (
        _reparse(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not path.resolve(strict=True).is_relative_to(root)
    ):
        raise IsolationFailure("Output is linked, non-regular or escaped its owned job")
    return info


def _validate_tree(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            info = path.stat(follow_symlinks=False)
            if _reparse(info) or path.is_symlink():
                raise IsolationFailure("Output tree contains a link or junction")
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                _plain_file(path, root)


def _open_plain(path: Path, root: Path):
    expected = _plain_file(path, root)
    stream = path.open("rb")
    actual = os.fstat(stream.fileno())
    if (
        actual.st_ino != expected.st_ino
        or actual.st_dev != expected.st_dev
        or actual.st_nlink != 1
        or not stat.S_ISREG(actual.st_mode)
    ):
        stream.close()
        raise IsolationFailure("Output changed while opening the sealed file")
    return stream


def _seal_outputs(job: Path) -> dict[str, str]:
    _validate_tree(job)
    present = {name for name in OUTPUT_FILES if (job / name).exists()}
    if not REQUIRED_OUTPUTS.issubset(present):
        raise AdapterError("Converter did not produce the complete required artifact set")
    result = {}
    for name in sorted(present):
        with _open_plain(job / name, job) as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _control_json(job: Path, name: str, hashes: dict):
    with _open_plain(job / name, job) as stream:
        data = stream.read(2 * 1024**2 + 1)
    if len(data) > 2 * 1024**2:
        raise AdapterError("Converter control JSON exceeds the bounded protocol size")
    if hashlib.sha256(data).hexdigest() != hashes[name]:
        raise IsolationFailure("Converter control JSON changed after sealing")
    return json.loads(data)


def _input_mesh_report(path: Path, units: str):
    from app.mesh import load_mesh

    return load_mesh(path, "input.stl", units).report


def _same_mesh(expected, actual) -> bool:
    first, second = expected.model_dump(), actual.model_dump()
    float_keys = {"dimensions_mm", "volume_mm3", "surface_area_mm2", "unit_scale"}
    if any(first[key] != second[key] for key in first.keys() - float_keys):
        return False
    for key in float_keys:
        left, right = first[key], second[key]
        if left is None or right is None:
            if left != right:
                return False
            continue
        left = left if isinstance(left, tuple | list) else (left,)
        right = right if isinstance(right, tuple | list) else (right,)
        if len(left) != len(right) or any(
            not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
            for a, b in zip(left, right, strict=True)
        ):
            return False
    return True


def _code_identity() -> dict[str, str]:
    return {
        name: frozen.digest(Path(path))
        for name, path in (
            ("adapter", __file__),
            ("sandbox", sandbox.__file__),
            ("verifier", frozen.__file__),
            ("locked_identity", locked.__file__),
            ("seal", sealed_runtime.__file__),
        )
    }


def _copy_outputs(job: Path, output: Path, hashes: dict[str, str]) -> None:
    for name, expected in hashes.items():
        digest = hashlib.sha256()
        with _open_plain(job / name, job) as source, (output / name).open("xb") as target:
            while chunk := source.read(1024**2):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected or frozen.digest(output / name) != expected:
            raise IsolationFailure("Actual output changed during retention; do not grade")
    if _seal_outputs(job) != hashes:
        raise IsolationFailure("Output changed after retention; do not grade")


def convert_isolated(
    artifact: Path,
    manifest_sha256: str,
    *,
    profile: str,
    owned_root: Path,
    input_stl: Path,
    input_sha256: str,
    output_dir: Path,
    units: str = "mm",
    case_deadline: float | None = None,
    verification_session: sealed_runtime.SealedRuntimeSession | None = None,
) -> ConvertedArtifact:
    """Convert one neutral STL and return retained outputs plus host attestation.

    ``output_dir`` must be an existing owned temporary directory containing at
    most an unchanged ``input.stl``. Existing outputs and failed jobs are never
    overwritten/deleted. No STEP reference, corpus manifest or source name enters
    the converter API. A returned report is not a strict or reference grade.
    Verification defaults to full hashes. Reuse requires the actual live session
    covering the complete pinned converter artifact and runtime, including its
    manifest and license; a trace digest or serialized record is insufficient.
    """
    adapter_started = time.perf_counter()
    if case_deadline is not None and (
        isinstance(case_deadline, bool) or not isinstance(case_deadline, (int, float))
        or not math.isfinite(case_deadline)
    ):
        raise ValueError("Expected a finite monotonic case deadline")
    if units not in ("mm", "cm", "m", "in") or not profile:
        raise ValueError("Expected supported units and an existing profile name")
    input_stl = Path(input_stl)
    if input_stl.suffix.lower() != ".stl" or not input_stl.is_file():
        raise ValueError("Expected an existing STL input, never reference CAD or a manifest")
    if len(input_sha256) != 64 or any(c not in "0123456789abcdef" for c in input_sha256):
        raise ValueError("Expected a pinned lowercase SHA-256 for the input STL")
    artifact = sandbox.owned_path(Path(artifact), owned_root)
    output = sandbox.owned_path(Path(output_dir), owned_root)
    identity_started = time.perf_counter()
    try:
        if verification_session is None:
            metadata, session_identity = frozen.verify(artifact, manifest_sha256), None
        else:
            metadata, session_identity = locked.verify_locked_converter(
                artifact, manifest_sha256, verification_session,
            )
    except Exception as exc:
        raise IsolationFailure(
            "Pinned converter/runtime could not be verified before launch"
        ) from exc
    identity_seconds = time.perf_counter() - identity_started
    runtime = sandbox.owned_path(Path(metadata["python_runtime"]), owned_root)
    if (
        not output.is_dir()
        or output.is_relative_to(artifact)
        or output.is_relative_to(runtime)
        or any(p.name != "input.stl" for p in output.iterdir())
    ):
        raise ValueError("Use a separate empty owned output directory; preserve previous results")
    if (output / "input.stl").exists():
        _plain_file(output / "input.stl", output)
        if frozen.digest(output / "input.stl") != input_sha256:
            raise ValueError("Existing output input differs from the pinned STL")
    if frozen.digest(input_stl) != input_sha256:
        raise ValueError("Input differs from its pinned hash")
    identities = _code_identity()
    if identities["sandbox"] != HOST_SHA256:
        raise IsolationFailure("The host differs from the qualified frozen source")
    job = Path(owned_root).resolve(strict=True) / ("isolation-job-" + uuid.uuid4().hex)
    job.mkdir(exist_ok=False)
    attestation = {
        "schema": 1,
        "backend": BACKEND,
        "code_sha256": identities,
        "converter_manifest_sha256": manifest_sha256,
        "converter_revision": metadata["source_revision"],
        "input_sha256": input_sha256,
        "input_units": units,
        "job": str(job),
        "profile": profile,
        "conversion_timeout_seconds": CONVERSION_TIMEOUT,
        "cleanup_timeout_seconds": CLEANUP_TIMEOUT,
        "identity_verification_wall_seconds": identity_seconds,
        "identity_verification_mode": "live_locked_session" if session_identity else "full_hash",
        "verification_session_identity": session_identity,
        "permissions_revoked": False,
    }
    grant = None
    launch = None
    error = None
    try:
        grant = _JobGrant(job, Path(owned_root).resolve(strict=True), profile)
        grant.enable()
        shutil.copyfile(input_stl, job / "input.stl")
        if frozen.digest(job / "input.stl") != input_sha256:
            raise IsolationFailure("Copied STL differs before conversion")
        effective_timeout = CONVERSION_TIMEOUT
        if case_deadline is not None:
            effective_timeout = min(CONVERSION_TIMEOUT, case_deadline - time.monotonic())
        attestation["effective_conversion_timeout_seconds"] = effective_timeout
        if effective_timeout <= 0:
            raise ConversionTimeout("Whole-case deadline exhausted before converter launch")
        started = time.perf_counter()
        launch = sandbox.run_converter(
            runtime / "python.exe",
            artifact / "application/converter.pyc",
            job,
            units,
            owned_root=owned_root,
            profile=profile,
            timeout=effective_timeout,
            cleanup_timeout=CLEANUP_TIMEOUT,
        )
        attestation["conversion_wall_seconds"] = time.perf_counter() - started
    except Exception as exc:
        error = exc
    finally:
        revocation_error = None
        try:
            if grant is not None:
                grant.revoke()
                attestation["permissions_revoked"] = True
        except Exception as exc:
            revocation_error = exc
        verification_error = None
        identity_started = time.perf_counter()
        try:
            if verification_session is None:
                after_metadata, after_session = frozen.verify(artifact, manifest_sha256), None
            else:
                after_metadata, after_session = locked.verify_locked_converter(
                    artifact, manifest_sha256, verification_session,
                )
            if after_metadata != metadata or after_session != session_identity:
                raise IsolationFailure("Pinned converter verification identity changed")
            if _code_identity() != identities:
                raise IsolationFailure("Adapter/host/verifier changed during conversion")
        except Exception as exc:
            verification_error = exc
        attestation["identity_verification_wall_seconds"] += time.perf_counter() - identity_started
        if revocation_error is not None:
            raise IsolationFailure(
                "Scoped job grant could not be revoked; do not grade",
                job=job,
                attestation=attestation,
            ) from revocation_error
        if verification_error is not None:
            raise IsolationFailure(
                "Frozen converter/runtime/code changed; do not grade",
                job=job,
                attestation=attestation,
            ) from verification_error
    if error is not None:
        kind = ConversionTimeout if isinstance(error, TimeoutError) else IsolationFailure
        if isinstance(error, AdapterError):
            kind = type(error)
        raise kind(str(error), job=job, attestation=attestation) from error
    try:
        audits = launch.process_audits
        if (
            not launch.job_drained
            or not audits
            or launch.root_pid not in {a.pid for a in audits}
            or len({a.pid for a in audits}) != len(audits)
            or any(
                not a.in_job or a.capability_count or a.appcontainer_sid != grant.sid
                for a in audits
            )
            or launch.affinity_mask <= 0
            or launch.affinity_mask & (launch.affinity_mask - 1)
        ):
            raise IsolationFailure("Host process/drain/CPU attestation is incomplete")
        launch_record = asdict(launch)
        launch_record["process_audits"] = list(launch_record["process_audits"])
        attestation["launch"] = launch_record
        if launch.exit_code:
            raise ConverterExit(f"Isolated converter exited with code {launch.exit_code}")
        if (
            frozen.digest(input_stl) != input_sha256
            or frozen.digest(job / "input.stl") != input_sha256
        ):
            raise IsolationFailure("Pinned or copied input changed during conversion")
        verification_started = time.perf_counter()
        hashes = _seal_outputs(job)
        report = ReconstructionReport.model_validate(_control_json(job, "report.json", hashes))
        bootstrap = _control_json(job, "bootstrap-result.json", hashes)
        if (
            bootstrap != {"complete": True, "status": report.status}
            or report.id != "case"
            or report.engine != "surface_brep"
            or not _same_mesh(_input_mesh_report(job / "input.stl", units), report.mesh)
        ):
            raise AdapterError("Report/bootstrap/input identity mismatch")
        attestation["output_verification_wall_seconds"] = time.perf_counter() - verification_started
        copy_started = time.perf_counter()
        _copy_outputs(job, output, hashes)
        attestation["artifact_copy_wall_seconds"] = time.perf_counter() - copy_started
        if verification_session is not None:
            identity_started = time.perf_counter()
            try:
                verification_session.check()
                if _code_identity() != identities:
                    raise IsolationFailure("Converter verification code changed during retention")
            except Exception as exc:
                raise IsolationFailure("Live converter seal changed during retention") from exc
            finally:
                attestation["identity_verification_wall_seconds"] += (
                    time.perf_counter() - identity_started
                )
        attestation.update(
            output_sha256=hashes,
            input_unchanged=True,
            sealed_converter_unchanged=True,
            second_conversion_not_run=True,
        )
        attestation["adapter_wall_seconds"] = time.perf_counter() - adapter_started
        with (output / ATTESTATION).open("xb") as stream:
            stream.write((json.dumps(attestation, indent=2) + "\n").encode())
        return ConvertedArtifact(report, attestation)
    except AdapterError as exc:
        raise type(exc)(str(exc), job=job, attestation=attestation) from exc
    except Exception as exc:
        raise AdapterError(
            f"{type(exc).__name__}: {exc}", job=job, attestation=attestation
        ) from exc

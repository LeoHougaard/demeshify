import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.schemas import MeshReport, ReconstructionReport
from tools import isolated_conversion as adapter
from tools import locked_artifact_identity as locked
from tools import sealed_runtime as seal
from tools.windows_evaluation_sandbox import IsolationError, ProcessAudit, SandboxResult

SID = "S-1-15-2-protocol-fixture"
INPUT = b"protocol STL bytes; no native reconstruction in these tests"


def test_checkout_preserves_the_exact_qualified_native_host_bytes():
    assert adapter.frozen.digest(Path(adapter.sandbox.__file__)) == adapter.HOST_SHA256


def mesh_report():
    return MeshReport(
        file_name="input.stl",
        triangle_count=4,
        vertex_count=4,
        watertight=True,
        body_count=1,
        solid_count=1,
        dimensions_mm=(1, 2, 3),
        volume_mm3=1,
        surface_area_mm2=2,
        input_units="mm",
        unit_scale=1,
    )


def reconstruction_report():
    return ReconstructionReport(
        id="case",
        status="best_effort",
        engine="surface_brep",
        mesh=mesh_report(),
        plan=None,
        score=None,
        elapsed_seconds=0.2,
    )


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "owned"
        self.root.mkdir()
        self.input = tmp_path / "private-model-name.stl"
        self.input.write_bytes(INPUT)
        self.reference = tmp_path / "original.step"
        self.reference.write_bytes(b"reference fixture must not enter adapter API or converter")
        self.output = self.root / "grading-temp"
        self.output.mkdir()
        self.artifact = self.root / "frozen"
        (self.artifact / "application").mkdir(parents=True)
        (self.artifact / "application/converter.pyc").write_bytes(b"protocol entrypoint")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.runtime / "python.exe").write_bytes(b"protocol interpreter")
        self.events = []
        self.granted = False
        self.verify_calls = 0
        self.mutation = None
        self.raise_run = None
        self.fail_verify_after = False
        self.fail_enable = False
        self.fail_revoke = False
        self.exit_code = 0
        self.launch_change = None
        self.job = None
        self.expected_timeout = 240
        self.manifest_pin = "pinned-converter-manifest"
        fixture = self

        class Grant:
            sid = SID

            def __init__(self, job, root, profile):
                fixture.job = job
                assert root == fixture.root and profile == "existing-profile"

            def enable(self):
                fixture.events.append("grant")
                fixture.granted = True
                if fixture.fail_enable:
                    raise RuntimeError("partially applied grant")

            def revoke(self):
                fixture.events.append("revoke")
                fixture.granted = False
                if fixture.fail_revoke:
                    raise RuntimeError("revocation failure")

        def verify(artifact, identity):
            self.events.append("verify")
            self.verify_calls += 1
            assert artifact == self.artifact and identity == self.manifest_pin
            if self.fail_verify_after and self.verify_calls == 2:
                raise ValueError("sealed runtime changed")
            return {"python_runtime": str(self.runtime), "source_revision": "frozen-commit"}

        def run(executable, entrypoint, job, units, **kwargs):
            self.events.append("run")
            assert self.granted
            assert executable == self.runtime / "python.exe"
            assert entrypoint == self.artifact / "application/converter.pyc"
            assert self.input.name not in repr((executable, entrypoint, job, units, kwargs))
            assert str(self.reference) not in repr((executable, entrypoint, job, units, kwargs))
            assert {p.name for p in job.iterdir()} == {"input.stl"}
            assert (job / "input.stl").read_bytes() == INPUT
            assert kwargs == {
                "owned_root": self.root,
                "profile": "existing-profile",
                "timeout": self.expected_timeout,
                "cleanup_timeout": 10,
            }
            if self.raise_run:
                raise self.raise_run
            (job / "reconstruction.step").write_bytes(b"actual retained protocol output")
            (job / "reconstruction.stl").write_bytes(b"actual retained preview")
            (job / "surface_graph.json").write_text("{}")
            (job / "report.json").write_text(reconstruction_report().model_dump_json())
            (job / "bootstrap-result.json").write_text(
                json.dumps({"complete": True, "status": "best_effort"})
            )
            (job / "ignored-cache.dat").write_bytes(b"never copied or graded")
            if self.mutation:
                self.mutation(job)
            result = SandboxResult(self.exit_code, 10, (ProcessAudit(10, SID, True, 0),), True, 1)
            return self.launch_change(result) if self.launch_change else result

        def read_mesh(path, units):
            self.events.append("read_mesh")
            assert not self.granted and path.parent == self.job
            return mesh_report()

        original_control = adapter._control_json

        def control(*args):
            self.events.append("read_artifact")
            assert not self.granted
            return original_control(*args)

        monkeypatch.setattr(adapter, "_JobGrant", Grant)
        monkeypatch.setattr(adapter.frozen, "verify", verify)
        monkeypatch.setattr(
            adapter,
            "_code_identity",
            lambda: {
                "adapter": "adapter-sha",
                "sandbox": adapter.HOST_SHA256,
                "verifier": "verifier-sha",
            },
        )
        monkeypatch.setattr(adapter.sandbox, "run_converter", run)
        monkeypatch.setattr(adapter, "_input_mesh_report", read_mesh)
        monkeypatch.setattr(adapter, "_control_json", control)

    def convert(self, **options):
        return adapter.convert_isolated(
            self.artifact,
            self.manifest_pin,
            profile="existing-profile",
            owned_root=self.root,
            input_stl=self.input,
            input_sha256=hashlib.sha256(INPUT).hexdigest(),
            output_dir=self.output,
            **options,
        )


def test_one_conversion_revoke_before_artifact_reads_and_retain_exact_bytes(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    converted = fixture.convert()
    assert converted.report == reconstruction_report()
    assert fixture.events.count("run") == 1 and fixture.events.count("revoke") == 1
    assert fixture.events.index("revoke") < fixture.events.index("read_artifact")
    assert fixture.events.index("revoke") < fixture.events.index("read_mesh")
    assert fixture.verify_calls == 2
    assert (
        fixture.output / "reconstruction.step"
    ).read_bytes() == b"actual retained protocol output"
    assert not (fixture.output / "ignored-cache.dat").exists()
    assert fixture.job.is_dir() and fixture.reference.read_bytes().startswith(b"reference fixture")
    attestation = json.loads((fixture.output / adapter.ATTESTATION).read_text())
    assert attestation == converted.attestation
    assert attestation["input_unchanged"] and attestation["permissions_revoked"]
    assert attestation["second_conversion_not_run"]
    assert "artifact_copy_wall_seconds" in attestation and "conversion_wall_seconds" in attestation
    assert "private-model-name" not in fixture.job.name


@pytest.mark.parametrize(
    "failure,expected",
    [
        (TimeoutError("bounded timeout"), adapter.ConversionTimeout),
        (IsolationError("wrong process token"), adapter.IsolationFailure),
    ],
)
def test_native_failure_revokes_rechecks_and_never_reads_outputs(
    tmp_path, monkeypatch, failure, expected
):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.raise_run = failure
    with pytest.raises(expected) as error:
        fixture.convert()
    assert fixture.events[-2:] == ["revoke", "verify"]
    assert "read_artifact" not in fixture.events
    assert error.value.job == fixture.job and fixture.job.is_dir()
    assert list(fixture.output.iterdir()) == []


@pytest.mark.parametrize("stage", ["enable", "revoke", "verify_after"])
def test_partial_setup_or_cleanup_or_identity_failure_never_qualifies(tmp_path, monkeypatch, stage):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.fail_enable = stage == "enable"
    fixture.fail_revoke = stage == "revoke"
    fixture.fail_verify_after = stage == "verify_after"
    with pytest.raises(adapter.IsolationFailure):
        fixture.convert()
    assert "revoke" in fixture.events and fixture.job.is_dir()
    assert fixture.verify_calls == 2
    assert "read_artifact" not in fixture.events
    assert list(fixture.output.iterdir()) == []


def test_nonzero_converter_exit_is_distinct_and_not_graded(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.exit_code = 17
    with pytest.raises(adapter.ConverterExit) as error:
        fixture.convert()
    assert error.value.classification == "converter_exit"
    assert "revoke" in fixture.events and "read_artifact" not in fixture.events


@pytest.mark.parametrize("mutation", ["input", "mesh_report", "bootstrap", "missing"])
def test_rejects_partial_or_mismatched_actual_results(tmp_path, monkeypatch, mutation):
    fixture = Fixture(tmp_path, monkeypatch)

    def mutate(job):
        if mutation == "input":
            (job / "input.stl").write_bytes(b"changed")
        elif mutation == "mesh_report":
            report = reconstruction_report().model_dump()
            report["mesh"]["triangle_count"] = 500
            (job / "report.json").write_text(json.dumps(report))
        elif mutation == "bootstrap":
            (job / "bootstrap-result.json").write_text('{"complete": false}')
        else:
            (job / "reconstruction.step").unlink()

    fixture.mutation = mutate
    with pytest.raises(adapter.AdapterError):
        fixture.convert()
    assert not fixture.granted and fixture.job.is_dir()
    assert list(fixture.output.iterdir()) == []


def test_hardlinked_output_is_rejected_and_external_target_preserved(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)

    def link(job):
        (job / "reconstruction.step").unlink()
        os.link(fixture.reference, job / "reconstruction.step")

    fixture.mutation = link
    with pytest.raises(adapter.IsolationFailure, match="linked"):
        fixture.convert()
    assert fixture.reference.read_bytes().startswith(b"reference fixture")
    assert list(fixture.output.iterdir()) == []


def test_existing_target_is_preserved_without_launch_or_grants(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    previous = fixture.output / "reconstruction.step"
    previous.write_bytes(b"previous reviewed output")
    with pytest.raises(ValueError, match="preserve previous"):
        fixture.convert()
    assert previous.read_bytes() == b"previous reviewed output"
    assert "grant" not in fixture.events and "run" not in fixture.events


def test_target_created_during_conversion_is_not_overwritten(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.mutation = lambda _: (fixture.output / "reconstruction.step").write_bytes(
        b"new user data"
    )
    with pytest.raises(adapter.AdapterError):
        fixture.convert()
    assert (fixture.output / "reconstruction.step").read_bytes() == b"new user data"
    assert not (fixture.output / adapter.ATTESTATION).exists()


def test_post_seal_tamper_is_detected_before_grade(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    original = adapter._copy_outputs

    def tamper(job, output, hashes):
        (job / "reconstruction.step").write_bytes(b"tampered after seal")
        original(job, output, hashes)

    monkeypatch.setattr(adapter, "_copy_outputs", tamper)
    with pytest.raises(adapter.IsolationFailure, match="changed during retention"):
        fixture.convert()
    assert not (fixture.output / adapter.ATTESTATION).exists()


def test_scoped_grant_commands_only_target_fresh_job_and_partial_setup_revokes(
    tmp_path, monkeypatch
):
    root = tmp_path / "owned"
    root.mkdir()
    job = root / "neutral-job"
    job.mkdir()
    calls = []
    monkeypatch.setattr(adapter, "_existing_profile_sid", lambda _: SID)
    monkeypatch.setattr(adapter.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    grant = adapter._JobGrant(job, root, "existing-profile")
    grant.enable()
    grant.revoke()
    assert all(Path(command[1]) == job for command in calls)
    assert calls[0][2:] == ["/grant:r", "*" + SID + ":(OI)(CI)(M)", "/Q"]
    assert calls[-1][2:] == ["/remove:g", "*" + SID, "/T", "/L", "/Q"]


@pytest.mark.parametrize("bad_result", ["not_drained", "wrong_sid", "multiple_cpus"])
def test_partial_host_attestation_is_not_treated_as_conversion(tmp_path, monkeypatch, bad_result):
    fixture = Fixture(tmp_path, monkeypatch)
    if bad_result == "not_drained":
        fixture.launch_change = lambda result: replace(result, job_drained=False)
    elif bad_result == "wrong_sid":
        fixture.launch_change = lambda result: replace(
            result, process_audits=(ProcessAudit(10, "other-profile", True, 0),)
        )
    else:
        fixture.launch_change = lambda result: replace(result, affinity_mask=3)
    with pytest.raises(adapter.IsolationFailure, match="attestation"):
        fixture.convert()
    assert "revoke" in fixture.events and "read_artifact" not in fixture.events


def test_unsafe_job_tree_cannot_trigger_recursive_acl_mutation(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    job = root / "new-job"
    job.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"external security descriptor must not be touched")
    calls = []
    monkeypatch.setattr(adapter, "_existing_profile_sid", lambda _: SID)
    monkeypatch.setattr(adapter.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    grant = adapter._JobGrant(job, root, "existing-profile")
    grant.enable()
    os.link(outside, job / "linked-output.step")
    with pytest.raises(adapter.IsolationFailure, match="recursive grant revocation refused"):
        grant.revoke()
    assert calls[-1][1:] == [str(job), "/remove:g", "*" + SID, "/L", "/Q"]
    assert all(str(outside) not in command for command in calls)
    assert outside.read_bytes() == b"external security descriptor must not be touched"


def test_reparse_metadata_is_rejected_before_output_read(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    original = adapter._reparse
    monkeypatch.setattr(
        adapter,
        "_reparse",
        lambda info: original(info) or info.st_size == len(b"actual retained protocol output"),
    )
    with pytest.raises(adapter.IsolationFailure, match="link or junction"):
        fixture.convert()
    assert "read_artifact" not in fixture.events


def test_prelaunch_manifest_failure_is_isolation_not_a_converter_solve(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)

    def changed(*_):
        raise ValueError("pinned bytes changed")

    monkeypatch.setattr(adapter.frozen, "verify", changed)
    with pytest.raises(adapter.IsolationFailure, match="before launch"):
        fixture.convert()
    assert "grant" not in fixture.events and "run" not in fixture.events


def test_reference_cad_path_cannot_be_accidentally_used_as_converter_input(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    wrong = tmp_path / "wrong.step"
    wrong.write_bytes(INPUT)
    fixture.input = wrong
    with pytest.raises(ValueError, match="never reference CAD"):
        fixture.convert()
    assert fixture.events == []


def test_whole_case_deadline_shortens_conversion_without_skipping_cleanup(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: 100.)
    fixture.expected_timeout = 7.5
    converted = fixture.convert(case_deadline=107.5)
    assert converted.attestation['effective_conversion_timeout_seconds'] == 7.5
    assert fixture.events.index('run') < fixture.events.index('revoke')


def test_expired_case_deadline_never_launches_and_revokes_partial_grant(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: 100.)
    with pytest.raises(adapter.ConversionTimeout, match='before converter launch') as error:
        fixture.convert(case_deadline=99.)
    assert 'run' not in fixture.events and fixture.events[-2:] == ['revoke', 'verify']
    assert error.value.attestation['permissions_revoked']


def test_later_case_deadline_cannot_increase_original_converter_budget(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: 100.)
    converted = fixture.convert(case_deadline=1000.)
    assert converted.attestation['effective_conversion_timeout_seconds'] == 240


def pin_fixture(fixture):
    (fixture.artifact / 'LICENSE').write_bytes(b'pinned fixture license')
    metadata = {'schema': 1, 'source_revision': 'a' * 40,
                'python_magic': importlib.util.MAGIC_NUMBER.hex(),
                'python_runtime': str(fixture.runtime),
                'runtime_files': adapter.frozen.inventory(fixture.runtime),
                'application_files': adapter.frozen.inventory(fixture.artifact / 'application'),
                'license_sha256': adapter.frozen.digest(fixture.artifact / 'LICENSE')}
    (fixture.artifact / 'manifest.json').write_text(json.dumps(metadata))
    fixture.manifest_pin = adapter.frozen.digest(fixture.artifact / 'manifest.json')
    return locked.converter_catalogs(fixture.artifact, fixture.manifest_pin)[1]


WINDOWS_SEAL = pytest.mark.skipif(sys.platform != 'win32', reason='Requires live Windows seal')


@WINDOWS_SEAL
def test_live_session_preserves_conversion_protocol_and_avoids_rehashing(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    catalogs = pin_fixture(fixture)
    with seal.SealedRuntimeSession(catalogs, module_sha256=seal.module_digest()) as active:
        converted = fixture.convert(verification_session=active)
        assert fixture.verify_calls == 0
        assert fixture.events.count('run') == fixture.events.count('revoke') == 1
        assert fixture.events.index('revoke') < fixture.events.index('read_artifact')
        assert converted.attestation['identity_verification_mode'] == 'live_locked_session'
        assert converted.attestation['verification_session_identity'] == active.identity
        assert converted.report == reconstruction_report()
        assert converted.attestation['effective_conversion_timeout_seconds'] == 240


@WINDOWS_SEAL
def test_closed_live_session_never_launches_or_receives_grants(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    active = seal.SealedRuntimeSession(pin_fixture(fixture), module_sha256=seal.module_digest())
    with active:
        pass
    with pytest.raises(adapter.IsolationFailure, match='before launch'):
        fixture.convert(verification_session=active)
    assert fixture.verify_calls == 0 and 'grant' not in fixture.events


@WINDOWS_SEAL
def test_wrong_live_catalog_never_launches_or_receives_grants(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    full = pin_fixture(fixture)
    partial = {fixture.artifact / 'application':
               {name.removeprefix('application/'): value
                for name, value in full[fixture.artifact].items()
                if name.startswith('application/')},
               fixture.runtime: full[fixture.runtime]}
    with pytest.raises(seal.SealError, match='closed'):
        with seal.SealedRuntimeSession(partial, module_sha256=seal.module_digest()) as active:
            with pytest.raises(adapter.IsolationFailure, match='before launch'):
                fixture.convert(verification_session=active)
            active.check()
    assert fixture.verify_calls == 0 and 'grant' not in fixture.events


@WINDOWS_SEAL
@pytest.mark.parametrize('change', ['extra_file', 'closed'])
def test_lost_live_protection_revokes_and_never_reads_outputs(tmp_path, monkeypatch, change):
    fixture = Fixture(tmp_path, monkeypatch)
    active = seal.SealedRuntimeSession(pin_fixture(fixture), module_sha256=seal.module_digest())
    with pytest.raises(adapter.IsolationFailure, match='changed; do not grade'):
        with active:
            if change == 'extra_file':
                fixture.mutation = lambda _: (fixture.artifact / 'extra.py').write_bytes(
                    b'unpinned')
            else:
                fixture.mutation = lambda _: active.close()
            fixture.convert(verification_session=active)
    assert 'revoke' in fixture.events and 'read_artifact' not in fixture.events
    assert list(fixture.output.iterdir()) == []


@WINDOWS_SEAL
def test_retention_time_namespace_change_cannot_publish_live_attestation(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    active = seal.SealedRuntimeSession(pin_fixture(fixture), module_sha256=seal.module_digest())
    original = adapter._copy_outputs

    def changing(job, output, hashes):
        original(job, output, hashes)
        (fixture.artifact / 'late.py').write_bytes(b'added after post-conversion check')

    monkeypatch.setattr(adapter, '_copy_outputs', changing)
    with pytest.raises(adapter.IsolationFailure, match='during retention'):
        with active:
            fixture.convert(verification_session=active)
    assert not (fixture.output / adapter.ATTESTATION).exists()


@pytest.mark.parametrize('fake', [False, True, 'recorded-session-identity', {}, object()])
def test_fake_live_session_values_cannot_select_a_skip_mode(tmp_path, monkeypatch, fake):
    fixture = Fixture(tmp_path, monkeypatch)
    with pytest.raises(adapter.IsolationFailure, match='before launch'):
        fixture.convert(verification_session=fake)
    assert fixture.verify_calls == 0 and 'grant' not in fixture.events

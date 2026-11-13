import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import frozen_evaluation_controller as controller


class ConversionFailure(RuntimeError):
    classification = "converter_exit"

    def __init__(self, attestation):
        super().__init__("actual converter exited with code17")
        self.attestation = attestation


class FakeBackend:
    """Real files/hashes with fake child outputs; no native conversion claims."""

    allowed_outputs = {"reconstruction.step", "report.json"}

    def __init__(self, fixture):
        self.fixture = fixture
        self.convert_calls = []
        self.grade_calls = []
        self.fail_conversion = False
        self.mutate_after_grade = None
        self.grade_change = None
        self.tool_version = "tools-v1"
        self.grade_timeouts = []
        self.close_calls = 0
        self.readiness_calls = []
        self.readiness_error = None
        self.readiness_change = None

    def initialise(self, configuration):
        self.configuration = configuration
        from tools.multires_summary import aggregate_details

        self.aggregate = aggregate_details

    def close(self):
        self.close_calls += 1

    def verify(self):
        for path, expected in self.fixture.pinned_files.items():
            if controller._digest(path) != expected:
                raise controller.ControllerError("pinned runtime/library changed")

    def identity(self):
        config = self.configuration
        return {
            "backend": "protocol-only",
            "converter_manifest_sha256": config.converter_sha256,
            "grader_manifest_sha256": config.grader_sha256,
            "grader_source_revision": "frozen-grade-revision",
            "grader_runtime_inventory_sha256": "runtime-inventory",
            "tools_sha256": {"tools/" + name: self.tool_version for name in controller.TOOLS},
        }

    @contextmanager
    def sample(self):
        yield {
            "sampled_peak_tree_rss_bytes": 100,
            "memory_sample_count": 1,
            "memory_sampling_interval_seconds": 1,
        }

    def convert(self, **kwargs):
        self.convert_calls.append(kwargs)
        assert set(kwargs) == {"input_stl", "input_sha256", "units", "output_dir", "case_deadline"}
        assert kwargs["input_stl"].name == "input.stl"
        assert "secret-model" not in str(kwargs)
        assert str(self.configuration.manifest) not in str(kwargs)
        assert str(self.fixture.source_step) not in str(kwargs)
        output = kwargs["output_dir"]
        attestation = {
            "permissions_revoked": True,
            "input_unchanged": True,
            "sealed_converter_unchanged": True,
            "second_conversion_not_run": True,
            "launch": {"job_drained": True},
            "input_sha256": kwargs["input_sha256"],
            "input_units": kwargs["units"],
            "converter_manifest_sha256": self.configuration.converter_sha256,
            "code_sha256": {name: self.tool_version for name in ("adapter", "sandbox", "verifier")},
            "artifact_copy_wall_seconds": 0.02,
            "conversion_wall_seconds": 1.25,
            "adapter_wall_seconds": 1.5,
        }
        if self.fail_conversion:
            raise ConversionFailure(attestation)
        (output / "reconstruction.step").write_bytes(b"actual retained protocol STEP")
        (output / "report.json").write_text('{"protocol":"not a native grade"}')
        attestation["output_sha256"] = {p.name: controller._digest(p) for p in output.iterdir()}
        (output / "isolation-attestation.json").write_text(json.dumps(attestation))
        return SimpleNamespace(attestation=attestation)

    def grade(self, case_configuration, retained, destination, artifacts, *, timeout):
        assert 0 < timeout <= 360
        self.grade_timeouts.append(timeout)
        self.grade_calls.append(case_configuration)
        configuration = json.loads(case_configuration.read_text())
        case = configuration["case"]
        assert set(configuration) == {
            "grader_manifest_sha256",
            "case",
            "manifest_parent",
            "scratch_root",
            "retained_sha256",
        }
        assert Path(configuration["scratch_root"]) == self.fixture.root
        assert configuration["retained_sha256"] == {
            name: controller._digest(retained / name) for name in self.allowed_outputs
        }
        result = {field: case[field] for field in ("id", "source_model_id", "split", "preset")}
        result.update(
            accepted=True,
            status="complete",
            stl_strict_accepted=True,
            second_conversion_not_run=True,
            grader_manifest_sha256=self.configuration.grader_sha256,
            grader_source_revision="frozen-grade-revision",
            grader_case_sha256=controller._digest(case_configuration),
            grader_runtime_files_sha256="runtime-inventory",
            actual_output_sha256=configuration["retained_sha256"],
            fixture_sha256={"stl": case["input_sha256"], "source_step": case["source_sha256"]},
            artifact_copy_wall_seconds=0.03,
            verification_wall_seconds=0.7,
            fixture_readiness={"fixture_ready": True},
        )
        if self.grade_change:
            self.grade_change(result)
        destination.write_text(json.dumps(result))
        if self.mutate_after_grade:
            self.mutate_after_grade(retained)
        return result

    def readiness(self, case_configuration, destination, *, timeout):
        assert timeout <= 360
        self.readiness_calls.append(case_configuration)
        if self.readiness_error:
            raise self.readiness_error
        configuration = json.loads(case_configuration.read_text())
        assert Path(configuration["scratch_root"]) == self.fixture.root
        case = configuration["case"]
        result = {key: case[key] for key in ("id", "source_model_id", "preset", "split")}
        result.update(
            accepted=False,
            reference_readiness_only=True,
            fixture_readiness={"fixture_ready": True},
            second_conversion_not_run=True,
            grader_manifest_sha256=self.configuration.grader_sha256,
            grader_source_revision="frozen-grade-revision",
            grader_runtime_files_sha256="runtime-inventory",
            grader_case_sha256=controller._digest(case_configuration),
            fixture_sha256={"source_step": case["source_sha256"]},
            actual_output_sha256={},
        )
        if self.readiness_change:
            self.readiness_change(result)
        destination.write_text(json.dumps(result))
        return result


class Fixture:
    def __init__(self, tmp_path, cases=3):
        self.root = tmp_path / "owned"
        self.root.mkdir()
        self.converter = self.root / "converter"
        self.grader = self.root / "grader"
        self.converter.mkdir()
        self.grader.mkdir()
        self.library = self.grader / "dependency.dll"
        self.library.write_bytes(b"pinned native dependency bytes")
        self.pinned_files = {self.library: controller._digest(self.library)}
        self.source_step = tmp_path / "secret-model.step"
        self.source_step.write_bytes(b"protocol ground truth; no real CAD")
        self.manifest = self.root / "manifest.json"
        rows = []
        for i in range(cases):
            stl = tmp_path / f"secret-model-q{i:02}.stl"
            stl.write_bytes(f"protocol STL{i}".encode())
            rows.append(
                {
                    "id": f"secret-model-q{i:02}",
                    "source_model_id": "source-one",
                    "geometry_family": "family-one",
                    "preset": f"q{i:02}",
                    "split": "development",
                    "stl": str(stl),
                    "source_step": str(self.source_step),
                    "input_sha256": controller._digest(stl),
                    "source_sha256": controller._digest(self.source_step),
                    "input_units": "mm",
                }
            )
        self.manifest.write_text(json.dumps({"cases": rows, "complete": False}))
        self.config = controller.Configuration(
            self.manifest,
            self.converter,
            "converter-pin",
            self.grader,
            "grader-pin",
            "existing-profile",
            self.root,
            self.root / "baseline.json",
        )
        self.backend = FakeBackend(self)

    def run(self, batch=None, output=None):
        config = replace(
            self.config,
            batch_cases=batch,
            output=output if output is not None else self.config.output,
        )
        return controller.run_controller(config, _backend=self.backend)


def test_actual_files_retained_then_graded_once_and_certified(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    result = fixture.run()
    row = result["results"][0]
    assert row["accepted"] and row["certified"] and row["reused"] is False
    assert len(fixture.backend.convert_calls) == len(fixture.backend.grade_calls) == 1
    assert row["conversion_wall_seconds"] == 1.25
    assert row["artifact_copy_wall_seconds"] == 0.02
    assert row["grader_artifact_copy_wall_seconds"] == 0.03
    assert result["summary"]["complete"]
    assert not result["summary"]["full_1000x10_evaluation_complete"]
    assert result["summary"]["workflow_qualified"] is False
    cache = list((fixture.root / "frozen-controller-completed-v1").glob("*.json"))
    assert len(cache) == 1 and json.loads(cache[0].read_text())["schema"] == controller.CACHE_SCHEMA


def test_resume_skips_reused_prefix_and_batch_counts_only_fresh(tmp_path):
    fixture = Fixture(tmp_path)
    first = fixture.run(batch=1)
    assert first["summary"]["evaluated_cases"] == 1 and not first["summary"]["complete"]
    second = fixture.run(batch=1)
    assert second["summary"]["evaluated_cases"] == 2
    assert second["summary"]["fresh_cases"] == 1 and second["summary"]["reused_cases"] == 1
    assert len(fixture.backend.convert_calls) == 2
    assert second["results"][0]["conversion_wall_seconds"] == 1.25
    assert second["results"][0]["timing_provenance"] == "historical_verified_case"
    assert "reuse_lookup_wall_seconds" in second["results"][0]
    third = fixture.run(batch=1)
    assert third["summary"]["complete"] and len(fixture.backend.convert_calls) == 3


def test_converter_failures_count_as_fresh_and_reuse_only_after_cleanup_checks(tmp_path):
    fixture = Fixture(tmp_path, cases=2)
    fixture.backend.fail_conversion = True
    first = fixture.run(batch=1)
    row = first["results"][0]
    assert row["failure_kind"] == "converter_exit" and not row["accepted"]
    assert row["execution_certified"] and row["certified"] and row["solver_outcome_scored"]
    assert len(fixture.backend.readiness_calls) == 1
    assert first["summary"]["fresh_cases"] == 1 and not fixture.backend.grade_calls
    second = fixture.run(batch=1)
    assert second["summary"]["reused_cases"] == 1 and second["summary"]["fresh_cases"] == 1
    assert len(fixture.backend.convert_calls) == 2


def test_case_runtime_or_controller_identity_cannot_reuse_old_cache(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    fixture.run()
    fixture.backend.tool_version = "tools-v2"
    with pytest.raises(controller.ControllerError, match="contract changed"):
        fixture.run()
    another = fixture.run(output=fixture.root / "new-qualified-run.json")
    assert another["summary"]["fresh_cases"] == 1 and len(fixture.backend.convert_calls) == 2


@pytest.mark.parametrize("tamper", ["retained", "record"])
def test_cache_rejects_changed_record_or_retained_bytes(tmp_path, tamper):
    fixture = Fixture(tmp_path, cases=1)
    fixture.run()
    path = next((fixture.root / "frozen-controller-completed-v1").glob("*.json"))
    record = json.loads(path.read_text())
    if tamper == "retained":
        (Path(record["retained"]) / "reconstruction.step").write_bytes(b"changed")
    else:
        record["result"]["accepted"] = False
        path.write_text(json.dumps(record))
    with pytest.raises(controller.ControllerError, match="changed"):
        fixture.run()
    assert len(fixture.backend.convert_calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "second_conversion_not_run",
        "grader_manifest_sha256",
        "actual_output_sha256",
        "grader_case_sha256",
    ],
)
def test_wrong_or_reconverted_grader_output_is_not_a_strict_pass(tmp_path, field):
    fixture = Fixture(tmp_path, cases=1)
    fixture.backend.grade_change = lambda result: result.update({field: False})
    result = fixture.run()
    row = result["results"][0]
    assert row["failure_kind"] == "grader" and not row["accepted"] and not row["certified"]
    assert not list((fixture.root / "frozen-controller-completed-v1").glob("*.json"))
    assert (Path(row["attempt"]) / "grade.json").exists()


@pytest.mark.parametrize("mutation", ["runtime", "output", "source_fixture"])
def test_after_case_tamper_never_publishes_a_completed_record(tmp_path, mutation):
    fixture = Fixture(tmp_path, cases=1)

    def mutate(retained):
        if mutation == "runtime":
            fixture.library.write_bytes(b"changed native code")
        elif mutation == "output":
            (retained / "reconstruction.step").write_bytes(b"changed STEP")
        else:
            fixture.source_step.write_bytes(b"changed reference")

    fixture.backend.mutate_after_grade = mutate
    result = fixture.run()
    row = result["results"][0]
    assert not row["accepted"] and not row["certified"]
    assert row["failure_kind"] == ("fixture" if mutation == "source_fixture" else "isolation")
    assert not list((fixture.root / "frozen-controller-completed-v1").glob("*.json"))
    assert (Path(row["attempt"]) / "attempt-result.json").exists()


def test_missing_fixture_is_not_called_a_solver_failure_or_cached(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    fixture.source_step.unlink()
    result = fixture.run(batch=1)
    row = result["results"][0]
    assert row["failure_kind"] == "fixture" and not row["certified"] and not row["accepted"]
    assert result["summary"]["fresh_cases"] == 1 and not result["summary"]["complete"]
    assert not fixture.backend.convert_calls


def test_all_ten_variants_are_one_design_and_small_run_is_not_full_corpus(tmp_path):
    fixture = Fixture(tmp_path, cases=10)
    result = fixture.run()
    summary = result["summary"]
    assert summary["source_model_rates"]["all_ten_strict_pass_source_models"] == 1
    assert summary["coverage"]["expected_source_models"] == 1
    assert not summary["full_1000x10_evaluation_complete"]


def test_split_grouping_is_validated_before_any_conversion(tmp_path):
    fixture = Fixture(tmp_path, cases=2)
    manifest = json.loads(fixture.manifest.read_text())
    manifest["cases"][1].update(source_model_id="different-model", split="holdout")
    fixture.manifest.write_text(json.dumps(manifest))
    with pytest.raises(controller.ControllerError, match="crosses splits"):
        fixture.run()
    assert not fixture.backend.convert_calls


def test_working_app_edits_are_absent_from_controller_tool_identity(tmp_path, monkeypatch):
    (tmp_path / "tools").mkdir()
    (tmp_path / "app").mkdir()
    for name in controller.TOOLS:
        (tmp_path / "tools" / name).write_text("stable tool")
    application = tmp_path / "app/surface_brep.py"
    application.write_text("first live experiment")
    monkeypatch.setattr(controller, "ROOT", tmp_path)
    before = controller._tool_identity()
    application.write_text("different live experiment")
    assert controller._tool_identity() == before
    assert all(name.startswith("tools/") for name in before)


def test_bootstrap_refuses_wrong_runtime_flags_without_loading_app(tmp_path):
    with pytest.raises(controller.ControllerError, match="-I -B"):
        controller._require_runtime({"python_runtime": str(tmp_path)})


def test_wrong_loaded_app_origin_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "app.controller_test_probe",
        SimpleNamespace(__file__=str(tmp_path / "working-source.py")),
    )
    with pytest.raises(controller.ControllerError, match="outside the sealed"):
        controller._assert_app_origins(tmp_path / "sealed-application")


def test_controller_module_import_does_not_import_any_app_in_fresh_process():
    path = Path(controller.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        's=importlib.util.spec_from_file_location("checked_controller",sys.argv[1]); '
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m); "
        'print(any(n=="app" or n.startswith("app.") for n in sys.modules))'
    )
    result = subprocess.check_output([sys.executable, "-I", "-B", "-c", code, str(path)], text=True)
    assert result.strip() == "False"


def test_one_deadline_clamps_remaining_grader_budget(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])
    original = fixture.backend.convert

    def convert(**kwargs):
        assert kwargs["case_deadline"] == 360
        clock["now"] = 200
        return original(**kwargs)

    fixture.backend.convert = convert
    result = fixture.run()
    assert result["results"][0]["accepted"]
    assert fixture.backend.grade_timeouts == [160]


def test_case_deadline_cannot_accept_late_grade(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])
    fixture.backend.mutate_after_grade = lambda _: clock.update(now=361)
    result = fixture.run()
    row = result["results"][0]
    assert not row["accepted"] and row["failure_kind"] == "case_timeout"
    assert row["certified"] and row["solver_outcome_scored"]


def test_expired_conversion_budget_does_not_start_reference_grader(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])
    original = fixture.backend.convert

    def convert(**kwargs):
        result = original(**kwargs)
        clock["now"] = 361
        return result

    fixture.backend.convert = convert
    result = fixture.run()
    row = result["results"][0]
    assert row["failure_kind"] == "case_timeout" and not row["accepted"]
    assert row["execution_certified"] and row["solver_outcome_scored"]
    assert not fixture.backend.grade_calls
    assert len(fixture.backend.readiness_calls) == 1


def test_unready_ground_truth_is_unscored_not_a_solver_failure(tmp_path):
    fixture = Fixture(tmp_path, cases=1)

    def unready(result):
        result.update(
            accepted=False,
            status="fixture_error",
            evaluation_failure_kind="fixture",
            fixture_readiness={"fixture_ready": False, "reason": "unrepresented_native_faces"},
        )

    fixture.backend.grade_change = unready
    result = fixture.run()
    row = result["results"][0]
    assert row["failure_kind"] == "fixture" and row["execution_certified"]
    assert not row["certified"] and not row["solver_outcome_scored"] and not row["accepted"]
    assert result["summary"]["reference_fixture_unready_cases"] == 1
    assert result["summary"]["solver_coverage"]["evaluated_cases"] == 0
    assert not result["summary"]["complete"]
    ledger = json.loads(fixture.config.output.with_suffix(".fixture-exclusions.json").read_text())
    assert len(ledger["sources"]) == 1
    assert next(iter(ledger["sources"].values()))["source_sha256"] == controller._digest(
        fixture.source_step
    )
    repeated = fixture.run()
    assert repeated["results"][0]["reused"] and len(fixture.backend.convert_calls) == 1


def test_adapter_unit_mismatch_cannot_reach_reference_grading(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    original = fixture.backend.convert

    def mismatch(**kwargs):
        converted = original(**kwargs)
        converted.attestation["input_units"] = "in"
        (kwargs["output_dir"] / "isolation-attestation.json").write_text(
            json.dumps(converted.attestation)
        )
        return converted

    fixture.backend.convert = mismatch
    result = fixture.run()
    row = result["results"][0]
    assert row["failure_kind"] == "isolation" and not row["accepted"]
    assert not fixture.backend.grade_calls


def test_backend_cleanup_runs_on_resume_contract_and_initialisation_failures(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    fixture.run()
    assert fixture.backend.close_calls == 1
    fixture.backend.tool_version = "changed"
    with pytest.raises(controller.ControllerError, match="contract changed"):
        fixture.run()
    assert fixture.backend.close_calls == 2

    def fail(_configuration):
        raise RuntimeError("initialisation failed after opening owned handles")

    fixture.backend.initialise = fail
    with pytest.raises(RuntimeError, match="initialisation failed"):
        fixture.run()
    assert fixture.backend.close_calls == 3


def test_native_conversion_bridge_passes_live_session_and_same_case_deadline(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    backend = controller._NativeBackend()
    backend.configuration = fixture.config
    backend.verification_session = object()
    calls = []
    backend.adapter = SimpleNamespace(
        convert_isolated=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    backend.convert(
        input_stl=fixture.root / "input.stl",
        input_sha256="stl-pin",
        units="mm",
        output_dir=fixture.root / "retained",
        case_deadline=360,
    )
    kwargs = calls[0][1]
    assert kwargs["verification_session"] is backend.verification_session
    assert kwargs["case_deadline"] == 360
    assert "source_step" not in kwargs and "case" not in kwargs


def test_native_session_is_closed_if_bootstrap_fails_after_enter(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, cases=1)
    runtime = fixture.root / "runtime"
    runtime.mkdir()
    module = fixture.root / "seal-module.py"
    module.write_text("protocol seal module")
    events = []

    class Session:
        def __init__(self, catalogs, *, module_sha256):
            pass

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *args):
            events.append("exit")

    def catalogs(artifact, pin, *, expected_kind):
        return {"python_runtime": str(runtime)}, {
            artifact: {"manifest.json": "pin"},
            runtime: {"python.exe": "pin"},
        }

    def fail(*args, **kwargs):
        raise RuntimeError("post-enter verification failed")

    modules = {
        "freeze_evaluation_converter": SimpleNamespace(),
        "freeze_evaluation_grader": SimpleNamespace(),
        "sealed_runtime": SimpleNamespace(__file__=str(module), SealedRuntimeSession=Session),
        "locked_artifact_identity": SimpleNamespace(
            artifact_catalogs=catalogs, verify_locked_artifact=fail
        ),
    }
    monkeypatch.setattr(controller, "_load_tool", lambda name: modules[name])
    monkeypatch.setattr(controller, "_require_runtime", lambda _: None)
    with pytest.raises(RuntimeError, match="post-enter verification failed"):
        controller.run_controller(fixture.config, _backend=controller._NativeBackend())
    assert events == ["enter", "exit"]


def test_unscored_cached_failure_upgrades_readiness_without_reconversion(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    fixture.backend.fail_conversion = True
    fixture.backend.readiness_error = RuntimeError("reference preparation temporarily unavailable")
    first = fixture.run(batch=1)
    original = first["results"][0]
    assert original["execution_certified"] and not original["solver_outcome_scored"]
    assert not first["summary"]["complete"] and len(fixture.backend.convert_calls) == 1
    fixture.backend.readiness_error = None
    second = fixture.run(batch=1)
    upgraded = second["results"][0]
    assert upgraded["reused"] and upgraded["fixture_preparation_fresh"]
    assert upgraded["certified"] and upgraded["solver_outcome_scored"] and not upgraded["accepted"]
    assert upgraded["case_wall_seconds"] == original["case_wall_seconds"]
    assert upgraded["conversion_wall_seconds"] == original["conversion_wall_seconds"]
    assert second["summary"]["fresh_evaluations"] == 1 and second["summary"]["complete"]
    assert len(fixture.backend.convert_calls) == 1 and len(fixture.backend.readiness_calls) == 2
    records = list((fixture.root / "frozen-controller-completed-v1").glob("*.json"))
    assert len(records) == 2
    primary = next(path for path in records if not path.name.endswith(".ready.json"))
    assert json.loads(primary.read_text())["result"]["solver_outcome_scored"] is False
    third = fixture.run(batch=1)
    assert third["results"][0]["reused"] and not third["results"][0]["fixture_preparation_fresh"]
    assert len(fixture.backend.convert_calls) == 1 and len(fixture.backend.readiness_calls) == 2


def test_normal_grade_readiness_is_reused_for_other_export_failure(tmp_path):
    fixture = Fixture(tmp_path, cases=2)
    fixture.run(batch=1)
    fixture.backend.fail_conversion = True
    second = fixture.run(batch=1)
    assert second["results"][1]["failure_kind"] == "converter_exit"
    assert second["results"][1]["solver_outcome_scored"] and second["results"][1]["certified"]
    assert not fixture.backend.readiness_calls
    assert (
        second["results"][1]["fixture_preparation_timing_provenance"]
        == "reused_source_readiness_receipt"
    )


@pytest.mark.parametrize(
    "wrong_field",
    ["grader_manifest_sha256", "grader_case_sha256", "reference_readiness_only", "fixture_sha256"],
)
def test_readiness_only_bad_attestation_cannot_score_execution_failure(tmp_path, wrong_field):
    fixture = Fixture(tmp_path, cases=1)
    fixture.backend.fail_conversion = True
    fixture.backend.readiness_change = lambda proof: proof.update({wrong_field: False})
    result = fixture.run()
    row = result["results"][0]
    assert row["execution_certified"] and not row["solver_outcome_scored"] and not row["accepted"]
    assert not list((fixture.root / "frozen-reference-readiness-v1").glob("*.json"))
    assert len(fixture.backend.convert_calls) == 1 and not fixture.backend.grade_calls


def test_source_readiness_receipt_tampering_is_not_accepted(tmp_path):
    fixture = Fixture(tmp_path, cases=2)
    fixture.run(batch=1)
    receipt = next((fixture.root / "frozen-reference-readiness-v1").glob("*.json"))
    data = json.loads(receipt.read_text())
    data["proof"]["fixture_readiness"]["fixture_ready"] = False
    receipt.write_text(json.dumps(data))
    fixture.backend.fail_conversion = True
    second = fixture.run(batch=1)
    row = second["results"][1]
    assert not row["solver_outcome_scored"] and not row["accepted"]
    assert "receipt changed" in row["fixture_preparation_error"]
    assert len(fixture.backend.convert_calls) == 2


def test_native_readiness_bridge_uses_frozen_cli_without_retained_output_args(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    backend = controller._NativeBackend()
    backend.configuration = fixture.config
    backend.application = fixture.grader / "application"
    backend.grader_metadata = {
        "python_runtime": str(fixture.grader / "runtime"),
        "entrypoint": "application/grader.pyc",
    }
    calls = []
    destination = fixture.root / "ready.json"

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        destination.write_text('{"reference_readiness_only":true}')
        return 0

    backend.runner = runner
    backend.readiness(fixture.root / "source-case.json", destination, timeout=59)
    command, kwargs = calls[0]
    assert command[1:3] == ["-I", "-B"] and command[4] == "--reference-readiness"
    assert len(command) == 7 and kwargs["timeout"] == 59


@pytest.mark.parametrize("source_ready", [True, False])
def test_host_timeout_preserves_verified_conversion_and_qualifies_only_ready_source(
    tmp_path, monkeypatch, source_ready,
):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])

    def grade_timeout(*args, **kwargs):
        clock["now"] = 361.0
        raise TimeoutError("host reference preparation exceeded the original case deadline")

    fixture.backend.grade = grade_timeout
    if not source_ready:
        fixture.backend.readiness_change = lambda row: row.update(
            fixture_readiness={"fixture_ready": False}, evaluation_failure_kind="fixture",
        )
    result = fixture.run()
    row = result["results"][0]
    assert row["accepted"] is False and row["execution_certified"] is True
    assert row["failure_origin"] == "grader_timeout"
    assert row["conversion_attestation"]["permissions_revoked"] is True
    assert row["conversion_attestation"]["launch"]["job_drained"] is True
    assert row["conversion_wall_seconds"] == 1.25
    assert row["solver_outcome_scored"] is source_ready
    assert row["certified"] is source_ready
    assert row["failure_kind"] == ("case_timeout" if source_ready else "fixture")
    assert len(fixture.backend.convert_calls) == 1
    assert len(fixture.backend.readiness_calls) == 1
    resumed = fixture.run()["results"][0]
    assert resumed["reused"] and not resumed["accepted"]
    assert len(fixture.backend.convert_calls) == 1


def test_host_timeout_with_unknown_source_retries_readiness_without_reconversion(
    tmp_path, monkeypatch,
):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])

    def grade_timeout(*args, **kwargs):
        clock["now"] = 361.0
        raise TimeoutError("host grading exhausted the original case budget")

    fixture.backend.grade = grade_timeout
    fixture.backend.readiness_error = RuntimeError("source qualification unavailable")
    row = fixture.run()["results"][0]
    assert row["execution_certified"] and not row["solver_outcome_scored"]
    assert not row["certified"] and not row["accepted"]
    assert row["failure_kind"] == "case_timeout"
    fixture.backend.readiness_error = None
    row = fixture.run()["results"][0]
    assert row["reused"] and row["certified"] and not row["accepted"]
    assert row["solver_outcome_scored"] and row["failure_kind"] == "case_timeout"
    assert len(fixture.backend.convert_calls) == 1
    assert len(fixture.backend.readiness_calls) == 2


def test_grader_protocol_error_is_not_certified_as_a_timeout(tmp_path):
    fixture = Fixture(tmp_path, cases=1)

    def protocol_error(*args, **kwargs):
        raise controller.GraderError("actual result protocol failure")

    fixture.backend.grade = protocol_error
    row = fixture.run()["results"][0]
    assert not row["execution_certified"] and not row["certified"] and not row["accepted"]
    assert row["failure_origin"] == "grader"
    assert row["conversion_attestation"]["permissions_revoked"]
    assert not fixture.backend.readiness_calls


def test_expired_clock_cannot_certify_incomplete_isolation_attestation(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])
    original = fixture.backend.convert

    def unsafe_conversion(**kwargs):
        value = original(**kwargs)
        value.attestation["permissions_revoked"] = False
        clock["now"] = 361.0
        return value

    fixture.backend.convert = unsafe_conversion
    row = fixture.run()["results"][0]
    assert row["failure_kind"] == "isolation"
    assert not row["execution_certified"] and not row["certified"] and not row["accepted"]
    assert not fixture.backend.readiness_calls


def _prefix_fixture(tmp_path, cases=15):
    fixture = Fixture(tmp_path, cases=cases)
    document = json.loads(fixture.manifest.read_text())
    for index, row in enumerate(document['cases']):
        row.update(source_model_id=f'source-{index // 10}',
                   geometry_family=f'family-{index // 10}', preset=f'q{index % 10:02d}')
    fixture.manifest.write_text(json.dumps(document))
    return fixture


def test_certified_prefix_uses_two_boundary_checks_without_new_work(tmp_path):
    fixture = _prefix_fixture(tmp_path)
    fixture.run()
    previous_conversions = len(fixture.backend.convert_calls)
    previous_gradings = len(fixture.backend.grade_calls)
    original_verify = fixture.backend.verify
    checks = []

    def verify():
        checks.append(True)
        original_verify()

    fixture.backend.verify = verify
    result = fixture.run(batch=1)
    assert len(checks) == 3  # Initial bootstrap, then before/after cached transaction.
    assert len(result['results']) == 15 and result['summary']['fresh_cases'] == 0
    assert all(row['cache_lookup_protocol'] == 'certified-prefix-full-boundary-checks-v1'
               for row in result['results'])
    assert all(row['reused'] and row['timing_provenance'] == 'historical_verified_case'
               for row in result['results'])
    assert len(fixture.backend.convert_calls) == previous_conversions
    assert len(fixture.backend.grade_calls) == previous_gradings


def test_prefix_stops_at_first_missing_case_and_preserves_fresh_batch_count(tmp_path):
    fixture = _prefix_fixture(tmp_path, cases=4)
    first = fixture.run(batch=2)
    assert len(first['results']) == 2
    resumed = fixture.run(batch=1)
    assert len(resumed['results']) == 3 and resumed['summary']['fresh_cases'] == 1
    assert [row['reused'] for row in resumed['results']] == [True, True, False]
    assert len(fixture.backend.convert_calls) == 3


def test_prefix_keeps_cache_checksum_and_retained_output_hash_checks(tmp_path):
    fixture = _prefix_fixture(tmp_path, cases=2)
    fixture.run()
    original_output = fixture.config.output.read_bytes()
    record = next((fixture.root / 'frozen-controller-completed-v1').glob('*.json'))
    envelope = json.loads(record.read_text())
    retained = Path(envelope['retained']) / 'reconstruction.step'
    retained.write_bytes(b'tampered retained CAD output')
    with pytest.raises(controller.ControllerError, match='retained output changed'):
        fixture.run(batch=1)
    assert fixture.config.output.read_bytes() == original_output
    assert len(fixture.backend.convert_calls) == 2


def test_prefix_detects_fixture_change_before_reusing_a_cached_record(tmp_path):
    fixture = _prefix_fixture(tmp_path, cases=3)
    fixture.run()
    case = json.loads(fixture.manifest.read_text())['cases'][1]
    Path(case['stl']).write_bytes(b'changed input bytes')
    resumed = fixture.run(batch=1)
    assert resumed['results'][0]['reused']
    assert resumed['results'][1]['status'] == 'fixture_error'
    assert not resumed['results'][1]['accepted']
    assert len(fixture.backend.convert_calls) == 3  # No converter ran for bad fixture.


def test_unscored_cache_record_does_not_enter_fast_prefix(tmp_path):
    fixture = _prefix_fixture(tmp_path, cases=1)
    fixture.run()
    record = next((fixture.root / 'frozen-controller-completed-v1').glob('*.json'))
    envelope = json.loads(record.read_text())
    envelope['result'].update(certified=False, solver_outcome_scored=False)
    envelope.pop('record_sha256')
    envelope['record_sha256'] = controller._hash(envelope)
    record.write_text(json.dumps(envelope))
    expected = controller._cases(json.loads(fixture.manifest.read_text()),
                                 fixture.manifest.parent, None)
    rows, halt = controller._load_scored_cached_prefix(
        expected, fixture.backend.identity(), record.parent, fixture.root, fixture.backend,
        fixture.manifest, controller._digest(fixture.manifest))
    assert rows == [] and halt is None
    assert len(fixture.backend.grade_calls) == 1 and not fixture.backend.readiness_calls


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows seal proof')
def test_native_prefix_boundary_detects_transient_catalog_changes(tmp_path, monkeypatch):
    from tools import sealed_runtime as sealed
    fixture = _prefix_fixture(tmp_path, cases=3)
    fixture.run()
    original_output = fixture.config.output.read_bytes()
    catalog = {fixture.grader: {'dependency.dll': controller._digest(fixture.library)}}
    active = sealed.SealedRuntimeSession(catalog, module_sha256=sealed.module_digest())
    original_load = controller._load_completed
    original_verify = fixture.backend.verify
    loaded = []

    def load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        loaded.append(True)
        if len(loaded) == 2:
            transient = fixture.grader / 'transient-private.step'
            transient.write_bytes(b'must invalidate even after removal')
            transient.unlink()
        return result

    def verify():
        original_verify()
        active.require_catalogs(catalog)

    fixture.backend.verify = verify
    monkeypatch.setattr(controller, '_load_completed', load)
    with pytest.raises(sealed.SealError, match='changed'):
        with active:
            fixture.run(batch=1)
    assert fixture.config.output.read_bytes() == original_output
    assert len(fixture.backend.convert_calls) == 3
    with pytest.raises(sealed.SealError, match='closed'):
        active.check()


class GraderResourceFailure(controller.GraderError):
    classification = "grader_resource_limit"

    def __init__(self, *, drained=True):
        super().__init__("protocol-only bounded host memory exhaustion")
        self.resource_evidence = {
            "worker_drained": drained,
            "process_memory_limit_bytes": 8 * 1024**3,
            "memory_limit_events": [{"message": 9, "pid": 123}],
        }


@pytest.mark.parametrize("expired", [False, True])
def test_drained_host_resource_failure_is_cached_but_always_unscored(
    tmp_path, monkeypatch, expired,
):
    fixture = Fixture(tmp_path, cases=1)
    clock = {"now": 0.0}
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock["now"])

    def resource_failure(*args, **kwargs):
        if expired:
            clock["now"] = 361.0
        raise GraderResourceFailure()

    fixture.backend.grade = resource_failure
    row = fixture.run()["results"][0]
    assert row["execution_certified"] and not row["accepted"]
    assert not row["certified"] and not row["solver_outcome_scored"]
    assert row["failure_kind"] == row["failure_origin"] == "grader_resource_limit"
    assert row["resource_evidence"]["worker_drained"] is True
    assert row["conversion_attestation"]["permissions_revoked"] is True
    assert not fixture.backend.readiness_calls
    resumed = fixture.run()["results"][0]
    assert resumed["reused"] and not resumed["solver_outcome_scored"]
    assert len(fixture.backend.convert_calls) == 1
    assert not fixture.backend.readiness_calls
    assert not fixture.config.output.with_suffix(".fixture-exclusions.json").exists()


def test_ready_source_receipt_cannot_score_partial_invalid_host_resource_grade(tmp_path):
    fixture = Fixture(tmp_path, cases=2)
    original = fixture.backend.grade
    calls = []

    def grade(case, retained, destination, artifacts, *, timeout):
        calls.append(case)
        if len(calls) == 1:
            return original(case, retained, destination, artifacts, timeout=timeout)
        destination.write_text(json.dumps({
            "accepted": False, "fixture_readiness": {"fixture_ready": False},
            "evaluation_failure_kind": "fixture",
        }))
        raise GraderResourceFailure()

    fixture.backend.grade = grade
    result = fixture.run()
    assert result["results"][0]["accepted"]
    row = result["results"][1]
    assert row["execution_certified"] and not row["solver_outcome_scored"]
    assert row["failure_kind"] == "grader_resource_limit" and not row["accepted"]
    assert "fixture_readiness" not in row
    assert not fixture.backend.readiness_calls
    assert not fixture.config.output.with_suffix(".fixture-exclusions.json").exists()
    resumed = fixture.run()
    assert all(row["reused"] for row in resumed["results"])
    assert not resumed["results"][1]["solver_outcome_scored"]
    assert len(fixture.backend.convert_calls) == len(calls) == 2


@pytest.mark.parametrize("drained", [False, None])
def test_undrained_host_resource_failure_is_not_execution_certified(tmp_path, drained):
    fixture = Fixture(tmp_path, cases=1)

    def resource_failure(*args, **kwargs):
        raise GraderResourceFailure(drained=drained)

    fixture.backend.grade = resource_failure
    row = fixture.run()["results"][0]
    assert row["failure_kind"] == "grader_resource_limit"
    assert not row["execution_certified"] and not row["certified"] and not row["accepted"]
    assert not list((fixture.root / "frozen-controller-completed-v1").glob("*.json"))


def test_readiness_resource_failure_preserves_unknown_source_without_repeating_work(tmp_path):
    fixture = Fixture(tmp_path, cases=1)
    fixture.backend.fail_conversion = True
    fixture.backend.readiness_error = GraderResourceFailure()
    row = fixture.run()["results"][0]
    assert row["execution_certified"] and not row["solver_outcome_scored"]
    assert row["failure_kind"] == "converter_exit"
    assert row["fixture_preparation_failure_kind"] == "grader_resource_limit"
    assert row["fixture_preparation_resource_evidence"]["worker_drained"] is True
    assert not row.get("fixture_readiness") and not row["certified"] and not row["accepted"]
    fixture.backend.readiness_error = None
    resumed = fixture.run()["results"][0]
    assert resumed["reused"] and not resumed["solver_outcome_scored"]
    assert len(fixture.backend.convert_calls) == len(fixture.backend.readiness_calls) == 1
    assert not fixture.config.output.with_suffix(".fixture-exclusions.json").exists()


def test_resource_exception_attestation_cannot_replace_validated_conversion(tmp_path):
    fixture = Fixture(tmp_path, cases=1)

    def no_completed_conversion(**kwargs):
        error = GraderResourceFailure()
        error.attestation = {"permissions_revoked": True, "input_unchanged": True}
        raise error

    fixture.backend.convert = no_completed_conversion
    row = fixture.run()["results"][0]
    assert not row["execution_certified"] and not row["certified"] and not row["accepted"]
    assert not row["solver_outcome_scored"] and not fixture.backend.readiness_calls
    assert not list((fixture.root / "frozen-controller-completed-v1").glob("*.json"))

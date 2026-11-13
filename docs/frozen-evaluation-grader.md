# Frozen host-side grading

`tools/freeze_evaluation_grader.py` creates a separate sourceless grading
artifact from an explicit Git commit. It compiles committed `app` modules and
the static `tools` import closure needed by `benchmark_multires_corpus.evaluate`
and the artifact verifier. It retains the repository license and exact committed
`pyproject.toml`/`uv.lock` declarations. Working application edits do not enter
the snapshot. This is an artifact, not another checkout or runtime copy.

The manifest pins committed source bytes, generated entrypoint, builder and
helper bytes, interpreter magic, every reused Python runtime file, compiled
application files, dependency declarations and license. Verification requires
the manifest's separately pinned SHA-256. Changed files, extra CAD assets,
links/junctions, incompatible bytecode and existing artifact destinations are
refused. Runtime changes during building leave a partial artifact preserved
without a completed manifest. The builder helper must match its committed
source, allowing LF/CRLF normalization; both original byte identities remain
recorded. The artifact contains no source geometry or private manifests.

```powershell
python tools/freeze_evaluation_grader.py --python-runtime PATH_TO_EXISTING_SEALED_PYTHON --revision FULL_GRADER_COMMIT --output NEW_GRADER_ARTIFACT
python tools/freeze_evaluation_grader.py --verify --output NEW_GRADER_ARTIFACT --manifest-sha256 PRINTED_MANIFEST_SHA256
```

The neutral `application/grader.pyc` entrypoint accepts a host-owned frozen case
JSON, retained converter-output directory, new result JSON and optional new
failure-artifact directory. Launch with the declared runtime using `-I -B`.
The host must verify the pinned artifact before launching it. The entrypoint
also verifies the frozen artifact before and after grading. It does not invoke
either benchmark `main()` or Git against the compiled snapshot.

```json
{
  "grader_manifest_sha256": "PINNED_GRADER_MANIFEST_SHA256",
  "manifest_parent": "ABSOLUTE_OR_CASE_RELATIVE_FIXTURE_DIRECTORY",
  "case": {
    "id": "source-q05",
    "stl": "input.stl",
    "source_step": "reference.step",
    "input_sha256": "PINNED_STL_SHA256",
    "source_sha256": "PINNED_REFERENCE_SHA256",
    "source_model_id": "source",
    "split": "development",
    "preset": "q05",
    "input_units": "mm"
  },
  "retained_sha256": {
    "reconstruction.step": "ACTUAL_RETAINED_STEP_SHA256",
    "report.json": "ACTUAL_RETAINED_REPORT_SHA256"
  }
}
```

Use a real complete fixture case, including any additional acquisition metadata.
`retained_sha256` must contain every present file from the explicit output
allowlist, including optional preview/graph/diagnostic/log files. STEP and
report are mandatory. Paths are fixed basenames; arbitrary files and copied
input STL are never imported or copied from the converter's output folder.
The original STL and reference STEP must match the case's pinned hashes.
The caller must seal and hash the completed output directory only after all
converter descendants exit and their access is revoked. That production host
boundary is separate from this artifact utility.

Before importing frozen application storage/jobs, the entrypoint creates a
fresh grader work directory beneath the requested result parent. Its process
environment directs `STL_TO_STEP_RUNS_DIR`, `MPLCONFIGDIR` and temporary paths
there. Storage import side effects and verification scratch files therefore
stay outside the sealed artifact. These are process-local settings; no global
configuration, AppContainer profile or permission grant is changed.

Production controllers pass an existing absolute task-owned `scratch_root` in
the pinned case JSON. The grader creates its fresh working directory there,
independently of a potentially deep results hierarchy. This avoids Windows CAD
file API path limits without changing long-path or temporary-directory settings
globally. Scratch roots traversing links or inside sealed artifacts/retained
outputs are refused. Existing callers default to the result parent.

Grading temporarily replaces `reconstruct_in_worker` with a callback that
copies only pinned, retained outputs and parses the retained actual report.
For a ready reference, exactly one callback is required. The unchanged evaluator grades the
actual STEP against the host-accessible original STEP. `conversion_wall_seconds`
is renamed `artifact_copy_wall_seconds`; `second_conversion_not_run=true`
records that this execution did not reconstruct again. The original report's
conversion elapsed time remains historical evidence, not new conversion latency.
Results record grader revision, manifest/case/runtime identity and actual
output/fixture hashes. Inputs, outputs and artifact halves are rechecked before
publishing; an existing result is never overwritten.

Before scoring, the entrypoint separately loads the original and qualifies its
fine reference tessellation with `validated_reference_mesh`, using the original
deflection and expected solid count. This separate shape cannot change the
original scorer's bounds or gates. An unready reference returns
`evaluation_failure_kind=fixture`, readiness diagnostics and the same pinned
case/output/runtime attestations without consuming the retained conversion in
the scorer. It is not a Demeshify solve failure and must be replaced or repaired
before full-corpus completeness is claimed.

The converter must never receive this case JSON, reference path, grading
artifact path or access grants. Converter and grader may reuse the same sealed
Python libraries, but their application artifacts and permissions remain
separate. The manifest keeps `workflow_qualified=false` and
`isolation_qualified=false`; successful artifact verification is not proof of
OS isolation or the full reconstruction workflow. The root controller still
needs native integration, full dependency/runtime qualification and matched
end-to-end evidence before certifying results or publishing shared cache records.

The same frozen entrypoint supports host-only fixture qualification when a
converter crashed or timed out and has no complete output set:

```powershell
<pinned-runtime>/python.exe -I -B <grader>/application/grader.pyc `
  --reference-readiness <case.json> <new-readiness-result.json>
```

This mode requires the grader pin, manifest parent and source case, but no
retained-output hashes. It verifies only the source STEP fixture, not an STL or
converter report, and never invokes the converter or reconstruction callback.
The result has `reference_readiness_only=true`, `accepted=false`, empty output
hashes and pinned source/grader/runtime/case attestations. The controller can use
this distinct fixture proof to score an already observed execution failure;
reference-readiness success alone can never turn it into a reconstruction pass.

Twenty-three focused protocol checks pass, including actual cross-process execution
of generated sourceless entrypoint/modules against a tiny fake grading package.
They establish retained-report truthfulness, no second conversion/benchmark CLI,
output allowlisting, process-local storage paths, no runtime copy, immutable
source/output hashes, tamper refusal, incompatible interpreter refusal,
preserved existing outputs/partial builds, committed static dependency closure,
and runtime mutation refusal before result publication. They do not run CAD
conversion, export, original-model grading or a native sandbox.

```powershell
python -m pytest tests/test_frozen_evaluation_grader.py -q
python -m ruff check tools/freeze_evaluation_grader.py tests/test_frozen_evaluation_grader.py
```

# Converter verification with a live file seal

`convert_isolated()` retains its original full-hash verification by default.
Its optional `verification_session` accepts the actual live
`SealedRuntimeSession`, not a boolean, digest, dictionary, subclass or serialized
record. Conversion gates, 240-second conversion budget, cleanup budget, process
auditing, output retention and the existing whole-case deadline remain intact.
The session is checked before launch, after process drain/grant revocation and
after retaining outputs. Identity-check time is recorded separately from the
actual conversion time. Failure to retain live protection prevents a qualifying
attestation, preserves failed artifacts and still revokes the job grant.

`tools/locked_artifact_identity.py` builds catalogs from a separately pinned
manifest and binds them to a live session. It verifies the manifest hash,
snapshot schema, current bytecode magic and artifact role. The required catalog
includes the **whole artifact root**: manifest JSON, license, every compiled
application file, and grader dependency declarations. It also includes every
runtime file. Application-directory-only locks do not qualify because they omit
the manifest and license. Catalog construction alone is not byte verification;
the session must hash the expected files while holding its native read locks.

```python
from tools.isolated_conversion import convert_isolated
from tools.locked_artifact_identity import artifact_catalogs, verify_locked_artifact
from tools.sealed_runtime import SealedRuntimeSession

metadata, catalogs = artifact_catalogs(
    converter_artifact, pinned_converter_manifest, expected_kind='converter')
grader_metadata, grader_catalogs = artifact_catalogs(
    grader_artifact, pinned_grader_manifest, expected_kind='grader')
# The controller must reject any conflicting catalog for a shared runtime.
for root, files in grader_catalogs.items():
    if root in catalogs and catalogs[root] != files:
        raise ValueError('Artifact runtimes differ')
    catalogs[root] = files

with SealedRuntimeSession(catalogs, module_sha256=pinned_loaded_seal_module) as session:
    converted = convert_isolated(
        converter_artifact, pinned_converter_manifest,
        profile=existing_profile, owned_root=owned_jobs_root,
        input_stl=stl_path, input_sha256=pinned_stl_hash,
        output_dir=new_owned_output_directory,
        case_deadline=original_monotonic_deadline,
        verification_session=session,
    )
    verify_locked_artifact(
        grader_artifact, pinned_grader_manifest, session, expected_kind='grader')
    # Independent frozen grading still uses its original full verification.
    # The controller checks again before publishing any qualified result/cache.
```

`artifact_catalogs(...)` returns `(metadata, catalogs)` for a converter or grader.
`converter_catalogs(...)` is the converter-role wrapper.
`verify_locked_artifact(...)` returns `(metadata, trace_identity)` only after
`session.require_catalogs(...)` checks native protection and exact catalog
coverage. Every required root must match completely; additional separately
sealed roots are allowed and remain monitored. Wrong coverage invalidates the
session. The trace identity is evidence, never permission for future reuse.

The adapter records `identity_verification_mode` and
`verification_session_identity`. Its code identity now also includes the locked
artifact helper and seal module. Converter children receive no session, grader
path, original STEP path or additional privileges. This change does not bypass
the child grader's verification or alter reconstruction acceptance/cache gates.

Focused checks exercise the unchanged default path, whole-case deadlines,
native tiny-file converter/grader catalog equivalence to the full verifiers,
locked manifests/licenses/declarations, missing/wrong coverage, closed and fake
sessions, pre-seal tampering, revocation before output reads, loss of protection
during conversion and mutation during retention. The actual geometry converter
and AppContainer grants are replaced by protocol fixtures in adapter tests; the
file locks and coverage checks in the native tests are real. The later
[actual three-case workflow](isolated-evaluation-workflow-2026-11-13.md) qualifies
this path with real conversions, frozen grading and retained outputs. It does
not establish full-corpus reconstruction yield or a broad speed improvement.

```powershell
python -m pytest tests/test_isolated_conversion.py tests/test_locked_artifact_identity.py tests/test_sealed_runtime.py -q
python -m ruff check tools/isolated_conversion.py tools/locked_artifact_identity.py tools/sealed_runtime.py tests/test_isolated_conversion.py tests/test_locked_artifact_identity.py
```

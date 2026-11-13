# Resume progressive evaluation without repeating unchanged conversions

## Bounded evaluation batches

`--batch-cases 20` now stops after at most 20 new case evaluations. Failures
consume a slot; identical cached cases do not. The aggregate and summary are
saved at the batch boundary, including explicit partial-run status. Repeating
the command resumes completed cases and advances the next batch. A positive
batch size can change between invocations because it changes scheduling, not
the case identity or grading contract; each launch records the chosen size.

```powershell
python tools/benchmark_multires_corpus.py frozen-manifest.json --output baseline.json --batch-cases 20
```

The 240-second conversion and 360-second case limits, strict acceptance gates,
grading function and summary function remain unchanged. Their syntax trees were
checked against the preceding controller. Source/runtime/input identities still
invalidate changed results; this does not permit editing the converter inside
a frozen run. It allows finite evaluation chunks between acquisition and
reconstruction experiments instead of starting all 10,000 cases at once.

Eight protocol checks pass, covering successive batches, preserved failed
outcomes, changed batch sizes, complete cached resumes and rejected zero/negative
limits, alongside the preceding provenance checks. Two real q00/q05 workflow
cases were then evaluated with a one-case limit: the first invocation reports
one of two cases and `complete=false`; the second reuses that conversion and
evaluates the other, reporting one fresh/one reused case and `complete=true`.
Their acceptance and reference metrics match the preceding converter within
1e-9. This completes only that two-case manifest, not the requested full corpus.
Evidence is local under `experiments/case-batches-*`.

The latest composed snapshot at this milestone is 467 distinct sources / 4,670
inputs (`partial-20261112-pass9.json`), explicitly incomplete. Further fixture
qualification and the full 1,000-source / 10,000-input baseline remain pending.
The sections below preserve the original cache milestone's evidence.

## Original cache milestone

The multiresolution runner previously included the entire manifest and every
tool in each case identity, and required the same Git revision to resume. An
expanded cohort, documentation commit or unrelated acquisition change could
therefore prevent reuse despite identical geometry, converter and grader.
Runtime dependency versions were in the case fingerprint but absent from the
run contract, which could allow a changed environment to overwrite old case
records inside a resumed run.

The new schema uses verified STL/reference hashes, the complete case settings,
all application source, the evaluator's declared tool dependencies, lockfiles,
Python executable/DLL hashes, installed dependency versions and numerical pool
settings. It excludes unrelated manifest rows and acquisition tools. The frozen
run contract includes runtime identity; each invocation separately records its
actual Git revision. Cached results retain the original conversion revision.
Different code, geometry, settings or runtime require a new run. Legacy output
contracts are preserved and are not silently migrated.

An immutable shared case cache allows a larger cohort to reuse identical prior
cases. Reports explicitly distinguish fresh conversions, shared reuse and reuse
within the same resumed run. `--fresh` skips the shared cache while still resuming
completed cases in that run; use a new output path for a fresh matched experiment.
Artifacts remain at their recorded original location. Existing strict gates,
reference grading, 240-second conversion and 360-second case limits are unchanged.

Five protocol tests cover progressive reuse, provenance, runtime invalidation,
fresh runs, relevant code changes, unrelated acquisition changes and interrupted
workers. Raw worker attempts and logs have unique filenames; only the controller
publishes certified cache records after rechecking frozen code, runtime and input
bytes. A mid-case source change preserves the attempt without contaminating the
shared cache, and the original run can resume after the source is restored. These tests
use fake worker bytes and do not count as CAD fixtures or reconstruction evidence.
The final selected regression suite passes 26 tests in 19.56 seconds; Ruff passes.

The actual workflow was then exercised on development q01, q00 and q09 of source
`5b7f2dd6f8dee0946745`, with converter source `6ead82d`:

1. A two-case run performs two fresh conversions.
2. Expanding to three cases reuses those two and performs one new conversion.
3. All three retain the earlier acceptance, facet/topology counts and recorded
   input/reference geometry metrics, compared within 1e-9 numerical tolerance.

After adding the interruption guard, the final runner was proved again on actual
q00/q05 conversions: expanding from one to two cases performs one fresh conversion
and reuses one. Their geometry and acceptance metrics exactly match V13. The
grading function's syntax tree is identical to `6ead82d`; this change is controller
and provenance work. Final proof is `progressive-cache-final-workflow-proof.json`.

These are small measured workflows, not a new 20-case or full-corpus evaluation.
Local evidence: `benchmarks/multires-1000/experiments/progressive-cache-actual-proof.json`,
the `open-profile-v14-baseline-{two,three}` reports and their contracts/launches.

```powershell
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/open-profile-v14-two.json --output benchmarks/multires-1000/experiments/open-profile-v14-baseline-two.json --artifacts benchmarks/multires-1000/experiments/open-profile-v14-baseline-artifacts
python tools/benchmark_multires_corpus.py benchmarks/multires-1000/open-profile-v14-three.json --output benchmarks/multires-1000/experiments/open-profile-v14-baseline-three.json --artifacts benchmarks/multires-1000/experiments/open-profile-v14-baseline-artifacts
```

The qualified raw primary collection has reached 299 sources / 2,990 exports;
five native-qualified KiCad sources add 50 exports. The newly composed snapshot
`partial-20261112-pass4.json` verifies **304 distinct sources / 3,040 exports**,
with no geometry-family split leakage. The 1,000-source / 10,000-input objective
and complete frozen baseline remain unfinished. The task-specific AppContainer
file-access probe succeeds, but the actual Python converter still needs a
complete isolated workflow proof before claiming OS-enforced reference isolation.

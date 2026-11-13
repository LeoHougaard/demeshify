# Qualified serial frozen grader candidate

`tools/sealed_grader_session.py` is a performance candidate, not the current
evaluation backend. It keeps a trusted grader worker alive across serial
requests. No acceptance formula is copied: the worker loads and executes the
actual pinned `application/grader.pyc` code object, with fresh globals each time.
Its fixture/output/case hashes, reference readiness, no-second-conversion
callback, geometry checks and strict acceptance remain inside that unchanged
entrypoint and its frozen scoring modules.

The supported entrypoint source SHA-256 is
`c21c621b92571217c54c7cc4a88f6249d424846e95c4e493ca1dcbc77f9407e0`,
shared by the final-v4 grader and the corrected final-v5 grader from revision
`0a64aa9b00cb2b17f380df3609641a71e750e771`. Another entrypoint version
is refused until its request-state behavior is qualified. This is a protocol
version boundary, not a per-model exception.

`freeze_worker(project, revision, new_output)` compiles an explicit committed
revision of this worker and its two live-proof helpers into a small separate
artifact. It retains the license, source/compiled hashes, interpreter magic,
builder identity and qualified parent-job supervisor hash. It never copies a
runtime, grader, model or project checkout. Existing and partial outputs are
preserved; `workflow_qualified=false` remains explicit.

The parent API requires an actual live `bootstrap_session` covering the entire
worker artifact, grader artifact and shared runtime, including manifests,
licenses and dependency declarations. Catalogs or trace strings are not proof.
This protects interpreter/bootstrap execution before the child establishes its
own full `SealedRuntimeSession`. Startup passes no case paths or geometry;
imports may warm, while models and retained outputs are read only for a request.
A separately locked tiny configuration directory carries immutable startup
paths and pins. No ACL, AppContainer profile, global setting or existing
artifact is modified.

```python
from tools.sealed_grader_session import SealedGraderSession

session = SealedGraderSession(
    frozen_worker, worker_manifest_sha256,
    frozen_grader, grader_manifest_sha256,
    new_session_work_directory,
    bootstrap_session=actual_live_parent_seal,
)
try:
    session.start(deadline=original_startup_deadline)
    result, execution = session.grade(
        frozen_case_json, retained_actual_converter_outputs, new_result_json,
        artifacts=new_failure_directory,
        deadline=original_case_deadline,
    )
finally:
    session.close()
```

The caller must include the worker catalog when creating the bootstrap seal;
an application-only or already closed seal is insufficient. Worker catalogs
come from its pinned manifest; grader/runtime catalogs use the existing
`artifact_catalogs(..., expected_kind='grader')` helper. Conflicting shared
runtime catalogs must be rejected. Startup and each request require finite
monotonic deadlines no more than the original 360-second case ceiling away.
There is no automatic retry, cache publication, schedule or unbounded request
mode. Request sending and response waiting share that same deadline.

The supervisor creates the worker detached and suspended, assigns its Windows
Job, then reopens/resumes its sole initial thread. Detachment prevents an
implicit Windows console helper without changing the pinned Python binary or
the worker's redirected stdin/stdout protocol. The Job limits CPU affinity to one
core, runs below normal priority, disallows breakaway and permits only the
original process. No child may execute; a kernel-denied launch does not create
a job member. The original guard still requires exactly one active and one
total process throughout the worker's lifetime. A timeout, malformed response, identity
drift or request error kills and drains the owned Job and waits for the root
process handle to signal. Cleanup has the existing separate ten-second bound.
The frozen AppContainer supervisor source remains untouched; only its pinned
native primitives are reused for this trusted-host Job. A runtime requiring a
launcher child or grading subprocess is unsupported by this candidate.

Within the worker, only the frozen verifier's `verify()` callable is delegated
to actual native live proof. Both original ENTRY verification sites still run,
and the original dependency inventory/hash checks remain. A final native check
precedes the completion response. Each request restores argv, path, environment,
temporary-directory state, storage run directory and evaluator patches. Its own
temporary directories are cleaned and their atexit callbacks unregistered.
`USERPROFILE`, `HOME`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA` and `LOCALAPPDATA`
point into a new owned home during bootstrap and into each request's owned
temporary home. The parent environment is not changed. This allows libraries
such as ezdxf to resolve `Path('~').expanduser()` without reading Leo's actual
home/configuration. Request completion restores the bootstrap environment.
Process-lifetime callbacks registered by imported libraries remain for worker
shutdown. Loaded application/tools origins must remain pinned compiled files.
An unexpected evaluator/verifier patch invalidates the request even if cleanup
can restore it.

Original ENTRY results are preserved, including failures. A raw result may exist
when a later hygiene/deadline check fails; it is **not qualified** without the
successful request response and controller checks. Request logs/failure records
remain under the new session work directory. Native library stdout stays in the
session log, while Python ENTRY stdout/stderr are captured per request. No
second reconstruction or shared benchmark-cache record is produced here.

The reviewed worker snapshot passed 29 focused checks using tiny protocol fixtures and real Windows
file locks/jobs. They cover repeated exact-ENTRY execution, accepted/rejected
retained reports, unchanged hash-tamper rejection, reference-readiness and
fixture-error paths, state/temporary cleanup, rejected origin/patch leaks,
worker artifact identity, invalid deadlines/bootstrap objects, bounded IPC,
native suspended startup, a delayed stdin/stdout handshake with exact job-PID
membership, child-process refusal and timeout kill/drain. The
home-path regression also imports actual ezdxf with only the owned environment.
The fake fixture packages contain no CAD implementation; those checks are not
reconstruction or full-runtime qualification.

The first actual proof of worker revision `512eadd` stopped before grading:
its neutral environment omitted a home directory, and CadQuery's ezdxf import
failed with `Could not determine home directory`. That worker artifact and
failed proof are preserved. The qualification attempt using worker revision
`1f547dd` then completed all four normal cold V5 grading controls: the V39 pass,
V37 dimension rejection, C84 faceted
rejection and retained q02 through-hole control. All compared original metrics
were unchanged; q02's fresh topology diagnostic matched. Warm startup still
failed before any request because `CREATE_NO_WINDOW` implicitly added
`System32/conhost.exe`: actual job PID/image diagnostics showed two members.
A tiny same-binary `DETACHED_PROCESS` diagnostic retained exactly one member
after a full stdin/stdout handshake. An explicit child launch was denied with
Windows error 1816, executed no child code and retained one member. This
corrects a protocol/test assumption about console allocation and denied
attempts; no grading gate, process limit or accounting guard is relaxed.
These ignored native diagnostic and failed qualification records are preserved.

The reviewed detached worker from source
`5f32dac834705e91acca32f79d0e192d955ebb0c` subsequently passed actual retained-output
qualification with the corrected V5 grader. The pinned identities are:

| Artifact or source | SHA-256 |
| --- | --- |
| Worker manifest, `evaluation-grader-workers/5f32dac-detached-v3` | `a19d69613c6f2ce29b1f0da8b10c0e2b08315c1156f5e5587935b0097bf3c943` |
| V5 grader manifest, `evaluation-graders/final-v5-20261113` | `ea9197146dcab366d76f102c9418e1800be37d2d9db9133d24bcfde63cd1f8a0` |
| Runtime inventory | `02d1a6644070bbb96a4ebf04704a43531730756362c3f659b5a359b6575791b9` |
| Original parent helper, cold4/warm7 proof | `4bb66a940ef04f94931947292d3f642b354a45d2f206fca47ce79778172954df` |
| Original qualification receipt, preserved history | `1daac8fcac4a3bb7d4635da0a9fb7f11de459548627430bc843d99c9dd2014e8` |
| Current parent `tools/sealed_grader_session.py`, cleanup correction | `f5b1bee443b6e0f6828417bca157c5c1bc2fa28abdbec782ae14497c32caacdf` |
| Current incremental qualification receipt | `e261bc52a7799f28c5779e713258e9ab24b0630d41a1360619526fcfc1623e3c` |

The frozen worker, grader and runtime remain unchanged. The parent helper has
one narrowly qualified cleanup correction described below. The frozen artifact's
original `workflow_qualified=false` declaration is preserved; the separate
qualification receipt binds the actual evidence and identities. It does not
replace either process's live native seal.

The fresh comparison used four retained converter outputs across two source
models: V39, V37 and C84 are three reconstructions of the same difficult q05
input; q02 is a separate through-hole source. All four were graded cold, then
the first three were graded warm forward and reverse, followed by q02. Every
non-timing result field matched the corresponding cold result exactly across
all seven warm requests. Only `verification_wall_seconds` and
`artifact_copy_wall_seconds` were excluded. Original strict gates and geometry
measurements remained unchanged: V39 passed, V37 remained rejected at
0.22786240468727215 mm dimension error, and C84 remained rejected with 2,072
facets. All 30 configured q02 original measurements were unchanged; its fresh
STEP topology diagnostic matched with Euler characteristic zero and genus one
for both source and output. No conversion was repeated.

A separate native supplement exercised the original `--reference-readiness`
mode: fixture readiness was true, acceptance was false, the original source
hash was preserved, and no retained output was copied. A forced 0.05-second
parent deadline raised `GraderRequestTimeout`; termination and drain took
0.264 seconds including cleanup. The root process was signalled, no qualified
result existed, and its failure record was preserved. Native handle checks
confirmed that all four owned job handles and 12,829 parent-seal handles were
closed; the closed seal refused reuse. Normal request cleanup removed two
temporary directories and one temporary atexit registration per graded case.

A subsequent source review found that failure-record persistence could itself
raise before the parent called `close()`. The current parent helper attempts
that record on a best-effort basis and always closes in `finally`, preserving
the original request exception if the record cannot be written. Twelve pure
fault-injection regressions passed for mkdir/write allocation failure, disk
full and permission refusal, with both an original timeout and an original
`MemoryError`. The original 29 native/protocol checks belong to the earlier
snapshot; the entire suite was not rerun as part of this narrow delta.

Actual native delta qualification used that new parent helper with the same
frozen worker, grader and runtime. One retained V39 positive passed in 9.93
seconds and matched every original cold non-timing field. A forced
0.05-second deadline with an explicitly synthetic ENOSPC refusal of the parent
failure record still preserved `GraderRequestTimeout`, signalled/drained the
root in 0.296 seconds and produced no qualified result. All four job handles
and 12,829 parent-seal handles were confirmed closed again. This was a
controlled refusal of evidence persistence, not an actual full-disk or
allocation-pressure experiment. The new receipt binds all original proof
references plus this positive/timeout delta; it does not relabel the earlier
seven-request run as a fresh run with the new parent helper.

In the original single matched four-case observation with parent helper
`4bb66a94`, cold grading took 77.75 seconds.
The same four warm requests took 44.94 seconds plus 19.36 seconds startup:
64.30 seconds, **17.3% lower including startup**. The shared parent seal took
another 8.00 seconds, recorded separately; warm close took 0.208 seconds. This
measures grading transport on retained outputs, not reconstruction latency or
full-corpus throughput. Warm peak RSS was higher: 552.5 MB versus 495.8 MB for
the largest cold process. Warm RSS after startup and after seven requests was
424.2 MB and 481.4 MB. Long-run memory behavior remains unverified.

Use bounded batches of at most **15 requests per worker**, counting readiness
and failed requests as well as grading. The opt-in controller cap must recycle
the process within the original request budget. This is a conservative
operational bound, not a measured guarantee for 15 cases or a 10,000-case worker
lifetime. Complete controller import/conversion/grading/cache workflow
qualification and delivery remain separate from this worker proof.
The optional controller memory-limit/resource-error policy also requires its
own qualification; neither receipt claims that policy was tested here.

Local ignored evidence is preserved under `benchmarks/multires-1000/`:

- `experiments/warm-grader-qualification-5f32dac-v5/result.json`: fresh cold4/warm7 results, order, timing, resources and drain.
- `experiments/warm-grader-qualification-5f32dac-v5/analysis.json`: exact matched comparisons; no differing non-timing fields.
- `experiments/warm-grader-qualification-5f32dac-v5/qualification.json`: original receipt for parent helper `4bb66a94` and its 23 evidence files.
- `experiments/warm-grader-cleanup-delta-f5b1bee-v5/qualification.json`: current receipt binding 27 evidence files, the unchanged worker and the corrected parent helper.
- `experiments/warm-grader-cleanup-delta-f5b1bee-v5/result.json` and `positive.json`: actual positive comparison and timeout/drain despite injected evidence refusal.
- `experiments/warm-grader-supplement-5f32dac-v5/result.json` and `readiness.json`: actual readiness, forced deadline and handle-release proof.
- `wss-5f-v5/request-70bd648c0a3f42cabf1beeb49c94db6f/parent-failure.json`: unqualified forced-timeout record.
- `experiments/warm-grader-qualification-512eadd-v4/`: original owned-home failure.
- `experiments/warm-grader-qualification-1f547dd-v5/` and `experiments/warm-grader-qualification-1f547dd-v5-r1/`: metadata harness failure and later console-helper failure.
- `experiments/warm-grader-job-identity-1f547dd/` and `experiments/warm-grader-detached-probe-1f547dd/`: native PID/image, unchanged-limit, handshake and denied-child diagnostics.

```powershell
python -m pytest tests/test_sealed_grader_session.py -q
python -m ruff check tools/sealed_grader_session.py tests/test_sealed_grader_session.py
```

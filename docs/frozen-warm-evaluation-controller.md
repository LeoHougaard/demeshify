# Opt-in warm grader controller

`tools/frozen_warm_evaluation_controller.py` supplies a grading backend to the
existing `frozen_evaluation_controller.run_controller(..., _backend=...)` seam.
It does not copy the evaluator, converter, acceptance formulas, case loop or
cache implementation. The cold entrypoint remains available. The accompanying
controller changes preserve validated timeout/resource evidence and accelerate
certified cached-prefix lookup; their changed source hash creates a new cache
identity. Existing cache records remain preserved under their original identity.

**Native use is gated.** The corrected worker from `5f32dac` has a reviewed V5
native qualification receipt covering matched grading, readiness and deadline/
drain checks. Earlier home-directory and console-startup failures remain preserved
and unqualified. This entrypoint requires both the exact qualified worker artifact
pin and a pinned receipt bound to the exact grader, ENTRY, runtime and parent
helper. V4 and V5 grader artifacts require their own matching receipt pins. A
matching ENTRY hash alone does not qualify a different grader artifact. The
integrated workflow has a separate native conversion/grade/resume proof below.

The receipt uses schema 1, kind `sealed-grader-native-qualification-v1`, and must
record `qualified=true` only after reviewing actual native evidence. Required
bindings are `worker_manifest_sha256`, `grader_manifest_sha256`,
`entry_source_sha256`, `runtime_inventory_sha256` and `parent_helper_sha256`.
The runtime catalog digest is canonical JSON with sorted keys and compact
separators, matching the original controller. A nonempty `evidence` list pins
preserved native result files by `path` and `sha256`. Receipt and evidence files
must be plain files inside the owned evaluation root. Altered, foreign or
unqualified evidence is refused before converter work begins. There is no bypass
flag or serialized substitute for a live native seal.

The receipt also sets `maximum_cases_per_worker`, a process-lifetime limit.
An explicit finite positive integer `--batch-cases` controls how many fresh cases
the controller processes before stopping. Batch length is independent of worker
lifetime: batches larger than 15 still recycle each worker before its sixteenth
request. The current receipt limits each worker to 15 requests; long-run memory
is unqualified. Every grading or readiness request consumes that limit. When it
is reached, the backend closes the old worker and starts a new one within the
same remaining deadline. Closing, startup and grading never receive a fresh
allowance. The worker cap and replacement policy enter case/readiness identities.
Larger explicit batches avoid repeatedly scanning a growing cached prefix on
every CLI resume. This does not authorize an unbounded batch, concurrent workers,
a 10,000-request worker or relaxed geometry/time gates.

Each warm grader also has an **8 GiB committed-memory process limit** on its
existing Windows Job. The wrapper queries the original extended limits, retains
every existing flag, affinity, BelowNormal priority and one-process limit, then
adds `JOB_OBJECT_LIMIT_PROCESS_MEMORY` before launch. An already lower memory
limit stays lower. Readback must confirm that no unrelated limit changed. This
is a committed-memory cap, not an RSS estimate. Windows refuses allocations
exceeding it; see Microsoft's
[extended limit definition](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information).

A host-side observer latches process/job memory-limit completion events during
requests and termination/drain. A hit terminates only that owned worker and
refuses its response, even if the child reports success or source invalidity.
Explicit `MemoryError`, native out-of-memory and `std::bad_alloc` structured
errors are also fatal, including errors caught by optional topology diagnostics.
These are unscored `grader_resource_limit` outcomes, not converter failures or
fixture exclusions. Actual child JSON is preserved; a separate
`*.grader-resource.json` records host evidence after draining. Logging failure
cannot skip termination. Windows does not guarantee every ordinary completion
notification, so absence of an event is not proof that memory was sufficient;
see [completion notifications](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port).

The resource policy enters both case and readiness identities. The converter,
source validity, strict acceptance formulas and original deadlines are unchanged.
A native probe confirmed default 8 GiB readback and preserved job policies. A
separate 64 MiB probe refused a 128 MiB allocation with WinError 1455, captured
message 9 and drained/closed in 0.153 seconds. That tiny probe ran no CAD or
conversion and does not establish full integrated grader performance or prove
that an exhausted source is invalid. Its evidence remains local under
`experiments/warm-grader-memory-guard-proof/`.

The native backend retains the original bootstrap seal and creates an additional
actual live union seal covering the converter, grader, worker and runtime.
Conflicting shared-root catalogs are refused. The inherited converter adapter
and the warm grader bootstrap receive that same union object. A
`warm-bootstrap-<id>.json` records the additional startup time and observed parent
handle counts; these varying observations do not enter cache identities. This
extra parent seal has an upfront time/handle cost that must be measured in the
actual integrated workflow, not hidden in a per-case speed claim.

The worker is constructed and started only when the first grading or reference
readiness request has a remaining deadline. Startup imports/full hashing and the
request share that same allowance; no case geometry is warmed outside its budget.
Later requests reuse the live worker. Its unchanged pinned ENTRY executes with
fresh request globals, and each request rechecks its own live OS seal. Identity
metadata truthfully distinguishes startup full hashing from live per-request
verification; it does not claim a fresh complete runtime hash on every case.
The original ENTRY still checks actual fixtures, retained outputs and dependency
declarations. Conversion and every strict geometry/quality gate stay unchanged.

Request results are returned unchanged. Separate `*.warm-response.json` files
retain the worker's response and transport timing, including first-request
startup. Startup/request failure drains that worker before reuse can proceed;
the next request can create a fresh bounded worker. Final cleanup closes the
worker before releasing either parent seal. All evidence and failed work folders
are preserved.

The mode, wrapper/helper source hashes, worker and qualification pins, and live
verification policy enter the original case fingerprint. Transport metadata also
binds readiness receipts to warm mode. The existing cache namespace is reused
with different keys; cold results are not relabelled as warm results or new
evaluations. Larger matched reconstruction cohorts and full-corpus evaluation
remain separate from transport qualification.

```powershell
& '<PINNED-GRADER-RUNTIME>/python.exe' -I -B tools/frozen_warm_evaluation_controller.py `
  '<FROZEN-MANIFEST>' --converter '<CONVERTER>' --converter-sha256 '<PIN>' `
  --grader '<GRADER>' --grader-sha256 '<PIN>' --worker '<QUALIFIED-WORKER>' `
  --worker-sha256 '<PIN>' --qualification '<REVIEWED-NATIVE-RECEIPT>' `
  --qualification-sha256 '<PIN>' --profile 'Leo.Demeshify.Evaluation.20261112' `
  --owned-root '<OWNED-ROOT>' --output '<OWNED-ROOT>/warm-results.json' --batch-cases 3
```

Forty-four focused protocol checks pass, with no native worker, conversion or
geometry workload. They cover qualification binding, union/bootstrap object
identity, deferred startup, shared deadlines, unchanged result bytes, serial
grade/readiness reuse, failed-worker replacement, cleanup ordering, mode identity,
the inherited converter bridge, finite explicit batches, independent worker
request limits, batches above 15 with bounded recycling, and import without
application loading, native-limit-layout preservation, request/drain memory
events, caught OOM refusal and logging-failure cleanup. Test
receipts and fake services live only in temporary protocol fixtures and are not
native qualification evidence. Ruff passes.

The final controller, wrapper and corrected parent helper were then exercised
in a fresh native workflow using the frozen baseline converter and V5 grader.
One real development STL passed isolated conversion and unchanged reference
grading. Native conversion took 10.05 seconds and the case took 49.87 seconds,
including deferred worker startup; sampled whole-tree peak RSS was 963 MB.
Those are one-case observations, not full-corpus throughput claims.

A second process resumed the same case with zero fresh conversions or grades.
Its cached-prefix row retained exactly the same native attempt, output hashes
and drained/revoked conversion attestation, with explicit historical timings.
The lookup took 2.25 seconds, excluding bootstrap and closing checks. Exact
source pins, receipt and result hashes are preserved in
`experiments/b11w5/native-workflow-and-resume-proof.json` under the ignored
evaluation root. Earlier warm observations remain distinct historical evidence;
no old cache was recertified under the new controller hash.
